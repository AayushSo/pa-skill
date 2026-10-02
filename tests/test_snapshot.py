"""Tests for tools/snapshot.py: what goes into a snapshot, when one is due, the integrity check, pruning,
diff and restore, and crash-safe writes. Synthetic fixtures only."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import due  # noqa: E402
import snapshot as S  # noqa: E402

ENV = {"PYTHONIOENCODING": "utf-8", "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
NL = chr(10)

TASKS = NL.join([
    "# Tasks — home",
    "Last reviewed: 2099-01-01",
    "- [ ] Pay rent | due:2099-01-05 | id:rent1",
    "- [ ] Weekly check | every:mon | next:2099-01-05 | id:week1",
    "- [ ] Call the bank | due:2099-01-06 | id:bank1",
    "- [ ] Tidy desk | id:desk1",
    "- [ ] Water plants | id:plnt1",
    "",
])
DONE = NL.join(["# Done — home", "- 2098-12-30 | Old thing | fine | id:old01", "- 2098-12-31 | Other | ok", ""])


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.top = Path(self._tmp.name)
        self.root = self.top / "data"
        home = self.root / "home"
        home.mkdir(parents=True)
        (home / "tasks.md").write_text(TASKS, encoding="utf-8")
        (home / "done.md").write_text(DONE, encoding="utf-8")
        (home / "notes.md").write_text("notes", encoding="utf-8")
        (home / "photo.png").write_bytes(b"\x89PNG....")
        (home / "tools").mkdir()
        (home / "tools" / ".creds").write_text("SECRET", encoding="utf-8")
        (home / "tools" / "sweep.py").write_text("print(1)", encoding="utf-8")
        (home / "big.txt").write_bytes(b"x" * (S.MAX_BYTES + 1))
        w = self.root / "_widget"
        w.mkdir()
        (w / "key").write_text("KEY", encoding="utf-8")
        (w / "details.json").write_text("{}", encoding="utf-8")
        (w / "server.log").write_text("log", encoding="utf-8")
        self.cfg = {"data_root": str(self.root)}
        self.cfg_path = self.top / "cfg.json"
        self.cfg_path.write_text(json.dumps(self.cfg), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def tool(self, *args):
        r = subprocess.run([sys.executable, str(TOOLS / "snapshot.py"), "--config", str(self.cfg_path), *args],
                           capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=30)
        return r.returncode, r.stdout + r.stderr

    def edit(self, rel, old, new):
        p = self.root / rel
        text = p.read_text(encoding="utf-8")
        self.assertIn(old, text)
        p.write_text(text.replace(old, new, 1), encoding="utf-8")


class TestTake(Base):
    def test_contents(self):
        path, n = S.take(self.root, self.cfg, "manual")
        names = set(zipfile.ZipFile(path).namelist())
        self.assertEqual(names, {"home/tasks.md", "home/done.md", "home/notes.md", "home/tools/sweep.py",
                                 "_widget/details.json"})
        self.assertEqual(n, 5)
        self.assertTrue(S.NAME_RE.match(path.name))

    def test_excludes_and_outside_areas(self):
        outside = self.top / "elsewhere" / "taxes"
        outside.mkdir(parents=True)
        (outside / "tasks.md").write_text("# Tasks — taxes" + NL, encoding="utf-8")
        cfg = {**self.cfg, "areas": ["home", str(outside)], "snapshot_exclude": ["home/tools/*"]}
        path, _ = S.take(self.root, cfg, "manual")
        names = set(zipfile.ZipFile(path).namelist())
        self.assertIn("_external/taxes/tasks.md", names)
        self.assertNotIn("home/tools/sweep.py", names)

    def test_same_minute_does_not_overwrite(self):
        now = datetime(2099, 1, 1, 9, 0, 0)
        a, _ = S.take(self.root, self.cfg, "bulk", now)
        b, _ = S.take(self.root, self.cfg, "bulk", now)
        self.assertNotEqual(a, b)
        self.assertEqual(len(S.snapshots(self.root, self.cfg)), 2)


class TestSchedule(Base):
    def test_due_notice(self):
        today = date(2099, 1, 10)
        self.assertIn("none yet", S.due_notice(self.root, self.cfg, today))
        S.take(self.root, self.cfg, "manual", datetime(2099, 1, 5, 8, 0))
        self.assertIsNone(S.due_notice(self.root, self.cfg, today))
        self.assertIn("last 2099-01-05", S.due_notice(self.root, self.cfg, date(2099, 1, 12)))
        self.assertIsNone(S.due_notice(self.root, {**self.cfg, "snapshot_every_days": 14}, date(2099, 1, 12)))
        self.assertIsNone(S.due_notice(self.root, {**self.cfg, "snapshot_every_days": 0}, date(2099, 1, 12)))

    def test_briefing_mentions_it_only_when_due(self):
        r = subprocess.run([sys.executable, str(TOOLS / "due.py"), "--config", str(self.cfg_path)],
                           capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=30)
        self.assertIn("Snapshot + integrity check due (none yet)", r.stdout)
        S.take(self.root, self.cfg, "manual")
        r = subprocess.run([sys.executable, str(TOOLS / "due.py"), "--config", str(self.cfg_path)],
                           capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=30)
        self.assertNotIn("Snapshot", r.stdout)

    def test_weekly_runs_only_when_due(self):
        code, out = self.tool("weekly")
        self.assertEqual(code, 0, out)
        self.assertIn("Snapshot ", out)
        self.assertNotIn("Checked against", out)             # nothing to compare with the first time
        code, out = self.tool("weekly")
        self.assertIn("Snapshot not due", out)
        self.assertEqual(len(S.snapshots(self.root, self.cfg)), 1)

    def test_prune(self):
        for day in range(1, 31):
            S.take(self.root, self.cfg, "weekly", datetime(2099, 1, day, 8, 0))
        for i in range(12):
            S.take(self.root, self.cfg, "bulk", datetime(2099, 2, 1, 8, i))
        gone = S.prune(self.root, {**self.cfg, "snapshot_keep": 26})
        left = [p.name for p in S.snapshots(self.root, self.cfg)]
        self.assertEqual(len(gone), 4 + 2)
        self.assertEqual(sum("weekly" in n for n in left), 26)
        self.assertEqual(sum("bulk" in n for n in left), 10)
        self.assertTrue(left[0].startswith("2099-01-05"))       # the oldest scheduled ones went first


class TestCheck(Base):
    def setUp(self):
        super().setUp()
        self.snap, _ = S.take(self.root, self.cfg, "weekly")

    def findings(self):
        return S.check(self.root, self.cfg, self.snap)

    def test_normal_work_is_not_flagged(self):
        # a task finished and logged with its id, one logged by text only, a recurrence advanced, one added
        self.edit("home/tasks.md", "- [ ] Pay rent | due:2099-01-05 | id:rent1" + NL, "")
        self.edit("home/tasks.md", "- [ ] Tidy desk | id:desk1" + NL, "")
        self.edit("home/tasks.md", "next:2099-01-05 | id:week1", "next:2099-01-12 | id:week1")
        self.edit("home/tasks.md", "- [ ] Water plants", "- [ ] New thing | id:new01" + NL + "- [ ] Water plants")
        with open(self.root / "home" / "done.md", "a", encoding="utf-8") as f:
            f.write("- 2099-01-05 | Pay rent | paid | id:rent1" + NL + "- 2099-01-05 | Tidy desk | done" + NL)
        self.assertEqual(self.findings(), [])

    def test_done_log_edits_are_flagged(self):
        self.edit("home/done.md", "| Old thing | fine |", "| Old thing | rewritten |")
        f = self.findings()
        self.assertEqual(len(f), 1, f)
        self.assertIn("home/done.md:2 changed above the end", f[0])
        self.assertIn("rewritten", f[0])

    def test_done_log_removals_are_flagged(self):
        self.edit("home/done.md", "- 2098-12-31 | Other | ok" + NL, "")
        self.assertIn("done.md:3 changed above the end", self.findings()[0])
        (self.root / "home" / "done.md").unlink()
        self.assertIn("was in the snapshot, now missing", self.findings()[0])

    def test_line_endings_alone_are_not_a_change(self):
        p = self.root / "home" / "done.md"
        p.write_bytes(DONE.replace(NL, chr(13) + NL).encode("utf-8"))    # CRLF
        self.assertEqual(self.findings(), [])
        p.write_bytes(DONE.encode("utf-8"))                              # LF
        self.assertEqual(self.findings(), [])

    def test_vanished_redated_reworded(self):
        self.edit("home/tasks.md", "- [ ] Call the bank | due:2099-01-06 | id:bank1" + NL, "")
        self.edit("home/tasks.md", "Pay rent | due:2099-01-05", "Pay the rent | due:2099-01-20")
        self.edit("home/tasks.md", "next:2099-01-05 | id:week1", "next:2098-12-29 | id:week1")
        f = self.findings()
        self.assertEqual(len(f), 4, f)
        text = NL.join(f)
        self.assertIn("task bank1 disappeared without a done.md record", text)
        self.assertIn("task rent1 due 2099-01-05 → 2099-01-20", text)
        self.assertIn("task rent1 reworded", text)
        self.assertIn("task week1 next 2099-01-05 → 2098-12-29", text)   # backwards is not a normal advance

    def test_cli_check_diff_restore(self):
        self.edit("home/done.md", "| Old thing | fine |", "| Old thing | rewritten |")
        code, out = self.tool("check")
        self.assertIn("1 finding(s)", out)
        code, out = self.tool("diff", self.snap.name, "home/done.md")
        self.assertIn("-- 2098-12-30 | Old thing | fine", out)
        self.assertIn("+- 2098-12-30 | Old thing | rewritten", out)
        code, out = self.tool("restore", self.snap.name, "home/done.md")
        self.assertEqual(code, 0, out)
        restored = self.root / "home" / "done.md.restored"
        self.assertEqual(restored.read_text(encoding="utf-8"), DONE)
        self.assertIn("rewritten", (self.root / "home" / "done.md").read_text(encoding="utf-8"))  # untouched
        self.assertEqual(self.tool("restore", self.snap.name, "home/done.md")[0], 1)            # never overwrites
        self.assertEqual(self.tool("diff", self.snap.name, "home/nope.md")[0], 1)
        self.assertEqual(self.tool("check", "--against", "nope.zip")[0], 1)

    def test_weekly_reports_findings_against_the_previous_snapshot(self):
        self.edit("home/done.md", "| Other | ok", "| Other | changed")
        old = datetime.now() - timedelta(days=8)
        renamed = self.snap.with_name(f"{old:%Y-%m-%d_%H%M%S}_weekly.zip")
        self.snap.rename(renamed)
        code, out = self.tool("weekly")
        self.assertIn(f"Checked against {renamed.name}: 1 finding(s)", out)
        self.assertIn("done.md:3 changed above the end", out)
        self.assertEqual(len(S.snapshots(self.root, self.cfg)), 2)


class TestAtomicWrite(unittest.TestCase):
    def test_replaces_whole_file_and_leaves_no_temp(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "tasks.md"
            p.write_text("old", encoding="utf-8")
            due.atomic_write(p, b"new")
            self.assertEqual(p.read_text(encoding="utf-8"), "new")
            self.assertEqual([x.name for x in Path(d).iterdir()], ["tasks.md"])


if __name__ == "__main__":
    unittest.main()
