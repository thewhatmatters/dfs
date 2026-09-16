"""Week-1 score and slate projection board."""

from __future__ import annotations

import unittest

from ncaaf.players import Player
from ncaaf.projections import (
    FLOOR_SALARY,
    PROP_FACTOR_HI,
    PROP_FACTOR_LO,
    VALUE_EPS,
    board_row,
    projection_board,
    week1_score,
)


def _pl(**kw) -> Player:
    fields = dict(
        pid=kw.get("name", "p"),
        name="Cam",
        position="WR",
        salary=8000,
        team="MISS",
        opponent="LOU",
        game="LOU@MISS",
        fppg=None,
        injury="",
        roster_position="",
        implied_total=32.5,
        depth_rank=1,
        spread=-7.0,
        prop_fd=None,
        script_applied=True,
    )
    fields.update(kw)
    obj = week1_score(
        fields["implied_total"] or 0.0,
        fields["salary"],
        depth_rank=fields.get("depth_rank"),
        position=fields["position"],
        prop_fd=fields.get("prop_fd"),
        team_spread=fields.get("spread"),
        apply_script=fields.get("script_applied", True),
    )
    fields["objective"] = obj
    allowed = Player.__dataclass_fields__
    return Player(**{k: v for k, v in fields.items() if k in allowed})


class Week1ScoreTest(unittest.TestCase):
    def test_prop_is_clamped_tilt_not_override(self):
        leftover = VALUE_EPS * (FLOOR_SALARY / 10000)
        core = week1_score(40.0, 10000, 1, "QB", team_spread=-28.0) - leftover
        hi = week1_score(40.0, 10000, 1, "QB", prop_fd=100.0, team_spread=-28.0)
        lo = week1_score(40.0, 10000, 1, "QB", prop_fd=0.1, team_spread=-28.0)
        self.assertAlmostEqual(hi, core * PROP_FACTOR_HI + leftover, places=6)
        self.assertAlmostEqual(lo, core * PROP_FACTOR_LO + leftover, places=6)
        a = week1_score(40.0, 10000, 1, "QB", prop_fd=20.0, team_spread=-28.0)
        b = week1_score(40.0, 10000, 1, "QB", prop_fd=20.0, team_spread=None)
        self.assertNotAlmostEqual(a, b)


class BoardTest(unittest.TestCase):
    def test_board_rows_match_week1_score(self):
        model = _pl(name="Walk On WR", position="WR", depth_rank=1, salary=8000)
        prop = _pl(
            name="Trinidad Chambliss",
            position="QB",
            depth_rank=1,
            salary=11600,
            prop_fd=23.5,
        )
        rows = {r["player"]: r for r in projection_board([model, prop])}
        want_model = week1_score(
            32.5, 8000, 1, "WR", team_spread=-7.0
        )
        want_prop = week1_score(
            32.5, 11600, 1, "QB", prop_fd=23.5, team_spread=-7.0
        )
        self.assertAlmostEqual(rows["Walk On WR"]["proj"], want_model, places=4)
        self.assertAlmostEqual(
            rows["Trinidad Chambliss"]["proj"], want_prop, places=4
        )
        self.assertEqual(rows["Walk On WR"]["source"], "model")
        self.assertEqual(rows["Trinidad Chambliss"]["source"], "props")
        self.assertEqual(rows["Walk On WR"]["depth"], "!")
        self.assertEqual(board_row(model)["proj"], rows["Walk On WR"]["proj"])

    def test_default_is_d1_d2_and_props(self):
        d1 = _pl(name="Starter", position="RB", depth_rank=1)
        d2 = _pl(name="Backup", position="RB", depth_rank=2)
        d3 = _pl(name="Third", position="RB", depth_rank=3)
        unlisted = _pl(name="Walk On", position="WR", depth_rank=None)
        priced = _pl(
            name="Priced d3",
            position="WR",
            depth_rank=3,
            prop_fd=8.4,
        )
        names = {r["player"] for r in projection_board(
            [d1, d2, d3, unlisted, priced], mode="default"
        )}
        self.assertEqual(names, {"Starter", "Backup", "Priced d3"})
        all_names = {r["player"] for r in projection_board(
            [d1, d2, d3, unlisted, priced], mode="all"
        )}
        self.assertEqual(
            all_names, {"Starter", "Backup", "Third", "Walk On", "Priced d3"}
        )
        self.assertEqual(
            projection_board([unlisted], mode="all")[0]["depth"], "—"
        )

    def test_sim_columns_optional(self):
        from ncaaf.sim import SimStats

        pl = _pl(name="Walk On WR", position="WR", depth_rank=1)
        bare = projection_board([pl])[0]
        self.assertNotIn("mean", bare)
        st = SimStats(
            mean=7.1, p10=4.0, p50=7.0, p90=10.5, n=10000, source="model"
        )
        row = projection_board([pl], sim_by_pid={pl.pid: st})[0]
        self.assertAlmostEqual(row["mean"], 7.1)
        self.assertAlmostEqual(row["p10"], 4.0)
        self.assertAlmostEqual(row["p50"], 7.0)
        self.assertAlmostEqual(row["p90"], 10.5)
        self.assertAlmostEqual(row["proj"], bare["proj"])


if __name__ == "__main__":
    unittest.main()
