"""Per-player lineup notes: implied×depth, usage tilt, optional prop tilt, DST, stack fade."""

from __future__ import annotations

from collections.abc import Sequence

from cli_table import format_picker_sources
from nfl.players import Player
from nfl.projections import POS_FD_SHARE, depth_prior, usage_factor
from nfl.rules import (
    STACK_COEF,
    dst_pa_key,
    FANDUEL_NFL,
    SkillSide,
    bring_back_players,
    opp_dst_illegal,
    pass_catchers_by_team,
    pass_stack_players,
)

BOOK_LABEL = {
    "fanduel": "FanDuel",
    "median": "median books",
    "gangstash": "Gangstash",
}


def picker_name(name: str, starter: bool) -> str:
    return f"{name} (*)" if starter else name


def is_starter(player: Player) -> bool:
    return player.depth_rank == 1


def _g(x: float) -> str:
    xf = float(x)
    if xf.is_integer():
        return str(int(xf))
    return f"{xf:g}"


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


def _prop_line_bits(player: Player) -> list[str]:
    bits: list[str] = []
    if player.prop_pass_yds is not None:
        bits.append(f"{_g(player.prop_pass_yds)} pass yds")
    if player.prop_pass_tds is not None:
        bits.append(f"{_g(player.prop_pass_tds)} pass TDs")
    if player.prop_rush_yds is not None:
        bits.append(f"{_g(player.prop_rush_yds)} rush yds")
    if player.prop_rec_yds is not None:
        bits.append(f"{_g(player.prop_rec_yds)} rec yds")
    if player.prop_receptions is not None:
        bits.append(f"{_g(player.prop_receptions)} recs")
    return bits


def player_source_fields(player: Player, *, prop_status: str | None) -> dict:
    """Picker join tags from fields already on the player. No scrapes."""
    return {
        "sources": format_picker_sources(
            {
                "position": player.position,
                "depth_rank": player.depth_rank,
                "depth_source": player.depth_source,
                "target_share": player.target_share,
                "snap_share": player.snap_share,
                "prop_status": prop_status,
                "prop_fd": player.prop_fd,
                "prop_pass_yds": player.prop_pass_yds,
                "prop_pass_tds": player.prop_pass_tds,
                "prop_rush_yds": player.prop_rush_yds,
                "prop_rec_yds": player.prop_rec_yds,
                "prop_receptions": player.prop_receptions,
                "prop_book": player.prop_book,
                "injury": player.injury,
                "implied_opp": player.implied_opp,
            }
        )
    }


def explain_player(player: Player, *, slot_key: str | None = None) -> dict:
    _ = slot_key
    starter = is_starter(player)
    if player.position == "D":
        opp = player.implied_opp
        band = dst_pa_key(opp or 0.0)
        if opp is None:
            note = "DST PA proxy: no implied opp — sack/TO prior only (+3.0)."
        else:
            note = (
                f"DST PA proxy: opp implied {_g(opp)} → {band} "
                f"({FANDUEL_NFL.scoring[band]:g} pts) + 3.0 sack/TO prior."
            )
        info = {"note": note, "starter": False, "prop_status": None}
        info.update(player_source_fields(player, prop_status=None))
        return info

    if player.prop_fd is not None:
        bits = _prop_line_bits(player)
        lines = ", ".join(bits) if bits else f"{_g(player.prop_fd)} FD pts"
        note = (
            f"Props ({_book_label(player)}): {lines} "
            "— ±20% tilt on implied."
        )
        info = {"note": note, "starter": starter, "prop_status": "props"}
        info.update(player_source_fields(player, prop_status="props"))
        return info

    why = ""
    if player.prop_status == "unmatched":
        why = " (name unmatched vs books)"
    elif player.prop_status == "no_market":
        why = " (no market)"
    share = POS_FD_SHARE.get((player.position or "WR").upper(), 0.18)
    implied = player.implied_total
    imp_s = "—" if implied is None else _g(implied)
    usage = usage_factor(
        player.target_share,
        player.position,
        player.depth_rank,
        snap_share=player.snap_share,
    )
    usage_s = ""
    bits: list[str] = []
    if player.snap_share is not None:
        bits.append(f"snap {_g(player.snap_share)}")
    if player.target_share is not None:
        bits.append(f"tgt {_g(player.target_share)}")
    if bits:
        usage_s = f" × usage {usage:g} ({', '.join(bits)})"
    elif player.snaps_status == "unmatched" or player.targets_status == "unmatched":
        usage_s = " × usage 1.0 (Lineups name unmatched)"
    note = (
        f"no player props{why} — implied {imp_s} × "
        f"{_prior_label(player.depth_rank)} ({depth_prior(player.depth_rank):g}) × "
        f"{player.position} {share:.2f} share{usage_s}."
    )
    status = player.prop_status or "no_market"
    info = {
        "note": note,
        "starter": starter,
        "prop_status": status,
    }
    info.update(player_source_fields(player, prop_status=status))
    return info


