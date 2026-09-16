"""Load a FanDuel NFL players-list CSV into a flat player pool."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Player:
    pid: str
    name: str
    position: str
    salary: int
    team: str
    opponent: str
    game: str
    fppg: float | None
    injury: str
    roster_position: str
    spread: float | None = None
    total: float | None = None
    implied_total: float | None = None
    implied_opp: float | None = None
    moneyline: float | None = None
    lines_provider: str | None = None
    lines_source: str | None = None
    objective: float | None = None
    depth_rank: int | None = None
    prop_fd: float | None = None
    prop_pass_yds: float | None = None
    prop_pass_tds: float | None = None
    prop_rush_yds: float | None = None
    prop_rec_yds: float | None = None
    prop_receptions: float | None = None
    prop_book: str | None = None
    prop_status: str | None = None
    target_share: float | None = None
    targets: int | None = None
    targets_week: int | None = None
    targets_status: str | None = None
    ownership: float | None = None
    weather: str | None = None

    @property
    def projection(self) -> float:
        if self.objective is not None:
            return self.objective
        return 0.0 if self.fppg is None else self.fppg


def load_fanduel_csv(path: str | Path) -> list[Player]:
    """Parse FanDuel's 'Download Players List' CSV.

    I/O: path → list[Player]. Raises FileNotFoundError / ValueError.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)
    players: list[Player] = []
    with p.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise ValueError(f"empty CSV: {p}")
        required = {"Id", "Position", "Nickname", "Salary", "Team"}
        missing = required - set(reader.fieldnames)
        if missing:
            raise ValueError(f"{p} missing columns {sorted(missing)}")
        for row in reader:
            pid = (row.get("Id") or "").strip()
            if not pid:
                continue
            raw_fppg = (row.get("FPPG") or "").strip()
            fppg = float(raw_fppg) if raw_fppg else None
            salary_raw = (row.get("Salary") or "").strip().replace(",", "")
            if not salary_raw:
                continue
            players.append(
                Player(
                    pid=pid,
                    name=(row.get("Nickname") or "").strip()
                    or f"{row.get('First Name', '')} {row.get('Last Name', '')}".strip(),
                    position=(row.get("Position") or "").strip().upper(),
                    salary=int(float(salary_raw)),
                    team=(row.get("Team") or "").strip().upper(),
                    opponent=(row.get("Opponent") or "").strip().upper(),
                    game=(row.get("Game") or "").strip(),
                    fppg=fppg,
                    injury=(row.get("Injury Indicator") or "").strip().upper(),
                    roster_position=(row.get("Roster Position") or "").strip(),
                )
            )
    return players


def filter_pool(
    players: list[Player],
    *,
    drop_out: bool = True,
    drop_questionable: bool = False,
    out_codes: frozenset[str] = frozenset({"IR", "NA"}),
    questionable_codes: frozenset[str] = frozenset({"Q"}),
) -> list[Player]:
    """Keep QB/RB/WR/TE/D. Position D fills the DEF slot. Drop IR/NA; keep Q."""
    out: list[Player] = []
    for pl in players:
        if drop_out and pl.injury in out_codes:
            continue
        if drop_questionable and pl.injury in questionable_codes:
            continue
        if pl.position not in {"QB", "RB", "WR", "TE", "D"}:
            continue
        out.append(pl)
    return out
