"""FanDuel NFL classic lineup-upload CSV.

Two template layouts:

- Legacy lineup-upload: header starts with ``QB`` (9 slots from column 0).
- Entries upload: leading metadata (``entry_id``, ``contest_id``, …) then
  ``QB``…``DEF``. Slot cells go in those columns only; ``entry_id`` and
  Instructions stay put.

Default template is the newest ``FanDuel-NFL-*-entries-upload-template.csv``
under ``nfl/data/``, else a ``*-lineup-upload-template.csv``, else
``FanDuel-NFL-2026-09-20-134251-entries-upload-template.csv``. ``--upload``
needs a matching contest file on disk (or pass ``template=``).
"""

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
_DATA_DIR = _NFL_DIR / "data"
_ENTRIES_GLOB = "FanDuel-NFL-*-entries-upload-template.csv"
_LINEUP_GLOB = "FanDuel-NFL-*-lineup-upload-template.csv"
_FALLBACK_TEMPLATE_NAME = "FanDuel-NFL-2026-09-20-134251-entries-upload-template.csv"
SLOT_COUNT = len(FANDUEL_PICKER_ORDER)


def resolve_upload_template(data_dir: str | Path | None = None) -> Path:
    """Newest entries-upload template, else lineup-upload, else fallback name."""
    folder = Path(data_dir) if data_dir is not None else _DATA_DIR
    entries = sorted(folder.glob(_ENTRIES_GLOB))
    if entries:
        return entries[-1]
    lineups = sorted(folder.glob(_LINEUP_GLOB))
    if lineups:
        return lineups[-1]
    return folder / _FALLBACK_TEMPLATE_NAME


UPLOAD_TEMPLATE = resolve_upload_template()


def _cell(value: object) -> str:
    return (value if isinstance(value, str) else str(value or "")).strip()


def slot_start_index(header: list[str]) -> int:
    """Column index of the first ``QB`` header (0 = legacy layout)."""
    labels = [_cell(c) for c in header]
    try:
        return labels.index("QB")
    except ValueError as exc:
        raise ValueError("upload template header has no QB column") from exc


def template_entry_rows(template_rows: list[list[str]]) -> list[list[str]]:
    """Data rows that receive lineups.

    Entries templates: rows with a non-empty ``entry_id``. Legacy templates
    (header starts at ``QB``): every row after the header.
    """
    if not template_rows:
        return []
    header = template_rows[0]
    labels = [_cell(c) for c in header]
    start = slot_start_index(header)
    if start == 0 or "entry_id" not in labels:
        return [list(row) for row in template_rows[1:]]
    entry_idx = labels.index("entry_id")
    out: list[list[str]] = []
    for row in template_rows[1:]:
        if entry_idx < len(row) and _cell(row[entry_idx]):
            out.append(list(row))
    return out


def _legacy_fallback_rows() -> list[list[str]]:
    header = [label for _key, label in FANDUEL_PICKER_ORDER] + ["", "Instructions"]
    instr = [""] * SLOT_COUNT + ["", "Create a lineup"]
    return [header] + [list(instr) for _ in range(5)]


def _apply_slots(base: list[str], slots: list[str], start: int) -> list[str]:
    row = list(base)
    need = start + SLOT_COUNT
    if len(row) < need:
        row.extend([""] * (need - len(row)))
    row[start : start + SLOT_COUNT] = slots
    return row


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
    if not path.is_file():
        if template is not None:
            raise FileNotFoundError(
                f"upload template not found: {path}. "
                "Pass template= or place FanDuel-NFL-*-entries-upload-template.csv "
                "in nfl/data/."
            )
        return _legacy_fallback_rows()
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


def slate_contest(players) -> str:
    """Majority FanDuel contest prefix on player ids (`134251-129458` → `134251`)."""
    from collections import Counter

    counts: Counter[str] = Counter()
    for player in players:
        pid = getattr(player, "pid", None)
        if pid is None and isinstance(player, dict):
            pid = player.get("id") or player.get("pid") or ""
        text = str(pid or "")
        if "-" not in text:
            continue
        prefix = text.split("-", 1)[0].strip()
        if prefix:
            counts[prefix] += 1
    if not counts:
        return ""
    return counts.most_common(1)[0][0]


