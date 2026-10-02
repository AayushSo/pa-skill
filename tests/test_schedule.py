"""Tests for tools/schedule.py, schedulectl.py, widgetctl plan and the at: field. Synthetic fixtures only."""
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
import schedule as sch  # noqa: E402
from widget_store import Store  # noqa: E402

ENV = {"PYTHONIOENCODING": "utf-8", "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}

SCHED = {
    "categories": {
        "job": {"label": "Job", "color": "#777777", "fixed": True},
        "deep": {"label": "Deep work", "color": "#b7791f"},
        "sales": {"label": "Sales", "color": "#7b57b0", "areas": ["crm"]},
        "home": {"label": "Home", "color": "#23857e", "fixed": True},
        "holiday": {"label": "Holiday", "color": "#c0562f"},
    },
    "week_kinds": {"cycle": ["busy", "quiet"], "anchor": "2026-09-14", "anchor_kind": "quiet",
                   "overrides": {"2026-10-05": "quiet"}},
    "phases": [{"from": "2026-10-19", "until": "2026-10-30", "label": "Crunch", "note": "deep → review"}],
    "blocks": [
        {"id": "job-am", "days": ["mon", "tue", "wed", "thu"], "start": "08:00", "end": "12:00", "title": "Job", "category": "job"},
        {"id": "deep-tue", "days": ["tue"], "start": "17:00", "end": "19:00", "title": "Deep work", "category": "deep", "until": "2026-10-18"},
        {"id": "review-tue", "days": ["tue"], "start": "17:00", "end": "19:00", "title": "Review", "category": "deep", "from": "2026-10-19", "until": "2026-10-30"},
        {"id": "sales-thu", "days": ["thu"], "start": "20:00", "end": "22:00", "title": "Sales calls", "category": "sales"},
        {"id": "cook-mon", "days": ["mon"], "start": "18:00", "end": "19:00", "title": "Cook", "category": "home", "week_kind": "busy"},
    ],
    "events": [
        {"id": "holiday", "date": "2026-11-26", "title": "Holiday", "category": "holiday", "all_day": True, "cancels": ["*"]},
        {"id": "dentist", "date": "2026-09-15", "start": "11:00", "end": "11:30", "title": "Dentist", "category": "home"},
    ],
}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "crm").mkdir()
        (self.root / "crm" / "tasks.md").write_text(
            "# Tasks — crm\nLast reviewed: 2026-09-14\n"
            "- [ ] Call the lead | due:2026-09-15 | id:crm1\n"
            "- [ ] Weekly sync | every:thu | next:2026-09-17 | at:16:00 | id:crm2\n"
            "- [ ] Bad time | due:2026-09-16 | at:25:00 | id:crm3\n", encoding="utf-8")
        (self.root / "plan").mkdir()
        (self.root / "plan" / "schedule.json").write_text(json.dumps(SCHED), encoding="utf-8")
        (self.root / "plan" / "week.md").write_text("# Week\n\n<!-- schedule:begin -->\nold\n<!-- schedule:end -->\n\nkept\n", encoding="utf-8")
        self.cfg = {"data_root": str(self.root), "schedule_file": "plan/schedule.json", "schedule_doc": "plan/week.md"}
        self.cfg_path = self.root / "cfg.json"
        self.cfg_path.write_text(json.dumps(self.cfg), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def tool(self, script, *args):
        r = subprocess.run([sys.executable, str(TOOLS / script), "--config", str(self.cfg_path), *args],
                           capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=20)
        return r.returncode, r.stdout + r.stderr


class TestModel(Base):
    def test_validate_clean_and_broken(self):
        self.assertEqual(sch.validate(SCHED), [])
        broken = json.loads(json.dumps(SCHED))
        broken["blocks"].append({"id": "job-am", "days": ["funday"], "start": "9:00", "end": "08:00", "title": "", "category": "nope"})
        problems = "\n".join(sch.validate(broken))
        for expected in ("duplicate id", "missing title", "unknown category", "bad start", "days must be"):
            self.assertIn(expected, problems)

    def test_week_kinds_alternate_with_overrides(self):
        self.assertEqual(sch.week_kind(SCHED, date(2026, 9, 16)), "quiet")   # anchor week
        self.assertEqual(sch.week_kind(SCHED, date(2026, 9, 21)), "busy")
        self.assertEqual(sch.week_kind(SCHED, date(2026, 9, 28)), "quiet")
        self.assertEqual(sch.week_kind(SCHED, date(2026, 10, 5)), "quiet")   # override (cycle would say busy)
        self.assertEqual(sch.week_kind(SCHED, date(2026, 10, 12)), "quiet")  # cycle continues from anchor, not override

    def test_day_resolution(self):
        mon_quiet = sch.day(SCHED, date(2026, 9, 14))
        self.assertEqual([b["id"] for b in mon_quiet["blocks"]], ["job-am"])
        mon_busy = sch.day(SCHED, date(2026, 9, 21))
        self.assertIn("cook-mon", [b["id"] for b in mon_busy["blocks"]])
        tue = sch.day(SCHED, date(2026, 9, 15))
        self.assertEqual([b["id"] for b in tue["blocks"]], ["job-am", "dentist", "deep-tue"])
        self.assertTrue(tue["blocks"][0]["fixed"])
        tue_crunch = sch.day(SCHED, date(2026, 10, 20))
        self.assertIn("review-tue", [b["id"] for b in tue_crunch["blocks"]])
        self.assertNotIn("deep-tue", [b["id"] for b in tue_crunch["blocks"]])
        self.assertEqual(tue_crunch["phases"][0]["label"], "Crunch")
        self.assertNotIn("dentist", [b["id"] for b in sch.day(SCHED, date(2026, 9, 22))["blocks"]])  # one day only
        holiday = sch.day(SCHED, date(2026, 11, 26))
        self.assertEqual(holiday["blocks"], [])
        self.assertEqual(holiday["all_day"][0]["title"], "Holiday")

    def test_overlaps_and_task_clashes(self):
        s = json.loads(json.dumps(SCHED))
        s["events"].append({"id": "clash", "date": "2026-09-15", "start": "11:15", "end": "12:30", "title": "Meeting", "category": "home"})
        tasks = [t for a in due.load_areas(self.root) for t in a.tasks]
        out = "\n".join(sch.clashes(s, tasks, date(2026, 9, 14), 7))
        self.assertIn("'Dentist' 11:00–11:30 overlaps 'Meeting'", out)
        self.assertIn("'Job' 08:00–12:00 overlaps 'Meeting'", out)
        self.assertIn("'Call the lead' is due 2026-09-15 but the next Sales block is 2026-09-17 (Thu)", out)

    def test_short_names(self):
        self.assertEqual(sch.short_title("Lab call — Prof. Someone's group"), "Lab call")
        self.assertEqual(sch.short_title("Job · desk time"), "Job")
        self.assertEqual(sch.short_title("EE101 lab (HLS)"), "EE101 lab")
        self.assertEqual(sch.short_title("An extremely long block title"), "An extremely…")
        s = json.loads(json.dumps(SCHED))
        s["blocks"][1]["short"] = "Deep"
        tue = {b["id"]: b for b in sch.day(s, date(2026, 9, 15))["blocks"]}
        self.assertEqual(tue["deep-tue"]["short"], "Deep")      # explicit short wins
        self.assertEqual(tue["dentist"]["short"], "Dentist")    # fallback from the title

    def test_render_table(self):
        table = sch.render_table(SCHED, date(2026, 9, 16))
        self.assertIn("| **08:00** | Job | Job | Job | Job | — | — | — |", table)
        self.assertIn("| **17:00** | — | Deep work | — |", table)
        self.assertNotIn("Dentist", table)  # one-off events are not part of the regular week grid
        self.assertIn("quiet week", table)


class TestCli(Base):
    def test_show_check_render_week_kind(self):
        code, out = self.tool("schedulectl.py", "show", "--date", "2026-09-15")
        self.assertIn("17:00–19:00  open   Deep work  (Deep work)  id:deep-tue", out)
        self.assertIn("08:00–12:00  fixed  Job", out)
        code, out = self.tool("schedulectl.py", "check", "--today", "2026-09-14")
        self.assertEqual(code, 1)
        self.assertIn("is due 2026-09-15 but the next Sales block", out)
        code, out = self.tool("schedulectl.py", "render", "--date", "2026-09-16")
        doc = (self.root / "plan" / "week.md").read_text(encoding="utf-8")
        self.assertIn("| **08:00** | Job |", doc)
        self.assertIn("kept", doc)
        self.assertNotIn("old", doc)
        code, out = self.tool("schedulectl.py", "week-kind", "busy", "--week-of", "2026-09-16")
        self.assertEqual(json.loads((self.root / "plan" / "schedule.json").read_text(encoding="utf-8"))["week_kinds"]["overrides"]["2026-09-14"], "busy")
        code, out = self.tool("schedulectl.py", "week-kind", "nope")
        self.assertIn("Unknown week kind", out)

    def test_plan(self):
        code, out = self.tool("widgetctl.py", "plan", "crm1", "deep-tue", "--date", "2026-09-15")
        self.assertIn("Planned crm1 into 17:00–19:00 Deep work on 2026-09-15", out)
        self.tool("widgetctl.py", "plan", "crm1", "job-am", "--date", "2026-09-15")  # moves, not duplicates
        plan = Store(self.root).details()["plan"]["2026-09-15"]
        self.assertEqual(plan, {"job-am": ["crm1"]})
        _, out = self.tool("widgetctl.py", "plan", "crm1", "sales-thu", "--date", "2026-09-15")
        self.assertIn("No block 'sales-thu' on 2026-09-15", out)
        _, out = self.tool("widgetctl.py", "plan", "nope", "job-am", "--date", "2026-09-15")
        self.assertIn("Not an open task id", out)
        _, out = self.tool("widgetctl.py", "plan", "--clear", "--date", "2026-09-15")
        self.assertNotIn("2026-09-15", Store(self.root).details()["plan"])

    def test_at_field(self):
        _, out = self.tool("due.py", "--today", "2026-09-14")
        self.assertIn("bad at '25:00'", out)
        self.assertNotIn("unknown field 'at", out)
        data = json.loads(self.tool("due.py", "--json", "--today", "2026-09-14")[1])
        sync = next(t for t in data["tasks"] if t["id"] == "crm2")
        self.assertEqual(sync["fields"]["at"], "16:00")


class TestWidgetData(Base):
    def test_schedule_in_widget_data(self):
        data = pa_widget.build_data(self.root, self.cfg, Store(self.root), today=date(2026, 9, 14))
        self.assertEqual(len(data["schedule"]["days"]), 7)
        self.assertEqual(data["schedule"]["days"][1]["date"], "2026-09-15")
        self.assertEqual(data["schedule"]["problems"], [])
        self.assertTrue(any("Sales block" in c for c in data["schedule"]["clashes"]))

    def test_no_schedule_configured(self):
        cfg = {"data_root": str(self.root)}
        data = pa_widget.build_data(self.root, cfg, Store(self.root), today=date(2026, 9, 14))
        self.assertIsNone(data["schedule"])

    def test_unreadable_schedule_reports_instead_of_crashing(self):
        (self.root / "plan" / "schedule.json").write_text("{not json", encoding="utf-8")
        data = pa_widget.build_data(self.root, self.cfg, Store(self.root), today=date(2026, 9, 14))
        self.assertIn("unreadable", data["schedule"]["problems"][0])


if __name__ == "__main__":
    unittest.main()
