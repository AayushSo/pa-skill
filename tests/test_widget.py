"""Tests for task ids, --json, --assign-ids, the widget store, widgetctl and the widget server.
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
from datetime import date
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import due  # noqa: E402
import pa_widget  # noqa: E402
from widget_store import Store  # noqa: E402

ENV = {"PYTHONIOENCODING": "utf-8", "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
TASKS = """# Tasks — alpha
Last reviewed: 2026-09-14
- [ ] A1 overdue | due:2026-09-10 | id:aaa1
- [ ] A2 today | due:2026-09-14 | id:aaa2
- [ ] A3 no id yet | due:2026-09-16
- [?] A4 unknown
- [ ] A5 later | start:2026-10-01 | due:2026-10-05
- [x] A6 done
"""


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "alpha").mkdir()
        (self.root / "alpha" / "tasks.md").write_text(TASKS, encoding="utf-8")
        self.cfg_path = self.root / "cfg.json"
        self.cfg_path.write_text(json.dumps({"data_root": str(self.root), "widget_port": 0}), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def tool(self, script, *args):
        r = subprocess.run([sys.executable, str(TOOLS / script), "--config", str(self.cfg_path), *args],
                           capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=20)
        return r.returncode, r.stdout + r.stderr


class TestIds(Base):
    def test_json_buckets_and_ids(self):
        code, out = self.tool("due.py", "--json", "--today", "2026-09-14")
        self.assertEqual(code, 0, out)
        data = json.loads(out)
        by_text = {t["text"]: t for t in data["tasks"]}
        self.assertEqual(by_text["A1 overdue"]["bucket"], "overdue")
        self.assertEqual(by_text["A2 today"]["bucket"], "today")
        self.assertEqual(by_text["A3 no id yet"]["bucket"], "soon")
        self.assertEqual(by_text["A4 unknown"]["bucket"], "unknown")
        self.assertEqual(by_text["A5 later"]["bucket"], "deferred")
        self.assertEqual(by_text["A1 overdue"]["id"], "aaa1")
        self.assertNotIn("A6 done", by_text)
        self.assertEqual(data["done_left"], 1)

    def test_id_not_shown_in_text_output(self):
        _, out = self.tool("due.py", "--today", "2026-09-14")
        self.assertNotIn("aaa1", out)
        self.assertNotIn("unknown field", out)

    def test_assign_ids_is_unique_idempotent_and_preserves_lines(self):
        path = self.root / "alpha" / "tasks.md"
        path.write_bytes(TASKS.replace("\n", "\r\n").encode("utf-8"))
        code, out = self.tool("due.py", "--assign-ids")
        self.assertIn("Assigned 3 new id(s)", out)  # A3, A4, A5; A6 is [x] and gets none
        raw = path.read_bytes()
        self.assertIn(b"\r\n", raw)
        self.assertEqual(raw.count(b"\r\n"), TASKS.count("\n"))  # no lines added or merged
        text = raw.decode("utf-8")
        self.assertIn("- [ ] A1 overdue | due:2026-09-10 | id:aaa1\r\n", text)
        self.assertIn("- [x] A6 done\r\n", text)
        ids = [t.id for t in due.parse_area(path).tasks if t.mark != "x"]
        self.assertTrue(all(ids))
        self.assertEqual(len(ids), len(set(ids)))
        _, again = self.tool("due.py", "--assign-ids")
        self.assertIn("Assigned 0 new id(s)", again)

    def test_duplicate_and_bad_ids_warn(self):
        (self.root / "beta").mkdir()
        (self.root / "beta" / "tasks.md").write_text(
            "# Tasks — beta\nLast reviewed: 2026-09-14\n- [ ] B1 | id:aaa1\n- [ ] B2 | id:BAD ID\n", encoding="utf-8")
        _, out = self.tool("due.py", "--today", "2026-09-14")
        self.assertIn("duplicate id 'aaa1'", out)
        self.assertIn("bad id 'BAD ID'", out)


class TestNote(Base):
    """Other sessions queue a comment instead of editing tasks.md."""

    def test_note_queues_a_comment_for_an_open_task(self):
        code, out = self.tool("widgetctl.py", "note", "aaa1", "found the reply in the archive")
        self.assertEqual(code, 0, out)
        self.assertIn("Noted on aaa1 (alpha)", out)
        pending = [e for e in Store(self.root).events()]
        self.assertEqual([(e["type"], e["task_id"], e["comment"]) for e in pending],
                         [("comment", "aaa1", "found the reply in the archive")])
        self.assertEqual((self.root / "alpha" / "tasks.md").read_text(encoding="utf-8"), TASKS)   # file untouched

    def test_note_refuses_unknown_ids_and_empty_text(self):
        code, out = self.tool("widgetctl.py", "note", "nope", "x")
        self.assertEqual((code, "Not an open task id: nope" in out), (1, True))
        code, out = self.tool("widgetctl.py", "note", "aaa1", "   ")
        self.assertEqual((code, "Nothing to note" in out), (2, True))
        self.assertEqual(Store(self.root).events(), [])


class TestBrand(Base):
    """The app is named after the assistant and coloured by "accent"; the page and icons follow."""

    def setUp(self):
        super().setUp()
        self.cfg = {"data_root": str(self.root), "assistant_name": "Wren", "accent": "#8A5A2F",
                    "accent_dark": "#e0b183"}
        self.srv = pa_widget.make_server(self.root, self.cfg, 0)
        self.port = self.srv.server_address[1]
        self.key = Store(self.root).key()
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); super().tearDown()

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
            return r.read()

    def test_manifest_is_named_after_the_assistant(self):
        m = json.loads(self.get("/manifest.webmanifest"))
        self.assertEqual((m["name"], m["short_name"], m["theme_color"]), ("Wren", "Wren", "#8a5a2f"))
        self.assertIn("Wren", m["description"])
        plain = pa_widget.manifest({})                        # no settings: a generic name and the page's own blue
        self.assertEqual((plain["name"], plain["theme_color"]), ("Assistant", "#2f5d8a"))

    def test_page_carries_the_accent_the_brand_row_and_the_signature(self):
        html = self.get("/").decode("utf-8")
        self.assertIn("--accent: #8a5a2f", html)
        self.assertIn("#e0b183", html)                       # the dark-mode accent too
        self.assertNotIn("#2f5d8a", html)
        self.assertIn('class="brandbar"', html)
        self.assertIn('id="brandname"', html)
        self.assertIn('class="ibis"', html)                   # the mark is inline SVG, no image request
        self.assertIn("github.com/AayushSo", html)

    def test_theme_switch_and_help_panel_are_in_the_page(self):
        html = self.get("/").decode("utf-8")
        # dark applies from the system setting unless the viewer forced light, or when they forced dark
        self.assertIn(':root:not([data-theme="light"])', html)
        self.assertIn(':root[data-theme="dark"]', html)
        self.assertIn('id="themebtn"', html)
        self.assertIn('id="helpwrap"', html)
        self.assertIn('id="helpbtn"', html)
        self.assertIn("this panel", html)                     # the ? row of the shortcut list

    def test_icons_follow_the_accent(self):
        mine = self.get("/icon-192.png")
        self.assertTrue(mine.startswith(b"\x89PNG"))
        self.assertNotEqual(mine, pa_widget.icon_png(192))    # not the default blue
        self.assertEqual(mine, pa_widget.icon_png(192, "#8a5a2f"))
        self.assertEqual(self.get("/favicon.ico"), mine)

    def test_bad_accent_falls_back(self):
        for bad in ("red", "#12345", "", None, "#gggggg"):
            self.assertEqual(pa_widget.accent({"accent": bad}), "#2f5d8a")

    def test_api_reports_the_name_and_accent(self):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/data", headers={"X-PA-Key": self.key})
        with urllib.request.urlopen(r, timeout=10) as resp:
            d = json.loads(resp.read())
        self.assertEqual((d["assistant_name"], d["accent"]), ("Wren", "#8a5a2f"))


class TestBriefingIds(Base):
    def test_briefing_asks_for_ids_until_assigned(self):
        _, out = self.tool("due.py", "--today", "2026-09-14")
        self.assertIn("! 3 open task(s) have no id", out)
        self.tool("due.py", "--assign-ids")
        _, out = self.tool("due.py", "--today", "2026-09-14")
        self.assertNotIn("have no id", out)


class TestStoreAndCtl(Base):
    def test_net_state_and_ack(self):
        s = Store(self.root)
        e1 = s.append_event({"type": "done", "task_id": "aaa1"})
        s.append_event({"type": "undone", "task_id": "aaa1"})
        s.append_event({"type": "done", "task_id": "aaa1"})
        s.append_event({"type": "snooze", "task_id": "aaa2", "until": "2026-09-20"})
        c = s.append_event({"type": "comment", "task_id": "aaa2", "comment": "spoke to X"})
        st = s.task_states()
        self.assertTrue(st["aaa1"]["done"])
        self.assertEqual(st["aaa2"]["snooze"], "2026-09-20")
        self.assertFalse(st["aaa2"]["comments"][0]["filed"])
        self.assertEqual(s.ack([c["eid"], e1["eid"], "nope"]), 2)
        st = s.task_states()
        self.assertTrue(st["aaa2"]["comments"][0]["filed"])  # comment history kept after ack
        with self.assertRaises(ValueError):
            s.append_event({"type": "delete", "task_id": "aaa1"})

    def test_widgetctl_inbox_focus_detail(self):
        Store(self.root).append_event({"type": "comment", "task_id": "aaa1", "comment": "note one"})
        code, out = self.tool("widgetctl.py", "inbox")
        self.assertIn("[aaa1] alpha/tasks.md:3  A1 overdue", out)
        self.assertIn("comment: note one", out)
        _, out = self.tool("widgetctl.py", "focus", "aaa2", "zzzz")
        self.assertIn("Not open task ids: zzzz", out)
        _, out = self.tool("widgetctl.py", "focus", "aaa2", "aaa1")
        self.assertEqual(Store(self.root).details()["focus"], ["aaa2", "aaa1"])
        self.tool("widgetctl.py", "detail", "aaa1", "Why it matters.")
        self.assertEqual(Store(self.root).details()["details"]["aaa1"]["text"], "Why it matters.")
        _, out = self.tool("widgetctl.py", "ack", "--all")
        self.assertIn("Acknowledged 1", out)
        _, out = self.tool("widgetctl.py", "inbox")
        self.assertIn("nothing pending", out)


class TestServer(Base):
    def setUp(self):
        super().setUp()
        self.cfg = {"data_root": str(self.root)}
        self.srv = pa_widget.make_server(self.root, self.cfg, 0)
        self.port = self.srv.server_address[1]
        self.key = Store(self.root).key()
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, path, method="GET", body=None, key=True, host=None):
        headers = {"Content-Type": "application/json"}
        if key:
            headers["X-PA-Key"] = self.key if key is True else key
        if host:
            headers["Host"] = host
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_status_and_url_mask_the_key(self):
        self.cfg_path.write_text(json.dumps({"data_root": str(self.root), "widget_port": self.port}), encoding="utf-8")
        code, out = self.tool("open_widget.py", "--status")
        self.assertEqual(code, 0)
        self.assertIn("running", out)
        self.assertNotIn(self.key, out)
        _, out = self.tool("widgetctl.py", "url")
        self.assertNotIn(self.key, out)
        _, out = self.tool("widgetctl.py", "url", "--show-key")
        self.assertIn(self.key, out)

    def test_doc_links_need_key_and_stay_inside_data_root(self):
        (self.root / "plan.html").write_text("<h1>plan</h1>", encoding="utf-8")
        (self.root.parent / "outside.html").write_text("secret", encoding="utf-8")
        # global links (local settings "links") belong to the pseudo-area "_"; area links are tested in test_areas
        self.cfg["links"] = [{"id": "plan", "label": "Plan", "file": "plan.html"},
                             {"id": "escape", "label": "X", "file": "../outside.html"},
                             {"id": "missing", "label": "M", "file": "nope.html"}]
        code, body = self.req(f"/doc/_/plan?k={self.key}", key=False)
        self.assertEqual((code, body), (200, b"<h1>plan</h1>"))
        self.assertEqual(self.req("/doc/_/plan", key=False)[0], 403)
        self.assertEqual(self.req("/doc/_/plan?k=wrong", key=False)[0], 403)
        self.assertEqual(self.req(f"/doc/_/escape?k={self.key}", key=False)[0], 404)
        self.assertEqual(self.req(f"/doc/_/missing?k={self.key}", key=False)[0], 404)
        data = json.loads(self.req("/api/data")[1])
        self.assertEqual(data["links"], [{"id": "_/plan", "label": "Plan", "href": "/doc/_/plan", "kind": "doc", "area": "_"}])
        self.assertTrue(any("links[1]" in w for w in data["warnings"]), data["warnings"])

    def test_page_served(self):
        code, body = self.req("/", key=False)
        self.assertEqual(code, 200)
        self.assertIn(b"<title>PA</title>", body)

    def test_manifest_and_icons_are_public_and_keyless(self):
        code, body = self.req("/manifest.webmanifest", key=False)
        self.assertEqual(code, 200)
        manifest = json.loads(body)
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "/")
        self.assertNotIn(self.key, body.decode())
        for size in (192, 512):
            code, png = self.req(f"/icon-{size}.png", key=False)
            self.assertEqual(code, 200)
            self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertEqual(int.from_bytes(png[16:20], "big"), size)  # IHDR width

    def test_key_required(self):
        self.assertEqual(self.req("/api/data", key=False)[0], 403)
        self.assertEqual(self.req("/api/data", key="wrong")[0], 403)
        self.assertEqual(self.req("/api/event", "POST", {"type": "done", "task_id": "aaa1"}, key=False)[0], 403)

    def test_bad_host_rejected(self):
        self.assertEqual(self.req("/api/data", host="evil.example:80")[0], 403)

    def test_data_merges_inbox_and_details(self):
        s = Store(self.root)
        s.save_details({"focus": ["aaa2", "gone"], "details": {"aaa1": {"text": "D", "updated": "2026-09-14"}}})
        self.assertEqual(self.req("/api/event", "POST", {"type": "done", "task_id": "aaa1"})[0], 200)
        self.assertEqual(self.req("/api/event", "POST", {"type": "comment", "task_id": "aaa2", "comment": "hi"})[0], 200)
        code, body = self.req("/api/data")
        self.assertEqual(code, 200)
        data = json.loads(body)
        t = {x["id"]: x for x in data["tasks"] if x["id"]}
        self.assertTrue(t["aaa1"]["pending_done"])
        self.assertEqual(t["aaa1"]["detail"], "D")
        self.assertEqual(t["aaa2"]["comments"][0]["text"], "hi")
        self.assertEqual(data["focus"], ["aaa2"])  # unknown ids dropped
        self.assertEqual(data["missing_ids"], 3)  # A3, A4, A5

    def test_snooze_past_the_due_date_needs_confirmation(self):
        # aaa2 is due 2026-09-14; a snooze to a later date must say the date moves
        code, body = self.req("/api/event", "POST", {"type": "snooze", "task_id": "aaa2", "until": "2099-01-01"})
        self.assertEqual(code, 409)
        code, body = self.req("/api/event", "POST", {"type": "snooze", "task_id": "aaa2", "until": "2099-01-01", "moves_date": True})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["moves_date"], "due")
        # a snooze that stays before the due date needs nothing extra
        code, body = self.req("/api/event", "POST", {"type": "snooze", "task_id": "aaa2", "until": "2026-09-13"})
        self.assertEqual(code, 200)
        self.assertNotIn("moves_date", json.loads(body))
        # an overdue task (aaa1, due 2026-09-10) can be snoozed once the move is confirmed
        code, body = self.req("/api/event", "POST", {"type": "snooze", "task_id": "aaa1", "until": "2099-01-01", "moves_date": True})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["moves_date"], "due")
        # a task with no date (A4, given an id here) needs no confirmation
        path = self.root / "alpha" / "tasks.md"
        path.write_text(path.read_text(encoding="utf-8").replace("- [?] A4 unknown", "- [?] A4 unknown | id:aaa4"), encoding="utf-8")
        code, body = self.req("/api/event", "POST", {"type": "snooze", "task_id": "aaa4", "until": "2099-01-01"})
        self.assertEqual(code, 200)
        self.assertNotIn("moves_date", json.loads(body))

    def test_inbox_prints_the_exact_snooze_instruction(self):
        s = Store(self.root)
        s.append_event({"type": "snooze", "task_id": "aaa2", "until": "2099-01-01", "moves_date": "due"})
        s.append_event({"type": "snooze", "task_id": "aaa1", "until": "2026-09-01"})
        s.append_event({"type": "snooze", "task_id": "aaa1", "until": "2099-01-01"})
        _, out = self.tool("widgetctl.py", "inbox")
        self.assertIn("user confirmed moving the date: set start:2099-01-01 and due:2099-01-01", out)
        self.assertIn("snooze until 2026-09-01 → set start:2026-09-01", out)
        self.assertIn("due is 2026-09-10, before 2099-01-01, and the move was not confirmed: ASK", out)

    def test_event_validation(self):
        self.assertEqual(self.req("/api/event", "POST", {"type": "done", "task_id": "nope"})[0], 400)
        self.assertEqual(self.req("/api/event", "POST", {"type": "delete", "task_id": "aaa1"})[0], 400)
        self.assertEqual(self.req("/api/event", "POST", {"type": "comment", "task_id": "aaa1", "comment": "  "})[0], 400)
        self.assertEqual(self.req("/api/event", "POST", {"type": "snooze", "task_id": "aaa1", "until": "soon"})[0], 400)
        self.assertEqual(self.req("/api/event", "DELETE")[0], 405)

    def test_tasks_md_never_modified_by_server(self):
        path = self.root / "alpha" / "tasks.md"
        before = path.read_bytes()
        self.req("/api/event", "POST", {"type": "done", "task_id": "aaa1"})
        self.req("/api/event", "POST", {"type": "snooze", "task_id": "aaa2", "until": "2026-09-30"})
        self.req("/api/data")
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
