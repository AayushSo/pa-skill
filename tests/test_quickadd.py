"""Tests for quick add: new tasks queued from the widget (or widgetctl capture) for /pa to write up.
Synthetic fixtures only.  Run from the skill folder:  python -m unittest discover -s tests
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import pa_widget  # noqa: E402
from widget_store import CAPTURE_MAX, Store  # noqa: E402

ENV = {"PYTHONIOENCODING": "utf-8", "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
TASKS = "# Tasks — home\nLast reviewed: 2026-09-14\n- [ ] Existing task | due:2026-09-20 | id:h001\n"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for folder in ("home", "garden"):
            (self.root / folder).mkdir()
            (self.root / folder / "tasks.md").write_text(TASKS.replace("home", folder), encoding="utf-8")
        self.cfg = {"data_root": str(self.root)}
        self.cfg_path = self.root / "cfg.json"
        self.cfg_path.write_text(json.dumps(self.cfg), encoding="utf-8")
        self.store = Store(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def tool(self, script, *args):
        r = subprocess.run([sys.executable, str(TOOLS / script), "--config", str(self.cfg_path), *args],
                           capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=20)
        return r.returncode, r.stdout + r.stderr


class TestStore(Base):
    FOLDERS = {"home", "garden"}

    def test_capture_normalises_and_validates(self):
        e = self.store.capture("  Fix the\n  shed door  ", "garden", "2026-10-05", self.FOLDERS)
        self.assertEqual((e["type"], e["text"], e["area"], e["due"]), ("capture", "Fix the shed door", "garden", "2026-10-05"))
        self.assertEqual(self.store.capture("No area or date")["area"], "")
        for args, msg in [(("   ",), "empty"), (("x" * (CAPTURE_MAX + 1),), "too long"),
                          (("t", "nowhere", None, self.FOLDERS), "unknown area"), (("t", "", "soon"), "bad date")]:
            with self.assertRaises(ValueError) as cm:
                self.store.capture(*args)
            self.assertIn(msg, str(cm.exception))

    def test_pending_list_drops_filed_and_withdrawn(self):
        a = self.store.capture("First")
        b = self.store.capture("Second")
        c = self.store.capture("Third")
        self.assertEqual([x["text"] for x in self.store.captures()], ["First", "Second", "Third"])
        self.store.uncapture(b["eid"])
        self.store.ack([a["eid"]])
        self.assertEqual([x["eid"] for x in self.store.captures()], [c["eid"]])
        with self.assertRaises(ValueError):
            self.store.uncapture(a["eid"])              # already filed: too late to withdraw
        with self.assertRaises(ValueError):
            self.store.uncapture("e-nonsense")

    def test_captures_never_look_like_task_state(self):
        self.store.capture("New thing")
        self.assertEqual(self.store.task_states(), {})


class TestCtl(Base):
    def test_capture_command_and_inbox(self):
        code, out = self.tool("widgetctl.py", "capture", "Order seeds", "--area", "garden", "--due", "2026-10-01")
        self.assertEqual(code, 0, out)
        self.assertIn("Queued a new task for garden: Order seeds", out)
        code, out = self.tool("widgetctl.py", "capture", "Somewhere", "--area", "attic")
        self.assertEqual(code, 2)
        self.assertIn("unknown area 'attic'", out)
        self.assertIn("Areas: garden, home", out)
        self.tool("widgetctl.py", "capture", "Undecided thing")
        withdrawn = self.store.capture("Changed my mind")
        self.store.uncapture(withdrawn["eid"])
        _, out = self.tool("widgetctl.py", "inbox")
        self.assertIn("NEW TASKS (quick add)", out)
        self.assertIn("[area: garden · by 2026-10-01]  Order seeds", out)
        self.assertIn("[area: you decide (ask if unclear)]  Undecided thing", out)
        self.assertIn("withdrawn before filing — nothing to write, just ack:", out)
        self.assertNotIn("Changed my mind", out.split("withdrawn")[0])
        self.tool("widgetctl.py", "ack", "--all")
        _, out = self.tool("widgetctl.py", "inbox")
        self.assertIn("nothing pending", out)


class TestServer(Base):
    def setUp(self):
        super().setUp()
        self.srv = pa_widget.make_server(self.root, self.cfg, 0)
        self.port = self.srv.server_address[1]
        self.key = self.store.key()
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def post(self, body, key=True):
        headers = {"Content-Type": "application/json"}
        if key:
            headers["X-PA-Key"] = self.key
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/event", data=json.dumps(body).encode(),
                                   headers=headers, method="POST")
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def data(self):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/data", headers={"X-PA-Key": self.key})
        with urllib.request.urlopen(r, timeout=5) as resp:
            return json.loads(resp.read())

    def test_capture_round_trip(self):
        code, ev = self.post({"type": "capture", "text": "Fix the gate", "area": "garden", "due": "2026-10-03"})
        self.assertEqual(code, 200, ev)
        caps = self.data()["captures"]
        self.assertEqual([(c["text"], c["area"], c["due"]) for c in caps], [("Fix the gate", "garden", "2026-10-03")])
        code, _ = self.post({"type": "uncapture", "ref": ev["eid"]})
        self.assertEqual(code, 200)
        self.assertEqual(self.data()["captures"], [])

    def test_validation_and_auth(self):
        self.assertEqual(self.post({"type": "capture", "text": "x"}, key=False)[0], 403)
        for body, msg in [({"type": "capture", "text": " "}, "empty"),
                          ({"type": "capture", "text": "x" * (CAPTURE_MAX + 1)}, "too long"),
                          ({"type": "capture", "text": "x", "area": "../home"}, "unknown area"),
                          ({"type": "capture", "text": "x", "due": "next week"}, "bad date"),
                          ({"type": "uncapture", "ref": "nope"}, "not a quick add")]:
            code, out = self.post(body)
            self.assertEqual(code, 400, body)
            self.assertIn(msg, out["error"])

    def test_tasks_md_untouched(self):
        before = {f: (self.root / f / "tasks.md").read_bytes() for f in ("home", "garden")}
        self.post({"type": "capture", "text": "Something new", "area": "home"})
        self.assertEqual({f: (self.root / f / "tasks.md").read_bytes() for f in before}, before)


if __name__ == "__main__":
    unittest.main()