def template_contest_ids(rows: list[list[str]]) -> set[str]:
    """Non-empty `contest_id` cells. Empty when the template has no such column."""
    if not rows:
        return set()
    labels = [_cell(c) for c in rows[0]]
    if "contest_id" not in labels:
        return set()
    idx = labels.index("contest_id")
    out: set[str] = set()
    for row in rows[1:]:
        if idx < len(row):
            val = _cell(row[idx])
            if val:
                out.add(val)
    return out


def contest_mismatch_message(
    template_ids: set[str],
    slate_contest_id: str,
) -> str | None:
    """Loud warning when the entries file is from a different contest.

    No ids, or no slate contest, is not a mismatch. A template whose
    contest ids are exactly the slate contest is quiet.
    """
    slate = (slate_contest_id or "").strip()
    ids = {item for item in template_ids if item}
    if not ids or not slate or ids == {slate}:
        return None
    shown = ", ".join(sorted(ids))
    return (
        f"WARNING: entries template contest_id ({shown}) does not match "
        f"players CSV contest {slate}. Do not upload this file."
    )


def export_lineups(
    lineups: list[Lineup] | list[dict],
    *,
    n_lineups: int,
    contest: str,
    objective: str,
    upload: str | Path | None = None,
    export: bool = False,
    when: datetime | None = None,
    export_dir: str | Path | None = None,
    contest_ids: set[str] | None = None,
    template: str | Path | None = None,
) -> dict[str, Path]:
    """Write an upload CSV only when `export` or `upload` is set.

    `n_lineups` is kept for callers. It does not write a file. `export`
    writes the stamped `nfl/export/` CSV. `upload` writes that path only.
    """
    del n_lineups
    written: dict[str, Path] = {}
    if not export and not upload:
        return written
    n = len(lineups)
    template_rows = load_template_rows(template)
    note = contest_mismatch_message(template_contest_ids(template_rows), contest)
    if note:
        print(note, file=sys.stderr)
    if export:
        dest = stamped_export_path(
            contest, objective, when=when, export_dir=export_dir
        )
        write_upload_csv(dest, lineups, template=template)
        if contest_ids is not None:
            validate_upload(dest, contest_ids, n)
        print(f"export {n} rows → {_display_path(dest)}", file=sys.stderr)
        written["export"] = dest
    if upload:
        up = write_upload_csv(Path(upload).expanduser(), lineups, template=template)
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
    """Header + one row per 9 from the contest template.

    Legacy (header starts at QB): first five lineup rows keep the template's
    right-side instruction cells. Entries templates: copy each matching
    ``entry_id`` row and fill only the QB…DEF columns.
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    template_rows = load_template_rows(template)
    if not template_rows:
        raise ValueError("empty upload template")
    header = template_rows[0]
    start = slot_start_index(header)
    bases = template_entry_rows(template_rows)
    if start == 0:
        # Rows 1–5: instruction lines (empty 9s + text in the Instructions col).
        instruction_rows = template_rows[1:6]
    else:
        instruction_rows = []
        if lineups and len(bases) < len(lineups):
            raise ValueError(
                f"template has {len(bases)} entry rows, need {len(lineups)}"
            )

    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        for i, lu in enumerate(lineups):
            slots = upload_row(lu)
            if start == 0:
                if i < len(instruction_rows):
                    base = instruction_rows[i]
                else:
                    base = [""] * max(len(header), SLOT_COUNT)
                w.writerow(_apply_slots(base, slots, start))
            else:
                w.writerow(_apply_slots(bases[i], slots, start))
        if not lineups:
            for extra in instruction_rows if start == 0 else bases:
                w.writerow(extra)
    return out


def lineup_data_rows(path: str | Path) -> list[list[str]]:
    """Filled 9-slot rows from the QB column (skips empty placeholders)."""
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return []
    start = slot_start_index(rows[0])
    out: list[list[str]] = []
    for row in rows[1:]:
        padded = list(row) + [""] * max(0, start + SLOT_COUNT - len(row))
        cells = [_cell(c) for c in padded[start : start + SLOT_COUNT]]
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
