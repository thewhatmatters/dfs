"""Per-player lineup notes: implied×depth, optional prop tilt, game script.

Pure functions. Lineup.to_dict and optimize stderr both read these so the
printed line and JSON cannot drift. Game script uses only spread, implied
totals, this team's attached props, and roster construction — never school
reputation or invented rush/target share.
"""

from __future__ import annotations

from collections.abc import Sequence

from ncaaf.ourlads import match_key
from ncaaf.players import Player
from ncaaf.projections import POS_FD_SHARE, usage_raw
from ncaaf.rules import FANDUEL_NCAAF
from ncaaf.script import BLOWOUT, SIT_RUN, script_mult

BOOK_LABEL = {"fanduel": "FanDuel", "median": "median books"}


def picker_name(name: str, starter: bool) -> str:
    """Stderr display name; starter gets ` (*)` after the nickname."""
    return f"{name} (*)" if starter else name


def is_starter(player: Player) -> bool:
    return player.depth_rank == 1


def prop_status_of(player: Player) -> str:
    """One of props | no_market | unmatched."""
    if player.prop_fd is not None or player.prop_status == "props":
        return "props"
    if player.prop_status == "unmatched":
        return "unmatched"
    return "no_market"


def applied_script_mult(player: Player) -> float:
    """script_mult on the implied base (props no longer skip it)."""
    if not player.script_applied:
        return 1.0
    return script_mult(
        player.position,
        pass_rate=player.pass_rate,
        opp_pass_rate=player.opp_pass_rate,
        team_spread=player.spread,
        depth_rank=player.depth_rank,
        opp_pass_ppa=player.opp_pass_ppa,
        opp_rush_ppa=player.opp_rush_ppa,
    )


def explain_player(
    player: Player,
    *,
    pool: Sequence[Player] = (),
    lineup: Sequence[Player] = (),
    slot_key: str | None = None,
) -> dict:
    """note / starter / prop_status / script_mult for one picker row."""
    status = prop_status_of(player)
    starter = is_starter(player)
    applied = applied_script_mult(player)
    head = [_objective_clause(player, status), _depth_clause(player)]
    note = _sentence(head)
    script = _script_clause(
        player, pool, lineup, slot_key=slot_key, applied_mult=applied
    )
    if script:
        note = f"{note} {script[0].upper()}{script[1:]}."
    return {
        "note": note,
        "starter": starter,
        "prop_status": status,
        "script_mult": round(applied, 4),
    }


def player_note(
    player: Player,
    *,
    pool: Sequence[Player] = (),
    lineup: Sequence[Player] = (),
    slot_key: str | None = None,
) -> str:
    return explain_player(
        player, pool=pool, lineup=lineup, slot_key=slot_key
    )["note"]


def lineup_script_notes(
    players: Sequence[Player],
    pool: Sequence[Player] = (),
) -> list[str]:
    """A few lineup-level lines (blowout favorites, books pricing rush)."""
    by_team: dict[str, Player] = {}
    for p in players:
        if p.spread is None:
            continue
        prev = by_team.get(p.team)
        if prev is None or abs(p.spread) > abs(prev.spread or 0):
            by_team[p.team] = p
    src = list(pool) if pool else list(players)
    rows: list[tuple[float, str]] = []
    for team, p in by_team.items():
        if p.spread is None or p.spread > -SIT_RUN:
            continue
        rushers = [
            x
            for x in src
            if x.team == team
            and x.position == "RB"
            and x.prop_rush_yds is not None
        ]
        rushers.sort(key=lambda x: x.prop_rush_yds or 0, reverse=True)
        bit = f"{team} {p.spread:g}: huge implied"
        if rushers:
            bit += f", books price rush on {_display_last(rushers[0].name)}"
        rows.append((p.spread, bit))
    rows.sort(key=lambda t: t[0])
    return [text for _sp, text in rows[:3]]


def _display_last(name: str) -> str:
    """Last name with Jr/Sr/II stripped (match_key), original casing."""
    mk = match_key(name)
    last = mk.split()[-1] if mk else ""
    for tok in reversed((name or "").split()):
        if last and match_key(tok) == last:
            return tok
    return last.title() if last else (name or "").split()[-1]


def _g(x: float) -> str:
    xf = float(x)
    if xf.is_integer():
        return str(int(xf))
    return f"{xf:g}"


def _sentence(parts: Sequence[str]) -> str:
    bits = [p.rstrip(" .;") for p in parts if p]
    if not bits:
        return ""
    return "; ".join(bits) + "."


def _book_label(player: Player) -> str:
    raw = (player.prop_book or "").strip()
    return BOOK_LABEL.get(raw.lower(), raw or "books")


def _prior_label(rank: int | None) -> str:
    if rank == 1:
        return "starter prior"
    if rank == 2:
        return "d2 prior"
    if rank == 3:
        return "d3 prior"
    if rank is not None:
        return f"d{rank} prior"
    return "unlisted prior"


def _depth_clause(player: Player) -> str:
    if player.depth_rank == 1:
        return "OurLads starter"
    if player.depth_rank == 2:
        return "OurLads d2"
    if player.depth_rank == 3:
        return "OurLads d3"
    if player.depth_rank is not None:
        return f"OurLads d{player.depth_rank}"
    return "OurLads unlisted — tiny prior"


