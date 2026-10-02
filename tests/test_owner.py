"""Tests for owner:assistant tasks: parsing, the briefing's first section, run records from done.md, the widget
data, and widgetctl keeping them out of the user's focus / plan / nudges. Synthetic fixtures only."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import date, timedelta
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import donelog  # noqa: E402
import due  # noqa: E402
import pa_widget  # noqa: E402
from widget_store import Store  # noqa: E402

ENV = {"PYTHONIOENCODING": "utf-8", "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
TODAY = date.today()
D = lambda n: (TODAY + timedelta(days=n)).isoformat()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "home").mkdir()
        (self.root / "home" / "tasks.md").write_text(f"""# Tasks — home
Last reviewed: {D(0)}
- [ ] Mine, due today | due:{D(0)} | id:mine1
- [ ] Weekly import | every:mon | next:{D(-2)} | yields:rows added | owner:assistant | id:imp1
- [ ] Look up opening hours | yields:the hours | owner:Assistant | id:look1
- [ ] Later check | due:{D(10)} | owner:assistant | id:late1
- [ ] Blocked check | waiting:site back up | owner:assistant | id:wait1
- [ ] Odd owner | owner:robot | id:odd1
""", encoding="utf-8")
        (self.root / "home" / "done.md").write_text(f"""# Done — home
