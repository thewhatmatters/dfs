"""Randy's availability rule (2026-10-09). No network.

Practice-only and Questionable keep their projection. Out, IR, and
Doubtful project 0 and the next player at that position takes the share.
``--availability-rule legacy`` matches the pre-change sim for one seed.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from nfl.injuries import Availability
from nfl.players import Player
from nfl.projections import week1_score
from nfl.publish_projections import build_entries, parse_args
from nfl.rules import FANDUEL_NFL
from nfl.sim import (
    _HistoryIndex,
    _give_inactive_targets_to_next,
    _prepare_opportunity,
    simulate_games,
)
from nfl.sim_feed import sim_pool
from nfl.sim_inputs import sim_inputs_from_records
from nfl.targets import TargetWeekRow

GOLDEN = Path(__file__).resolve().parent / "testdata" / "availability_legacy_seed7.json"


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


def _chart() -> list[dict]:
    return [
        _depth("Alpha Receiver", "KC", "WR", 1, "wr1"),
        _depth("Beta Receiver", "KC", "WR", 2, "wr2"),
        _depth("Lead Back", "KC", "RB", 1, "rb1"),
        _depth("Next Back", "KC", "RB", 2, "rb2"),
        _depth("Starter Tight", "KC", "TE", 1, "te1"),
        _depth("Next Tight", "KC", "TE", 2, "te2"),
        _depth("Patrick Mahomes", "KC", "QB", 1, "qb-kc"),
        _depth("Josh Allen", "BUF", "QB", 1, "qb-buf"),
    ]


def _shares() -> list[TargetWeekRow]:
    return [
        _target("Alpha Receiver", "KC", "WR", 0.30),
        _target("Beta Receiver", "KC", "WR", 0.12),
        _target("Lead Back", "KC", "RB", 0.18),
        _target("Next Back", "KC", "RB", 0.06),
        _target("Starter Tight", "KC", "TE", 0.16),
        _target("Next Tight", "KC", "TE", 0.05),
    ]


def _by_pid(entries) -> dict:
    return {entry.player.pid: entry.player for entry in entries}


def _pl(**kw) -> Player:
    fields = dict(
        pid="p",
        name="Cam",
        position="WR",
        salary=5000,
        team="TB",
        opponent="CAR",
        game="CAR@TB",
        fppg=None,
        injury="",
        roster_position="",
        implied_total=24.0,
        implied_opp=20.0,
        total=44.0,
        spread=-4.0,
        depth_rank=1,
        target_share=0.2,
        snap_share=0.5,
    )
    fields.update(kw)
    fields["objective"] = week1_score(
        fields.get("implied_total") or 0.0,
        depth_rank=fields.get("depth_rank"),
        position=fields["position"],
        implied_opp=fields.get("implied_opp"),
        target_share=fields.get("target_share"),
        snap_share=fields.get("snap_share"),
    )
    allowed = Player.__dataclass_fields__
    return Player(**{k: v for k, v in fields.items() if k in allowed})


def _tb_slate() -> tuple[list[Player], object]:
    """Fixed seed fixture. Same players the legacy golden was drawn from."""
    common = dict(
        team="TB",
        opponent="CAR",
        game="CAR@TB",
        total=44.0,
        spread=-4.0,
        implied_total=24.0,
        implied_opp=20.0,
    )
    players = [
        _pl(
            pid="qb1",
            name="Baker Mayfield",
            position="QB",
            depth_rank=1,
            salary=8000,
            injury="O",
            target_share=None,
            snap_share=None,
            **common,
        ),
        _pl(
            pid="qb2",
            name="Jalon Daniels",
            position="QB",
            depth_rank=2,
            salary=6000,
            injury="",
            target_share=None,
            snap_share=None,
            **common,
        ),
        _pl(
            pid="wr1",
            name="Alpha Receiver",
            position="WR",
            depth_rank=1,
            injury="D",
            target_share=0.30,
            snap_share=0.80,
            **common,
        ),
        _pl(
            pid="wr2",
            name="Beta Receiver",
            position="WR",
            depth_rank=2,
            injury="",
            target_share=0.12,
            snap_share=0.40,
            **common,
        ),
        _pl(
            pid="wr3",
            name="Gamma Receiver",
            position="WR",
            depth_rank=3,
            injury="Q",
            target_share=0.08,
            snap_share=0.20,
            **common,
        ),
        _pl(
            pid="wr4",
            name="Practice Only",
            position="WR",
            depth_rank=4,
            injury="DNP",
            target_share=0.05,
            snap_share=0.10,
            **common,
        ),
        _pl(
            pid="rb1",
            name="Lead Back",
            position="RB",
            depth_rank=1,
            injury="IR",
            target_share=0.10,
            snap_share=0.70,
            **common,
        ),
        _pl(
            pid="rb2",
            name="Next Back",
            position="RB",
            depth_rank=2,
            injury="",
            target_share=0.04,
            snap_share=0.25,
            **common,
        ),
    ]
    rows = []
    for name, share, pos in (
        ("Alpha Receiver", 0.30, "WR"),
        ("Beta Receiver", 0.12, "WR"),
        ("Gamma Receiver", 0.08, "WR"),
        ("Practice Only", 0.05, "WR"),
        ("Lead Back", 0.10, "RB"),
        ("Next Back", 0.04, "RB"),
    ):
        for week in (1, 2, 3):
            rows.append(
                {
                    "season": 2025,
                    "week": week,
                    "position": pos,
                    "player_name": name,
                    "team_fd": "TB",
                    "targets": round(30 * share, 2),
                    "target_share": share,
                    "team_targets": 30,
                    "team_pass_attempts": 34,
                }
            )
    return players, sim_inputs_from_records(targets=rows)


class AvailabilityRuleTest(unittest.TestCase):
    def test_flags_default_to_the_new_rule(self) -> None:
        args = parse_args([])
        self.assertEqual(args.availability_rule, "ruled-out")
        self.assertTrue(args.zero_doubtful)
        self.assertEqual(
            parse_args(["--availability-rule", "legacy"]).availability_rule,
            "legacy",
        )
        self.assertFalse(parse_args(["--no-zero-doubtful"]).zero_doubtful)
        codes = Availability().out_codes()
        self.assertTrue(set(FANDUEL_NFL.out_codes) <= set(codes))
        self.assertIn("O", codes)
        self.assertIn("D", codes)
        self.assertNotIn("D", Availability(rule="legacy").out_codes())
        self.assertNotIn("D", Availability(zero_doubtful=False).out_codes())
        self.assertNotIn("Q", codes)
        self.assertNotIn("DNP", codes)

    def test_dnp_and_questionable_keep_their_projection(self) -> None:
        before = _by_pid(build_entries([_line()], _chart(), target_rows=_shares()))
        rows = [
            {
                "full_name": "Alpha Receiver",
                "report_status": "Questionable",
                "practice_status": "Limited Participation In Practice",
                "gsis_id": "wr1",
                "player_key": "wr1",
                "team": "KC",
                "season": 2026,
                "week": 3,
            },
            {
                "full_name": "Beta Receiver",
                "report_status": "DNP",
                "gsis_id": "wr2",
                "player_key": "wr2",
                "team": "KC",
                "season": 2026,
                "week": 3,
            },
            {
                "full_name": "Lead Back",
                "report_status": "",
                "practice_status": "Did Not Participate In Practice",
                "gsis_id": "rb1",
                "player_key": "rb1",
                "team": "KC",
                "season": 2026,
                "week": 3,
            },
            {
                "full_name": "Next Back",
                "report_status": "Full",
                "gsis_id": "rb2",
                "player_key": "rb2",
                "team": "KC",
                "season": 2026,
                "week": 3,
            },
        ]
        after = _by_pid(
            build_entries(
                [_line()],
                _chart(),
                target_rows=_shares(),
                injury_rows=rows,
            )
        )
        for pid in ("wr1", "wr2", "rb1", "rb2"):
            self.assertEqual(after[pid].depth_rank, before[pid].depth_rank)
            self.assertEqual(after[pid].target_share, before[pid].target_share)
            self.assertAlmostEqual(
                after[pid].objective, before[pid].objective, places=9
            )
        self.assertEqual(after["wr1"].injury, "Q")
        self.assertEqual(after["wr2"].injury, "DNP")
        self.assertEqual(after["rb1"].injury, "")
        self.assertEqual(after["rb2"].injury, "FULL")
        players, inputs = _tb_slate()
        sim = simulate_games(
            players,
            n=40,
            seed=7,
            inputs=inputs,
            availability_rule="ruled-out",
        )
        self.assertGreater(sim.by_pid["wr3"].mean, 1.0)
        self.assertGreater(sim.by_pid["wr4"].mean, 0.5)
        self.assertEqual(sim.by_pid["wr3"].n, 40)

    def test_out_ir_and_doubtful_are_zero_and_the_next_share_rises(self) -> None:
        before = _by_pid(build_entries([_line()], _chart(), target_rows=_shares()))
        rows = [
            {
                "full_name": "Alpha Receiver",
                "report_status": "Out",
                "gsis_id": "wr1",
                "team": "KC",
                "season": 2026,
                "week": 3,
            },
            {
                "full_name": "Lead Back",
                "report_status": "IR",
                "gsis_id": "rb1",
                "team": "KC",
                "season": 2026,
                "week": 3,
            },
            {
                "full_name": "Starter Tight",
                "report_status": "Doubtful",
                "gsis_id": "te1",
                "team": "KC",
                "season": 2026,
                "week": 3,
            },
        ]
        after = _by_pid(
            build_entries(
                [_line()],
                _chart(),
                target_rows=_shares(),
                injury_rows=rows,
            )
        )
        for gone, nxt, share in (
            ("wr1", "wr2", 0.30),
            ("rb1", "rb2", 0.18),
            ("te1", "te2", 0.16),
        ):
            self.assertEqual(after[gone].objective, 0.0)
            self.assertIsNone(after[gone].depth_rank)
            self.assertEqual(after[nxt].depth_rank, 1)
            self.assertEqual(after[nxt].target_share, share)
            self.assertGreater(after[nxt].objective, before[nxt].objective)
            self.assertAlmostEqual(
                after[nxt].target_share - before[nxt].target_share,
                before[gone].target_share - before[nxt].target_share,
                places=9,
            )

        wr_out = _pl(pid="wr-out", name="Alpha", position="WR", depth_rank=1, team="KC")
        wr_next = _pl(
            pid="wr-next",
            name="Beta",
            position="WR",
            depth_rank=2,
            team="KC",
            salary=4000,
        )
        te = _pl(pid="te", name="Tight", position="TE", depth_rank=1, team="KC")
        moved, leftover = _give_inactive_targets_to_next(
            [wr_next, te],
            [0.12, 0.15],
            [wr_out],
            [0.30],
        )
        self.assertAlmostEqual(moved[0], 0.42, places=9)
        self.assertAlmostEqual(moved[1], 0.15, places=9)
        self.assertEqual(leftover, [])
        self.assertAlmostEqual(sum(moved) + sum(leftover), 0.12 + 0.15 + 0.30, places=9)

        players, inputs = _tb_slate()
        index = _HistoryIndex(inputs)
        legacy_pool = sim_pool(players, availability=Availability(rule="legacy"))
        prep_legacy = _prepare_opportunity(legacy_pool, index)
        prep_new = _prepare_opportunity(
            players, index, availability=Availability()
        )
        self.assertAlmostEqual(
            sum(prep_legacy.simplex_means),
            sum(prep_new.simplex_means),
            places=9,
        )
        self.assertAlmostEqual(sum(prep_new.simplex_means), 1.0, places=9)
        self.assertEqual(prep_new.starter.pid, "qb2")
        new = simulate_games(
            players,
            n=40,
            seed=7,
            inputs=inputs,
            availability_rule="ruled-out",
        )
        old = simulate_games(
            players,
            n=40,
            seed=7,
            inputs=inputs,
            availability_rule="legacy",
        )
        self.assertAlmostEqual(new.by_pid["wr1"].mean, 0.0, places=9)
        self.assertAlmostEqual(new.by_pid["rb1"].mean, 0.0, places=9)
        self.assertAlmostEqual(new.by_pid["qb1"].mean, 0.0, places=9)
        self.assertGreater(new.by_pid["wr2"].mean, old.by_pid["wr2"].mean)
        self.assertGreater(new.by_pid["qb2"].mean, 1.0)
        self.assertGreater(new.by_pid["rb2"].mean, 1.0)

    def test_doubtful_flag_off_keeps_the_projection(self) -> None:
        before = _by_pid(build_entries([_line()], _chart(), target_rows=_shares()))
        rows = [
            {
                "full_name": "Starter Tight",
                "report_status": "Doubtful",
                "gsis_id": "te1",
                "team": "KC",
                "season": 2026,
                "week": 3,
            }
        ]
        spec = Availability(rule="ruled-out", zero_doubtful=False)
        after = _by_pid(
            build_entries(
                [_line()],
                _chart(),
                target_rows=_shares(),
                injury_rows=rows,
                availability=spec,
            )
        )
        flagged = after["te1"]
        self.assertEqual(flagged.injury, "D")
        self.assertEqual(flagged.depth_rank, before["te1"].depth_rank)
        self.assertEqual(flagged.target_share, before["te1"].target_share)
        self.assertAlmostEqual(flagged.objective, before["te1"].objective, places=9)
        self.assertEqual(after["te2"].depth_rank, before["te2"].depth_rank)
        self.assertAlmostEqual(
            after["te2"].objective, before["te2"].objective, places=9
        )
        pool = sim_pool([flagged, after["te2"]], availability=spec)
        kept = next(pl for pl in pool if pl.pid == "te1")
        self.assertEqual(kept.injury, "")
        sim = simulate_games(
            pool,
            n=40,
            seed=1,
            availability_rule="ruled-out",
            zero_doubtful=False,
        )
        self.assertGreater(sim.by_pid["te1"].mean, 1.0)

    def test_legacy_matches_the_previous_seed(self) -> None:
        players, inputs = _tb_slate()
        sim = simulate_games(
            players,
            n=40,
            seed=7,
            inputs=inputs,
            availability_rule="legacy",
        )
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(set(sim.draws), set(golden))
        for pid, draws in golden.items():
            self.assertEqual(len(sim.draws[pid]), len(draws))
            for got, expected in zip(sim.draws[pid], draws):
                self.assertAlmostEqual(got, expected, places=9)
        bare = simulate_games(
            sim_pool(players, availability=Availability(rule="legacy")),
            n=40,
            seed=7,
            inputs=inputs,
        )
        for pid in golden:
            for got, expected in zip(sim.draws[pid], bare.draws[pid]):
                self.assertAlmostEqual(got, expected, places=9)


if __name__ == "__main__":
    unittest.main()
