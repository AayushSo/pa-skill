"""Tests for context.md (short-lived cross-area notes) and nudge counts. Synthetic fixtures only."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import due  # noqa: E402
import pa_widget  # noqa: E402
from widget_store import Store  # noqa: E402

ENV = {"PYTHONIOENCODING": "utf-8", "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
TODAY = date.today().isoformat()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "work").mkdir()
        (self.root / "work" / "tasks.md").write_text(
            "# Tasks — work\nLast reviewed: 2099-01-01\n"
            "- [ ] Overdue thing | due:2000-01-01 | id:w001\n"
            "- [ ] Later thing | due:2099-01-01 | id:w002\n", encoding="utf-8")
        self.cfg = {"data_root": str(self.root)}
        self.cfg_path = self.root / "cfg.json"
        self.cfg_path.write_text(json.dumps(self.cfg), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def tool(self, script, *args):
        r = subprocess.run([sys.executable, str(TOOLS / script), "--config", str(self.cfg_path), *args],
                           capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=20)
        return r.returncode, r.stdout + r.stderr


class TestContext(Base):
    def write_ctx(self, text):
        (self.root / "context.md").write_text(text, encoding="utf-8")

    def test_active_upcoming_expired_and_malformed(self):
        self.write_ctx("# Context\n\nFree prose is fine.\n"
                       "- 2026-09-14 → 2026-09-20 | away, no laptop; freed hours → E2\n"
                       "- 2026-09-01 -> 2026-09-10 | old trip\n"
                       "- 2026-09-25 to 2026-09-27 | conference\n"
                       "- 2026-09-14 | no end date\n"
                       "- 2026-09-20 → 2026-09-14 | end before start\n")
        ctx = due.load_context(self.root, {}, date(2026, 9, 16))
        self.assertEqual([n for _, _, n in ctx.active], ["away, no laptop; freed hours → E2"])
        self.assertEqual([n for _, _, n in ctx.expired], ["old trip"])
        self.assertEqual([n for _, _, n in ctx.upcoming], ["conference"])
        self.assertEqual(len(ctx.warnings), 2)

    def test_briefing_shows_context_and_flags_expired(self):
        self.write_ctx("- 2026-09-14 → 2026-09-20 | away, no laptop\n- 2026-09-01 → 2026-09-10 | old trip\n")
        _, out = self.tool("due.py", "--today", "2026-09-16")
        self.assertIn("IMPORTANT (applies across areas", out)
        self.assertIn("until 2026-09-20 Sun  away, no laptop", out)
        self.assertIn("! context.md: expired 2026-09-10 — remove: old trip", out)
        data = json.loads(self.tool("due.py", "--json", "--today", "2026-09-16")[1])
        self.assertEqual(data["context"][0]["note"], "away, no laptop")

    def test_no_context_file_changes_nothing(self):
        _, out = self.tool("due.py", "--today", "2026-09-16")
        self.assertNotIn("CONTEXT", out)
        self.assertNotIn("context.md", out)

    def test_widget_data_includes_context(self):
        self.write_ctx(f"- 2000-01-01 → 2099-01-01 | always on\n")
        data = pa_widget.build_data(self.root, self.cfg, Store(self.root))
        self.assertEqual(data["context"][0]["note"], "always on")


class TestNudges(Base):
    def test_count_per_day_reset_on_redate_and_display(self):
        _, out = self.tool("widgetctl.py", "nudge", "w001", "nope")
        self.assertIn("Not open task ids (skipped): nope", out)
        self.assertIn("w001: nudged 1×", out)
        _, out = self.tool("widgetctl.py", "nudge", "w001")  # same day: still 1
        self.assertIn("w001: nudged 1×", out)
        # simulate an earlier day's nudge
        s = Store(self.root)
        data = s.nudges(); data["w001"]["dates"].insert(0, "1999-12-31"); s._write_json(s.nudges_path, data)
        _, out = self.tool("due.py")
        self.assertIn("nudged 2× — ask to re-date, drop or keep", out)
        # re-dating the task resets the count
        path = self.root / "work" / "tasks.md"
        path.write_text(path.read_text(encoding="utf-8").replace("due:2000-01-01", "due:2000-01-05"), encoding="utf-8")
        _, out = self.tool("due.py")
        self.assertNotIn("nudged", out)
        _, out = self.tool("widgetctl.py", "nudge", "w001")
        self.assertIn("w001: nudged 1×", out)

    def test_widget_and_json_carry_counts_and_prune(self):
        self.tool("widgetctl.py", "nudge", "w001", "w002")
        data = pa_widget.build_data(self.root, self.cfg, Store(self.root))
        counts = {t["id"]: t["nudged"] for t in data["tasks"]}
        self.assertEqual(counts, {"w001": 1, "w002": 1})
        path = self.root / "work" / "tasks.md"
        path.write_text(path.read_text(encoding="utf-8").replace("- [ ] Later thing | due:2099-01-01 | id:w002\n", ""), encoding="utf-8")
        _, out = self.tool("widgetctl.py", "prune")
        self.assertIn("1 nudge record(s)", out)
        self.assertNotIn("w002", Store(self.root).nudges())

    def test_no_nudges_file_changes_nothing(self):
        _, out = self.tool("due.py")
        self.assertNotIn("nudged", out)


class TestYields(Base):
    """A task whose point is the information cannot be closed without saying what it yielded."""

    def setUp(self):
        super().setUp()
        path = self.root / "work" / "tasks.md"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "- [ ] Overdue thing | due:2000-01-01 | id:w001",
            "- [ ] Log the week and decide | due:2000-01-01 | yields:HR trend, compliance, one change | id:w001"), encoding="utf-8")

    def test_field_parsed_and_shown(self):
        _, out = self.tool("due.py")
        self.assertIn("owes: HR trend, compliance, one change", out)
        self.assertNotIn("unknown field", out)
        data = json.loads(self.tool("due.py", "--json")[1])
        t = next(x for x in data["tasks"] if x["id"] == "w001")
        self.assertEqual(t["fields"]["yields"], "HR trend, compliance, one change")

    def test_long_yields_truncated_in_briefing(self):
        path = self.root / "work" / "tasks.md"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "yields:HR trend, compliance, one change", "yields:" + "x" * 90), encoding="utf-8")
        _, out = self.tool("due.py")
        self.assertIn("owes: " + "x" * 59 + "…", out)

    def test_ack_refuses_without_outcome_then_accepts(self):
        s = Store(self.root)
        e = s.append_event({"type": "done", "task_id": "w001", "yields": "HR trend, compliance, one change"})
        other = s.append_event({"type": "done", "task_id": "w002"})
        code, out = self.tool("widgetctl.py", "ack", e["eid"])
        self.assertEqual(code, 1)
        self.assertIn("owe an outcome", out)
        self.assertIn("HR trend, compliance, one change", out)
        self.assertNotIn(e["eid"], s.processed_ids())
        # a task without yields: acks as before
        code, out = self.tool("widgetctl.py", "ack", other["eid"])
        self.assertEqual((code, other["eid"] in Store(self.root).processed_ids()), (0, True))
        # --all is blocked by the same guard
        code, out = self.tool("widgetctl.py", "ack", "--all")
        self.assertEqual(code, 1)
        code, out = self.tool("widgetctl.py", "ack", e["eid"], "--outcome", "HR fell 8 bpm; keep the ceiling; add 5 min")
        self.assertEqual(code, 0)
        self.assertIn("Outcome recorded", out)
        self.assertIn(e["eid"], Store(self.root).processed_ids())

    def test_inbox_flags_the_debt(self):
        Store(self.root).append_event({"type": "done", "task_id": "w001", "yields": "HR trend, compliance, one change"})
        _, out = self.tool("widgetctl.py", "inbox")
        self.assertIn("OWES AN OUTCOME: HR trend, compliance, one change", out)

    def test_yields_recorded_on_the_event_survives_the_task_being_removed(self):
        s = Store(self.root)
        e = s.append_event({"type": "done", "task_id": "w001", "yields": "HR trend, compliance, one change"})
        path = self.root / "work" / "tasks.md"   # /pa already moved the line to done.md
        path.write_text(chr(10).join(["# Tasks — work", "Last reviewed: 2099-01-01", ""]), encoding="utf-8")
        code, out = self.tool("widgetctl.py", "ack", e["eid"])
        self.assertEqual(code, 1)
        self.assertIn("owes:", out)


if __name__ == "__main__":
    unittest.main()
