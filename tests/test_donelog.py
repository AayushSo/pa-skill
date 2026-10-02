"""Tests for tools/donelog.py and the /api/week route. Synthetic fixtures only."""
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import donelog  # noqa: E402
import pa_widget  # noqa: E402
from widget_store import Store  # noqa: E402

DONE = """# Done — work

Append-only. Newest at the bottom.

## September
- 2026-09-07 | Send the quarterly note | Sent; finance replied same day | added:2026-09-01 | id:aa11
- 2026-09-08 | Tidy the backlog
- ~2026-09-09 | Read the two papers | Both say the same thing: the gain is in the loader | id:bb22
- ≤2026-09-10 | Old thing from before the log | added:2026-08-30
- 2026-09-11 | Chase the vendor | dropped — they never replied, not worth another week | added:2026-09-02
- 2026-09-12 | Odd outcome with a bare | pipe in it | still counts
- not a date | something the agent typed wrong
- 2026-09-13 | Bad added date | fine otherwise | added:soon
"""


class TestParse(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "work").mkdir()
        (self.root / "work" / "done.md").write_text(DONE, encoding="utf-8")
        self.warnings = []
        self.entries = donelog.load_all(self.root, {"work": "day job"}, self.warnings)
        self.by_day = {e.day.isoformat(): e for e in self.entries}

    def tearDown(self):
        self._tmp.cleanup()

    def test_counts_and_area_name(self):
        self.assertEqual(len(self.entries), 7)                       # the "not a date" line is not one
        self.assertEqual(self.entries[0].day, date(2026, 9, 13))     # newest first
        self.assertEqual(self.entries[0].area, "day job")            # display name from the map
        self.assertEqual(self.entries[0].folder, "work")             # folder, for the area filter

    def test_outcome_added_and_took(self):
        e = self.by_day["2026-09-07"]
        self.assertEqual((e.text, e.outcome), ("Send the quarterly note", "Sent; finance replied same day"))
        self.assertEqual((e.added, e.task_id, e.took), (date(2026, 9, 1), "aa11", 6))

    def test_entry_without_outcome_or_fields(self):
        e = self.by_day["2026-09-08"]
        self.assertEqual((e.text, e.outcome, e.added, e.task_id, e.took), ("Tidy the backlog", "", None, None, None))

    def test_approximate_dates_are_kept_and_flagged(self):
        self.assertTrue(self.by_day["2026-09-09"].approx)            # ~
        self.assertTrue(self.by_day["2026-09-10"].approx)            # ≤
        self.assertFalse(self.by_day["2026-09-07"].approx)

    def test_dropped_is_detected_from_the_outcome(self):
        self.assertTrue(self.by_day["2026-09-11"].dropped)
        self.assertFalse(self.by_day["2026-09-07"].dropped)
        self.assertEqual(sum(1 for e in self.entries if e.dropped), 1)

    def test_bare_pipe_in_text_spills_into_the_outcome_but_loses_nothing(self):
        # In done.md " | " IS the column separator (unlike tasks.md, where a field name must follow),
        # so a bare pipe in the text reads as an early column break. Nothing is dropped; /pa avoids
        # writing bare pipes into done.md lines.
        e = self.by_day["2026-09-12"]
        self.assertEqual(e.text, "Odd outcome with a bare")
        self.assertEqual(e.outcome, "pipe in it | still counts")

    def test_warnings_name_the_file_and_line(self):
        bad_line = DONE.splitlines().index("- not a date | something the agent typed wrong") + 1
        self.assertTrue(any(f"done.md:{bad_line}" in w and "no date" in w for w in self.warnings), self.warnings)
        self.assertTrue(any("bad added date 'soon'" in w for w in self.warnings), self.warnings)
        self.assertIsNone(self.by_day["2026-09-13"].added)           # bad date dropped, entry kept

    def test_between_is_inclusive(self):
        got = donelog.between(self.entries, date(2026, 9, 8), date(2026, 9, 10))
        self.assertEqual([e.day.isoformat() for e in got], ["2026-09-10", "2026-09-09", "2026-09-08"])
        self.assertEqual(donelog.between(self.entries, date(2026, 1, 1), date(2026, 1, 7)), [])

    def test_missing_done_file_is_not_an_error(self):
        (self.root / "empty-area").mkdir()
        warns = []
        self.assertEqual(len(donelog.load_all(self.root, {}, warns)), 7)


class TestWeekRoute(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "work").mkdir()
        (self.root / "work" / "tasks.md").write_text(
            "# Tasks — work\nLast reviewed: 2099-01-01\n\n- [ ] A task | due:2099-01-01 | id:zz99\n", encoding="utf-8")
        (self.root / "work" / "about.md").write_text("# work\n", encoding="utf-8")
        (self.root / "work" / "done.md").write_text(DONE, encoding="utf-8")
        self.cfg = {"data_root": str(self.root)}
        self.srv = pa_widget.make_server(self.root, self.cfg, 0)
        self.port = self.srv.server_address[1]
        self.key = Store(self.root).key()
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); self._tmp.cleanup()

    def get(self, path, key=True):
        headers = {"X-PA-Key": self.key} if key else {}
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_needs_the_key(self):
        self.assertEqual(self.get("/api/week?start=2026-09-07", key=False)[0], 403)

    def test_returns_one_calendar_week_of_finished_work(self):
        code, d = self.get("/api/week?start=2026-09-07")
        self.assertEqual((code, d["start"], d["end"]), (200, "2026-09-07", "2026-09-13"))
        days = [e["date"] for e in d["done"]]
        self.assertTrue(all("2026-09-07" <= x <= "2026-09-13" for x in days), days)
        first = [e for e in d["done"] if e["date"] == "2026-09-07"][0]
        self.assertEqual((first["area"], first["took"], first["task_id"]), ("work", 6, "aa11"))
        self.assertTrue(any(e["dropped"] for e in d["done"]))
        self.assertEqual(d["days"], [])                              # no schedule configured
        self.assertTrue(any("no date" in w for w in d["warnings"]))

    def test_bad_or_missing_start_falls_back_to_this_week(self):
        for q in ("", "?start=", "?start=not-a-date"):
            code, d = self.get(f"/api/week{q}")
            self.assertEqual(code, 200)
            start = date.fromisoformat(d["start"])
            self.assertEqual(start.weekday(), 0)                     # a Monday
            self.assertLessEqual(abs((date.today() - start).days), 6)
            self.assertEqual(d["today"], date.today().isoformat())


if __name__ == "__main__":
    unittest.main()
