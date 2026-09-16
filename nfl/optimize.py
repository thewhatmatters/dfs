#!/usr/bin/env python3
"""Optimize a FanDuel NFL classic lineup from a players-list CSV.

I/O: flags in → picker tables on stderr. JSON on stdout for --agent / --json
/ non-TTY; --out always writes the file. Exit 0 on a feasible lineup, 2 if
infeasible, 1 on usage/IO errors.
Default objective is week1_score (implied×depth×share, ±20% prop tilt). No FPPG.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from cli_table import (  # noqa: E402
    format_games_table,
    format_picker_table,
    format_table,
    json_stdout_enabled,
    money,
    one_dec,
)
from nfl.choke import (  # noqa: E402
    depth_id,
    emit,
    inj_id,
    lines_id,
    props_id,
    stamp,
)
from nfl.depth import DepthError, attach_depth_ranks, ingest_slate_depth  # noqa: E402
from nfl.injuries import InjuryError, ingest_slate_injuries  # noqa: E402
from nfl.lines import (  # noqa: E402
    LinesAuthError,
    LinesError,
    LinesKeyMissing,
    infer_slate_date,
    ingest_slate_lines,
    unique_games,
)
from nfl.players import filter_pool, load_fanduel_csv  # noqa: E402
from nfl.projections import (  # noqa: E402
    attach_team_lines,
    print_projection_board,
    projection_board,
    score_player,
)
from nfl.props import (  # noqa: E402
    PropsError,
    PropsKeyMissing,
    attach_props,
    ingest_slate_props,
)
from nfl.rules import (  # noqa: E402
    FANDUEL_MAX_PER_TEAM,
    FANDUEL_NFL,
    HOUSE_CASH_LINE,
    MAX_LINEUPS,
    MIN_UNIQUE_DEFAULT,
)
from nfl.sim import (  # noqa: E402
    DEFAULT_DRAWS,
    DEFAULT_SEED,
    ILP_OBJECTIVES,
    SIM_OBJECTIVES,
    apply_ilp_objective,
    sim_header,
    simulate_games,
)
from nfl.solver import Infeasible, Lineup, solve_many  # noqa: E402
from nfl.teams import UnmappedTeam  # noqa: E402
from nfl.upload import export_lineups  # noqa: E402

VEGAS_LABEL = (
    "Odds API player-prop FD points when volume lines join; else implied "
    "team total × depth × position share (DEF: opp implied PA bucket + 3.0)"
)


def _nonneg_int(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return n


def _n_lineups(value: str) -> int:
    n = int(value)
    if n < 1 or n > MAX_LINEUPS:
        raise argparse.ArgumentTypeError(f"must be 1..{MAX_LINEUPS}")
    return n


def _min_unique(value: str) -> int:
    n = int(value)
    roster_n = len(FANDUEL_NFL.slot_names)
    if n < 1 or n > roster_n:
        raise argparse.ArgumentTypeError(f"must be 1..{roster_n}")
    return n


def _max_per_team(value: str) -> int:
    n = int(value)
    if n < 1 or n > FANDUEL_MAX_PER_TEAM:
        raise argparse.ArgumentTypeError(f"must be 1..{FANDUEL_MAX_PER_TEAM}")
    return n


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True, help="FanDuel players-list CSV")
    ap.add_argument("--out", help="also write the JSON payload to PATH")
    ap.add_argument("--agent", action="store_true", help="JSON on stdout; no prompts")
    ap.add_argument(
        "--json",
        action="store_true",
        dest="json_stdout",
        help="print JSON on stdout even on a TTY",
    )
    ap.add_argument(
        "--exclude-questionable",
        action="store_true",
        help="drop CSV Q (ESPN Out/Doubtful/IR/Suspension already dropped)",
    )
    ap.add_argument(
        "--keep-out",
        action="store_true",
        help="do not drop CSV Injury Indicator IR/NA",
    )
    ap.add_argument(
        "--greedy",
        action="store_true",
        help="skip PuLP even if installed (approximate)",
    )
    ap.add_argument(
        "--min-salary",
        type=int,
        default=FANDUEL_NFL.salary_floor,
        help="minimum combined salary (default 58000; 0 disables). Cap remains 60000.",
    )
    ap.add_argument(
        "--lines-json",
        help="replay Odds / simple-games JSON instead of a live API",
    )
    ap.add_argument(
        "--skip-depth",
        action="store_true",
        help="do not fetch/join ESPN depth (role prior stays unlisted)",
    )
    ap.add_argument(
        "--refresh-depth",
        action="store_true",
        help="bypass ESPN depth cache",
    )
    ap.add_argument(
        "--skip-injuries",
        action="store_true",
        help="do not fetch/join ESPN injuries",
    )
    ap.add_argument(
        "--refresh-injuries",
        action="store_true",
        help="bypass ESPN injury cache",
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
        "--board",
        nargs="?",
        const="default",
        default=None,
        choices=("default", "all"),
        help="print pool projections on stderr (default: depth 1–2 + props + DST; "
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
        "Percentiles on --board; lineup Fl/Cl are the joint 9. "
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
        "floor = sim p10; ceiling = sim p90.",
    )
    ap.add_argument(
        "--cash-line",
        type=float,
        default=HOUSE_CASH_LINE,
        help="house cash line for ceiling gap (default 150; not FanDuel)",
    )
    ap.add_argument(
        "--n-lineups",
        type=_n_lineups,
        default=1,
        metavar="N",
        help=f"unique legal 9s from one scored pool (default 1; max {MAX_LINEUPS})",
    )
    ap.add_argument(
        "--min-unique",
        type=_min_unique,
        default=MIN_UNIQUE_DEFAULT,
        metavar="N",
        help="min different players vs the previous 9 (default 2; overlap ≤ 7)",
    )
    ap.add_argument(
        "--bring-back",
        type=_nonneg_int,
        default=0,
        metavar="N",
        help="require N opposing WR/TE/QB vs a QB pass stack (default 0 = off). "
        "No stack (solo QB) does not require a bring-back. RB/DST do not count.",
    )
    ap.add_argument(
        "--max-per-team",
        type=_max_per_team,
        default=FANDUEL_NFL.max_per_team,
        metavar="N",
        help="max players from one team (house default 3; FanDuel lobby 4). "
        "--max-per-team=4 restores lobby.",
    )
    ap.add_argument(
        "--stack-qb",
        choices=("on", "off"),
        default="on",
        help="require that team's QB when 2+ WR/TE share a team (default on). "
        "One WR without QB is legal. Two RBs without QB is legal. "
        "--stack-qb=off disables.",
    )
    ap.add_argument(
        "--upload",
        help="write FanDuel upload CSV (Id:Nickname, picker order)",
    )
    return ap.parse_args(argv)


def _sim_n(args) -> int:
    n = int(args.sim or 0)
    if args.objective in SIM_OBJECTIVES and n <= 0:
        return DEFAULT_DRAWS
    return n


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
    try:
        rules = replace(
            FANDUEL_NFL,
            salary_floor=args.min_salary,
            bring_back=args.bring_back,
            max_per_team=args.max_per_team,
            require_qb_with_two_pass_catchers=args.stack_qb == "on",
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
        out_codes=FANDUEL_NFL.out_codes,
        questionable_codes=FANDUEL_NFL.questionable_codes,
    )

    slate_day = infer_slate_date(csv_path)
    json_path = Path(args.lines_json).expanduser() if args.lines_json else None
    try:
        by_team = ingest_slate_lines(
            pool, lines_json=json_path, slate_day=slate_day
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
        _print_games(games)
    if not pool:
        emit("LINES_ATTACH", "no players left after attaching implied totals")
        return 1

    if not args.skip_injuries:
        try:
            pool, istats = ingest_slate_injuries(
                pool, refresh=args.refresh_injuries
            )
        except InjuryError as e:
            emit(inj_id(e), str(e))
            return 1
        print(
            f"injuries dropped {istats['dropped']}  unmatched {istats['unmatched']}",
            file=sys.stderr,
        )

    if not args.skip_depth:
        slate_teams = {p.team for p in pool} | {p.opponent for p in pool if p.opponent}
        try:
            depth_rows = ingest_slate_depth(
                slate_teams, refresh=args.refresh_depth
            )
        except (DepthError, UnmappedTeam) as e:
            emit(depth_id(e), str(e))
            return 1
        pool, dstats = attach_depth_ranks(
            pool,
            depth_rows,
            score_fn=lambda pl, rank: score_player(pl, rank),
        )
        print(
            f"depth matched {dstats['matched']} / {dstats['players']}  "
            f"rows {dstats['depth_rows']}",
            file=sys.stderr,
        )

    if not args.skip_props:
        try:
            by_pid, pstats = ingest_slate_props(
                pool, refresh=args.refresh_props, slate_day=slate_day
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

    print(
        f"pool {len(pool)} / {len(raw)} players  "
        f"floor ${rules.salary_floor:,}  cap ${rules.salary_cap:,}",
        file=sys.stderr,
    )
    payload = {
        "title": "FanDuel NFL classic lineup",
        "date": date.today().isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "site": FANDUEL_NFL.site,
        "sport": FANDUEL_NFL.sport,
        "csv": str(csv_path),
        "contest": "133104",
        "salary_cap": rules.salary_cap,
        "salary_floor": rules.salary_floor,
        "min_teams": FANDUEL_NFL.min_teams,
        "max_per_team": rules.max_per_team,
        "require_qb_with_two_pass_catchers": rules.require_qb_with_two_pass_catchers,
        "pool_size": len(pool),
        "raw_size": len(raw),
        "projection_source": VEGAS_LABEL,
        "objective": args.objective,
        "lines": {
            "mode": "vegas",
            "source": games[0].source if games else None,
            "provider": games[0].provider if games else None,
            "slate_day": slate_day.isoformat(),
        },
        "games": games_payload,
        "flags": {
            "exclude_questionable": args.exclude_questionable,
            "keep_out": args.keep_out,
            "greedy": args.greedy,
            "skip_depth": args.skip_depth,
            "skip_props": args.skip_props,
            "skip_injuries": args.skip_injuries,
            "min_salary": args.min_salary,
            "board": args.board,
            "sim": args.sim,
            "sim_seed": args.sim_seed,
            "objective": args.objective,
            "cash_line": args.cash_line,
            "n_lineups": args.n_lineups,
            "min_unique": args.min_unique,
            "bring_back": args.bring_back,
            "max_per_team": args.max_per_team,
            "stack_qb": args.stack_qb,
            "upload": args.upload,
        },
        "bring_back": args.bring_back,
        "cash_line": args.cash_line,
        "n_lineups_requested": args.n_lineups,
        "min_unique": args.min_unique,
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
        )
    if args.n_lineups > 1:
        print(
            f"solving {args.n_lineups} unique 9s  min_unique {args.min_unique} "
            f"(one scored pool)",
            file=sys.stderr,
        )
    try:
        lineups = solve_many(
            pool,
            rules,
            prefer_ilp=not args.greedy,
            n_lineups=args.n_lineups,
            min_unique=args.min_unique,
        )
    except Infeasible as e:
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
    lu_dicts: list[dict] = []
    for lineup in lineups:
        lu = lineup.to_dict()
        if sim_by_pid:
            _attach_sim(lu, sim_by_pid)
        _attach_totals(lu, lineup, sim_by_pid, args.cash_line, game_sim=game_sim)
        lu_dicts.append(lu)
    if len(lu_dicts) > 1:
        paired = sorted(
            zip(lineups, lu_dicts),
            key=lambda t: float(t[1].get("lineup_proj") or 0.0),
            reverse=True,
        )
        lineups = [obj for obj, _ in paired]
        lu_dicts = [d for _, d in paired]
    payload["lineup"] = lu_dicts[0]
    payload["lineups"] = lu_dicts
    payload["n_lineups"] = len(lu_dicts)
    _write_payload(payload, args)
    _print_lineups(lineups, lu_dicts, args, sim_by_pid)
    try:
        export_lineups(
            lineups,
            n_lineups=args.n_lineups,
            contest=payload["contest"],
            objective=args.objective,
            upload=args.upload,
            contest_ids={p.pid for p in raw},
        )
    except (OSError, ValueError) as e:
        emit("UPLOAD_CSV", str(e))
        return 1
    if board_rows:
        print_projection_board(
            board_rows,
            mode=board_mode or "default",
            sim_n=sim_n or None,
        )
    return 0


def _attach_sim(lu: dict, sim_by_pid: dict) -> None:
    """Copy mean/floor/ceiling (and p10/p50/p90) onto picker + slots.

    Does not touch `projection` — that is the ILP score (week1_score, p10, or p90).
    """
    for row in lu.get("picker") or []:
        st = sim_by_pid.get(row.get("id"))
        if st is not None:
            row.update(st.to_dict())
    for row in (lu.get("slots") or {}).values():
        st = sim_by_pid.get(row.get("id"))
        if st is not None:
            row.update(st.to_dict())


def _attach_totals(
    lu: dict,
    lineup: Lineup,
    sim_by_pid: dict,
    cash_line: float,
    *,
    game_sim=None,
) -> None:
    """lineup_proj = sum week1_score (not ILP obj).

    Floor/ceiling = joint-9 p10/p90 when `game_sim` is set; else sum of
    player p10/p90 (tests that pass only a by_pid dict).
    """
    players = list(lineup.slots.values())
    lu["lineup_proj"] = round(sum(score_player(p) for p in players), 4)
    if not sim_by_pid and game_sim is None:
        return
    pids = [p.pid for p in players]
    joint = game_sim.lineup_stats(pids) if game_sim is not None else None
    if joint is not None:
        floor = joint.p10
        ceiling = joint.p90
        lu["lineup_sim_mean"] = round(joint.mean, 4)
    else:
        floor = 0.0
        ceiling = 0.0
        for p in players:
            st = sim_by_pid.get(p.pid)
            if st is None:
                continue
            floor += st.p10
            ceiling += st.p90
    lu["lineup_floor"] = round(floor, 4)
    lu["lineup_ceiling"] = round(ceiling, 4)
    lu["cash_line"] = cash_line
    lu["ceiling_minus_cash"] = round(ceiling - cash_line, 4)


def _print_picker(lu: dict, *, title: str | None = None) -> None:
    print(format_picker_table(lu, title=title), file=sys.stderr)


def _print_lineups(
    lineups: list[Lineup],
    lu_dicts: list[dict],
    args,
    sim_by_pid: dict,
) -> None:
    lu = lu_dicts[0]
    print(
        f"{lu['method']}  objective {args.objective}  "
        f"remain ${lu['salary_remaining']:,}  teams {lu['teams']}",
        file=sys.stderr,
    )
    if len(lu_dicts) != args.n_lineups:
        print(
            f"lineups {len(lu_dicts)}/{args.n_lineups}  "
            f"(CBC infeasible after {len(lu_dicts)}; returning what we got)",
            file=sys.stderr,
        )
    if len(lu_dicts) > 1:
        print(_format_summary(lineups, lu_dicts, sim_by_pid), file=sys.stderr)
        print(file=sys.stderr)
        for i, row in enumerate(lu_dicts, start=1):
            proj = row.get("lineup_proj", row.get("projection"))
            title = f"#{i}  {float(proj or 0):.1f}"
            _print_picker(row, title=title)
            print(file=sys.stderr)
    else:
        _print_picker(lu)


def qb_wr_stack(lineup: Lineup) -> str:
    """QB+WR same team (FLEX WR counts). Empty if none."""
    qb = lineup.slots.get("QB")
    if qb is None:
        return ""
    wrs = [
        p
        for p in lineup.slots.values()
        if p.position == "WR" and p.team == qb.team
    ]
    if not wrs:
        return ""
    names = "+".join(p.name for p in wrs)
    return f"{qb.team} {names}"


def _format_summary(
    lineups: list[Lineup],
    lu_dicts: list[dict],
    sim_by_pid: dict,
) -> str:
    headers = ("#", "Proj", "Floor", "Ceiling", "Sal", "QB", "Stack", "DEF")
    rows: list[tuple[str, ...]] = []
    for i, (lu_obj, lu) in enumerate(zip(lineups, lu_dicts), start=1):
        qb = lu_obj.slots.get("QB")
        dst = lu_obj.slots.get("DEF")
        if sim_by_pid:
            floor_s = one_dec(lu.get("lineup_floor"))
            ceil_s = one_dec(lu.get("lineup_ceiling"))
        else:
            floor_s = "-"
            ceil_s = "-"
        rows.append(
            (
                str(i),
                one_dec(lu.get("lineup_proj")),
                floor_s,
                ceil_s,
                money(lu.get("salary")),
                qb.name if qb is not None else "",
                qb_wr_stack(lu_obj) or "-",
                dst.name if dst is not None else "",
            )
        )
    return format_table(headers, rows)


if __name__ == "__main__":
    sys.exit(main())
