"""Gangstash injury fields and the nightly projection handoff. No network."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from nfl.injuries import injury_code, injury_rows_from_records, stamp_injuries
from nfl.players import Player
from nfl.projections import depth_prior, usage_factor, week1_score
from nfl.publish_projections import build_entries, projection_rows
from nfl.sim import simulate_games
from nfl.sim_feed import sim_pool
from nfl.snaps import SnapWeekRow
from nfl.targets import TargetWeekRow

FIXTURE = Path(__file__).resolve().parent / "testdata" / "gangstash_injuries.json"


def _line() -> dict:
    return {
        "game_id": "2026_03_KC_BUF",
        "season": 2026,
        "week": 3,
        "home_team_fd": "BUF",
        "away_team_fd": "KC",
        "spread": -3.5,
        "total": 47.5,
    }


def _gb_line() -> dict:
    return {
        "game_id": "2026_03_GB_BUF",
        "season": 2026,
        "week": 3,
        "home_team_fd": "BUF",
        "away_team_fd": "GB",
        "spread": -3.5,
        "total": 47.5,
    }


def _depth(name: str, team: str, pos: str, rank: int, gsis: str) -> dict:
    return {
        "player_name": name,
        "team_fd": team,
        "pos_abb": pos,
        "pos_rank": rank,
        "gsis_id": gsis,
        "player_id": gsis,
    }


def _target(name: str, team: str, pos: str, share: float) -> TargetWeekRow:
    return TargetWeekRow(
        player=name,
        team=team,
        position=pos,
        week=2,
        targets=8,
        target_share=share,
        targets_avg=8.0,
        targets_total=8,
        source="gangstash",
        asof="2026-09-20",
    )


def _slate_depth() -> list[dict]:
    return [
        _depth("Alpha Receiver", "KC", "WR", 1, "00-0036322"),
        _depth("Beta Receiver", "KC", "WR", 2, "00-0037001"),
        _depth("Gamma Receiver", "KC", "WR", 3, "00-0037002"),
        _depth("Patrick Mahomes", "KC", "QB", 1, "00-0033873"),
        _depth("Josh Allen", "BUF", "QB", 1, "00-0034857"),
    ]


def _snap(name: str, team: str, pos: str, share: float) -> SnapWeekRow:
    return SnapWeekRow(
        player=name,
        team=team,
        position=pos,
        week=2,
        snaps=40,
        snap_share=share,
        snaps_avg=40.0,
        snaps_total=40,
        team_snap_pct=None,
        source="gangstash",
        asof="2026-09-20",
    )


def _shares() -> list[TargetWeekRow]:
    return [
        _target("Alpha Receiver", "KC", "WR", 0.30),
        _target("Beta Receiver", "KC", "WR", 0.12),
        _target("Gamma Receiver", "KC", "WR", 0.08),
    ]


def _by_pid(entries) -> dict:
    return {entry.player.pid: entry.player for entry in entries}


def _role(player: Player) -> float:
    return (
        depth_prior(player.depth_rank)
        * usage_factor(
            player.target_share,
            player.position,
            player.depth_rank,
            snap_share=player.snap_share,
        )
    )


class InjuryFieldTest(unittest.TestCase):
    def test_fixture_uses_live_gangstash_names(self) -> None:
        raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertIn("report_status", raw[0])
        self.assertIn("full_name", raw[0])
        self.assertIn("player_key", raw[0])
        self.assertNotIn("player_name", raw[0])
        self.assertNotIn("status", raw[0])
        rows = injury_rows_from_records(raw, season=2026, week=3)
        by_gsis = {row.gsis_id: row for row in rows}
        out = by_gsis["00-0036322"]
        self.assertEqual(out.name, "Alpha Receiver")
        self.assertEqual(out.status, "Out")
        self.assertEqual(injury_code(out.status), "O")
        self.assertEqual(out.player_key, "00-0036322")
        self.assertEqual(out.team, "KC")
        questionable = by_gsis["00-0034796"]
        self.assertEqual(questionable.status, "Questionable")
        self.assertEqual(questionable.player_key, "buf-qb-key")
        self.assertEqual(injury_code(questionable.status), "Q")

    def test_old_keys_still_parse(self) -> None:
        rows = injury_rows_from_records(
            [
                {
                    "player_name": "Old Name",
                    "status": "Out",
                    "gsis_id": "old-gsis",
                    "player_id": "old-pid",
                    "team_fd": "KC",
                    "season": 2026,
                    "week": 3,
                }
            ],
            season=2026,
            week=3,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "Old Name")
        self.assertEqual(rows[0].status, "Out")
        self.assertEqual(rows[0].gsis_id, "old-gsis")
        self.assertEqual(rows[0].player_id, "old-pid")
        self.assertIsNone(rows[0].player_key)

    def test_report_status_wins_over_legacy_status(self) -> None:
        rows = injury_rows_from_records(
            [
                {
                    "full_name": "Both Keys",
                    "report_status": "Questionable",
                    "status": "Out",
                    "gsis_id": "both",
                    "player_key": "both-key",
                    "team": "BUF",
                    "season": 2026,
                    "week": 3,
                }
            ],
            season=2026,
            week=3,
        )
        self.assertEqual(rows[0].status, "Questionable")
        self.assertEqual(rows[0].player_key, "both-key")

    def test_player_key_matches_when_gsis_differs(self) -> None:
        rows = injury_rows_from_records(
            [
                {
                    "full_name": "Key Only",
                    "report_status": "IR",
                    "gsis_id": "gsis-real",
                    "player_key": "pid-key",
                    "team": "KC",
                    "season": 2026,
                    "week": 3,
                }
            ],
            season=2026,
            week=3,
        )
        player = Player(
            pid="pid-key",
            name="Someone Else",
            position="WR",
            salary=0,
            team="KC",
            opponent="BUF",
            game="KC@BUF",
            fppg=None,
            injury="",
            roster_position="WR",
        )
        stamped = stamp_injuries([player], rows)
        self.assertEqual(stamped[0].injury, "IR")


class ProjectionInjuryTest(unittest.TestCase):
    def _baseline(self):
        return build_entries([_line()], _slate_depth(), target_rows=_shares())

    def test_out_wr1_is_removed_and_wr2_inherits_wr1_usage(self) -> None:
        before = _by_pid(self._baseline())
        raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
        # The fixture's doubtful and questionable names are not on this chart.
        out_only = [row for row in raw if row["report_status"] == "Out"]
        after = _by_pid(
            build_entries(
                [_line()],
                _slate_depth(),
                target_rows=_shares(),
                injury_rows=out_only,
            )
        )
        starter = after["00-0036322"]
        backup = after["00-0037001"]
        third = after["00-0037002"]
        self.assertEqual(starter.injury, "O")
        self.assertEqual(starter.objective, 0.0)
        self.assertIsNone(starter.depth_rank)
        self.assertNotIn(starter.pid, {pl.pid for pl in sim_pool(list(after.values()))})
        self.assertEqual(backup.depth_rank, 1)
        self.assertEqual(backup.target_share, before["00-0036322"].target_share)
        self.assertEqual(backup.injury, "")
        self.assertAlmostEqual(_role(backup), _role(before["00-0036322"]))
        self.assertAlmostEqual(
            backup.objective,
            week1_score(
                backup.implied_total or 0.0,
                depth_rank=1,
                position="WR",
                target_share=0.30,
                implied_opp=backup.implied_opp,
            ),
        )
        self.assertGreater(backup.objective, before["00-0037001"].objective)
        self.assertEqual(third.depth_rank, 2)
        self.assertEqual(third.target_share, before["00-0037002"].target_share)
        self.assertAlmostEqual(
            backup.objective - before["00-0037001"].objective,
            before["00-0036322"].objective - before["00-0037001"].objective,
        )

    def test_questionable_is_unchanged(self) -> None:
        before = _by_pid(self._baseline())
        rows = [
            {
                "full_name": "Alpha Receiver",
                "report_status": "Questionable",
                "gsis_id": "00-0036322",
                "player_key": "00-0036322",
                "practice_status": "Limited Participation In Practice",
                "date_modified": "2026-09-24T15:04:00Z",
                "team": "KC",
                "season": 2026,
                "week": 3,
            }
        ]
        after = _by_pid(
            build_entries(
                [_line()],
                _slate_depth(),
                target_rows=_shares(),
                injury_rows=rows,
            )
        )
        q = after["00-0036322"]
        base = before["00-0036322"]
        self.assertEqual(q.injury, "Q")
        self.assertEqual(q.depth_rank, base.depth_rank)
        self.assertEqual(q.target_share, base.target_share)
        self.assertEqual(q.objective, base.objective)
        self.assertEqual(after["00-0037001"].depth_rank, 2)
        self.assertEqual(after["00-0037001"].objective, before["00-0037001"].objective)
        pool = sim_pool(list(after.values()))
        self.assertIn(q.pid, {pl.pid for pl in pool})
        kept = next(pl for pl in pool if pl.pid == q.pid)
        self.assertEqual(kept.injury, "Q")

    def test_doubtful_stays_and_is_flagged(self) -> None:
        before = _by_pid(self._baseline())
        rows = [
            {
                "full_name": "Alpha Receiver",
                "report_status": "Doubtful",
                "gsis_id": "00-0036322",
                "player_key": "00-0036322",
                "team": "KC",
                "season": 2026,
                "week": 3,
            }
        ]
        entries = build_entries(
            [_line()],
            _slate_depth(),
            target_rows=_shares(),
            injury_rows=rows,
        )
        after = _by_pid(entries)
        flagged = after["00-0036322"]
        base = before["00-0036322"]
        self.assertEqual(flagged.injury, "D")
        self.assertEqual(flagged.depth_rank, base.depth_rank)
        self.assertEqual(flagged.target_share, base.target_share)
        self.assertEqual(flagged.objective, base.objective)
        self.assertEqual(after["00-0037001"].depth_rank, 2)
        pool = sim_pool([entry.player for entry in entries])
        kept = next(pl for pl in pool if pl.pid == flagged.pid)
        self.assertEqual(kept.injury, "")
        sim = simulate_games(pool, n=40, seed=1)
        self.assertGreater(sim.by_pid[flagged.pid].mean, 1.0)

    def test_csv_o_indicator_is_honored(self) -> None:
        before = _by_pid(self._baseline())
        csv = [
            Player(
                pid="fd-alpha",
                name="Alpha Receiver",
                position="WR",
                salary=7000,
                team="KC",
                opponent="BUF",
                game="KC@BUF",
                fppg=None,
                injury="O",
                roster_position="WR",
            )
        ]
        after = _by_pid(
            build_entries(
                [_line()],
                _slate_depth(),
                target_rows=_shares(),
                csv_players=csv,
            )
        )
        starter = after["00-0036322"]
        backup = after["00-0037001"]
        self.assertEqual(starter.injury, "O")
        self.assertEqual(starter.objective, 0.0)
        self.assertEqual(backup.depth_rank, 1)
        self.assertEqual(backup.target_share, 0.30)
        self.assertAlmostEqual(_role(backup), _role(before["00-0036322"]))
        self.assertNotIn("00-0036322", {pl.pid for pl in sim_pool(list(after.values()))})

    def test_promotion_keeps_the_higher_share_and_does_not_cascade(self) -> None:
        """WR2's snap is larger than the Out WR1 slot. WR3 must not receive it."""
        depth = [
            _depth("Jayden Reed", "GB", "WR", 1, "wr1"),
            _depth("Matthew Golden", "GB", "WR", 2, "wr2"),
            _depth("Skyy Moore", "GB", "WR", 3, "wr3"),
            _depth("Jordan Love", "GB", "QB", 1, "qb-gb"),
            _depth("Josh Allen", "BUF", "QB", 1, "qb-buf"),
        ]
        targets = [
            _target("Jayden Reed", "GB", "WR", 0.116),
            _target("Matthew Golden", "GB", "WR", 0.261),
            _target("Skyy Moore", "GB", "WR", 0.058),
        ]
        snaps = [
            _snap("Jayden Reed", "GB", "WR", 0.30),
            _snap("Matthew Golden", "GB", "WR", 0.84),
            _snap("Skyy Moore", "GB", "WR", 0.20),
        ]
        injuries = [
            {
                "full_name": "Jayden Reed",
                "report_status": "Out",
                "gsis_id": "wr1",
                "player_key": "wr1",
                "team": "GB",
                "season": 2026,
                "week": 3,
            }
        ]
        before = _by_pid(
            build_entries([_gb_line()], depth, target_rows=targets, snap_rows=snaps)
        )
        after = _by_pid(
            build_entries(
                [_gb_line()],
                depth,
                target_rows=targets,
                snap_rows=snaps,
                injury_rows=injuries,
            )
        )
        golden = after["wr2"]
        moore = after["wr3"]
        self.assertEqual(after["wr1"].objective, 0.0)
        self.assertEqual(golden.depth_rank, 1)
        self.assertGreaterEqual(golden.target_share, before["wr2"].target_share)
        self.assertGreaterEqual(golden.snap_share, before["wr2"].snap_share)
        self.assertEqual(golden.target_share, 0.261)
        self.assertEqual(golden.snap_share, 0.84)
        self.assertEqual(moore.depth_rank, 2)
        self.assertEqual(moore.target_share, before["wr3"].target_share)
        self.assertEqual(moore.snap_share, before["wr3"].snap_share)
        self.assertLessEqual(moore.snap_share, golden.snap_share)
        self.assertLessEqual(moore.target_share, golden.target_share)
        self.assertNotEqual(moore.snap_share, before["wr2"].snap_share)
        self.assertNotEqual(moore.target_share, before["wr2"].target_share)

    def test_rank_gap_without_an_out_is_unchanged(self) -> None:
        depth = [
            _depth("Starter TE", "NYG", "TE", 1, "te1"),
            _depth("Second TE", "NYG", "TE", 2, "te2"),
            _depth("Thomas Fidone", "NYG", "TE", 4, "te4"),
            _depth("Jaxson Dart", "NYG", "QB", 1, "qb-nyg"),
            _depth("Dak Prescott", "DAL", "QB", 1, "qb-dal"),
            _depth("Out WR", "NYG", "WR", 1, "wr-out"),
            _depth("Next WR", "NYG", "WR", 2, "wr-next"),
        ]
        line = {
            "game_id": "2026_03_NYG_DAL",
            "season": 2026,
            "week": 3,
            "home_team_fd": "DAL",
            "away_team_fd": "NYG",
            "spread": -3.0,
            "total": 44.0,
        }
        injuries = [
            {
                "full_name": "Out WR",
                "report_status": "Out",
                "gsis_id": "wr-out",
                "player_key": "wr-out",
                "team": "NYG",
                "season": 2026,
                "week": 3,
            }
        ]
        before = _by_pid(build_entries([line], depth))
        after = _by_pid(build_entries([line], depth, injury_rows=injuries))
        for pid in ("te1", "te2", "te4"):
            self.assertEqual(after[pid].depth_rank, before[pid].depth_rank)
            self.assertEqual(after[pid].target_share, before[pid].target_share)
            self.assertEqual(after[pid].snap_share, before[pid].snap_share)
            self.assertEqual(after[pid].objective, before[pid].objective)
        self.assertEqual(after["te4"].depth_rank, 4)
        self.assertEqual(after["wr-next"].depth_rank, 1)
        self.assertEqual(after["wr-out"].objective, 0.0)

    def test_inherited_share_is_not_labeled_lineups(self) -> None:
        depth = [
            _depth("Jayden Reed", "GB", "WR", 1, "wr1"),
            _depth("Savion Williams", "GB", "WR", 2, "wr2"),
            _depth("Jordan Love", "GB", "QB", 1, "qb-gb"),
            _depth("Josh Allen", "BUF", "QB", 1, "qb-buf"),
        ]
        targets = [_target("Jayden Reed", "GB", "WR", 0.22)]
        snaps = [_snap("Jayden Reed", "GB", "WR", 0.70)]
        injuries = [
            {
                "full_name": "Jayden Reed",
                "report_status": "Out",
                "gsis_id": "wr1",
                "player_key": "wr1",
                "team": "GB",
                "season": 2026,
                "week": 3,
            }
        ]
        entries = build_entries(
            [_gb_line()],
            depth,
            target_rows=targets,
            snap_rows=snaps,
            injury_rows=injuries,
        )
        williams = next(entry for entry in entries if entry.player.pid == "wr2")
        self.assertEqual(williams.player.target_share, 0.22)
        self.assertEqual(williams.player.snap_share, 0.70)
        self.assertEqual(williams.player.targets_source, "inherited")
        self.assertEqual(williams.player.snaps_source, "inherited")
        rows = projection_rows(
            [williams],
            season=2026,
            week=3,
            season_type="REG",
            run_at="2026-09-25T04:00:00+00:00",
            model_version="test",
        )
        sources = rows[0]["inputs"]["sources"]
        self.assertIn("inherited", sources)
        self.assertNotIn("lineups-tgt", sources)
        self.assertNotIn("lineups-snap", sources)


if __name__ == "__main__":
    unittest.main()
