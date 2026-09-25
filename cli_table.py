"""Stdlib stderr tables. No rich/tabulate.

``format_table(headers, rows) -> str`` uses Unicode box-drawing when
``stderr.encoding`` can encode ``┌┬┐│─┼``; otherwise ``+--+``.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable, Sequence

BOX = {
    "tl": "┌",
    "tm": "┬",
    "tr": "┐",
    "ml": "├",
    "mm": "┼",
    "mr": "┤",
    "bl": "└",
    "bm": "┴",
    "br": "┘",
    "h": "─",
    "v": "│",
}
ASCII = {
    "tl": "+",
    "tm": "+",
    "tr": "+",
    "ml": "+",
    "mm": "+",
    "mr": "+",
    "bl": "+",
    "bm": "+",
    "br": "+",
    "h": "-",
    "v": "|",
}

PICKER_HEADERS = (
    "Slot",
    "Player",
    "Pos",
    "Team",
    "Opp",
    "Sal",
    "FPPG",
    "Proj",
    "Fl",
    "Cl",
    "Props",
    "Sources",
)
_NUM_RE = re.compile(r"^\$?-?\d[\d,]*(\.\d+)?$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_BOX_PROBE = "┌┬┐│─┼"
_DST_POS = frozenset({"D", "DEF", "DST"})
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"


def supports_box(stream=None) -> bool:
    stream = sys.stderr if stream is None else stream
    enc = getattr(stream, "encoding", None) or "utf-8"
    try:
        _BOX_PROBE.encode(enc)
        return True
    except (LookupError, UnicodeEncodeError):
        return False


def supports_color(stream=None) -> bool:
    stream = sys.stderr if stream is None else stream
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def visible_len(s: str) -> int:
    return len(_ANSI_RE.sub("", s))


def json_stdout_enabled(
    *,
    agent: bool = False,
    json_flag: bool = False,
    stream=None,
) -> bool:
    """Human TTY: no JSON. --agent / --json / non-TTY: JSON stdout."""
    if agent or json_flag:
        return True
    stream = sys.stdout if stream is None else stream
    try:
        return not stream.isatty()
    except Exception:
        return True


def money(n: object) -> str:
    if n is None or n == "":
        return ""
    return f"${int(n):,}"


def one_dec(x: object) -> str:
    if x is None or x == "":
        return "-"
    return f"{float(x):.1f}"


def format_proj_value(proj: object, salary: object) -> str:
    """`18.7 (1.9x)` — FD pts per $1,000 salary. 5x is $10k → 50 pts."""
    if proj is None or proj == "":
        return "-"
    pts = float(proj)
    try:
        sal = int(salary or 0)
    except (TypeError, ValueError):
        sal = 0
    if sal <= 0:
        return f"{pts:.1f}"
    return f"{pts:.1f} ({pts * 1000 / sal:.1f}x)"


def format_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[object]],
    *,
    box: bool | None = None,
    totals: bool = False,
) -> str:
    """Render a fixed-width table. ``totals=True`` rules off the last row."""
    if box is None:
        box = supports_box()
    glyphs = BOX if box else ASCII
    cols = [str(h) for h in headers]
    n = len(cols)
    body = [_cells(row, n) for row in rows]
    widths = [visible_len(c) for c in cols]
    for row in body:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], visible_len(cell))
    right = [_right_align(i, cols, body) for i in range(n)]

    def rule(left: str, mid: str, right_g: str) -> str:
        bits = [glyphs["h"] * (w + 2) for w in widths]
        return left + mid.join(bits) + right_g

    def data(row: Sequence[str]) -> str:
        cells = []
        for i, cell in enumerate(row):
            pad = widths[i] - visible_len(cell)
            spaces = " " * max(0, pad)
            padded = (spaces + cell) if right[i] else (cell + spaces)
            cells.append(f" {padded} ")
        return glyphs["v"] + glyphs["v"].join(cells) + glyphs["v"]

    lines = [
        rule(glyphs["tl"], glyphs["tm"], glyphs["tr"]),
        data(cols),
        rule(glyphs["ml"], glyphs["mm"], glyphs["mr"]),
    ]
    split_at = len(body) - 1 if totals and len(body) > 1 else None
    for i, row in enumerate(body):
        if split_at is not None and i == split_at:
            lines.append(rule(glyphs["ml"], glyphs["mm"], glyphs["mr"]))
        lines.append(data(row))
    lines.append(rule(glyphs["bl"], glyphs["bm"], glyphs["br"]))
    return "\n".join(lines)


def format_picker_props(p: dict) -> str:
    """Compact one-cell props. Not the full ``note``."""
    pos = str(p.get("position") or "").upper()
    slot = str(p.get("slot") or "").upper()
    if pos in _DST_POS or slot in _DST_POS:
        opp = p.get("implied_opp")
        if opp is None or opp == "":
            return "-"
        return f"PA {_g(opp)}"
    bits: list[str] = []
    if p.get("prop_pass_yds") is not None:
        bits.append(f"{_g(p['prop_pass_yds'])} pass")
    if p.get("prop_pass_tds") is not None:
        bits.append(f"{_g(p['prop_pass_tds'])} TD")
    if p.get("prop_rush_yds") is not None:
        bits.append(f"{_g(p['prop_rush_yds'])} rush")
    if p.get("prop_rec_yds") is not None:
        bits.append(f"{_g(p['prop_rec_yds'])} rec")
    if p.get("prop_receptions") is not None:
        bits.append(f"{_g(p['prop_receptions'])} recs")
    return " / ".join(bits) if bits else "-"


def format_picker_sources(
    p: dict,
    *,
    depth_source: str | None = None,
) -> str:
    """Compact join tags. Slash-separated; ``-`` if nothing joined."""
    if p.get("sources"):
        return str(p["sources"])
    tags: list[str] = []
    pos = str(p.get("position") or "").upper()
    slot = str(p.get("slot") or "").upper()
    is_dst = pos in _DST_POS or slot in _DST_POS
    src = str(p.get("depth_source") or depth_source or "").strip().lower()
    if p.get("depth_rank") is not None:
        if src == "espn":
            tags.append("espn")
        elif src == "gangstash":
            tags.append("gs-depth")
        else:
            tags.append("ourlads")
    if p.get("target_share") is not None:
        tgt = str(p.get("targets_source") or "").strip().lower()
        tags.append("gs-tgt" if tgt == "gangstash" else "lineups-tgt")
    if p.get("snap_share") is not None:
        snap = str(p.get("snaps_source") or "").strip().lower()
        tags.append("gs-snap" if snap == "gangstash" else "lineups-snap")
    prop_status = str(p.get("prop_status") or "").strip().lower()
    if (
        prop_status == "props"
        or p.get("prop_fd") is not None
        or any(
            p.get(k) is not None
            for k in (
                "prop_pass_yds",
                "prop_pass_tds",
                "prop_rush_yds",
                "prop_rec_yds",
                "prop_receptions",
            )
        )
    ):
        tags.append("gs-props")
    inj = str(p.get("injury") or "").strip().upper()
    if inj == "Q":
        tags.append("espn-inj")
    if is_dst and p.get("implied_opp") is not None:
        tags.append("vegas-dst")
    return "/".join(tags) if tags else "-"


def _round_pts(x: object) -> int | None:
    """Nearest integer, .5 up (25.5 → 26)."""
    if x is None or x == "":
        return None
    return int(float(x) + 0.5)


def _paint_pts(n: int, tone: str | None, color: bool) -> str:
    s = str(n)
    if not color or not tone:
        return s
    if tone == "hi":
        return f"{GREEN}{s}{RESET}"
    if tone == "lo":
        return f"{RED}{s}{RESET}"
    return s


def format_team_opp(p: dict, *, color: bool = False) -> tuple[str, str]:
    """`TEX (H) 26` / `OSU (A) 24`. Higher implied green, lower red when color."""
    us = _round_pts(p.get("implied_total"))
    them = _round_pts(p.get("implied_opp"))
    us_tone = them_tone = None
    if us is not None and them is not None and us != them:
        us_tone, them_tone = ("hi", "lo") if us > them else ("lo", "hi")
    team = format_team(p)
    opp = format_opp(p)
    if us is not None:
        team = f"{team} {_paint_pts(us, us_tone, color)}".strip()
    if them is not None:
        opp = f"{opp} {_paint_pts(them, them_tone, color)}".strip()
    return team, opp


def _venue(p: dict) -> str:
    """`H` or `A` for this player's team from FanDuel `AWAY@HOME`. Else empty."""
    game = str(p.get("game") or "").strip().upper()
    team = str(p.get("team") or "").strip().upper()
    if "@" not in game or not team:
        return ""
    away, _, home = game.partition("@")
    if team == away.strip():
        return "A"
    if team == home.strip():
        return "H"
    return ""


