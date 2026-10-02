"""Tests for the area registry, area.json manifests, hooks, links and area handlers (tools/areas.py and the
/area and /doc routes). The fixture is the example area shipped in examples/area-template."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
TOOLS = SKILL / "tools"
TEMPLATE = SKILL / "examples" / "area-template"
sys.path.insert(0, str(TOOLS))
import areas as AR  # noqa: E402
import due  # noqa: E402
import pa_widget  # noqa: E402
from widget_store import Store  # noqa: E402

ENV = {"PYTHONIOENCODING": "utf-8", "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}


def tasks(name):
    return f"# Tasks — {name}\nLast reviewed: 2099-01-01\n\n- [ ] {name} thing | due:2099-01-01\n"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.top = Path(self._tmp.name)
        self.root = self.top / "data"
        self.root.mkdir()
        shutil.copytree(TEMPLATE, self.root / "reading")
        for name in ("zeta", "alpha"):
            (self.root / name).mkdir()
            (self.root / name / "tasks.md").write_text(tasks(name), encoding="utf-8")
        self.cfg_path = self.top / "cfg.json"
        self.cfg_path.write_text(json.dumps({"data_root": str(self.root)}), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def local(self, **data):
        (self.root / "pa.local.json").write_text(json.dumps(data), encoding="utf-8")

    def manifest(self, folder, **data):
        (self.root / folder / "area.json").write_text(json.dumps(data), encoding="utf-8")

    def reading(self):
        return next(a for a in AR.area_dirs(self.root, {})[0] if a.slug == "reading")

    def tool(self, script, *args):
        r = subprocess.run([sys.executable, str(TOOLS / script), "--config", str(self.cfg_path), *args],
                           capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=30)
        return r.returncode, r.stdout + r.stderr


class TestRegistry(Base):
    def test_without_a_list_every_folder_with_tasks_is_an_area(self):
        dirs, warns = AR.area_dirs(self.root, {})
        self.assertEqual([d.slug for d in dirs], ["alpha", "reading", "zeta"])
        self.assertEqual(warns, [])

    def test_the_list_sets_membership_and_order_and_may_point_outside_the_root(self):
        outside = self.top / "elsewhere" / "taxes"
        outside.mkdir(parents=True)
        (outside / "tasks.md").write_text(tasks("taxes"), encoding="utf-8")
        cfg = {"areas": [{"path": "zeta"}, {"path": str(outside)}, "alpha", {"path": "gone"}, {"nopath": 1},
                         {"path": str(self.top / "elsewhere" / ".." / "data" / "zeta")}]}
        dirs, warns = AR.area_dirs(self.root, cfg)
        self.assertEqual([d.slug for d in dirs], ["zeta", "taxes", "alpha"])
        self.assertEqual(dirs[1].path, outside)
        self.assertTrue(any("gone" in w and "does not exist" in w for w in warns), warns)
        self.assertTrue(any("areas[4] has no path" in w for w in warns), warns)
        self.assertTrue(any("share the folder name 'zeta'" in w for w in warns), warns)
        # the scanner follows the same list, in the same order
        self.assertEqual([a.name for a in due.load_areas(self.root, cfg)], ["zeta", "taxes", "alpha"])

    def test_listed_area_without_tasks_is_reported(self):
        (self.root / "empty").mkdir()
        warns = []
        areas = due.load_areas(self.root, {"areas": ["empty", "alpha"]}, warns)
        self.assertEqual([a.name for a in areas], ["alpha"])
        self.assertTrue(any("empty: no tasks.md" in w for w in warns), warns)

    def test_local_settings_are_merged_over_config(self):
        self.local(areas=["alpha"], stale_after_days=3)
        cfg = due.settings(self.root, {"data_root": str(self.root), "stale_after_days": 9})
        self.assertEqual((cfg["stale_after_days"], cfg["areas"]), (3, ["alpha"]))
        same = {"data_root": "x"}
        (self.root / "pa.local.json").unlink()
        self.assertIs(due.settings(self.root, same), same)   # nothing to merge: the caller's dict, unchanged

    def test_no_stale_check_exempts_an_area(self):
        areas = due.load_areas(self.root, {"areas": ["alpha"]}, [])
        far = date(2099, 2, 1)   # fixture was reviewed 2099-01-01
        self.assertTrue(due.is_stale(areas[0], far, 7))
        self.assertFalse(due.is_stale(areas[0], far, 7, frozenset({"alpha"})))

    def test_broken_local_settings_are_reported_not_fatal(self):
        (self.root / "pa.local.json").write_text("{not json", encoding="utf-8")
        code, out = self.tool("due.py", "--today", "2099-01-01")
        self.assertEqual(code, 0, out)
        self.assertIn("pa.local.json unreadable", out)

    def test_shipped_examples_are_valid(self):
        local = json.loads((SKILL / "pa.local.example.json").read_text(encoding="utf-8"))
        self.assertIsInstance(local["areas"], list)
        self.assertEqual(json.loads((SKILL / "config.example.json").read_text(encoding="utf-8")), {"data_root": "~/pa-data"})
        a = self.reading()
        self.assertEqual(a.warnings, [])
        self.assertTrue(a.inside(a.manifest["instructions"]))

    def test_the_example_area_carries_the_read_only_notice(self):
        for name in ("tasks.md", "done.md"):
            text = (TEMPLATE / name).read_text(encoding="utf-8")
            self.assertIn("**Do not edit this file.**", text, name)
            self.assertIn("do not start the assistant yourself", text, name)

    def test_inline_week_numbering(self):
        cfg = {"week_numbering": {"assumed_week1_monday": "2026-08-24", "verified": True}, "week_label": "Term"}
        self.assertEqual(due.week_number(date(2026, 9, 16), self.root, cfg), "Term week 4")
        self.assertIsNone(due.week_number(date(2026, 9, 16), self.root, {"week_numbering": {"x": 1}}))


class TestManifest(Base):
    def test_bad_manifests_are_reported_and_ignored(self):
        (self.root / "alpha" / "area.json").write_text("[1, 2", encoding="utf-8")
        self.manifest("zeta", pages=["not", "a", "dict"], colour="red")
        dirs = {d.slug: d for d in AR.area_dirs(self.root, {})[0]}
        self.assertEqual(dirs["alpha"].manifest, {})
        self.assertTrue(any("unreadable" in w for w in dirs["alpha"].warnings))
        self.assertNotIn("pages", dirs["zeta"].manifest)
        self.assertTrue(any("unknown key 'colour'" in w for w in dirs["zeta"].warnings))
        # and the scanner shows them
        code, out = self.tool("due.py", "--today", "2099-01-01")
        self.assertIn("alpha/area.json unreadable", out)

    def test_hook_commands_are_built_from_files_inside_the_area(self):
        steps, problems = AR.commands(self.reading(), "on_load")
        self.assertEqual(problems, [])
        self.assertEqual(steps[0]["argv"][0], sys.executable)
        self.assertEqual(Path(steps[0]["argv"][1]), (self.root / "reading" / "tools" / "readinglist.py").resolve())
        self.assertEqual(steps[0]["argv"][2:], ["summary"])
        self.assertEqual(steps[0]["cwd"], str(self.root / "reading"))

    def test_hooks_refuse_anything_outside_the_rules(self):
        (self.top / "evil.py").write_text("print('x')", encoding="utf-8")
        (self.root / "alpha" / "notes.txt").write_text("x", encoding="utf-8")
        self.manifest("alpha", on_load=[
            {"script": "../../evil.py"},                    # outside the area
            {"script": "notes.txt"},                        # not python
            {"script": "missing.py"},                       # not there
            {"script": "tools/x.py; rm -rf /"},             # a shell string is just a (missing) file name
            {"script": "notes.txt", "args": "--all"},       # args must be a list
            "python evil.py",                               # not an object
        ])
        alpha = next(a for a in AR.area_dirs(self.root, {})[0] if a.slug == "alpha")
        steps, problems = AR.commands(alpha, "on_load")
        self.assertEqual(steps, [])
        self.assertEqual(len(problems), 6)

    def test_hooks_cli_prints_runnable_commands_and_the_scanner_points_at_it(self):
        code, out = self.tool("areas.py", "hooks", "on_load")
        self.assertEqual(code, 0, out)
        self.assertIn("# reading: how many items are waiting to be read", out)
        self.assertIn("readinglist.py summary", out)
        self.assertTrue(any(l.startswith("(cd ") and " && " in l for l in out.splitlines()), out)
        step = AR.commands(self.reading(), "on_load")[0][0]   # the same step, run the way /pa would
        run = subprocess.run(step["argv"], cwd=step["cwd"], capture_output=True, text=True, encoding="utf-8",
                             env=ENV, timeout=30)
        self.assertIn("Reading list: unread 1", run.stdout, run.stderr)
        _, out = self.tool("due.py", "--today", "2099-01-01")
        self.assertIn("Area steps to run before the briefing (reading)", out)
        _, out = self.tool("areas.py", "hooks", "review")
        self.assertIn("readinglist.py list", out)
        _, out = self.tool("areas.py", "show", "reading")
        self.assertIn("instructions:", out)
        self.assertIn("page: /area/reading/list", out)

    def test_links(self):
        self.manifest("zeta", links=[{"label": "Undeclared page", "page": "nope"},
                                     {"label": "Escape", "file": "../reading/notes.md"},
                                     {"label": "Mail", "url": "mailto:x@y.z"},
                                     {"label": "Site", "url": "https://example.com/"},
                                     {"page": "no label"}])
        dirs = AR.area_dirs(self.root, {})[0]
        links, problems = AR.links(dirs, self.root, {"links": [{"label": "Global", "url": "https://example.org/"}]})
        got = [(l["id"], l["kind"], l["href"]) for l in links]
        self.assertEqual(got, [("reading/list", "page", "/area/reading/list"),
                               ("reading/notes", "doc", "/doc/reading/notes"),
                               ("zeta/3", "url", "https://example.com/"),
                               ("_/0", "url", "https://example.org/")])
        self.assertEqual(len(problems), 4, problems)

    def test_handler_loading(self):
        mod, problem = AR.load_handler(self.reading())
        self.assertIsNone(problem)
        self.assertTrue(callable(mod.handle))
        cases = {
            "tools/none.py": ("def other():\n    pass\n", "no handle(request)"),
            "tools/broken.py": ("def handle(:\n", "failed to load"),
            "tools/raises.py": ("raise RuntimeError('boom')\n", "RuntimeError: boom"),
        }
        for rel, (code, expect) in cases.items():
            (self.root / "zeta" / "tools").mkdir(exist_ok=True)
            (self.root / "zeta" / rel).write_text(code, encoding="utf-8")
            self.manifest("zeta", handler=rel)
            zeta = next(a for a in AR.area_dirs(self.root, {})[0] if a.slug == "zeta")
            mod, problem = AR.load_handler(zeta)
            self.assertIsNone(mod)
            self.assertIn(expect, problem)
        self.manifest("zeta", handler="../reading/tools/widget_api.py")
        zeta = next(a for a in AR.area_dirs(self.root, {})[0] if a.slug == "zeta")
        self.assertIn("inside the area folder", AR.load_handler(zeta)[1])
        self.assertEqual(AR.load_handler(next(a for a in AR.area_dirs(self.root, {})[0] if a.slug == "alpha")),
                         (None, None))


class TestServer(Base):
    def setUp(self):
        super().setUp()
        (self.root / "zeta" / "tools").mkdir()
        (self.root / "zeta" / "tools" / "api.py").write_text(
            "def handle(req):\n    raise ValueError('zeta is broken')\n", encoding="utf-8")
        (self.root / "alpha" / "tools").mkdir()
        (self.root / "alpha" / "tools" / "api.py").write_text("def handle(:\n", encoding="utf-8")
        self.manifest("zeta", handler="tools/api.py")
        self.manifest("alpha", handler="tools/api.py")
        self.cfg = {"data_root": str(self.root)}
        self.srv = pa_widget.make_server(self.root, self.cfg, 0)
        self.port = self.srv.server_address[1]
        self.key = Store(self.root).key()
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); super().tearDown()

    def req(self, path, body=None, key=True):
        headers = {"Content-Type": "application/json"}
        if key:
            headers["X-PA-Key"] = self.key
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers,
                                   data=json.dumps(body).encode() if body is not None else None,
                                   method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def items(self):
        return json.loads((self.root / "reading" / "items.json").read_text(encoding="utf-8"))["items"]

    def test_pages_are_served_only_when_declared(self):
        code, body = self.req("/area/reading/list", key=False)
        self.assertEqual(code, 200)
        self.assertIn(b"<title>Reading list", body)
        for path in ("/area/reading/nope", "/area/nobody/list", "/area/reading/", "/area/reading/list/x",
                     "/area/reading/..%2Fitems.json", "/area/reading/items.json"):
            self.assertEqual(self.req(path, key=False)[0], 404, path)

    def test_area_api_goes_through_the_handler_and_needs_the_key(self):
        self.assertEqual(self.req("/area/reading/api/items", key=False)[0], 403)
        self.assertEqual(self.req("/area/reading/api/items", {"title": "x", "url": "https://x.test/"}, key=False)[0], 403)
        code, body = self.req("/area/reading/api/items")
        self.assertEqual((code, len(json.loads(body)["items"])), (200, 2))
        code, body = self.req("/area/reading/api/items", {"title": "New one", "url": "https://x.test/new"})
        self.assertEqual(code, 200)
        new_id = json.loads(body)["item"]["id"]
        self.assertEqual(self.items()[-1]["title"], "New one")
        self.assertEqual(self.req("/area/reading/api/status", {"id": new_id, "status": "done"})[0], 200)
        self.assertEqual(self.items()[-1]["status"], "done")
        code, body = self.req("/area/reading/api/status", {"id": new_id, "status": "shredded"})
        self.assertEqual(code, 400)
        self.assertIn("status must be", json.loads(body)["error"])
        self.assertEqual(self.req("/area/reading/api/nowhere")[0], 404)

    def test_a_failing_handler_only_affects_its_own_area(self):
        code, body = self.req("/area/zeta/api/anything")
        self.assertEqual(code, 500)
        self.assertIn("zeta is broken", json.loads(body)["error"])
        self.assertEqual(self.req("/area/alpha/api/anything")[0], 404)       # failed to load → no routes
        self.assertEqual(self.req("/area/nobody/api/x")[0], 404)
        self.assertEqual(self.req("/area/reading/api/items")[0], 200)        # others are fine
        code, body = self.req("/api/data")
        data = json.loads(body)
        self.assertEqual(code, 200)
        self.assertTrue(any("alpha: handler failed to load" in w for w in data["warnings"]), data["warnings"])

    def test_handlers_are_loaded_once_at_start(self):
        (self.root / "zeta" / "tools" / "api.py").write_text(
            "def handle(req):\n    return 200, {'changed': True}\n", encoding="utf-8")
        time.sleep(0.05)
        self.assertEqual(self.req("/area/zeta/api/x")[0], 500)               # still the old code until a restart

    def test_links_and_docs(self):
        code, body = self.req("/api/data")
        links = json.loads(body)["links"]
        self.assertEqual([(l["label"], l["kind"], l["href"]) for l in links],
                         [("Reading list", "page", "/area/reading/list"),
                          ("Reading notes", "doc", "/doc/reading/notes")])
        self.assertEqual(self.req("/doc/reading/notes", key=False)[0], 403)
        code, body = self.req(f"/doc/reading/notes?k={self.key}", key=False)
        self.assertEqual(code, 200)
        self.assertIn(b"Reading notes", body)
        self.assertEqual(self.req(f"/doc/reading/list?k={self.key}", key=False)[0], 404)   # a page is not a doc

    def test_fixed_labels_come_from_the_schedule(self):
        sched = {"categories": {"job": {"label": "Day job", "color": "#777777", "fixed": True},
                                "deep": {"label": "Deep work", "color": "#b7791f"}},
                 "blocks": [{"id": "j", "days": ["mon"], "start": "09:00", "end": "17:00", "title": "Job", "category": "job"}]}
        (self.root / "schedule.json").write_text(json.dumps(sched), encoding="utf-8")
        self.cfg["schedule_file"] = "schedule.json"
        data = json.loads(self.req("/api/data")[1])
        self.assertEqual(data["schedule"]["fixed_labels"], ["Day job"])

    def test_week_reads_done_logs_of_listed_areas_only(self):
        (self.root / "reading" / "done.md").write_text("- 2026-09-08 | Read a thing | good\n", encoding="utf-8")
        (self.root / "stray").mkdir()
        (self.root / "stray" / "done.md").write_text("- 2026-09-08 | Not an area | x\n", encoding="utf-8")
        data = json.loads(self.req("/api/week?start=2026-09-07")[1])
        self.assertEqual([e["text"] for e in data["done"]], ["Read a thing"])


if __name__ == "__main__":
    unittest.main()
