"""FanDuel NFL upload CSV: Id:Nickname, picker order, contest ids."""

from __future__ import annotations

import csv
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime
from pathlib import Path

from nfl.players import Player
from nfl.projections import week1_score
from nfl.solver import Lineup
from nfl.upload import (
    EXPORT_DIR,
    SLOT_COUNT,
    UPLOAD_TEMPLATE,
    _display_path,
    export_lineups,
    parse_upload_id,
    stamped_export_name,
    stamped_export_path,
    upload_cell,
    upload_row,
    validate_upload,
    write_upload_csv,
)


def _pl(**kw) -> Player:
    fields = dict(
        pid=kw.get("pid", "133104-1"),
        name=kw.get("name", "Cam"),
        position="WR",
        salary=5000,
        team="DET",
        opponent="NO",
        game="NO@DET",
        fppg=None,
        injury="",
        roster_position="",
        implied_total=24.0,
        implied_opp=22.0,
        objective=kw.get("objective"),
    )
    fields.update(kw)
    if fields.get("objective") is None:
        fields["objective"] = week1_score(
            fields.get("implied_total") or 0.0,
            depth_rank=fields.get("depth_rank"),
            position=fields["position"],
            prop_fd=fields.get("prop_fd"),
            implied_opp=fields.get("implied_opp"),
        )
    allowed = Player.__dataclass_fields__
    return Player(**{k: v for k, v in fields.items() if k in allowed})


def _nine(suffix: str = "a") -> Lineup:
    burrow = "Joe Burrow" if suffix == "a" else "Jared Goff"
    burrow_id = "133104-63336" if suffix == "a" else "133104-38435"
    slots = {
        "QB": _pl(pid=burrow_id, name=burrow, position="QB", team="CIN"),
        "RB1": _pl(pid=f"133104-rb1{suffix}", name=f"RB One {suffix}", position="RB"),
        "RB2": _pl(pid=f"133104-rb2{suffix}", name=f"RB Two {suffix}", position="RB"),
        "WR1": _pl(
            pid="133104-85701",
            name="Ja'Marr Chase",
            position="WR",
            team="CIN",
        ),
        "WR2": _pl(pid=f"133104-wr2{suffix}", name=f"WR Two {suffix}", position="WR"),
        "WR3": _pl(pid=f"133104-wr3{suffix}", name=f"WR Three {suffix}", position="WR"),
        "TE": _pl(pid=f"133104-te{suffix}", name=f"TE {suffix}", position="TE"),
        "FLEX": _pl(pid=f"133104-fx{suffix}", name=f"Flex {suffix}", position="RB"),
        "DEF": _pl(
            pid="133104-12534",
            name="Tennessee Titans",
            position="D",
            team="TEN",
        ),
    }
    return Lineup(slots=slots, method="test")


class UploadCellTest(unittest.TestCase):
    def test_id_colon_nickname(self):
        pl = _pl(pid="133104-63336", name="Joe Burrow", position="QB")
        self.assertEqual(upload_cell(pl), "133104-63336:Joe Burrow")
        self.assertEqual(parse_upload_id(upload_cell(pl)), "133104-63336")


class UploadCsvTest(unittest.TestCase):
    def test_template_exists(self):
        self.assertTrue(UPLOAD_TEMPLATE.is_file())

    def test_write_and_validate(self):
        a = _nine("a")
        b = _nine("b")
        ids = {p.pid for lu in (a, b) for p in lu.slots.values()}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "upload.csv"
            write_upload_csv(path, [a, b])
            rows = validate_upload(path, ids, 2)
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(rows[0]), SLOT_COUNT)
            self.assertEqual(rows[0], upload_row(a))
            self.assertEqual(rows[1][0], "133104-38435:Jared Goff")
            self.assertEqual(rows[0][3], "133104-85701:Ja'Marr Chase")
            self.assertEqual(rows[0][7], upload_cell(a.slots["FLEX"]))
            self.assertEqual(rows[0][8], "133104-12534:Tennessee Titans")
            with path.open(newline="", encoding="utf-8") as fh:
                all_rows = list(csv.reader(fh))
            self.assertEqual(
                all_rows[0][:9],
                ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DEF"],
            )
            self.assertIn("Instructions", all_rows[0])
            self.assertIn("Create a lineup", all_rows[1][-1])

    def test_rejects_id_not_in_contest(self):
        lu = _nine("a")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "upload.csv"
            write_upload_csv(path, [lu])
            with self.assertRaises(ValueError):
                validate_upload(path, {"not-an-id"}, 1)


