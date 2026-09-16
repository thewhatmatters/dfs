#!/usr/bin/env python3
"""Optimize a FanDuel NCAAF classic lineup from a players-list CSV.

I/O: flags in → picker tables on stderr. JSON on stdout for --agent / --json
/ non-TTY; --out always writes the file. Exit 0 on a feasible lineup, 2 if
infeasible, 1 on usage/IO errors.
Default objective is Vegas implied team totals (week 1). CSV FPPG is
prior-season / empty — only used with --use-fppg.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

# Allow `python3 ncaaf/optimize.py` from repo root.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from cli_table import (  # noqa: E402
    format_games_table,
    format_picker_table,
    json_stdout_enabled,
)
from ncaaf.lines import (  # noqa: E402
    LinesAuthError,
    LinesError,
    LinesKeyMissing,
    ingest_slate_lines,
    unique_games,
)
from ncaaf.depth import attach_depth_ranks, print_depth_board  # noqa: E402
from ncaaf.ourlads import DepthError, ingest_slate_depth  # noqa: E402
from ncaaf.players import filter_pool, load_fanduel_csv  # noqa: E402
from ncaaf.projections import (  # noqa: E402
    attach_team_lines,
    print_projection_board,
    projection_board,
)
from ncaaf.sim import (  # noqa: E402
    DEFAULT_DRAWS,
    DEFAULT_SEED,
    ILP_OBJECTIVES,
    SIM_OBJECTIVES,
    apply_ilp_objective,
    sim_header,
    simulate_games,
)
from ncaaf.props import (  # noqa: E402
    PropsError,
    PropsKeyMissing,
    attach_props,
    ingest_slate_props,
)
from ncaaf.mix import MixError, attach_mix, ingest_mix  # noqa: E402
from ncaaf.choke import (  # noqa: E402
    depth_id,
    emit,
    lines_id,
    mix_id,
    props_id,
    stamp,
)
from ncaaf.rules import FANDUEL_NCAAF  # noqa: E402
from ncaaf.interview import (  # noqa: E402
    InterviewError,
    LEAN_MULT,
    answers_from_flags,
    apply_interview,
    run_interview,
    selected_teams,
)
from ncaaf.leverage import print_smash_board, smash_board  # noqa: E402
from ncaaf.solver import Infeasible, solve  # noqa: E402
from ncaaf.teams import UnmappedTeam  # noqa: E402

FPPG_LABEL = "FPPG (prior season, not this slate)"
VEGAS_LABEL = (
    "implied team total × depth × position share × closing-script "
    "(±20% Odds prop tilt when a volume line joins; rank 1 is a role prior, "
    "not snaps)"
)


def _nonneg_int(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return n


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True, help="FanDuel players-list CSV")
    ap.add_argument("--out", help="also write the JSON payload to PATH")
    ap.add_argument("--agent", action="store_true", help="no prompts (spec A7b)")
    ap.add_argument(
        "--json",
        action="store_true",
        dest="json_stdout",
        help="print JSON on stdout even on a TTY",
    )
    ap.add_argument(
        "--interview-defaults",
        action="store_true",
        help="skip stdin grill; apply interview recommends (all/keep/qb/none)",
    )
    ap.add_argument(
        "--games",
        default="all",
        help="all | top | top:N | GAME,GAME (with --agent after a TUI grill)",
    )
    ap.add_argument(
        "--cut",
        choices=("exclusive", "lean"),
        default="lean",
        help="when --games is not all: drop other games or overweight them",
    )
    ap.add_argument(
        "--sit",
        choices=("keep", "ignore"),
        default="keep",
        help="blowout sit (≤ −21): keep script_mult or force 1.0 this run",
    )
    ap.add_argument(
        "--lock",
        action="append",
        default=None,
        help="force a player into the lineup (repeatable; names)",
    )
    ap.add_argument(
        "--fade",
        action="append",
        default=None,
        help="exclude a player from the pool (repeatable; names)",
    )
    ap.add_argument(
        "--use-fppg",
        action="store_true",
        help="opt-in: maximize CSV FPPG (prior season, not this slate)",
    )
    ap.add_argument(
        "--include-unprojected",
        action="store_true",
        help="FPPG mode only: keep rows with empty FPPG (scored as 0)",
    )
    ap.add_argument(
        "--exclude-questionable",
        action="store_true",
        help="drop Q/D injury flags (O is always dropped unless --keep-out)",
    )
    ap.add_argument(
        "--keep-out",
        action="store_true",
        help="do not drop Injury Indicator O",
    )
    ap.add_argument(
        "--greedy",
        action="store_true",
        help="skip PuLP even if installed (approximate)",
    )
    ap.add_argument(
        "--superflex",
        choices=("qb", "any"),
        default="qb",
        help="SuperFLEX: second QB (default) or any FanDuel-legal skill",
    )
    ap.add_argument(
        "--min-salary",
        type=int,
        default=FANDUEL_NCAAF.salary_floor,
        help="minimum combined salary (default 58000; 0 disables). Cap remains 60000.",
    )
    ap.add_argument("--year", type=int, help="season year for CFBD /lines (default: from CSV)")
    ap.add_argument("--week", type=int, help="CFBD week (omit to pull the whole season year)")
    ap.add_argument(
        "--lines-source",
        choices=("auto", "cfbd", "odds"),
        default="auto",
        help="betting source (auto: CFBD_API_KEY then ODDS_API_KEY)",
    )
    ap.add_argument(
        "--lines-json",
        help="replay a CFBD / Odds / simple-games JSON instead of a live API",
    )
    ap.add_argument(
        "--skip-depth",
        action="store_true",
        help="do not fetch/join OurLads depth (role prior stays unlisted)",
    )
    ap.add_argument(
        "--refresh-depth",
        action="store_true",
        help="bypass OurLads HTML cache",
    )
    ap.add_argument(
        "--skip-props",
        action="store_true",
        help="do not pull Odds API player props",
    )
    ap.add_argument(
        "--refresh-props",
        action="store_true",
        help="bypass Odds API player-prop cache (burns credits)",
    )
    ap.add_argument(
        "--skip-mix",
        action="store_true",
        help="do not join CFBD 2026 pass mix / player usage",
    )
    ap.add_argument(
        "--refresh-mix",
        action="store_true",
        help="bypass CFBD mix/usage cache",
    )
    ap.add_argument(
        "--board",
        nargs="?",
        const="default",
        default=None,
        choices=("default", "all"),
        help="print pool projections on stderr (default: depth 1–2 + props; "
        "all = full pool). Same rows in JSON / --out as payload['board']",
    )
    ap.add_argument(
        "--sim",
        nargs="?",
        const=DEFAULT_DRAWS,
        default=0,
        type=_nonneg_int,
        metavar="N",
        help="Monte Carlo FD-point draws per player (default 10000; 0 off). "
        "Game Monte Carlo (Vegas total+spread, teammates share the world). "
        "Percentiles on --board; lineup Fl/Cl are the joint 7. "
        "floor/ceiling ILP runs this even when --sim is omitted.",
    )
    ap.add_argument(
        "--sim-seed",
        type=int,
        default=DEFAULT_SEED,
        help="RNG seed for --sim (default 1)",
    )
    ap.add_argument(
        "--objective",
        choices=ILP_OBJECTIVES,
        default="mean",
        help="ILP score: mean = week1_score (default; not sim p50); "
        "floor = sim p10; ceiling = sim p99. --use-fppg only with mean.",
    )
    ap.add_argument(
        "--leverage",
        choices=("on", "off"),
        default="on",
        help="GPP seats on the mean card: ≥1 +14 WR1/TE1 and ≥1 +21 dog QB "
        "when the pool has them (default on). off = pure implied mean.",
    )
    return ap.parse_args(argv)


def fppg_objective_error(use_fppg: bool, objective: str) -> str | None:
    """--use-fppg is prior-season FPPG; do not silently mix with sim p10/p90."""
    kind = (objective or "mean").lower()
    if use_fppg and kind != "mean":
        return (
            f"--use-fppg cannot combine with --objective={kind} "
            "(FPPG is prior-season; floor/ceiling need sim p10/p99)"
        )
    return None


def _lean_pids(pool, games, answers) -> frozenset[str]:
    """Pids interview already scaled by LEAN_MULT (selected games, not exclusive)."""
    if (
        answers.games_mode == "all"
        or answers.exclusive
        or not answers.selected_games
    ):
        return frozenset()
    teams = selected_teams(games, answers.selected_games)
    if not teams:
        return frozenset()
    return frozenset(p.pid for p in pool if p.team in teams)


def _sim_n(args) -> int:
    n = int(args.sim or 0)
    if args.objective in SIM_OBJECTIVES and n <= 0:
        return DEFAULT_DRAWS
    return n


def infer_season_year(csv_path: Path) -> int:
    m = re.search(r"CFB-(\d{4})", csv_path.name)
    if m:
        return int(m.group(1))
    return date.today().year


def _print_games(games) -> None:
    if not games:
        return
    print(format_games_table(games), file=sys.stderr)


def _write_payload(payload: dict, args) -> None:
    text = json.dumps(payload, indent=2)
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    if json_stdout_enabled(agent=args.agent, json_flag=args.json_stdout):
        print(text)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    mix = fppg_objective_error(args.use_fppg, args.objective)
    if mix:
        emit("OBJECTIVE_FPPG", mix)
        return 1
    try:
        sflx = (
            frozenset({"QB", "RB", "WR", "TE"})
            if args.superflex == "any"
            else frozenset({"QB"})
        )
        rules = replace(
            FANDUEL_NCAAF,
            salary_floor=args.min_salary,
            superflex_eligible=sflx,
        )
    except ValueError as e:
        emit("SALARY_BOUNDS", str(e))
        return 1
    csv_path = Path(args.csv).expanduser()
    try:
        raw = load_fanduel_csv(csv_path)
    except (OSError, ValueError) as e:
        emit("CSV_FANDUEL", str(e))
        return 1

    pool = filter_pool(
        raw,
        drop_out=not args.keep_out,
        drop_questionable=args.exclude_questionable,
        include_unprojected=args.include_unprojected,
        require_fppg=args.use_fppg,
        out_codes=FANDUEL_NCAAF.out_codes,
        questionable_codes=FANDUEL_NCAAF.questionable_codes,
    )

    games: list = []
    games_payload: list[dict] = []
    projection_source = FPPG_LABEL if args.use_fppg else VEGAS_LABEL
    lines_meta: dict = {
        "mode": "fppg" if args.use_fppg else "vegas",
        "source": None,
        "provider": None,
        "year": None,
        "week": args.week,
    }

    if not args.use_fppg:
        year = args.year or infer_season_year(csv_path)
        lines_meta["year"] = year
        json_path = Path(args.lines_json).expanduser() if args.lines_json else None
        try:
            by_team = ingest_slate_lines(
                pool,
                year=year,
                week=args.week,
                source=args.lines_source,
                lines_json=json_path,
            )
        except LinesKeyMissing as e:
            emit("LINES_KEY", str(e))
            return 1
        except (LinesAuthError, LinesError, UnmappedTeam) as e:
            emit(lines_id(e), str(e))
            return 1
        pool = attach_team_lines(pool, by_team)
        games = unique_games(by_team)
        games_payload = [g.to_dict() for g in games]
        if games:
            lines_meta["source"] = games[0].source
            lines_meta["provider"] = games[0].provider
        _print_games(games)
        if not pool:
            emit("LINES_ATTACH", "no players left after attaching implied totals")
            return 1

        if not args.skip_depth:
            slate_teams = {p.team for p in pool} | {p.opponent for p in pool if p.opponent}
            try:
                depth_rows = ingest_slate_depth(
                    slate_teams, refresh=args.refresh_depth
                )
            except (DepthError, UnmappedTeam) as e:
                emit(depth_id(e), str(e))
                return 1
            pool, dstats = attach_depth_ranks(pool, depth_rows)
            print(
                f"depth matched {dstats['matched']} / {dstats['players']}  "
                f"rows {dstats['depth_rows']}",
                file=sys.stderr,
            )
            print_depth_board(depth_rows, pool)

        if not args.skip_mix:
            try:
                team_mix, usage = ingest_mix(year, refresh=args.refresh_mix)
            except MixError as e:
                emit(mix_id(e), str(e))
                return 1
            else:
                if not team_mix:
                    print("mix none (no 2026 CFBD advanced) — spread-only script", file=sys.stderr)
                else:
                    pool, mstats = attach_mix(pool, team_mix, usage)
                    print(
                        f"mix teams {mstats['teams_with_mix']}  "
                        f"usage {mstats['usage_joined']} / {mstats['players']}  "
                        f"cfbd {year}",
                        file=sys.stderr,
                    )

        if not args.skip_props:
            try:
                by_pid, pstats = ingest_slate_props(
                    pool, refresh=args.refresh_props
                )
            except PropsKeyMissing as e:
                emit("PROPS_ODDS_KEY", f"skip overlay — {e}")
            except PropsError as e:
                emit(props_id(e), str(e))
                return 1
            else:
                pool = attach_props(
                    pool, by_pid, unmatched=pstats.get("unmatched") or []
                )
                rem = pstats.get("credits_remaining")
                print(
                    f"props {pstats['players_with_props']} players  "
                    f"games {pstats['games_with_props']}/"
                    f"{pstats['games_with_props'] + pstats['games_unmatched']}"
                    + (f"  credits_left {rem}" if rem is not None else ""),
                    file=sys.stderr,
                )

    try:
        if args.agent:
            answers = answers_from_flags(args, games)
        else:
            tty = False
            try:
                tty = sys.stdin.isatty()
            except Exception:
                tty = False
            answers = run_interview(
                games,
                use_defaults=bool(args.interview_defaults or not tty),
            )
        pool, lock_ids = apply_interview(pool, games, answers)
    except InterviewError as e:
        emit("INTERVIEW", str(e))
        return 1

    sflx = (
        frozenset({"QB", "RB", "WR", "TE"})
        if answers.superflex == "any"
        else frozenset({"QB"})
    )
    rules = replace(rules, superflex_eligible=sflx)

    print(
        f"pool {len(pool)} / {len(raw)} players  "
        f"floor ${rules.salary_floor:,}  cap ${rules.salary_cap:,}",
        file=sys.stderr,
    )
    print(
        f"interview games={answers.games_mode} "
        f"sit={'keep' if answers.apply_sit else 'ignore'} "
        f"superflex={answers.superflex}",
        file=sys.stderr,
    )
    payload = {
        "title": "FanDuel NCAAF classic lineup",
        "date": date.today().isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "site": FANDUEL_NCAAF.site,
        "sport": FANDUEL_NCAAF.sport,
        "csv": str(csv_path),
        "salary_cap": rules.salary_cap,
        "salary_floor": rules.salary_floor,
        "min_teams": FANDUEL_NCAAF.min_teams,
        "max_per_team": FANDUEL_NCAAF.max_per_team,
        "pool_size": len(pool),
        "raw_size": len(raw),
        "projection_source": projection_source,
        "objective": args.objective,
        "lines": lines_meta,
        "games": games_payload,
        "interview": answers.to_dict(),
        "flags": {
            "use_fppg": args.use_fppg,
            "include_unprojected": args.include_unprojected,
            "exclude_questionable": args.exclude_questionable,
            "keep_out": args.keep_out,
            "greedy": args.greedy,
            "skip_depth": args.skip_depth,
            "skip_props": args.skip_props,
            "skip_mix": args.skip_mix,
            "min_salary": args.min_salary,
            "superflex": answers.superflex,
            "interview_defaults": bool(args.interview_defaults),
            "games": args.games,
            "cut": args.cut,
            "sit": args.sit,
            "board": args.board,
            "sim": args.sim,
            "sim_seed": args.sim_seed,
            "objective": args.objective,
            "leverage": args.leverage,
        },
    }
    sim_n = _sim_n(args)
    sim_by_pid: dict = {}
    game_sim = None
    if sim_n > 0:
        print(sim_header(sim_n), file=sys.stderr)
        game_sim = simulate_games(pool, n=sim_n, seed=args.sim_seed)
        sim_by_pid = game_sim.by_pid
    board_mode = None
    board_rows: list[dict] = []
    if args.board:
        board_mode = "all" if args.board == "all" else "default"
        board_rows = projection_board(
            pool, mode=board_mode, sim_by_pid=sim_by_pid or None
        )
        payload["board"] = board_rows
    if args.objective in SIM_OBJECTIVES:
        pool = apply_ilp_objective(
            pool,
            args.objective,
            sim_by_pid=sim_by_pid,
            lean_pids=_lean_pids(pool, games, answers),
            lean_mult=LEAN_MULT,
        )
    use_leverage = _leverage_on(args)
    try:
        lineup = solve(
            pool,
            rules,
            prefer_ilp=not args.greedy,
            lock_ids=lock_ids,
            leverage=use_leverage,
        )
    except Infeasible as e:
        if use_leverage:
            print(
                "leverage infeasible — retrying without dog WR1/QB seats",
                file=sys.stderr,
            )
            try:
                lineup = solve(
                    pool,
                    rules,
                    prefer_ilp=not args.greedy,
                    lock_ids=lock_ids,
                    leverage=False,
                )
                lineup.notes.append(
                    "leverage infeasible; fell back to unconstrained mean"
                )
            except Infeasible:
                payload["status"] = "infeasible"
                stamp(payload, "SOLVER_INFEASIBLE", str(e))
                _write_payload(payload, args)
                emit("SOLVER_INFEASIBLE", str(e))
                if board_rows:
                    print_projection_board(
                        board_rows,
                        mode=board_mode or "default",
                        sim_n=sim_n or None,
                    )
                return 2
        else:
            payload["status"] = "infeasible"
            stamp(payload, "SOLVER_INFEASIBLE", str(e))
            _write_payload(payload, args)
            emit("SOLVER_INFEASIBLE", str(e))
            if board_rows:
                print_projection_board(
                    board_rows,
                    mode=board_mode or "default",
                    sim_n=sim_n or None,
                )
            return 2

    payload["status"] = "ok"
    lu = lineup.to_dict()
    if sim_by_pid:
        _attach_sim(lu, sim_by_pid, game_sim=game_sim)
        lu["smash"] = smash_board(pool, sim_by_pid)
    payload["lineup"] = lu
    _write_payload(payload, args)
    print(
        f"{lu['method']}  objective {args.objective}  "
        f"remain ${lu['salary_remaining']:,}  teams {lu['teams']}",
        file=sys.stderr,
    )
    _print_picker(lu)
    if lu.get("smash"):
        print_smash_board(lu["smash"])
    if board_rows:
        print_projection_board(
            board_rows,
            mode=board_mode or "default",
            sim_n=sim_n or None,
        )
    return 0


def _leverage_on(args) -> bool:
    """Mean/ceiling get GPP seats; floor is cash — no dog lotteries."""
    if (getattr(args, "leverage", "on") or "on") == "off":
        return False
    return (args.objective or "mean") != "floor"


def _print_picker(lu: dict, *, title: str | None = None) -> None:
    print(format_picker_table(lu, title=title), file=sys.stderr)


def _attach_sim(lu: dict, sim_by_pid: dict, *, game_sim=None) -> None:
    """Copy mean/floor/ceiling (and p10/p50/p90) onto picker + slots.

    Does not touch `projection` — that is the ILP score (week1_score, p10, or p99).
    Lineup Fl/Cl are the joint 7 in the same game worlds when `game_sim` is set.
    """
    for row in lu.get("picker") or []:
        st = sim_by_pid.get(row.get("id"))
        if st is not None:
            row.update(st.to_dict())
    for row in (lu.get("slots") or {}).values():
        st = sim_by_pid.get(row.get("id"))
        if st is not None:
            row.update(st.to_dict())
    if game_sim is None:
        return
    pids = [str(row.get("id")) for row in lu.get("picker") or [] if row.get("id")]
    joint = game_sim.lineup_stats(pids)
    if joint is None:
        return
    lu["lineup_floor"] = round(joint.p10, 4)
    lu["lineup_ceiling"] = round(joint.p99, 4)
    lu["lineup_sim_mean"] = round(joint.mean, 4)


if __name__ == "__main__":
    sys.exit(main())
