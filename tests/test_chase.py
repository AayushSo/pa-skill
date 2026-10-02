"""Tests for chase: — the follow-up date on a waiting task. Synthetic fixtures only."""
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
import pa_widget  # noqa: E402
from widget_store import Store  # noqa: E402

ENV = {"PYTHONIOENCODING": "utf-8", "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
TODAY = "2026-09-14"  # a Monday

TASKS = """# Tasks — work
Last reviewed: 2026-09-14
- [ ] Past chase | waiting:reply from a contact | chase:2026-09-10 | id:c001
- [ ] Chase today | waiting:a reply | chase:2026-09-14 | id:c002
- [ ] Chase later | waiting:a quote | chase:2026-09-24 | id:c003
- [ ] No chase yet | waiting:a form back | id:c004
- [ ] Blocked by a task | waiting:task c009 | id:c005
- [ ] Due wins | waiting:a signature | chase:2026-09-01 | due:2026-09-16 | id:c006
- [ ] Ordinary task | due:2026-09-20 | id:c007
"""


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "work").mkdir()
        self.tasks = self.root / "work" / "tasks.md"
        self.tasks.write_text(TASKS, encoding="utf-8")
        self.cfg = {"data_root": str(self.root)}
        self.cfg_path = self.root / "cfg.json"
        self.cfg_path.write_text(json.dumps(self.cfg), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def tool(self, script, *args):
        r = subprocess.run([sys.executable, str(TOOLS / script), "--config", str(self.cfg_path), *args],
                           capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=20)
        return r.returncode, r.stdout + r.stderr

    @staticmethod
    def section(out, title):
        lines, grab = [], False
        for line in out.splitlines():
            if line.startswith(title):
                grab = True
                continue
            if grab:
                if not line.strip():
                    break
                lines.append(line)
        return "\n".join(lines)


class TestBriefing(Base):
    def test_follow_up_and_waiting_sections(self):
        _, out = self.tool("due.py", "--today", TODAY)
        follow = self.section(out, "FOLLOW UP")
        waiting = self.section(out, "WAITING ON SOMEONE")
        self.assertIn("Past chase", follow)
        self.assertIn("chase Thu 2026-09-10 — 4d ago", follow)
        self.assertIn("Chase today", follow)                     # the chase date itself counts
        self.assertIn("chase Mon 2026-09-14 — today", follow)
        self.assertNotIn("Chase later", follow)
        self.assertIn("Chase later", waiting)
        self.assertIn("chase Thu 2026-09-24", waiting)
        self.assertIn("No chase yet", waiting)
        # oldest chase date first; undated waits last
        self.assertLess(follow.index("Past chase"), follow.index("Chase today"))
        self.assertLess(waiting.index("Chase later"), waiting.index("No chase yet"))

    def test_a_due_date_wins_over_chase(self):
        _, out = self.tool("due.py", "--today", TODAY)
        self.assertIn("Due wins", self.section(out, "DUE IN NEXT"))
        self.assertNotIn("Due wins", self.section(out, "FOLLOW UP"))

    def test_unchased_summary_skips_task_chains_and_uses_chase_days(self):
        _, out = self.tool("due.py", "--today", TODAY)
        self.assertIn("Waiting with no chase date: 1 (work 1) — give each a chase: (default +14d → 2026-09-28)", out)
        self.cfg_path.write_text(json.dumps({**self.cfg, "chase_days": 7}), encoding="utf-8")
        _, out = self.tool("due.py", "--today", TODAY)
        self.assertIn("(default +7d → 2026-09-21)", out)
        self.tasks.write_text(TASKS.replace("| waiting:a form back |", "| waiting:a form back | chase:2026-09-30 |"),
                              encoding="utf-8")
        _, out = self.tool("due.py", "--today", TODAY)
        self.assertNotIn("Waiting with no chase date", out)

    def test_bad_chase_and_chase_without_waiting_warn(self):
        self.tasks.write_text(TASKS + "- [ ] Bad date | waiting:x | chase:soon | id:c010\n"
                                      "- [ ] Not waiting | chase:2026-09-01 | id:c011\n", encoding="utf-8")
        _, out = self.tool("due.py", "--today", TODAY)
        self.assertIn("bad chase date 'soon'", out)
        self.assertIn("chase: without waiting:", out)
        self.assertNotIn("Not waiting", self.section(out, "FOLLOW UP"))   # a stray chase: does nothing

    def test_area_view_and_json(self):
        _, out = self.tool("due.py", "--today", TODAY, "--area", "work")
        self.assertIn("chase Thu 2026-09-10 — 4d ago", out)
        _, out = self.tool("due.py", "--today", TODAY, "--json")
        buckets = {t["id"]: t["bucket"] for t in json.loads(out)["tasks"]}
        self.assertEqual(buckets["c001"], "chase")
        self.assertEqual(buckets["c002"], "chase")
        self.assertEqual(buckets["c003"], "waiting")
        self.assertEqual(buckets["c004"], "waiting")
        self.assertEqual(buckets["c006"], "soon")

    def test_a_chased_wait_is_not_newly_active(self):
        self.tasks.write_text("# Tasks — work\nLast reviewed: 2026-09-14\n"
                              "- [ ] Started wait | waiting:x | start:2026-09-12 | chase:2026-09-13 | id:c020\n",
                              encoding="utf-8")
        _, out = self.tool("due.py", "--today", TODAY)
        self.assertIn("Started wait", self.section(out, "FOLLOW UP"))
        self.assertNotIn("Started wait", self.section(out, "NEWLY ACTIVE"))


class TestNudgesAndSnooze(Base):
    def test_nudge_count_resets_when_the_chase_date_moves(self):
        _, out = self.tool("widgetctl.py", "nudge", "c001")
        self.assertIn("c001: nudged 1×", out)
        s = Store(self.root)
        data = s.nudges()
        self.assertEqual(data["c001"]["when"], "2026-09-10")
        data["c001"]["dates"].insert(0, "1999-12-31")
        s._write_json(s.nudges_path, data)
        _, out = self.tool("due.py", "--today", TODAY)
        self.assertIn("nudged 2×", self.section(out, "FOLLOW UP"))
        self.tasks.write_text(TASKS.replace("chase:2026-09-10", "chase:2026-09-12"), encoding="utf-8")
        _, out = self.tool("due.py", "--today", TODAY)
        self.assertNotIn("nudged", out)

    def test_snoozing_a_wait_sets_its_chase_date(self):
        s = Store(self.root)
        s.append_event({"type": "snooze", "task_id": "c004", "until": "2099-01-01"})
        s.append_event({"type": "snooze", "task_id": "c007", "until": "2026-09-18"})
        _, out = self.tool("widgetctl.py", "inbox")
        self.assertIn("waiting task: set chase:2099-01-01 (not start:)", out)
        self.assertIn("snooze until 2026-09-18 → set start:2026-09-18", out)   # ordinary tasks unchanged

    def test_widget_data_carries_the_bucket(self):
        data = pa_widget.build_data(self.root, self.cfg, Store(self.root))
        by_id = {t["id"]: t for t in data["tasks"]}
        self.assertEqual(by_id["c001"]["fields"]["chase"], "2026-09-10")
        self.assertIn(by_id["c001"]["bucket"], ("chase", "waiting"))   # depends on the real date; both are valid
        self.assertEqual(by_id["c003"]["fields"]["chase"], "2026-09-24")


class TestExport(Base):
    def test_chase_column_and_status(self):
        _, out = self.tool("export_csv.py", "--today", TODAY, "--out", "-")
        rows = {r["Task"]: r for r in csv.DictReader(io.StringIO(out.lstrip("﻿")))}
        self.assertEqual(rows["Past chase"]["Chase"], "2026-09-10")
        self.assertEqual(rows["Past chase"]["Status"], "follow up")
        self.assertEqual(rows["Chase later"]["Status"], "waiting")
        self.assertEqual(rows["Ordinary task"]["Chase"], "")


if __name__ == "__main__":
    unittest.main()
