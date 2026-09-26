"""Monte Carlo projections report. No network."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from nfl.report import (
    BAD_DATE_WARNING,
    NO_SLATE_WARNING,
    VEGAS_LABEL,
    _scorers,
    build_report,
    efficiency_from_rows,
    fill_salaries,
    graph_table,
    parse_args,
    parse_players_list_filename,
    report_path,
    resolve_auto_slate_csv,
    resolve_games,
    restrict_to_slate,
    runs_match,
    summarize_game_draws,
    write_games_sidecar,
    write_report,
)
from nfl.teams import canon_team

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
        self.assertIn("Tm", games_block.splitlines()[2])
        for name in (
            "Patrick Mahomes",
            "Josh Allen",
            "Travis Kelce",
            "CeeDee Lamb",
            "A.J. Brown",
            "Dak Prescott",
        ):
            self.assertIn(name, games_block)
        kelce = next(line for line in games_block.splitlines() if "Travis Kelce" in line)
        self.assertRegex(kelce, r"Travis Kelce\s+\|\s+KC\s+\|")
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

    def test_scorer_team_is_separate_from_the_score_row(self) -> None:
        rows = [
            _row("Patrick Mahomes", "KC", "MIA", "QB", 30, 9000),
            _row("Tua Tagovailoa", "MIA", "KC", "QB", 12, 7000),
        ]
        games = [
            {
                "game": "MIA@KC",
                "away": "MIA",
                "home": "KC",
                "source": "sim",
                "away_median": 17.4,
                "away_p10": 10.0,
                "away_p90": 24.0,
                "home_median": 27.0,
                "home_p10": 18.0,
                "home_p90": 36.0,
            }
        ]
        text = build_report(
            rows,
            games,
            season=2026,
            week=3,
            run_at=RUN,
            draws=20,
            efficiency="data",
        )
        line = next(row for row in text.splitlines() if "Patrick Mahomes" in row)
        self.assertRegex(line, r"MIA\s+\|\s+17\.4")
        self.assertRegex(line, r"Patrick Mahomes\s+\|\s+KC\s+\|")
        self.assertLessEqual(len(line), 100)

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
        priced = _row("Patrick Mahomes", "KC", "BUF", "QB", 30, None)
        missing = _row("Isiah Pacheco", "KC", "BUF", "RB", 10, None)
        away = _row("Joe Burrow", "CIN", "DET", "QB", 16, None)
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "players.csv"
            csv_path.write_text(
                "Id,Position,Nickname,Salary,Team,Opponent,Game\n"
                "1,QB,Patrick Mahomes,9000,KC,BUF,KC@BUF\n",
                encoding="utf-8",
            )
            filled = fill_salaries([priced, missing, away], csv_path)
        self.assertEqual(filled[0]["salary"], 9000)
        self.assertNotIn("off_slate", filled[0])
        self.assertIsNone(filled[1]["salary"])
        self.assertNotIn("off_slate", filled[1])
        self.assertIsNone(filled[2]["salary"])
        self.assertTrue(filled[2]["off_slate"])
        self.assertIsNone(priced["salary"])
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
            }
        ]
        shown = build_report(
            filled,
            games,
            season=2026,
            week=3,
            run_at=RUN,
            draws=20,
            efficiency="data",
        )
        self.assertIn("off slate", shown)
        self.assertIn("9,000", shown)
        pacheco = next(line for line in shown.splitlines() if "Isiah Pacheco" in line)
        self.assertNotIn("off slate", pacheco)
        self.assertIn("—", pacheco)
        unlabeled = build_report(
            [priced, missing, away],
            games,
            season=2026,
            week=3,
            run_at=RUN,
            draws=20,
            efficiency="data",
        )
        self.assertNotIn("off slate", unlabeled)

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


class SimGameResultsTest(unittest.TestCase):
    def test_simulate_games_feeds_the_report_sim_scores(self) -> None:
        from nfl.players import Player
        from nfl.projections import week1_score
        from nfl.report import fmt_points
        from nfl.sim import GameSim, simulate_games

        def player(
            pid: str,
            name: str,
            team: str,
            opponent: str,
            implied: float,
            implied_opp: float,
        ) -> Player:
            return Player(
                pid=pid,
                name=name,
                position="QB",
                salary=8000,
                team=team,
                opponent=opponent,
                game="KC@BUF",
                fppg=None,
                injury="",
                roster_position="",
                implied_total=implied,
                implied_opp=implied_opp,
                objective=week1_score(
                    implied,
                    depth_rank=1,
                    position="QB",
                    implied_opp=implied_opp,
                ),
                depth_rank=1,
            )

        kc = player("qb-kc", "Patrick Mahomes", "KC", "BUF", 22.0, 25.5)
        buf = player("qb-buf", "Josh Allen", "BUF", "KC", 25.5, 22.0)
        n = 20
        sim = simulate_games([kc, buf], n=n, seed=7)
        bare = GameSim(by_pid={}, draws={})
        self.assertEqual(bare.game_draws, ())
        self.assertEqual(len(sim.game_draws), n)
        self.assertEqual(len(sim.draws[kc.pid]), n)
        for game, away, home, away_pts, home_pts in sim.game_draws:
            self.assertEqual((game, away, home), ("KC@BUF", "KC", "BUF"))
            self.assertIsInstance(away_pts, float)
            self.assertIsInstance(home_pts, float)
        summaries = summarize_game_draws(sim.game_draws)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["source"], "sim")
        rows = [
            _row("Patrick Mahomes", "KC", "BUF", "QB", sim.by_pid[kc.pid].mean, 9000),
            _row("Josh Allen", "BUF", "KC", "QB", sim.by_pid[buf.pid].mean, 8800),
        ]
        text = build_report(
            rows,
            sim.game_draws,
            season=2026,
            week=3,
            run_at=RUN,
            draws=n,
            efficiency="data",
        )
        self.assertNotIn(VEGAS_LABEL, text)
        self.assertIn("[ GAMES ]", text)
        mahomes = next(line for line in text.splitlines() if "Patrick Mahomes" in line)
        self.assertRegex(mahomes, r"Patrick Mahomes\s+\|\s+KC\s+\|")
        self.assertIn(fmt_points(summaries[0]["away_median"]), text)
        self.assertIn(fmt_points(summaries[0]["home_median"]), text)
        self.assertIn(fmt_points(summaries[0]["away_p10"]), text)
        self.assertIn(fmt_points(summaries[0]["home_p90"]), text)


class InjuryTagTest(unittest.TestCase):
    def test_tags_q_and_d_and_hides_out(self) -> None:
        def row(name, team, pos, mean, code):
            return {
                "model": "sim",
                "player_name": name,
                "team": team,
                "opponent": "BUF",
                "position": pos,
                "mean": mean,
                "p10": 1.0,
                "p90": 20.0,
                "salary": 6000,
                "inputs": {"injury": code, "sim_efficiency": "data"},
            }

        rows = [
            row("Out Receiver", "KC", "WR", 40.0, "O"),
            row("Questionable Receiver", "KC", "WR", 18.0, "Q"),
            row("Doubtful Receiver", "KC", "WR", 16.0, "D"),
            row("Healthy Receiver", "KC", "WR", 12.0, ""),
            row("Patrick Mahomes", "KC", "QB", 22.0, ""),
            row("Josh Allen", "BUF", "QB", 21.0, ""),
        ]
        games = [
            {
                "game": "KC@BUF",
                "away": "KC",
                "home": "BUF",
                "source": "sim",
                "away_median": 22.0,
                "away_p10": 12.0,
                "away_p90": 32.0,
                "home_median": 24.0,
                "home_p10": 14.0,
                "home_p90": 34.0,
            }
        ]
        text = build_report(
            rows,
            games,
            season=2026,
            week=3,
            run_at=RUN,
            draws=100,
            efficiency="data",
        )
        self.assertIn("Questionable Receiver (Q)", text)
        self.assertIn("Doubtful Receiver (D)", text)
        self.assertNotIn("Out Receiver", text)
        self.assertIn("Healthy Receiver", text)


def _players_csv(path: Path, rows: list[tuple]) -> None:
    lines = ["Id,Position,Nickname,Salary,Team,Opponent,Game"]
    for item in rows:
        lines.append(",".join(str(part) for part in item))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _game(away: str, home: str, median: float = 21.0) -> dict:
    return {
        "game": f"{away}@{home}",
        "away": away,
        "home": home,
        "source": "sim",
        "away_median": median,
        "away_p10": 10.0,
        "away_p90": 30.0,
        "home_median": median + 3,
        "home_p10": 12.0,
        "home_p90": 34.0,
    }


def _report(rows: list[dict], games: list[dict], warnings=None) -> str:
    return build_report(
        rows,
        games,
        season=2026,
        week=3,
        run_at=RUN,
        draws=100,
        efficiency="data",
        warnings=warnings,
    )


class TeamCanonTest(unittest.TestCase):
    def test_aliases_collapse_to_fanduel_codes(self) -> None:
        self.assertEqual(canon_team("jax"), "JAC")
        self.assertEqual(canon_team("JAC"), "JAC")
        self.assertEqual(canon_team("WSH"), "WAS")
        self.assertEqual(canon_team("LA"), "LAR")
        self.assertEqual(canon_team("OAK"), "LV")
        self.assertEqual(canon_team("LAC"), "LAC")
        self.assertEqual(canon_team("nope"), "NOPE")
        self.assertEqual(canon_team(""), "")

    def test_scorers_match_alias_to_game_code(self) -> None:
        cases = (("JAX", "JAC"), ("LA", "LAR"), ("WSH", "WAS"), ("OAK", "LV"))
        for stored, game_code in cases:
            rows = [_row("Alias Player", stored, "NE", "QB", 19, 7000)]
            found = _scorers(rows, "NE", game_code)
            self.assertEqual([row["player_name"] for row in found], ["Alias Player"])
            found_flip = _scorers(
                [_row("Alias Player", game_code, "NE", "QB", 19, 7000)],
                "NE",
                stored,
            )
            self.assertEqual([row["player_name"] for row in found_flip], ["Alias Player"])

    def test_fixture_report_is_unchanged_by_canon(self) -> None:
        rows, games = _fixture()
        after = _report(rows, games)

        def raw(value: str) -> str:
            return str(value or "").strip().upper()

        with patch("nfl.report.canon_team", side_effect=raw):
            before = _report(rows, games)
        self.assertEqual(before, after)

    def test_only_jacksonville_rows_differ(self) -> None:
        rows = [
            _row("Patrick Mahomes", "KC", "BUF", "QB", 30, 9000),
            _row("Josh Allen", "BUF", "KC", "QB", 28, 8800),
            _row("Trevor Lawrence", "JAX", "NE", "QB", 20, 7600),
            _row("Travis Etienne", "JAX", "NE", "RB", 18, 7000),
            _row("Isiah Pacheco", "KC", "BUF", "RB", 14, 6500),
        ]
        games = [_game("KC", "BUF", 24.0), _game("NE", "JAC", 17.0)]
        after = _report(rows, games)

        def raw(value: str) -> str:
            return str(value or "").strip().upper()

        with patch("nfl.report.canon_team", side_effect=raw):
            before = _report(rows, games)
        jac_rows = []
        for row in rows:
            copy = dict(row)
            if copy["team"] == "JAX":
                copy["team"] = "JAC"
            jac_rows.append(copy)
        self.assertEqual(after, _report(jac_rows, games))
        self.assertNotIn("Trevor Lawrence", _fences(before)[0])
        self.assertIn("Trevor Lawrence", _fences(after)[0])
        self.assertIn("Travis Etienne", _fences(after)[0])
        tokens = ("Trevor", "Etienne", "JAX", "JAC")

        def stable(text: str) -> list[str]:
            return [line for line in text.splitlines() if not any(tok in line for tok in tokens)]

        self.assertEqual(stable(before), stable(after))
        before_qb = next(
            line for line in before.splitlines() if "Trevor Lawrence" in line and "@" not in line
        )
        after_qb = next(
            line for line in after.splitlines() if "Trevor Lawrence" in line and "@" not in line
        )
        self.assertEqual(before_qb.replace("JAX", "JAC"), after_qb)

    def test_csv_salary_matches_jacksonville_alias(self) -> None:
        row = _row("Trevor Lawrence", "JAX", "NE", "QB", 20, None)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "players.csv"
            _players_csv(
                path,
                [("1", "QB", "Trevor Lawrence", 7600, "JAC", "NE", "NE@JAC")],
            )
            filled = fill_salaries([row], path)
        self.assertEqual(filled[0]["salary"], 7600)
        self.assertNotIn("off_slate", filled[0])
        self.assertIsNone(row["salary"])


class SlateCsvTest(unittest.TestCase):
    def _slate(self) -> tuple[list[dict], list[dict]]:
        rows = [
            _row("Patrick Mahomes", "KC", "BUF", "QB", 30, 1000),
            _row("Josh Allen", "BUF", "KC", "QB", 28, 1000),
            _row("Bijan Robinson", "ATL", "GB", "RB", 22, 1000),
            _row("Trevor Lawrence", "JAX", "NE", "QB", 20, 1000),
            _row("Travis Etienne Jr.", "JAX", "NE", "RB", 18, 1000),
            _row("Jacksonville Jaguars", "JAX", "NE", "D", 8, 1000),
            _row("Qb 0", "KC", "BUF", "QB", 40, 1000),
            _row("Qb 1", "KC", "BUF", "QB", 16, 1000),
            _row("Qb 2", "KC", "BUF", "QB", 15, 1000),
            _row("Qb 3", "KC", "BUF", "QB", 14, 1000),
            _row("Qb 4", "KC", "BUF", "QB", 13, 1000),
        ]
        games = [
            _game("KC", "BUF", 24.0),
            _game("ATL", "GB", 23.0),
            _game("NE", "JAC", 17.0),
        ]
        return rows, games

    def test_flag_defaults_off(self) -> None:
        self.assertIsNone(parse_args([]).slate_csv)
        self.assertEqual(parse_args(["--slate-csv", "auto"]).slate_csv, "auto")

    def test_restricts_games_players_salaries_and_backfills(self) -> None:
        rows, games = self._slate()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "slate.csv"
            _players_csv(
                path,
                [
                    ("1", "QB", "Patrick Mahomes", 9000, "KC", "BUF", "KC@BUF"),
                    ("2", "QB", "Josh Allen", 8800, "BUF", "KC", "KC@BUF"),
                    ("3", "QB", "C.J. Stroud", 7500, "HOU", "IND", "HOU@IND"),
                    ("4", "WR", "Ja'Marr Chase", 8000, "CIN", "DET", "CIN@DET"),
                    ("5", "RB", "Travis Etienne", 6400, "JAC", "NE", "NE@JAC"),
                    ("6", "D", "Jaguars", 3800, "JAC", "NE", "NE@JAC"),
                    ("7", "QB", "Trevor Lawrence", 7600, "JAC", "NE", "NE@JAC"),
                    ("8", "QB", "Qb 1", 5000, "KC", "BUF", "KC@BUF"),
                    ("9", "QB", "Qb 2", 5000, "KC", "BUF", "KC@BUF"),
                    ("10", "QB", "Qb 3", 5000, "KC", "BUF", "KC@BUF"),
                    ("11", "QB", "Qb 4", 5000, "KC", "BUF", "KC@BUF"),
                    ("12", "WR", "Van Jefferson", 4500, "TEN", "NO", "TEN@NO"),
                    ("13", "QB", "Kenny Pickett Sr.", 5000, "PHI", "DAL", "PHI@DAL"),
                    ("14", "WR", "Robert Woods II", 5000, "PIT", "CLE", "PIT@CLE"),
                    ("15", "WR", "Odell Beckham III", 5000, "MIA", "NYJ", "MIA@NYJ"),
                ],
            )
            extra = [
                _row("CJ Stroud", "HOU", "IND", "QB", 19, 1000),
                _row("Jamarr Chase", "CIN", "DET", "WR", 21, 1000),
                _row("Kenny Pickett", "PHI", "DAL", "QB", 12, 1000),
                _row("Robert Woods", "PIT", "CLE", "WR", 11, 1000),
                _row("Odell Beckham", "MIA", "NYJ", "WR", 10, 1000),
            ]
            snapshot = [dict(row) for row in rows + extra]
            slate_games = games + [_game("TEN", "NO", 19.0)]
            filtered, kept, notes = restrict_to_slate(
                rows + extra, slate_games, str(path), today=date(2026, 9, 27)
            )
        self.assertEqual(rows + extra, snapshot)
        self.assertEqual(notes, ["warning: slate game TEN@NO has no projections"])
        labels = [f"{game['away']}@{game['home']}" for game in kept]
        self.assertNotIn("ATL@GB", labels)
        self.assertIn("KC@BUF", labels)
        self.assertIn("NE@JAC", labels)
        self.assertIn("TEN@NO", labels)
        text = _report(filtered, kept, notes)
        self.assertTrue(text.startswith("warning: slate game TEN@NO has no projections\n"))
        names = {row["player_name"] for row in filtered}
        self.assertNotIn("Bijan Robinson", names)
        self.assertNotIn("Qb 0", names)
        self.assertNotIn("Van Jefferson", names)
        for name in (
            "Patrick Mahomes",
            "CJ Stroud",
            "Jamarr Chase",
            "Travis Etienne Jr.",
            "Trevor Lawrence",
            "Jacksonville Jaguars",
            "Kenny Pickett",
            "Robert Woods",
            "Odell Beckham",
            "Qb 1",
        ):
            self.assertIn(name, names)
        self.assertNotIn("Bijan Robinson", text)
        self.assertNotIn("Qb 0", text)
        self.assertNotIn("Van Jefferson", text)
        self.assertIn("Qb 1", text)
        mahomes = next(line for line in text.splitlines() if "Patrick Mahomes" in line and "3.33" in line)
        self.assertIn("9,000", mahomes)
        self.assertNotIn("30.00", mahomes)
        jax = next(line for line in text.splitlines() if "Jacksonville Jaguars" in line)
        self.assertIn("3,800", jax)
        self.assertIn("JAC", jax)
        self.assertEqual(filtered[0]["salary"], 9000)
        self.assertEqual(rows[0]["salary"], 1000)

    def test_unusable_csv_falls_back_without_mutating_rows(self) -> None:
        rows, games = self._slate()
        snapshot = [dict(row) for row in rows]
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            missing = folder / "nope.csv"
            empty = folder / "empty.csv"
            empty.write_text("", encoding="utf-8")
            header = folder / "header.csv"
            header.write_text(
                "Id,Position,Nickname,Salary,Team,Opponent,Game\n",
                encoding="utf-8",
            )
            malformed = folder / "bad.csv"
            malformed.write_text("hello\n", encoding="utf-8")
            zero = folder / "zero.csv"
            _players_csv(zero, [("1", "QB", "Van Jefferson", 4500, "TEN", "NO", "TEN@NO")])
            cases = [
                (str(missing), "slate CSV missing"),
                (str(empty), "slate CSV empty"),
                (str(header), "slate CSV empty"),
                (str(malformed), "slate CSV malformed"),
                (str(zero), "matched no projected players"),
            ]
            for spec, needle in cases:
                got_rows, got_games, notes = restrict_to_slate(
                    rows, games, spec, today=date(2026, 9, 27)
                )
                self.assertEqual(rows, snapshot)
                self.assertIs(got_rows, rows)
                self.assertIs(got_games, games)
                self.assertEqual(len(notes), 1)
                self.assertIn(needle, notes[0])
                text = _report(got_rows, got_games, notes)
                self.assertTrue(text.startswith("warning:"))
                self.assertIn("Bijan Robinson", text)
                self.assertIn("ATL@GB", text)

    def test_filename_dates_and_auto_choice(self) -> None:
        parsed = parse_players_list_filename(
            "FanDuel-NFL-2026 CDT-09 CDT-27 CDT-134503-players-list.csv"
        )
        self.assertEqual(parsed, (date(2026, 9, 27), 134503))
        self.assertIsNone(
            parse_players_list_filename(
                "FanDuel-NFL-2026 CDT-09 CDT-27 CDT-999-entries-upload-template.csv"
            )
        )
        self.assertIsNone(
            parse_players_list_filename(
                "FanDuel-NFL-2026 CDT-13 CDT-40 CDT-1-players-list.csv"
            )
        )
        today = date(2026, 9, 27)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            older = "FanDuel-NFL-2026 CDT-09 CDT-24 CDT-111-players-list.csv"
            same_low = "FanDuel-NFL-2026 CDT-09 CDT-27 CDT-100-players-list.csv"
            same_high = "FanDuel-NFL-2026 CDT-09 CDT-27 CDT-500-players-list.csv"
            future = "FanDuel-NFL-2026 CDT-10 CDT-04 CDT-50-players-list.csv"
            template = "FanDuel-NFL-2026 CDT-10 CDT-11 CDT-999-entries-upload-template.csv"
            bad = "FanDuel-NFL-not-a-date-players-list.csv"
            for name in (older, same_low, same_high, future, template, bad):
                (folder / name).write_text("x\n", encoding="utf-8")
            path, warning = resolve_auto_slate_csv(folder, today)
            self.assertIsNone(warning)
            self.assertEqual(path.name, future)
            (folder / future).unlink()
            path, warning = resolve_auto_slate_csv(folder, today)
            self.assertEqual(path.name, same_high)
            for name in (same_low, same_high):
                (folder / name).unlink()
            path, warning = resolve_auto_slate_csv(folder, today)
            self.assertIsNone(path)
            self.assertEqual(warning, NO_SLATE_WARNING)
            for name in (older, template):
                (folder / name).unlink()
            path, warning = resolve_auto_slate_csv(folder, today)
            self.assertIsNone(path)
            self.assertEqual(warning, BAD_DATE_WARNING)

    def test_auto_filters_the_chosen_file_and_bad_auto_falls_back(self) -> None:
        rows, games = self._slate()
        today = date(2026, 9, 27)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            older = folder / "FanDuel-NFL-2026 CDT-09 CDT-24 CDT-111-players-list.csv"
            current = folder / "FanDuel-NFL-2026 CDT-09 CDT-27 CDT-222-players-list.csv"
            future = folder / "FanDuel-NFL-2026 CDT-10 CDT-04 CDT-333-players-list.csv"
            template = folder / "FanDuel-NFL-2026 CDT-10 CDT-04 CDT-999-entries-upload-template.csv"
            _players_csv(older, [("1", "RB", "Bijan Robinson", 8000, "ATL", "GB", "ATL@GB")])
            _players_csv(
                current,
                [("1", "QB", "Patrick Mahomes", 9100, "KC", "BUF", "KC@BUF")],
            )
            _players_csv(future, [("1", "QB", "Josh Allen", 9200, "BUF", "KC", "KC@BUF")])
            template.write_text("entry_id\n", encoding="utf-8")
            filtered, kept, notes = restrict_to_slate(
                rows, games, "auto", today=today, data_dir=folder
            )
            self.assertEqual(notes, [])
            text = _report(filtered, kept, notes)
            self.assertIn("Josh Allen", text)
            self.assertIn("9,200", text)
            self.assertNotIn("Patrick Mahomes", text)
            self.assertNotIn("Bijan Robinson", text)
            self.assertNotIn("ATL@GB", text)
            bad_dir = folder / "bad"
            bad_dir.mkdir()
            (bad_dir / "FanDuel-NFL-bogus-players-list.csv").write_text(
                "Id,Position,Nickname,Salary,Team\n1,QB,Patrick Mahomes,9000,KC\n",
                encoding="utf-8",
            )
            got_rows, got_games, notes = restrict_to_slate(
                rows, games, "auto", today=today, data_dir=bad_dir
            )
            self.assertIs(got_rows, rows)
            self.assertIs(got_games, games)
            self.assertEqual(notes, [BAD_DATE_WARNING])


if __name__ == "__main__":
    unittest.main()