def _prop_line_bits(player: Player) -> tuple[list[str], bool]:
    bits: list[str] = []
    volume = False
    if player.prop_pass_yds is not None:
        bits.append(f"{_g(player.prop_pass_yds)} pass yds")
        volume = True
    if player.prop_pass_tds is not None:
        bits.append(f"{_g(player.prop_pass_tds)} pass TDs")
    if player.prop_rush_yds is not None:
        bits.append(f"{_g(player.prop_rush_yds)} rush yds")
        volume = True
    if player.prop_rec_yds is not None:
        bits.append(f"{_g(player.prop_rec_yds)} rec yds")
        volume = True
    if player.prop_receptions is not None:
        bits.append(f"{_g(player.prop_receptions)} recs")
        volume = True
    return bits, volume


def _objective_clause(player: Player, status: str) -> str:
    if player.prop_fd is not None:
        bits, volume = _prop_line_bits(player)
        extra = ""
        if not volume and player.prop_pass_tds is not None:
            extra = " (pass TDs only)"
        lines = ", ".join(bits) if bits else f"{_g(player.prop_fd)} FD pts"
        return (
            f"Odds props ({_book_label(player)}): {lines}{extra} "
            "— ±20% tilt on implied"
        )
    if player.implied_total is None:
        if player.fppg is not None:
            return "CSV FPPG (prior season, not this slate)"
        return "no player props"
    why = ""
    if status == "unmatched":
        why = " (name unmatched vs books)"
    elif status == "no_market":
        why = " (no market)"
    share = POS_FD_SHARE.get((player.position or "WR").upper(), 0.22)
    raw = usage_raw(player.position, player.rush_share, player.target_share)
    if raw is not None:
        kind = "rush" if (player.position or "").upper() == "RB" else "pass"
        prior = f"usage {kind} {_g(raw)}"
    else:
        prior = _prior_label(player.depth_rank)
    return (
        f"no player props{why} — implied {_g(player.implied_total)} × "
        f"{prior} × {player.position} {share:.2f} share"
    )


def _team_pool(
    player: Player,
    pool: Sequence[Player],
    lineup: Sequence[Player],
) -> list[Player]:
    team = [x for x in pool if x.team == player.team]
    if team:
        return team
    return [x for x in lineup if x.team == player.team]


def _max_prop(
    players: Sequence[Player],
    positions: set[str],
    field: str,
) -> float | None:
    vals = [
        getattr(x, field)
        for x in players
        if x.position in positions and getattr(x, field) is not None
    ]
    return max(vals) if vals else None


def _script_clause(
    player: Player,
    pool: Sequence[Player],
    lineup: Sequence[Player],
    *,
    slot_key: str | None,
    applied_mult: float = 1.0,
) -> str | None:
    parts: list[str] = []
    team = _team_pool(player, pool, lineup)
    qb_pass = _max_prop(team, {"QB"}, "prop_pass_yds")
    rb_rush = _max_prop(team, {"RB"}, "prop_rush_yds")
    sc = FANDUEL_NCAAF.scoring
    spread = player.spread
    pos = (player.position or "").upper()
    haircut = applied_mult < 1.0 - 1e-9

    if (
        spread is not None
        and player.implied_total is not None
        and spread <= -BLOWOUT
    ):
        opp = (
            _g(player.implied_opp)
            if player.implied_opp is not None
            else "?"
        )
        clause = f"favorite {spread:g}, dog implied {opp}"
        if pos in {"WR", "TE"} and spread <= -SIT_RUN:
            if player.depth_rank == 1:
                clause += " — second-half sit, reduced starter usage"
            else:
                clause += " — second-half sit, reduced usage"
        elif pos == "QB" and spread <= -SIT_RUN:
            clause += " — less throwing late"
        elif pos == "RB" and spread <= -SIT_RUN:
            if player.depth_rank == 1:
                clause += " — featured back recedes if ahead; committee may eat"
            elif player.depth_rank in {2, 3}:
                clause += " — d2/d3 clock/garbage if ahead"
            else:
                clause += " — likely sit on the run if ahead"
        if haircut:
            clause += " (score haircut)"
        parts.append(clause)
    elif (
        spread is not None
        and player.implied_total is not None
        and spread >= BLOWOUT
    ):
        parts.append(
            f"dog {spread:+g}, implied {_g(player.implied_total)} "
            "— likely throw to keep up"
        )

    if player.opp_pass_ppa is not None and player.opp_rush_ppa is not None:
        gap = float(player.opp_pass_ppa) - float(player.opp_rush_ppa)
        if gap >= 0.15:
            parts.append("opp D leakier vs pass than run")
        elif gap <= -0.15:
            parts.append("opp D leakier vs run than pass")

    if player.position == "RB":
        if rb_rush is not None and qb_pass is not None:
            rush_fd = rb_rush * sc["rush_yd"]
            pass_fd = qb_pass * sc["pass_yd"]
            cmp = (
                f"books: RB rush {_g(rb_rush)} vs QB pass {_g(qb_pass)}"
            )
            if rush_fd >= pass_fd:
                cmp += " — run volume"
            parts.append(cmp)
        elif (
            player.prop_rush_yds is None
            and rb_rush is None
            and qb_pass is not None
        ):
            parts.append(
                "no rush props on this team — RB score is implied×depth share only"
            )

    rbs = [x for x in lineup if x.team == player.team and x.position == "RB"]
    if player.position == "RB" and len(rbs) >= 2 and rb_rush is not None:
        parts.append("two RBs from a team that books price on the ground")

    if slot_key == "SUPERFLEX" and player.position == "QB":
        parts.append("SuperFLEX is a second QB")

    parts = parts[:2]
    if not parts:
        return None
    return "; ".join(parts)
