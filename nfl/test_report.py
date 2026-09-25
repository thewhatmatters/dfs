"""Monte Carlo projections report. No network."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nfl.report import (
    VEGAS_LABEL,
    build_report,
    efficiency_from_rows,
    fill_salaries,
    graph_table,
    report_path,
    resolve_games,
    runs_match,
    summarize_game_draws,
    write_games_sidecar,
    write_report,
)

RUN = "2026-09-25T04:00:00+00:00"
OFFICIAL = """+----------- [ WHAT THE RESEARCH COST ] ------------+
|                                                   |
| Agent               |  Tokens | Tool calls | Time |
| --------------------+---------+------------+----- |
| Inks and paper      | 115,207 |        120 |  16m |
| Overprint and drift | 135,218 |        164 |  16m |
| Naming the patterns | 186,716 |        112 |  18m |
| --------------------+---------+------------+----- |
| Total               | 437,141 |        396 | ~50m |
|                                                   |
+---------------------------------------------------+"""


def _row(
    name: str,
    team: str,
    opp: str,
    pos: str,
    mean: float,
    salary: int | None = 5000,
    *,
    efficiency: str | None = "data",
) -> dict:
    return {
        "model": "sim",
        "player_name": name,
        "team": team,
        "opponent": opp,
        "position": pos,
        "mean": mean,
        "p10": round(mean * 0.6, 2),
        "p90": round(mean * 1.4, 2),
        "salary": salary,
        "run_at": RUN,
        "inputs": {} if efficiency is None else {"sim_efficiency": efficiency},
    }


def _fixture() -> tuple[list[dict], list[dict]]:
    long_name = "X" * 60 + " Longname"
    rows = [
        _row("Patrick Mahomes", "KC", "BUF", "QB", 30, 9000),
        _row("Josh Allen", "BUF", "KC", "QB", 28, 8800),
        _row("Dak Prescott", "DAL", "PHI", "QB", 18, 7500),
        _row("Jalen Hurts", "PHI", "DAL", "QB", 17, 8000),
        _row("Joe Burrow", "CIN", "DET", "QB", 16, None),
        _row("Travis Kelce", "KC", "BUF", "TE", 22, 7000),
        _row("Dallas Goedert", "PHI", "DAL", "TE", 15, 5500),
        _row("CeeDee Lamb", "DAL", "PHI", "WR", 21, 7800),
        _row("A.J. Brown", "PHI", "DAL", "WR", 19, 7600),
        _row(long_name, "CIN", "DET", "WR", 25, 6400),
    ]
    for index in range(10):
        rows.append(
            _row(f"Rb {index}", "DET", "CIN", "RB", 14 - index * 0.4, 6000 - index * 100)
        )
    for index in range(7):
        rows.append(_row(f"Wr {index}", "DET", "CIN", "WR", 12 - index * 0.3, 5000))
    for index in range(8):
        rows.append(_row(f"Te {index}", "DET", "CIN", "TE", 11 - index * 0.2, 4500))
    for index, team in enumerate(("KC", "BUF", "DAL", "PHI", "CIN")):
        rows.append(_row(f"Def {team}", team, "DET", "D", 9 - index, 4000))
    games = [
        {
            "game": "KC@BUF",
            "away": "KC",
            "home": "BUF",
            "source": "sim",
            "away_median": 24.1,
            "away_p10": 14.0,
            "away_p90": 35.0,
            "home_median": 27.4,
            "home_p10": 17.1,
            "home_p90": 38.2,
        },
        {
            "game": "DAL@PHI",
            "away": "DAL",
            "home": "PHI",
            "source": "sim",
            "away_median": 20.0,
            "away_p10": 10.0,
            "away_p90": 30.0,
            "home_median": 23.5,
            "home_p10": 13.0,
            "home_p90": 33.0,
        },
    ]
    return rows, games


def _fences(text: str) -> list[str]:
    parts = text.split("```")
    return [parts[index].strip("\n") for index in range(1, len(parts), 2)]


def _data_rows(block: str) -> int:
    return len(block.splitlines()) - 6


class GraphTableTest(unittest.TestCase):
    def test_matches_the_official_frame(self) -> None:
        got = graph_table(
            "WHAT THE RESEARCH COST",
            ["Agent", "Tokens", "Tool calls", "Time"],
            [
                ["Inks and paper", "115,207", "120", "16m"],
                ["Overprint and drift", "135,218", "164", "16m"],
                ["Naming the patterns", "186,716", "112", "18m"],
            ],
            footer=["Total", "437,141", "396", "~50m"],
        )
        self.assertEqual(got, OFFICIAL)


class ReportTest(unittest.TestCase):
    def test_sim_report_shape(self) -> None:
        rows, games = _fixture()
        text = build_report(
            rows,
            games,
            season=2026,
            week=3,
            run_at=RUN,
            draws=10000,
            efficiency="data",
        )
        self.assertIn("season 2026", text)
        self.assertIn("week 3", text)
        self.assertIn("run 2026-09-24 23:00 CT", text)
        self.assertIn("draws 10000", text)
        self.assertIn("efficiency data", text)
        self.assertIn("games 2", text)
        self.assertNotIn(VEGAS_LABEL, text)
        titles = [
            "[ GAMES ]",
            "[ QB TOP 5 ]",
            "[ RB TOP 10 ]",
            "[ WR TOP 10 ]",
            "[ TE TOP 10 ]",
            "[ DEF TOP 5 ]",
        ]
        indexes = [text.index(title) for title in titles]
        self.assertEqual(indexes, sorted(indexes))
        blocks = _fences(text)
        self.assertEqual(len(blocks), 6)
        self.assertEqual(
            [_data_rows(block) for block in blocks[1:]],
            [5, 10, 10, 10, 5],
        )
        self.assertEqual(_data_rows(blocks[0]), 6)
        games_block = blocks[0]
        for name in (
            "Patrick Mahomes",
            "Josh Allen",
            "Travis Kelce",
            "CeeDee Lamb",
            "A.J. Brown",
            "Dak Prescott",
        ):
            self.assertIn(name, games_block)
        self.assertIn("24.1", games_block)
        self.assertIn("27.4", games_block)
        self.assertIn("3.33", text)
        self.assertIn("—", blocks[1])
        long_name = "X" * 60 + " Longname"
        self.assertNotIn(long_name, text)
        self.assertIn("…", blocks[3])
        for block in blocks:
            lines = block.splitlines()
            width = len(lines[0])
            self.assertLessEqual(width, 100)
            self.assertTrue(all(len(line) == width for line in lines))
            self.assertEqual(lines[0][0], "+")
            self.assertEqual(lines[0][-1], "+")
            self.assertEqual(lines[-1][0], "+")
            self.assertEqual(lines[-1][-1], "+")
            self.assertIn("[", lines[0])
            self.assertIn("]", lines[0])
            for line in lines[1:-1]:
                self.assertTrue(line.startswith("|") and line.endswith("|"))

    def test_vegas_label_when_game_results_are_missing(self) -> None:
        rows, _games = _fixture()
        bare = []
        for row in rows:
            copy = dict(row)
            copy["inputs"] = {}
            bare.append(copy)
        text = build_report(
            bare,
            [],
            season=2026,
            week=3,
            run_at=RUN,
            draws=None,
            efficiency=None,
        )
        self.assertIn(VEGAS_LABEL, text)
        self.assertIn("efficiency unknown", text)
        self.assertIn("draws unknown", text)
        self.assertEqual(efficiency_from_rows(bare), "unknown")
        self.assertNotIn("24.1", text)

    def test_draw_summary_and_sidecar_window(self) -> None:
        points = [float(i) for i in range(1, 11)]
        draws = [("KC@BUF", "KC", "BUF", value, value + 10.0) for value in points]
        games = summarize_game_draws(draws)
        self.assertEqual(len(games), 1)
        self.assertAlmostEqual(games[0]["away_median"], 5.5)
        self.assertAlmostEqual(games[0]["away_p10"], 1.9)
        self.assertAlmostEqual(games[0]["away_p90"], 9.1)
        self.assertAlmostEqual(games[0]["home_median"], 15.5)
        self.assertTrue(runs_match(RUN, "2026-09-25T04:04:00+00:00"))
        self.assertTrue(runs_match(RUN, "2026-09-25T04:05:00+00:00"))
        self.assertFalse(runs_match(RUN, "2026-09-25T04:06:00+00:00"))
        text = build_report(
            [_row("Patrick Mahomes", "KC", "BUF", "QB", 30, 9000)],
            draws,
            season=2026,
            week=3,
            run_at=RUN,
            draws=10,
            efficiency="data",
        )
        self.assertIn("5.5", text)
        self.assertNotIn(VEGAS_LABEL, text)

    def test_write_report_uses_chicago_date(self) -> None:
        rows, games = _fixture()
        with tempfile.TemporaryDirectory() as tmp:
            path = write_report(
                rows,
                games,
                season=2026,
                week=3,
                run_at=RUN,
                draws=10000,
                efficiency="data",
                dest=Path(tmp),
            )
            side = write_games_sidecar(
                season=2026,
                week=3,
                run_at=RUN,
                games=games,
                dest=Path(tmp),
            )
        self.assertEqual(path.name, "2026-w3-2026-09-24.md")
        self.assertEqual(report_path(2026, 3, RUN, Path(tmp)).name, path.name)
        self.assertEqual(side.name, "2026-w3-games.json")

    def test_csv_fills_salary_by_name_team_position(self) -> None:
        rows = [_row("Patrick Mahomes", "KC", "BUF", "QB", 30, None)]
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "players.csv"
            csv_path.write_text(
                "Id,Position,Nickname,Salary,Team,Opponent,Game\n"
                "1,QB,Patrick Mahomes,9000,KC,BUF,KC@BUF\n",
                encoding="utf-8",
            )
            filled = fill_salaries(rows, csv_path)
        self.assertEqual(filled[0]["salary"], 9000)
        self.assertIsNone(rows[0]["salary"])

    def test_matching_sidecar_shows_sim_scores(self) -> None:
        rows, games = _fixture()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_games_sidecar(season=2026, week=3, run_at=RUN, games=games, dest=root)
            with patch("nfl.report.REPORTS_DIR", root), patch(
                "nfl.gangstash_data.fetch_game_lines"
            ) as fetch:
                resolved = resolve_games(rows, season=2026, week=3)
            fetch.assert_not_called()
        self.assertEqual(resolved[0]["source"], "sim")
        self.assertEqual(resolved[0]["away_median"], 24.1)

    def test_stale_sidecar_uses_vegas_lines(self) -> None:
        rows, games = _fixture()
        line = {
            "home_team_fd": "BUF",
            "away_team_fd": "KC",
            "spread": -3.5,
            "total": 47.5,
            "game_id": "2026_03_KC_BUF",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_games_sidecar(
                season=2026,
                week=3,
                run_at="2026-09-25T08:00:00+00:00",
                games=games,
                dest=root,
            )
            with patch("nfl.report.REPORTS_DIR", root), patch(
                "nfl.gangstash_data.fetch_game_lines",
                return_value=([line], {}),
            ):
                resolved = resolve_games(rows, season=2026, week=3)
        self.assertEqual(resolved[0]["source"], "vegas")
        self.assertAlmostEqual(resolved[0]["home_median"], 25.5)
        self.assertAlmostEqual(resolved[0]["away_median"], 22.0)
        text = build_report(
            rows,
            resolved,
            season=2026,
            week=3,
            run_at=RUN,
            draws=None,
            efficiency="unknown",
        )
        self.assertIn(VEGAS_LABEL, text)
        self.assertIn("25.5", text)
        self.assertIn("22.0", text)
        self.assertIsNone(resolved[0]["home_p10"])


if __name__ == "__main__":
    unittest.main()
