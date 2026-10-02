"""Tests for tools/export_csv.py: one row per task, statuses, filters, Excel-readable encoding. Synthetic only."""
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import snapshot as S  # noqa: E402

ENV = {"PYTHONIOENCODING": "utf-8", "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
NL = chr(10)
TASKS = NL.join([
    "# Tasks — home",
    "Last reviewed: 2026-09-14",
    "- [ ] Overdue thing | due:2026-09-10 | id:aaa1",
    "- [ ] Today thing | due:2026-09-14 | ref:notes.md | id:aaa2",
    "- [ ] Blocked thing | waiting:a reply | id:aaa3",
    "- [ ] Later thing | due:2026-12-01 | id:aaa4",
    "- [ ] Hidden until October | start:2026-10-01 | due:2026-10-05 | id:aaa5",
    "- [?] Unclear thing | id:aaa6",
    "- [ ] Weekly import | every:mon | next:2026-09-21 | yields:rows added | added:2026-09-01 | owner:assistant | id:aaa7",
    "- [x] Left behind | id:aaa8",
    "",
])
DONE = NL.join([
    "# Done — home",
    "- 2026-09-05 | Finished thing | went fine | added:2026-09-01 | id:bbb1",
    "- 2026-09-06 | Abandoned thing | dropped — not worth it | owner:assistant | id:bbb2",
    "- ≤2026-09-07 | Vague date thing | ok",
    "",
])
OTHER = NL.join(["# Tasks — work", "Last reviewed: 2026-09-14", "- [ ] Work thing | due:2026-09-15 | id:ccc1", ""])


class TestExport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "home").mkdir()
        (self.root / "home" / "tasks.md").write_text(TASKS, encoding="utf-8")
        (self.root / "home" / "done.md").write_text(DONE, encoding="utf-8")
        (self.root / "work").mkdir()
        (self.root / "work" / "tasks.md").write_text(OTHER, encoding="utf-8")
        self.cfg_path = self.root / "cfg.json"
        self.cfg_path.write_text(json.dumps({"data_root": str(self.root)}), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def run_tool(self, *args):
        r = subprocess.run([sys.executable, str(TOOLS / "export_csv.py"), "--config", str(self.cfg_path),
                            "--today", "2026-09-14", *args],
                           capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=30)
        return r.returncode, r.stdout + r.stderr

    def rows(self, *args):
        code, out = self.run_tool("--out", "-", *args)
        self.assertEqual(code, 0, out)
        table = list(csv.reader(io.StringIO(out)))
        return table[0], [dict(zip(table[0], r)) for r in table[1:]]

    def test_every_task_appears_once_with_its_status(self):
        head, rows = self.rows()
        self.assertEqual(head[:5], ["Area", "Status", "Title", "Task", "Due"])
        by_task = {r["Task"]: r for r in rows}
        self.assertEqual(len(rows), 11)                       # 8 open (not the [x] one) + 3 finished
        self.assertEqual(by_task["Overdue thing"]["Status"], "overdue")
        self.assertEqual(by_task["Today thing"]["Status"], "due today")
        self.assertEqual(by_task["Blocked thing"]["Status"], "waiting")
        self.assertEqual(by_task["Later thing"]["Status"], "later")
        self.assertEqual(by_task["Hidden until October"]["Status"], "not started")
        self.assertEqual(by_task["Unclear thing"]["Status"], "status unknown")
        self.assertEqual(by_task["Weekly import"]["Status"], "due soon")
        self.assertEqual(by_task["Finished thing"]["Status"], "done")
        self.assertEqual(by_task["Abandoned thing"]["Status"], "dropped")
        self.assertNotIn("Left behind", by_task)              # [x] still in tasks.md is not a task any more

    def test_fields_land_in_their_columns(self):
        _, rows = self.rows()
        r = {x["Task"]: x for x in rows}
        self.assertEqual(r["Weekly import"]["Repeats"], "mon")
        self.assertEqual(r["Weekly import"]["Next"], "2026-09-21")
        self.assertEqual(r["Weekly import"]["Owner"], "assistant")
        self.assertEqual(r["Weekly import"]["Owes"], "rows added")
        self.assertEqual(r["Weekly import"]["Days"], "13")     # written 9/1, still open on 9/14
        self.assertEqual(r["Today thing"]["Details in"], "notes.md")
        self.assertEqual(r["Today thing"]["Where"], "home/tasks.md:4")
        self.assertEqual(r["Blocked thing"]["Waiting on"], "a reply")
        self.assertEqual(r["Finished thing"]["Closed"], "2026-09-05")
        self.assertEqual(r["Finished thing"]["Days"], "4")     # written 9/1, closed 9/5
        self.assertEqual(r["Finished thing"]["Outcome"], "went fine")
        self.assertEqual(r["Abandoned thing"]["Owner"], "assistant")
        self.assertTrue(r["Vague date thing"]["Closed"].startswith("≈ 2026-09-07"))
        self.assertEqual(r["Vague date thing"]["Days"], "")     # no added date to measure from

    def test_open_tasks_first_then_finished_by_date(self):
        _, rows = self.rows()
        self.assertEqual([r["Status"] for r in rows][-3:], ["done", "dropped", "done"])
        self.assertEqual([r["Area"] for r in rows[:8]], ["home"] * 7 + ["work"])
        opens = [r["Task"] for r in rows if r["Area"] == "home" and r["Status"] not in ("done", "dropped")]
        # dated first in date order — a recurring task sorts by its next occurrence — then the undated ones
        self.assertEqual(opens, ["Overdue thing", "Today thing", "Weekly import", "Hidden until October",
                                 "Later thing", "Blocked thing", "Unclear thing"])

    def test_filters(self):
        _, rows = self.rows("--open")
        self.assertTrue(all(r["Status"] not in ("done", "dropped") for r in rows))
        _, rows = self.rows("--done")
        self.assertTrue(all(r["Status"] in ("done", "dropped") for r in rows))
        _, rows = self.rows("--area", "work")
        self.assertEqual([r["Task"] for r in rows], ["Work thing"])
        _, rows = self.rows("--done", "--since", "2026-09-06")
        self.assertEqual([r["Task"] for r in rows], ["Abandoned thing", "Vague date thing"])

    def test_file_output_is_excel_ready_and_not_snapshotted(self):
        code, out = self.run_tool()
        self.assertEqual(code, 0, out)
        path = self.root / "_exports" / "tasks-2026-09-14.csv"
        self.assertIn("11 row(s) — 8 open, 3 finished", out)
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))      # BOM: Excel reads it as UTF-8
        self.assertIn("≈ 2026-09-07".encode("utf-8"), raw)   # an inexact date keeps its marker
        self.assertEqual(raw.count(b"\r\n"), 12)              # header + 11 rows, CRLF as Excel expects
        names = [n for _, n in S.sources(self.root, {"data_root": str(self.root)})]
        self.assertFalse([n for n in names if n.startswith("_exports")], names)

    def test_custom_out_path(self):
        target = self.root / "sub" / "my.csv"
        code, out = self.run_tool("--out", str(target))
        self.assertEqual(code, 0, out)
        self.assertTrue(target.is_file())


if __name__ == "__main__":
    unittest.main()