def format_team(p: dict) -> str:
    """`TEX (H)` / `OSU (A)`."""
    team = str(p.get("team") or "").strip()
    loc = _venue(p)
    if team and loc:
        return f"{team} ({loc})"
    return team


def format_opp(p: dict) -> str:
    """Opponent abbrev with the opposite venue: `OSU (A)`."""
    opp = str(p.get("opponent") or "").strip()
    if not opp:
        return ""
    loc = _venue(p)
    if loc == "A":
        return f"{opp} (H)"
    if loc == "H":
        return f"{opp} (A)"
    return opp


def format_picker_table(
    lu: dict,
    *,
    title: str | None = None,
    box: bool | None = None,
    color: bool | None = None,
) -> str:
    """Picker 7/9: Slot | Player | Pos | Team | Opp | Sal | FPPG | Proj | Fl | Cl | Props | Sources."""
    if color is None:
        color = supports_color()
    flags = lu.get("flags") if isinstance(lu.get("flags"), dict) else {}
    depth_source = None
    if isinstance(flags, dict):
        depth_source = flags.get("depth_source")
    if depth_source is None:
        depth_source = lu.get("depth_source")
    picker = list(lu.get("picker") or [])
    rows: list[tuple[str, ...]] = []
    for p in picker:
        name = str(p.get("name") or "")
        if p.get("starter"):
            name = f"{name} (*)"
        team_cell, opp_cell = format_team_opp(p, color=color)
        rows.append(
            (
                str(p.get("slot") or ""),
                name,
                str(p.get("position") or ""),
                team_cell,
                opp_cell,
                money(p.get("salary")),
                one_dec(p.get("fppg")),
                format_proj_value(p.get("projection"), p.get("salary")),
                one_dec(p.get("floor")),
                one_dec(p.get("ceiling")),
                format_picker_props(p),
                format_picker_sources(p, depth_source=depth_source),
            )
        )
    sal = lu.get("salary")
    if sal is None:
        sal = sum(int(p.get("salary") or 0) for p in picker)
    proj = lu.get("lineup_proj")
    if proj is None:
        proj = lu.get("projection")
    if proj is None:
        proj = _sum_key(picker, "projection")
    floor = lu.get("lineup_floor")
    if floor is None:
        floor = _sum_key(picker, "floor")
    ceiling = lu.get("lineup_ceiling")
    if ceiling is None:
        ceiling = _sum_key(picker, "ceiling")
    fppg_total = _sum_key(picker, "fppg")
    rows.append(
        (
            "Lineup",
            "",
            "",
            "",
            "",
            money(sal),
            one_dec(fppg_total),
            format_proj_value(proj, sal),
            one_dec(floor),
            one_dec(ceiling),
            "",
            "",
        )
    )
    parts: list[str] = []
    if title:
        parts.append(title)
    parts.append(format_table(PICKER_HEADERS, rows, box=box, totals=True))
    cash = lu.get("cash_line")
    gap = lu.get("ceiling_minus_cash")
    if cash is not None and gap is not None:
        parts.append(f"cash {cash:g}  gap {gap:.1f}")
    for n in lu.get("notes") or []:
        parts.append(str(n))
    return "\n".join(parts)