- {D(-9)} | Weekly import | 3 rows added | owner:assistant | id:imp1
- {D(-2)} | Weekly import | 5 rows added, 1 aged out | owner:assistant | id:imp1
- {D(-1)} | One-off lookup | pool closed Friday | added:{D(-3)} | owner:assistant | id:gone1
- {D(-1)} | Something the user did | fine | id:user1
- {D(-30)} | Old one-off of the assistant | long ago | owner:assistant | id:old1
""", encoding="utf-8")
        self.cfg_path = self.root / "cfg.json"
        self.cfg_path.write_text(json.dumps({"data_root": str(self.root)}), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def tool(self, script, *args):
        r = subprocess.run([sys.executable, str(TOOLS / script), "--config", str(self.cfg_path), *args],
                           capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=30)
        return r.returncode, r.stdout + r.stderr


class TestParsing(Base):
    def test_owner_field(self):
        area = due.load_areas(self.root)[0]
        owners = {t.id: t.owner for t in area.tasks}
        self.assertEqual(owners, {"mine1": "me", "imp1": "assistant", "look1": "assistant", "late1": "assistant",
                                  "wait1": "assistant", "odd1": "me"})
        self.assertTrue(any("bad owner 'robot'" in w for w in area.warnings), area.warnings)

    def test_done_log_keeps_the_owner(self):
        entries = {e.task_id: e for e in donelog.load_all(self.root)}
        self.assertEqual(entries["gone1"].owner, "assistant")
        self.assertEqual(entries["user1"].owner, "me")
        self.assertEqual(entries["gone1"].outcome, "pool closed Friday")
        self.assertEqual(entries["gone1"].as_json()["owner"], "assistant")


class TestBriefing(Base):
    def test_assistant_section_comes_first_and_leaves_the_users_lists(self):
        code, out = self.tool("due.py")
        self.assertEqual(code, 0, out)
        first = out.index("FOR ASSISTANT")
        self.assertLess(first, out.index("DUE IN NEXT"))
        mine = out[out.index("DUE IN NEXT"):]
        section = out[first:out.index("DUE IN NEXT")]
        self.assertIn("(2)", section.splitlines()[0])                # due import + undated lookup
        self.assertIn("Weekly import", section)
        self.assertIn("last run " + D(-2) + ": 5 rows added, 1 aged out", section)   # newest run wins
        self.assertIn("Look up opening hours", section)
        self.assertNotIn("Blocked check", section)                   # waiting: not for this run
        self.assertIn("Assistant, later: 2", section)
        self.assertNotIn("Weekly import", mine)
        self.assertNotIn("OVERDUE", out)                             # the overdue one is the assistant's
        self.assertIn("Mine, due today", mine)
        # only the user's undated task counts (the bad-owner one); the assistant's two undated ones do not
        self.assertEqual(out.split("Open, undated:")[1].splitlines()[0].strip(), "home 1")

    def test_name_comes_from_local_settings(self):
        (self.root / "pa.local.json").write_text(json.dumps({"assistant_name": "Wren"}), encoding="utf-8")
        _, out = self.tool("due.py")
        self.assertIn("FOR WREN", out)
        self.assertIn("Wren, later: 2", out)

    def test_json_carries_the_owner(self):
        _, out = self.tool("due.py", "--json")
        owners = {t["id"]: t["owner"] for t in json.loads(out)["tasks"]}
        self.assertEqual(owners["imp1"], "assistant")
        self.assertEqual(owners["mine1"], "me")


class TestChains(Base):
    def test_waiting_on_a_closed_task_is_flagged(self):
        f = self.root / "home" / "tasks.md"
        extra = [f"- [ ] Send the message | due:{D(3)} | waiting:task look1 | id:msg1",
                 f"- [ ] Other | due:{D(3)} | waiting:Task gone1 | id:msg2",
                 "- [ ] Free text wait | waiting:reply from a contact | id:msg3"]
        f.write_text(f.read_text(encoding="utf-8") + "".join(line + chr(10) for line in extra), encoding="utf-8")
        warns = due.chain_warnings(due.load_areas(self.root))
        self.assertEqual(len(warns), 1, warns)                   # look1 is open; gone1 is closed; msg3 is prose
        self.assertIn("waits on task gone1, which is no longer open", warns[0])
        _, out = self.tool("due.py")
        self.assertIn("waits on task gone1", out)


class TestWidgetctl(Base):
    def test_assistant_tasks_stay_out_of_focus_plan_and_nudges(self):
        code, out = self.tool("widgetctl.py", "focus", "mine1", "imp1")
        self.assertEqual(code, 1)
        self.assertIn("not the user's to focus: imp1", out)
        code, out = self.tool("widgetctl.py", "focus", "mine1")
        self.assertEqual(code, 0, out)
        code, out = self.tool("widgetctl.py", "nudge", "mine1", "imp1")
        self.assertEqual(code, 0, out)
        self.assertIn("mine1: nudged 1×", out)
        self.assertNotIn("imp1: nudged", out)
        code, out = self.tool("widgetctl.py", "plan", "look1", "some-block")
        self.assertEqual(code, 1)
        self.assertIn("not the user's to plan", out)


class TestWidgetData(Base):
    def setUp(self):
        super().setUp()
        store = Store(self.root)
        store.save_details({"focus": ["imp1", "mine1"], "plan": {D(0): {"blk": ["look1", "mine1"]}}})
        self.cfg = {"data_root": str(self.root), "assistant_name": "Wren"}
        self.srv = pa_widget.make_server(self.root, self.cfg, 0)
        self.port = self.srv.server_address[1]
        self.key = store.key()
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); super().tearDown()

    def data(self):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/data", headers={"X-PA-Key": self.key})
        with urllib.request.urlopen(r, timeout=5) as resp:
            return json.loads(resp.read())

    def test_runs_done_and_name(self):
        d = self.data()
        self.assertEqual(d["assistant_name"], "Wren")
        self.assertEqual(d["assistant_runs"]["imp1"]["outcome"], "5 rows added, 1 aged out")
        self.assertNotIn("look1", d["assistant_runs"])               # never run
        self.assertNotIn("user1", d["assistant_runs"])
        self.assertEqual([e["task_id"] for e in d["assistant_done"]], ["gone1"])   # this week, finished, theirs
        self.assertEqual(d["focus"], ["mine1"])                       # stale focus/plan entries are dropped
        self.assertEqual(d["plan"][D(0)]["blk"], ["mine1"])

    def test_page_uses_the_name(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=5) as resp:
            html = resp.read().decode("utf-8")
        self.assertIn("function assistantSection", html)
        visible = [l for l in html.splitlines() if "/pa" in l and not l.strip().startswith("//")]
        self.assertEqual(visible, [], visible)                        # no "/pa" left in anything shown


if __name__ == "__main__":
    unittest.main()
