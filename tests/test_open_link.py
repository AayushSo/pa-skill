"""Tests for POST /api/open (outside links go to the system browser). The launcher is replaced; nothing opens."""
import json
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
from widget_store import Store  # noqa: E402


class TestOpenLink(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "work").mkdir()
        (self.root / "work" / "tasks.md").write_text("# Tasks — work\nLast reviewed: 2099-01-01\n", encoding="utf-8")
        self.cfg = {"data_root": str(self.root)}
        self.opened = []
        self._real = pa_widget.launch_url
        pa_widget.launch_url = lambda url, how: self.opened.append((url, how)) or how
        self.srv = pa_widget.make_server(self.root, self.cfg, 0)
        self.port = self.srv.server_address[1]
        self.key = Store(self.root).key()
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        pa_widget.launch_url = self._real
        self.srv.shutdown(); self.srv.server_close(); self._tmp.cleanup()

    def post(self, body, key=True):
        headers = {"Content-Type": "application/json"}
        if key:
            headers["X-PA-Key"] = self.key if key is True else key
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/open", data=json.dumps(body).encode(),
                                   headers=headers, method="POST")
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_opens_outside_links_in_the_configured_browser(self):
        self.assertEqual(self.post({"url": "https://jobs.example.com/r/123?src=x"}), 200)
        self.assertEqual(self.opened, [("https://jobs.example.com/r/123?src=x", "default")])
        self.cfg["link_browser"] = "firefox"        # the server reads the live config dict
        self.post({"url": "http://example.org/"})
        self.assertEqual(self.opened[-1], ("http://example.org/", "firefox"))

    def test_needs_the_key(self):
        self.assertEqual(self.post({"url": "https://example.com/"}, key=False), 403)
        self.assertEqual(self.post({"url": "https://example.com/"}, key="wrong"), 403)
        self.assertEqual(self.opened, [])

    def test_refuses_anything_but_outside_http_links(self):
        nl, tab = chr(10), chr(9)
        for bad in ["file:///C:/Windows/System32/calc.exe", "javascript:alert(1)", "ms-settings:",
                    "http://127.0.0.1:8765/?k=x", "http://localhost/", "https://", "not a url",
                    "https://example.com/a b", "https://example.com/" + nl + "-x", "", None, 42,
                    "https://e.com/" + "a" * 5000]:
            self.assertEqual(self.post({"url": bad}), 400, bad)
        self.assertEqual(self.opened, [])
        self.assertIsNone(pa_widget.external_url("https://a.test/" + tab + "x"))
        self.assertIsNone(pa_widget.external_url("https://[::1]/"))
        self.assertEqual(pa_widget.external_url("https://a.test/x"), "https://a.test/x")

    def test_app_mode_leaves_links_to_the_page(self):
        self.cfg["link_browser"] = "app"
        self.assertEqual(self.post({"url": "https://example.com/"}), 409)
        self.assertEqual(self.opened, [])

    def test_raise_window_gives_up_quietly_without_a_window(self):
        self.assertFalse(pa_widget.raise_window("NoSuchWindowClass_pa_test", wait=0.2))


if __name__ == "__main__":
    unittest.main()