def format_games_table(games: Iterable[object], *, box: bool | None = None) -> str:
    headers = ("Game", "Spread (home)", "Total", "Implied away", "Implied home")
    rows: list[tuple[str, ...]] = []
    for g in games:
        home_fd = _attr(g, "home_fd")
        away_fd = _attr(g, "away_fd")
        spread = _attr(g, "home_spread")
        total = _attr(g, "total")
        implied_away = _attr(g, "implied_away")
        implied_home = _attr(g, "implied_home")
        home_s = f"{home_fd} {spread:+g}" if spread is not None else str(home_fd)
        rows.append(
            (
                str(_attr(g, "game") or ""),
                home_s,
                "" if total is None else f"{total:g}",
                f"{away_fd} {implied_away:g}",
                f"{home_fd} {implied_home:g}",
            )
        )
    return format_table(headers, rows, box=box)


def _attr(obj: object, key: str):
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _cells(row: Sequence[object], n: int) -> list[str]:
    out = ["" if c is None else str(c) for c in row]
    if len(out) < n:
        out.extend([""] * (n - len(out)))
    return out[:n]


def _right_align(i: int, headers: Sequence[str], body: Sequence[Sequence[str]]) -> bool:
    if headers[i] in {"Props", "Sources"}:
        return False
    cells = [headers[i], *(row[i] for row in body)]
    nonempty = [c for c in cells if c and c != headers[i]]
    if not nonempty:
        return headers[i] in {
            "Sal",
            "FPPG",
            "Proj",
            "Fl",
            "Cl",
            "Floor",
            "Ceiling",
            "#",
            "Total",
        }
    return all(c == "-" or _NUM_RE.match(c) for c in nonempty)


def _g(x: object) -> str:
    xf = float(x)
    if xf.is_integer():
        return str(int(xf))
    return f"{xf:g}"


def _sum_key(rows: Sequence[dict], key: str) -> float | None:
    vals = [r.get(key) for r in rows]
    if not vals or any(v is None for v in vals):
        return None
    return sum(float(v) for v in vals)
