"""Tests for widget presence: knowing when a widget window is already open, so a second one is not opened by
accident. Synthetic fixtures only.  Run from the skill folder:  python -m unittest discover -s tests
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import pa_widget  # noqa: E402
from widget_store import PAGE_ALIVE_SECONDS, Store  # noqa: E402

ENV = {"PYTHONIOENCODING": "utf-8", "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
A, B, C = "page-aaaaaaaa", "page-bbbbbbbb", "page-cccccccc"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "home").mkdir()
        (self.root / "home" / "tasks.md").write_text("# Tasks — home\nLast reviewed: 2026-09-14\n", encoding="utf-8")
        self.store = Store(self.root)

    def tearDown(self):
        self._tmp.cleanup()


class TestStore(Base):
    def test_older_pages_and_expiry(self):
        self.assertEqual(self.store.page_seen(A, now=1000), {"older": 0, "open": 1})
        self.assertEqual(self.store.page_seen(B, now=1010), {"older": 1, "open": 2})    # the second window is told
        self.assertEqual(self.store.page_seen(A, now=1030), {"older": 0, "open": 2})    # the first one never is
        self.assertEqual(self.store.open_pages(now=1030), [A, B])
        # A stops checking in (crashed): it stops counting after PAGE_ALIVE_SECONDS, and B becomes the oldest
        later = 1030 + PAGE_ALIVE_SECONDS + 1
        self.assertEqual(self.store.page_seen(B, now=later), {"older": 0, "open": 1})
        self.assertEqual(self.store.open_pages(now=later), [B])

    def test_closing_removes_a_page_at_once(self):
        self.store.page_seen(A, now=1000)
        self.store.page_seen(B, now=1001)
        self.store.page_gone(A)
        self.assertEqual(self.store.page_seen(B, now=1002), {"older": 0, "open": 1})
        self.store.page_gone("never-seen-page")                                         # harmless

    def test_list_survives_a_new_store_and_rejects_bad_ids(self):
        self.store.page_seen(A)
        self.assertEqual(Store(self.root).open_pages(), [A])     # what a restarted server or open_widget.py reads
        for bad in ("", "short", "../../etc/passwd", "x" * 65, "has space in it"):
            with self.assertRaises(ValueError):
                self.store.page_seen(bad)


class TestServer(Base):
    def setUp(self):
        super().setUp()
        self.cfg = {"data_root": str(self.root), "widget_browser": "none"}
        self.srv = pa_widget.make_server(self.root, self.cfg, 0)
        self.port = self.srv.server_address[1]
        self.key = self.store.key()
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def post(self, body, key=True):
        headers = {"Content-Type": "application/json", **({"X-PA-Key": self.key} if key else {})}
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/presence", data=json.dumps(body).encode(),
                                   headers=headers, method="POST")
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_check_in_and_leave(self):
        self.assertEqual(self.post({"page": A}), (200, {"older": 0, "open": 1}))
        self.assertEqual(self.post({"page": B}), (200, {"older": 1, "open": 2}))
        self.assertEqual(self.post({"page": A, "gone": True})[0], 200)
        self.assertEqual(self.post({"page": B}), (200, {"older": 0, "open": 1}))
        self.assertEqual(self.post({"page": C}, key=False)[0], 403)
        self.assertEqual(self.post({"page": "bad id"})[0], 400)
        # routine check-ins stay out of the request log; rejected ones are kept for troubleshooting
        lines = [l for l in (self.store.dir / "server.log").read_text(encoding="utf-8").splitlines() if "/api/presence" in l]
        self.assertEqual([l.split('" ')[1].split()[0] for l in lines], ["403", "400"])

    def open_widget(self, *args):
        cfg_path = self.root / "cfg.json"
        cfg_path.write_text(json.dumps({**self.cfg, "widget_port": self.port}), encoding="utf-8")
        r = subprocess.run([sys.executable, str(TOOLS / "open_widget.py"), "--config", str(cfg_path), *args],
                           capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=20)
        return r.stdout + r.stderr

    def test_open_widget_skips_a_duplicate_window(self):
        self.assertIn("not opening a window (widget_browser = none)", self.open_widget())   # nothing open yet
        self.post({"page": A})
        out = self.open_widget()
        self.assertIn("already open in 1 window — not opening another", out)
        self.assertNotIn("server started", out)                                           # used the running one
        self.assertIn("not opening a window (widget_browser = none)", self.open_widget("--new-window"))
        self.post({"page": A, "gone": True})
        self.assertNotIn("already open", self.open_widget())

    def test_a_page_seen_just_before_a_restart_still_counts(self):
        self.post({"page": A})
        self.srv.shutdown(); self.srv.server_close()                 # the restart: the old server is gone ...
        self.srv = pa_widget.make_server(self.root, self.cfg, 0)     # ... and a new one has not heard from A yet
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.assertIn("already open in 1 window", self.open_widget())


if __name__ == "__main__":
    unittest.main()
