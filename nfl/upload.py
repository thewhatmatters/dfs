"""FanDuel NFL classic lineup-upload CSV (contest 133104 template)."""

from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path

from nfl.players import Player
from nfl.rules import FANDUEL_PICKER_ORDER
from nfl.solver import Lineup

_NFL_DIR = Path(__file__).resolve().parent
_ROOT = _NFL_DIR.parent
EXPORT_DIR = _NFL_DIR / "export"
UPLOAD_TEMPLATE = (
    _NFL_DIR / "data" / "FanDuel-NFL-2026-09-13-133104-lineup-upload-template.csv"
)
SLOT_COUNT = len(FANDUEL_PICKER_ORDER)


def upload_cell(player: Player | dict) -> str:
    """`Id:Nickname` as in the FanDuel template (`133104-63336:Joe Burrow`)."""
    if isinstance(player, Player):
        return f"{player.pid}:{player.name}"
    return f"{player['id']}:{player['name']}"


def picker_players(lineup: Lineup | dict) -> list[Player | dict]:
    if isinstance(lineup, Lineup):
        return [lineup.slots[key] for key, _label in FANDUEL_PICKER_ORDER]
    picker = lineup.get("picker") or []
    if len(picker) != SLOT_COUNT:
        raise ValueError(f"picker has {len(picker)} rows, want {SLOT_COUNT}")
    return picker


def upload_row(lineup: Lineup | dict) -> list[str]:
    return [upload_cell(p) for p in picker_players(lineup)]


def parse_upload_id(cell: str) -> str:
    text = (cell or "").strip()
    if not text:
        return ""
    if ":" in text:
        return text.split(":", 1)[0].strip()
    return text


def load_template_rows(template: str | Path | None = None) -> list[list[str]]:
    path = Path(template) if template else UPLOAD_TEMPLATE
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.reader(fh))


def _display_path(path: Path) -> str:
    """Repo-relative posix path when under the repo; otherwise `str(path)`."""
    try:
        return path.resolve().relative_to(_ROOT).as_posix()
    except ValueError:
        return str(path)


def stamped_export_name(
    contest: str,
    objective: str,
    when: datetime | None = None,
) -> str:
    """`nfl-{contest}-{objective}-{YYYYMMDD}-{HHMMSS}.csv` in local time."""
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"nfl-{contest}-{objective}-{stamp}.csv"


def stamped_export_path(
    contest: str,
    objective: str,
    *,
    when: datetime | None = None,
    export_dir: str | Path | None = None,
) -> Path:
    folder = Path(export_dir) if export_dir is not None else EXPORT_DIR
    return folder / stamped_export_name(contest, objective, when)


def export_lineups(
    lineups: list[Lineup] | list[dict],
    *,
    n_lineups: int,
    contest: str,
    objective: str,
    upload: str | Path | None = None,
    when: datetime | None = None,
    export_dir: str | Path | None = None,
    contest_ids: set[str] | None = None,
) -> dict[str, Path]:
    """Stamped `nfl/export/` CSV when more than one 9; `--upload` path if set.

    `n_lineups=1` and a single 9: no auto-export unless `upload` is set.
    """
    written: dict[str, Path] = {}
    n = len(lineups)
    if n_lineups > 1 or n > 1:
        dest = stamped_export_path(
            contest, objective, when=when, export_dir=export_dir
        )
        write_upload_csv(dest, lineups)
        if contest_ids is not None:
            validate_upload(dest, contest_ids, n)
        print(f"export {n} rows → {_display_path(dest)}", file=sys.stderr)
        written["export"] = dest
    if upload:
        up = write_upload_csv(Path(upload).expanduser(), lineups)
        if contest_ids is not None:
            validate_upload(up, contest_ids, n)
        print(f"upload {n} rows → {up}", file=sys.stderr)
        written["upload"] = up
    return written


def write_upload_csv(
    path: str | Path,
    lineups: list[Lineup] | list[dict],
    *,
    template: str | Path | None = None,
) -> Path:
    """Header + instructions from the contest template, then one row per 9.

    First five lineup rows keep the template's right-side instruction cells.
    Player bank is omitted (FanDuel ignores columns right of DEF).
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    template_rows = load_template_rows(template)
    if not template_rows:
        raise ValueError("empty upload template")
    header = template_rows[0]
    # Rows 1–5: instruction lines (empty 9s + text in the Instructions col).
    instruction_rows = template_rows[1:6]

    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        for i, lu in enumerate(lineups):
            row = upload_row(lu)
            if i < len(instruction_rows):
                rest = instruction_rows[i][SLOT_COUNT:]
                w.writerow(row + rest)
            else:
                pad = max(0, len(header) - SLOT_COUNT)
                w.writerow(row + [""] * pad)
        if not lineups:
            for extra in instruction_rows:
                w.writerow(extra)
    return out


def lineup_data_rows(path: str | Path) -> list[list[str]]:
    """Filled 9-slot rows (skips header / empty instruction placeholders)."""
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    out: list[list[str]] = []
    for row in rows[1:]:
        cells = [(c or "").strip() for c in row[:SLOT_COUNT]]
        if not any(cells):
            continue
        if not all(cells):
            raise ValueError(f"incomplete lineup row: {cells}")
        out.append(cells)
    return out


def validate_upload(
    path: str | Path,
    contest_ids: set[str],
    n: int,
) -> list[list[str]]:
    """N rows, 9 ids each, every id in the contest players-list."""
    rows = lineup_data_rows(path)
    if len(rows) != n:
        raise ValueError(f"expected {n} lineup rows, got {len(rows)}")
    for i, cells in enumerate(rows, start=1):
        if len(cells) != SLOT_COUNT:
            raise ValueError(f"lineup {i} has {len(cells)} slots, want {SLOT_COUNT}")
        for cell in cells:
            pid = parse_upload_id(cell)
            if pid not in contest_ids:
                raise ValueError(f"lineup {i}: id not in contest CSV: {pid}")
            if ":" not in cell:
                raise ValueError(f"lineup {i}: want Id:Nickname, got {cell!r}")
    return rows
