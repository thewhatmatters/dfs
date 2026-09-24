#!/usr/bin/env python3
"""Optimize a FanDuel NFL classic lineup from a players-list CSV.

I/O: flags in → picker tables on stderr. JSON on stdout for --agent / --json
/ non-TTY; --out always writes the file. Exit 0 on a feasible lineup, 2 if
infeasible, 1 on usage/IO errors.
Default objective is week1_score (implied×depth×share×usage, ±20% prop tilt). No FPPG.
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
from nfl.gangstash import (  # noqa: E402
    GangstashDataError,
    GangstashDataKeyMissing,
    GangstashTruncated,
)
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
    DIVERSITY_CHOICES,
    DIVERSITY_COVERAGE,
    FANDUEL_MAX_PER_TEAM,
    FANDUEL_NFL,
    HOUSE_CASH_LINE,
    MAX_EXPOSURE_DEFAULT,
    MAX_LINEUPS,
    MIN_UNIQUE_DEFAULT,
    MIN_UNIQUE_MULTI_DEFAULT,
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
from nfl.slate_status import build_slate_status, format_slate_status  # noqa: E402
from nfl.solver import Infeasible, Lineup, solve_many  # noqa: E402
from nfl.snaps import (  # noqa: E402
    DEFAULT_OUT as DEFAULT_SNAPS_CSV,
    SnapsError,
    attach_snaps,
    load_optimizer_snaps,
    print_snaps_gaps,
)
from nfl.targets import (  # noqa: E402
    DEFAULT_OUT as DEFAULT_TARGETS_CSV,
    TargetsError,
    attach_targets,
    load_optimizer_targets,
    print_targets_gaps,
)
from nfl.teams import UnmappedTeam  # noqa: E402
from nfl.upload import export_lineups  # noqa: E402

VEGAS_LABEL = (
    "Gangstash player-prop FD points when volume lines join; else implied "
    "team total × depth × position share × target/snap usage tilt "
    "(DEF: opp implied PA bucket + 3.0)"
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


def _max_exposure(value: str) -> float:
    x = float(value)
    if x <= 0 or x > 1:
        raise argparse.ArgumentTypeError("must be in (0, 1]; 1 disables")
    return x


def _apply_multi_lineup_defaults(args: argparse.Namespace) -> argparse.Namespace:
    """n>1 defaults: min-unique 3, max-exposure 0.60, diversity=coverage."""
    n = args.n_lineups
    if args.min_unique is None:
        args.min_unique = MIN_UNIQUE_MULTI_DEFAULT if n > 1 else MIN_UNIQUE_DEFAULT
    if args.max_exposure is None:
        args.max_exposure = MAX_EXPOSURE_DEFAULT if n > 1 else 1.0
    if args.diversity is None:
        args.diversity = (
            DIVERSITY_COVERAGE if (args.coverage or n > 1) else "chalk"
        )
    elif args.coverage:
        args.diversity = DIVERSITY_COVERAGE
    return args


def _max_per_team(value: str) -> int:
    n = int(value)
    if n < 1 or n > FANDUEL_MAX_PER_TEAM:
        raise argparse.ArgumentTypeError(f"must be 1..{FANDUEL_MAX_PER_TEAM}")
    return n


def _positive_week(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def _gangstash_snaps_weeks(
    source: str,
    snaps_weeks: list[int] | None,
    targets_weeks: list[int] | None,
) -> list[int] | None:
    """Explicit snaps window, else the targets window, else every returned week."""
    if snaps_weeks is not None:
        return snaps_weeks
    if (source or "lineups").strip().lower() == "gangstash":
        return targets_weeks
    return None


def _week_list(value: str) -> list[int]:
    parts = [p.strip() for p in (value or "").split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError("comma-separated weeks, e.g. 1,2")
    out: list[int] = []
    for part in parts:
        n = int(part)
        if n < 1:
            raise argparse.ArgumentTypeError("weeks must be >= 1")
        out.append(n)
    return out


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
        "--lines-source",
        choices=("oddsapi", "gangstash"),
        default="oddsapi",
        help="game lines provider (default oddsapi). gangstash uses "
        "GANGSTASH_API_KEY. --lines-json still wins.",
    )
    ap.add_argument(
        "--skip-depth",
        action="store_true",
        help="do not fetch/join OurLads depth (role prior stays unlisted)",
    )
    ap.add_argument(
        "--refresh-depth",
        action="store_true",
        help="bypass OurLads HTML cache (slate teams only)",
    )
    ap.add_argument(
        "--depth-source",
        choices=("ourlads", "espn", "gangstash"),
        default="ourlads",
        help="depth provider (default ourlads; espn or gangstash are optional)",
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
        "--skip-targets",
        action="store_true",
        help="do not join Lineups RB/WR/TE targets (usage uses snaps or 1.0)",
    )
    ap.add_argument(
        "--targets-csv",
        type=Path,
        default=DEFAULT_TARGETS_CSV,
        help=f"Lineups targets CSV (default: {DEFAULT_TARGETS_CSV})",
    )
    ap.add_argument(
        "--targets-source",
        choices=("lineups", "gangstash"),
        default="lineups",
        help="RB/WR/TE target share source (default lineups CSV). "
        "gangstash uses sum(targets)/sum(team_targets) over the week window.",
    )
    ap.add_argument(
        "--targets-week",
        type=_positive_week,
        default=None,
        metavar="N",
        help="targets week to join (lineups CSV default: latest week; "
        "gangstash: that single week)",
    )
    ap.add_argument(
        "--targets-weeks",
        type=_week_list,
        default=None,
        metavar="LIST",
        help="gangstash target window, comma-separated (e.g. 1,2). "
        "Share is sum(targets)/sum(team_targets). Ignored for lineups.",
    )
    ap.add_argument(
        "--refresh-targets",
        action="store_true",
        help="bypass the gangstash targets day cache "
        "(lineups refresh stays: python3 -m nfl.targets --refresh)",
    )
    ap.add_argument(
        "--skip-snaps",
        action="store_true",
        help="do not join snap counts (RB usage uses targets or 1.0)",
    )
    ap.add_argument(
        "--snaps-source",
        choices=("lineups", "gangstash"),
        default="lineups",
        help="snap counts provider (default lineups CSV). gangstash uses "
        "dataset=snaps and offense_pct as the 0-1 snap_share.",
    )
    ap.add_argument(
        "--snaps-csv",
        type=Path,
        default=DEFAULT_SNAPS_CSV,
        help=f"Lineups snaps CSV (default: {DEFAULT_SNAPS_CSV})",
    )
    ap.add_argument(
        "--snaps-week",
        type=_positive_week,
        default=None,
        metavar="N",
        help="snaps.csv week to join (default: latest week in the CSV)",
    )
    ap.add_argument(
        "--snaps-weeks",
        type=_week_list,
        default=None,
        metavar="LIST",
        help="gangstash snap window, comma-separated (e.g. 1,2). "
        "If omitted, uses --targets-weeks, else every week returned. "
        "Ignored for lineups.",
    )
    ap.add_argument(
        "--refresh-snaps",
        action="store_true",
        help="bypass the gangstash snaps day cache "
        "(lineups refresh stays: python3 -m nfl.snaps --refresh)",
    )
    ap.add_argument(
        "--skip-props",
        action="store_true",
        help="do not pull Gangstash player props",
    )
    ap.add_argument(
        "--refresh-props",
        action="store_true",
        help="bypass the Gangstash player-prop cache and refetch",
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
        default=None,
        metavar="N",
        help="min different players vs **every** locked 9 (default 2 when n=1; "
        "3 when n>1). Overlap ≤ 9−N. --min-unique=2 restores the old default.",
    )
    ap.add_argument(
        "--max-exposure",
        type=_max_exposure,
        default=None,
        metavar="F",
        help="max fraction of the set any one player may appear in "
        f"(default {MAX_EXPOSURE_DEFAULT:.2f} when n>1; 1.0 when n=1). "
        "--max-exposure=1 disables. Enforced as a running count in the ILP.",
    )
    ap.add_argument(
        "--diversity",
        choices=DIVERSITY_CHOICES,
        default=None,
        help="multi-lineup objective. chalk = keep maximizing mean for every 9. "
        "coverage = lineup #1 is mean-optimal, later 9s soft-penalize "
        "high-exposure and unmatched-Lineups fillers (default coverage when n>1).",
    )
    ap.add_argument(
        "--coverage",
        action="store_true",
        help="alias for --diversity=coverage",
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
        help="write FanDuel upload CSV (Id:Nickname, picker order). "
        "Uses nfl/data/FanDuel-NFL-*-entries-upload-template.csv when present "
        "(keeps entry_id); legacy QB-first templates still work.",
    )
    ap.add_argument(
        "--slate-status",
        nargs="?",
        const="only",
        default=None,
        choices=("only", "with-solve"),
        help="print source-coverage after ingest (cash-relevant salary "
        "bands; full pool on the all-pool line). "
        "--slate-status (or =only) exits 0 after the report; "
        "--slate-status=with-solve prints then solves. "
        "JSON always includes slate_status after a successful ingest.",
    )
    return _apply_multi_lineup_defaults(ap.parse_args(argv))


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

    n_raw = len(raw)
    pool = filter_pool(
        raw,
        drop_out=not args.keep_out,
        drop_questionable=args.exclude_questionable,
        out_codes=FANDUEL_NFL.out_codes,
        questionable_codes=FANDUEL_NFL.questionable_codes,
    )
    n_after_ir = len(pool)

    slate_day = infer_slate_date(csv_path)
    json_path = Path(args.lines_json).expanduser() if args.lines_json else None
    try:
        by_team = ingest_slate_lines(
            pool,
            lines_json=json_path,
            slate_day=slate_day,
            source=args.lines_source,
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

    istats: dict = {"skipped": True, "dropped": 0, "unmatched": 0}
    if not args.skip_injuries:
        try:
            pool, istats = ingest_slate_injuries(
                pool, refresh=args.refresh_injuries
            )
        except InjuryError as e:
            emit(inj_id(e), str(e))
            return 1
        istats = {**istats, "skipped": False}
        print(
            f"injuries dropped {istats['dropped']}  unmatched {istats['unmatched']}",
            file=sys.stderr,
        )
    n_after_inj = len(pool)

    dstats: dict = {"skipped": True, "matched": 0, "players": 0}
    if not args.skip_depth:
        slate_teams = {p.team for p in pool} | {p.opponent for p in pool if p.opponent}
        try:
            depth_rows = ingest_slate_depth(
                slate_teams,
                refresh=args.refresh_depth,
                source=args.depth_source,
            )
        except (DepthError, UnmappedTeam) as e:
            emit(depth_id(e), str(e))
            return 1
        pool, dstats = attach_depth_ranks(
            pool,
            depth_rows,
            score_fn=lambda pl, rank: score_player(pl, rank),
            source=args.depth_source,
        )
        dstats = {**dstats, "skipped": False, "source": args.depth_source}
        print(
            f"depth matched {dstats['matched']} / {dstats['players']}  "
            f"rows {dstats['depth_rows']}",
            file=sys.stderr,
        )

    tstats: dict = {"skipped": True}
    if args.skip_targets:
        print("targets skipped", file=sys.stderr)
    else:
        if args.targets_source == "lineups" and args.targets_weeks:
            print(
                "targets-weeks applies to --targets-source=gangstash; "
                "lineups join uses --targets-week",
                file=sys.stderr,
            )
        tpath = Path(args.targets_csv).expanduser()
        try:
            trows, tmeta = load_optimizer_targets(
                source=args.targets_source,
                csv_path=tpath,
                week=args.targets_week,
                weeks=args.targets_weeks,
                season=slate_day.year,
                refresh=args.refresh_targets,
            )
        except TargetsError as e:
            emit(e.choke, str(e))
            if e.choke != "TARGETS_CSV":
                return 1
            tstats = {
                "skipped": True,
                "csv": str(tpath),
                "choke": e.choke,
                "error": str(e),
                "source": args.targets_source,
            }
        except GangstashDataKeyMissing as e:
            emit("TARGETS_GANGSTASH_KEY", str(e))
            tstats = {
                "skipped": True,
                "choke": "TARGETS_GANGSTASH_KEY",
                "error": str(e),
                "source": "gangstash",
            }
        except GangstashTruncated as e:
            emit("TARGETS_GANGSTASH", str(e))
            return 1
        except UnmappedTeam as e:
            emit("TARGETS_JOIN", str(e))
            return 1
        except GangstashDataError as e:
            emit("TARGETS_GANGSTASH", str(e))
            return 1
        else:
            if args.targets_source == "gangstash":
                pool, tstats = attach_targets(
                    pool, trows, week=tmeta.get("join_week")
                )
            else:
                pool, tstats = attach_targets(pool, trows, week=args.targets_week)
                tstats["csv"] = str(tpath)
            tstats["source"] = tmeta.get("source", args.targets_source)
            if tmeta.get("cache"):
                tstats["cache"] = tmeta.get("cache")
            if tmeta.get("cache_stale"):
                tstats["cache_stale"] = True
                print(
                    f"targets using cache {tmeta.get('cache')} "
                    "(live gangstash unreachable or key unset)",
                    file=sys.stderr,
                )
            if tmeta.get("weeks") is not None:
                tstats["weeks"] = tmeta.get("weeks")
            print_targets_gaps(tstats)

    sstats: dict = {"skipped": True}
    if args.skip_snaps:
        print("snaps skipped", file=sys.stderr)
    else:
        if args.snaps_source == "lineups" and args.snaps_weeks:
            print(
                "snaps-weeks applies to --snaps-source=gangstash; "
                "lineups join uses --snaps-week",
                file=sys.stderr,
            )
        snaps_weeks = _gangstash_snaps_weeks(
            args.snaps_source, args.snaps_weeks, args.targets_weeks
        )
        spath = Path(args.snaps_csv).expanduser()
        try:
            srows, smeta = load_optimizer_snaps(
                source=args.snaps_source,
                csv_path=spath,
                week=args.snaps_week,
                weeks=snaps_weeks,
                season=slate_day.year,
                refresh=args.refresh_snaps,
            )
        except SnapsError as e:
            emit(e.choke, str(e))
            if e.choke != "SNAPS_CSV":
                return 1
            sstats = {
                "skipped": True,
                "csv": str(spath),
                "choke": e.choke,
                "error": str(e),
                "source": args.snaps_source,
            }
        except GangstashDataKeyMissing as e:
            emit("SNAPS_GANGSTASH_KEY", str(e))
            sstats = {
                "skipped": True,
                "choke": "SNAPS_GANGSTASH_KEY",
                "error": str(e),
                "source": "gangstash",
            }
        except GangstashTruncated as e:
            emit("SNAPS_GANGSTASH", str(e))
            return 1
        except UnmappedTeam as e:
            emit("SNAPS_JOIN", str(e))
            return 1
        except GangstashDataError as e:
            emit("SNAPS_GANGSTASH", str(e))
            return 1
        else:
            if args.snaps_source == "gangstash":
                pool, sstats = attach_snaps(
                    pool, srows, week=smeta.get("join_week")
                )
            else:
                pool, sstats = attach_snaps(pool, srows, week=args.snaps_week)
                sstats["csv"] = str(spath)
            sstats["source"] = smeta.get("source", args.snaps_source)
            if smeta.get("cache"):
                sstats["cache"] = smeta.get("cache")
            if smeta.get("cache_stale"):
                sstats["cache_stale"] = True
                print(
                    f"snaps using cache {smeta.get('cache')} "
                    "(live gangstash unreachable or key unset)",
                    file=sys.stderr,
                )
            if smeta.get("weeks") is not None:
                sstats["weeks"] = smeta.get("weeks")
            print_snaps_gaps(sstats)

    pstats: dict = {"skipped": True, "reason": "skip-props"}
    if not args.skip_props:
        try:
            by_pid, pstats = ingest_slate_props(
                pool, refresh=args.refresh_props, slate_day=slate_day
            )
        except PropsKeyMissing as e:
            emit("PROPS_GANGSTASH_KEY", f"skip overlay — {e}")
            pstats = {"skipped": True, "reason": "PROPS_GANGSTASH_KEY"}
        except PropsError as e:
            emit(props_id(e), str(e))
            return 1
        else:
            pstats = {**pstats, "skipped": False}
            pool = attach_props(
                pool, by_pid, unmatched=pstats.get("unmatched") or []
            )
            print(
                f"props {pstats['players_with_props']} players  "
                f"games {pstats['games_with_props']}/"
                f"{pstats['games_with_props'] + pstats['games_unmatched']}"
                f"  source {pstats.get('source', 'gangstash')}",
                file=sys.stderr,
            )
            if pstats.get("cache_stale"):
                print(
                    f"props using cache {pstats.get('cache')} "
                    "(live gangstash unreachable or key unset)",
                    file=sys.stderr,
                )
            unmapped = pstats.get("unmapped_props") or {}
            if unmapped:
                shown = ", ".join(
                    f"{name} ({count})" for name, count in sorted(unmapped.items())
                )
                print(f"props unmapped (not scored): {shown}", file=sys.stderr)

    print(
        f"pool {len(pool)} / {len(raw)} players  "
        f"floor ${rules.salary_floor:,}  cap ${rules.salary_cap:,}",
        file=sys.stderr,
    )
    slate_status = build_slate_status(
        raw_n=n_raw,
        after_ir_n=n_after_ir,
        after_inj_n=n_after_inj,
        pool=pool,
        depth_source=args.depth_source,
        depth=dstats,
        targets=tstats,
        snaps=sstats,
        props=pstats,
        injuries=istats,
        flags={
            "skip_depth": args.skip_depth,
            "skip_targets": args.skip_targets,
            "skip_snaps": args.skip_snaps,
            "skip_props": args.skip_props,
            "skip_injuries": args.skip_injuries,
            "depth_source": args.depth_source,
            "lines_source": args.lines_source,
            "targets_source": args.targets_source,
        },
    )
    if args.slate_status:
        print(format_slate_status(slate_status), file=sys.stderr)
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
        "targets": tstats,
        "snaps": sstats,
        "slate_status": slate_status,
        "flags": {
            "exclude_questionable": args.exclude_questionable,
            "keep_out": args.keep_out,
            "greedy": args.greedy,
            "skip_depth": args.skip_depth,
            "depth_source": args.depth_source,
            "lines_source": args.lines_source,
            "skip_targets": args.skip_targets,
            "targets_source": args.targets_source,
            "targets_csv": str(args.targets_csv),
            "targets_week": args.targets_week,
            "targets_weeks": args.targets_weeks,
            "refresh_targets": args.refresh_targets,
            "skip_snaps": args.skip_snaps,
            "snaps_source": args.snaps_source,
            "snaps_csv": str(args.snaps_csv),
            "snaps_week": args.snaps_week,
            "snaps_weeks": args.snaps_weeks,
            "refresh_snaps": args.refresh_snaps,
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
            "max_exposure": args.max_exposure,
            "diversity": args.diversity,
            "bring_back": args.bring_back,
            "max_per_team": args.max_per_team,
            "stack_qb": args.stack_qb,
            "upload": args.upload,
        },
        "bring_back": args.bring_back,
        "cash_line": args.cash_line,
        "n_lineups_requested": args.n_lineups,
        "min_unique": args.min_unique,
        "max_exposure": args.max_exposure,
        "diversity": args.diversity,
    }
    if args.slate_status == "only":
        payload["status"] = "ok"
        _write_payload(payload, args)
        return 0
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
            f"max_exposure {args.max_exposure:g}  diversity {args.diversity} "
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
            max_exposure=args.max_exposure,
            diversity=args.diversity,
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