class StampedExportTest(unittest.TestCase):
    _STAMP = datetime(2026, 9, 6, 13, 27, 48)
    _NAME = "nfl-133104-ceiling-20260906-132748.csv"

    def test_name_local_sortable(self):
        self.assertEqual(
            stamped_export_name("133104", "ceiling", self._STAMP),
            self._NAME,
        )
        self.assertNotIn(" ", self._NAME)

    def test_default_dir_is_nfl_export(self):
        self.assertEqual(EXPORT_DIR, Path(__file__).resolve().parent / "export")
        dest = stamped_export_path("133104", "ceiling", when=self._STAMP)
        self.assertEqual(dest, EXPORT_DIR / self._NAME)
        self.assertEqual(_display_path(dest), f"nfl/export/{self._NAME}")

    def test_path_under_export_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "export"
            got = stamped_export_path(
                "133104",
                "ceiling",
                when=self._STAMP,
                export_dir=folder,
            )
            self.assertEqual(got, folder / self._NAME)

    def test_multi_writes_stamped_csv(self):
        a, b = _nine("a"), _nine("b")
        ids = {p.pid for lu in (a, b) for p in lu.slots.values()}
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "export"
            buf = io.StringIO()
            with redirect_stderr(buf):
                written = export_lineups(
                    [a, b],
                    n_lineups=150,
                    contest="133104",
                    objective="ceiling",
                    when=self._STAMP,
                    export_dir=folder,
                    contest_ids=ids,
                )
            dest = written["export"]
            self.assertEqual(dest.name, self._NAME)
            self.assertTrue(dest.is_file())
            self.assertEqual(len(validate_upload(dest, ids, 2)), 2)
            self.assertEqual(
                buf.getvalue().strip(),
                f"export 2 rows → {dest}",
            )
            self.assertNotIn("upload", written)

    def test_single_skips_auto_export(self):
        lu = _nine("a")
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "export"
            buf = io.StringIO()
            with redirect_stderr(buf):
                written = export_lineups(
                    [lu],
                    n_lineups=1,
                    contest="133104",
                    objective="mean",
                    when=self._STAMP,
                    export_dir=folder,
                )
            self.assertEqual(written, {})
            self.assertFalse(folder.exists())
            self.assertEqual(buf.getvalue(), "")

    def test_single_upload_only(self):
        lu = _nine("a")
        ids = {p.pid for p in lu.slots.values()}
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "export"
            upload = Path(tmp) / "custom.csv"
            buf = io.StringIO()
            with redirect_stderr(buf):
                written = export_lineups(
                    [lu],
                    n_lineups=1,
                    contest="133104",
                    objective="mean",
                    upload=upload,
                    when=self._STAMP,
                    export_dir=folder,
                    contest_ids=ids,
                )
            self.assertNotIn("export", written)
            self.assertEqual(written["upload"], upload)
            self.assertTrue(upload.is_file())
            self.assertFalse((folder / stamped_export_name("133104", "mean", self._STAMP)).exists())
            self.assertIn("upload 1 rows →", buf.getvalue())

    def test_multi_writes_export_and_upload(self):
        a, b = _nine("a"), _nine("b")
        ids = {p.pid for lu in (a, b) for p in lu.slots.values()}
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "export"
            upload = Path(tmp) / "also.csv"
            buf = io.StringIO()
            with redirect_stderr(buf):
                written = export_lineups(
                    [a, b],
                    n_lineups=2,
                    contest="133104",
                    objective="ceiling",
                    upload=upload,
                    when=self._STAMP,
                    export_dir=folder,
                    contest_ids=ids,
                )
            self.assertEqual(written["export"].name, self._NAME)
            self.assertEqual(written["upload"], upload)
            self.assertTrue(written["export"].is_file())
            self.assertTrue(upload.is_file())
            self.assertEqual(len(validate_upload(upload, ids, 2)), 2)
            log = buf.getvalue()
            self.assertIn(f"export 2 rows → {written['export']}", log)
            self.assertIn("upload 2 rows →", log)

    def test_requested_multi_exports_even_one_nine(self):
        lu = _nine("a")
        ids = {p.pid for p in lu.slots.values()}
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "export"
            buf = io.StringIO()
            with redirect_stderr(buf):
                written = export_lineups(
                    [lu],
                    n_lineups=20,
                    contest="133104",
                    objective="floor",
                    when=self._STAMP,
                    export_dir=folder,
                    contest_ids=ids,
                )
            self.assertEqual(
                written["export"].name,
                "nfl-133104-floor-20260906-132748.csv",
            )


if __name__ == "__main__":
    unittest.main()