def _last_names(players: Sequence[Player]) -> str:
    return ", ".join(p.name.split()[-1] for p in players)


def bring_back_note(slots: dict[str, Player], n: int) -> str:
    """Picker line: `bring-back off` or `bring-back TB WR/TE (Egbuka)`."""
    if n <= 0:
        return "bring-back off"
    qb = slots.get("QB")
    if qb is None or not pass_stack_players(slots):
        return "bring-back n/a (no pass stack)"
    bbs = bring_back_players(slots)
    names = _last_names(bbs)
    pos = "WR/TE"
    if any((p.position or "").upper() == "QB" for p in bbs):
        pos = "WR/TE/QB"
    if names:
        return f"bring-back {qb.opponent} {pos} ({names})"
    return f"bring-back {qb.opponent} {pos} (missing)"


def _stack_qb_note(slots: dict[str, Player], qb: Player) -> str | None:
    """`stack LAC QB+WR (McConkey, Johnston)` when 2+ WR/TE share the QB's team."""
    catchers = list(
        pass_catchers_by_team(slots).get((qb.team or "").strip().upper()) or []
    )
    if len(catchers) < 2:
        return None
    catchers.sort(
        key=lambda p: (-float(getattr(p, "projection", 0.0) or 0.0), p.name)
    )
    names = _last_names(catchers)
    pos = { (p.position or "").strip().upper() for p in catchers }
    if pos == {"WR"}:
        kind = "QB+WR"
    elif pos == {"TE"}:
        kind = "QB+TE"
    else:
        kind = "QB+WR/TE"
    return f"stack {qb.team} {kind} ({names})"


def lineup_notes(
    slots: dict[str, Player],
    *,
    bring_back: int = 0,
    max_per_team: int | None = None,
    require_qb_with_two_pass_catchers: bool = True,
) -> list[str]:
    notes: list[str] = []
    players = list(slots.values())
    qb = slots.get("QB")
    dst = slots.get("DEF")
    stacked = pass_stack_players(slots)
    two_note = (
        _stack_qb_note(slots, qb)
        if qb is not None and require_qb_with_two_pass_catchers
        else None
    )
    if two_note:
        notes.append(two_note)
    elif qb is not None and stacked:
        names = _last_names(stacked)
        notes.append(f"stack {qb.team} QB+WR/TE ({names})")
    else:
        notes.append("no QB+WR stack")
    notes.append(bring_back_note(slots, bring_back))
    if STACK_COEF:
        notes.append("stack premium on")

    cap = FANDUEL_NFL.max_per_team if max_per_team is None else max_per_team
    counts: dict[str, int] = {}
    for p in players:
        counts[p.team] = counts.get(p.team, 0) + 1
    bound = [t for t, n in counts.items() if n >= cap]
    if bound:
        notes.append(f"house {cap}/team bound on {', '.join(sorted(bound))}")

    if dst is not None:
        qb_opp = qb.opponent if qb is not None else None
        skill = [
            SkillSide(p.position, p.opponent, p.salary)
            for p in players
            if p.position != "D"
        ]
        illegal = opp_dst_illegal(
            def_team=dst.team,
            qb_opp=qb_opp,
            skill=skill,
            stud_rb_min_salary=FANDUEL_NFL.stud_rb_min_salary,
        )
        if illegal:
            notes.append("opp DST was NOT avoided")
        else:
            notes.append(f"opp DST avoided ({dst.team} vs qb_opp={qb_opp})")
    return notes
