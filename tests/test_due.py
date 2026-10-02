"""Regression tests for tools/due.py. Synthetic fixtures only — never point these at real data.

Run from the skill folder:  python -m unittest discover -s tests
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

DUE = Path(__file__).resolve().parent.parent / "tools" / "due.py"
TODAY = "2026-09-14"  # a Monday
sys.path.insert(0, str(DUE.parent))
import due  # noqa: E402  (most tests run the script; the title helpers are called directly)


class DueTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def area(self, folder, text, encoding="utf-8", raw=None):
        (self.root / folder).mkdir(parents=True, exist_ok=True)
        (self.root / folder / "tasks.md").write_bytes(raw if raw is not None else text.encode(encoding))

    def run_due(self, *args, timeout=10):
        # --config points at a non-existent file so a real local config is never picked up
        cmd = [sys.executable, str(DUE), "--today", TODAY, "--root", str(self.root),
               "--config", str(self.root / "no-config.json"), *args]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                           env={"PYTHONIOENCODING": "utf-8", "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")})
        return r.returncode, r.stdout + r.stderr

    @staticmethod
    def section(out, title):
        """Lines of one briefing section (between its header and the next blank line)."""
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


HDR = "# Tasks — {n}\nLast reviewed: 2026-09-14\n"


class TestBucketing(DueTestCase):
    def setUp(self):
        super().setUp()
        self.area("alpha", HDR.format(n="alpha") + """
- [ ] T01 overdue | due:2026-09-09
- [ ] T02 today | due:2026-09-14
- [ ] T03 edge of window | due:2026-09-21
- [ ] T04 just outside | due:2026-09-22
- [?] T05 unknown and overdue | due:2026-09-01
- [?] T06 unknown undated
- [ ] T07 waiting undated | waiting:someone
- [ ] T08 waiting but overdue | due:2026-09-10 | waiting:someone
- [ ] T09 future start | start:2026-09-20 | due:2026-09-25
- [ ] T10 recurring overdue two cycles | every:sun | next:2026-08-30
- [x] T11 checked but not moved
- [ ] T12 newly active and waiting | start:2026-09-12 | waiting:someone
- [ ] T13 far future | due:2026-12-01
- [ ] T14 start after due must not hide | start:2026-09-20 | due:2026-09-10
""")

    def test_overdue_captures_every_kind(self):
        _, out = self.run_due()
        overdue = self.section(out, "OVERDUE")
        for t in ("T01", "T05", "T08", "T10", "T14"):
            self.assertIn(t, overdue)

    def test_window_edges(self):
        _, out = self.run_due()
        soon = self.section(out, "DUE IN NEXT 7 DAYS")
        self.assertIn("T02", soon)
        self.assertIn("T03", soon)
        self.assertNotIn("T04", out.split("Dated beyond")[0])

    def test_future_start_hidden_in_briefing_but_listed_in_area(self):
        _, out = self.run_due()
        self.assertNotIn("T09", out)
        _, area_out = self.run_due("--area", "alpha")
        self.assertIn("T09", area_out)
        self.assertIn("[starts 2026-09-20]", area_out)

    def test_recurring_suggests_next_on_the_right_weekday(self):
        _, out = self.run_due()
        self.assertIn("after done → next:2026-09-20", out)  # 2026-09-20 is a Sunday

    def test_no_duplicate_listing(self):
        _, out = self.run_due()
        self.assertEqual(out.count("T12"), 1)

    def test_counts_and_housekeeping(self):
        _, out = self.run_due()
        self.assertIn("Dated beyond 7 days: 2", out)  # T04, T13
        self.assertIn("1 checked-off task(s)", out)
        self.assertIn("start 2026-09-20 is after due 2026-09-10", out)


class TestMalformedInput(DueTestCase):
    def test_zero_interval_does_not_hang(self):
        self.area("loop", HDR.format(n="loop") + "- [ ] Z zero | every:0d | next:2026-09-10\n")
        code, out = self.run_due(timeout=5)
        self.assertEqual(code, 0)
        self.assertIn("bad every '0d'", out)

    def test_warnings_instead_of_silent_drops(self):
        self.area("bad", HDR.format(n="bad") + """
- [ ] M1 bad month | due:2026-13-01
- [ ] M2 no separator due:2026-09-10
- [ ] M3 empty value | due:
- [ ] M4 invalid every | every:monday | next:2026-09-01
- [ ] M5 unknown field | note:hi
- [ ] M6 wrong weekday | every:sun | next:2026-09-05
""")
        _, out = self.run_due()
        for expected in ("bad due date '2026-13-01'", "field-like text", "empty due: value",
                         "bad every 'monday'", "unknown field 'note:hi'", "is a Sat, but every:sun"):
            self.assertIn(expected, out)

    def test_pipes(self):
        self.area("pipes", HDR.format(n="pipes") + """
- [ ] P1 compare A|B designs | due:2026-09-15
- [ ] P2 bare pipe before field|due:2026-09-10
- [ ] P3 one-sided |ref:x.md | due:2026-09-15
- [ ] P4 text |reference docs
""")
        _, out = self.run_due("--area", "pipes")
        self.assertIn("P1 compare A|B designs", out)
        self.assertIn("2026-09-10", out)          # P2 parsed
        self.assertIn("P4 text |reference docs", out)
        self.assertNotIn("unknown field", out)


class TestEncodings(DueTestCase):
    BODY = "# Tasks — {n}\nLast reviewed: 2026-09-14\n- [ ] {n} task overdue | due:2026-09-10\n"

    def check_overdue(self, name):
        _, out = self.run_due()
        self.assertIn(f"{name} task overdue", self.section(out, "OVERDUE"))

    def test_utf8_bom_keeps_header_name(self):
        self.area("folder", self.BODY.format(n="hdrname"), "utf-8-sig")
        _, out = self.run_due()
        self.assertIn("hdrname    hdrname task", out)

    def test_utf16_le_bom(self):
        self.area("u16", self.BODY.format(n="u16le"), "utf-16")
        self.check_overdue("u16le")

    def test_utf16_be_bom(self):
        self.area("u16be", "", raw=b"\xfe\xff" + self.BODY.format(n="u16be").encode("utf-16-be"))
        self.check_overdue("u16be")

    def test_utf16_no_bom(self):
        self.area("u16nobom", self.BODY.format(n="nobom"), "utf-16-le")
        self.check_overdue("nobom")

    def test_ansi_does_not_crash(self):
        self.area("ansi", self.BODY.format(n="café"), "cp1252")
        self.check_overdue("café")

    def test_crlf(self):
        self.area("crlf", self.BODY.format(n="crlf").replace("\n", "\r\n"))
        self.check_overdue("crlf")


class TestConfig(DueTestCase):
    def test_missing_root_and_config_is_a_clear_error(self):
        r = subprocess.run([sys.executable, str(DUE), "--config", str(self.root / "none.json")],
                           capture_output=True, text=True, encoding="utf-8",
                           env={"PYTHONIOENCODING": "utf-8", "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")})
        # falls back to the skill's own config.json if present, so only assert it never crashes
        self.assertIn(r.returncode, (0, 1, 2))
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_empty_root(self):
        code, out = self.run_due()
        self.assertEqual(code, 1)
        self.assertIn("No areas with a tasks.md found", out)

    def test_unknown_area(self):
        self.area("alpha", HDR.format(n="alpha") + "- [ ] a | due:2026-09-15\n")
        code, out = self.run_due("--area", "nope")
        self.assertEqual(code, 1)
        self.assertIn("No area 'nope'", out)


class TestTitles(unittest.TestCase):
    """The short name a task shows until it is expanded: its own title:, else one derived from the text."""

    def test_derives_from_the_first_clause_or_a_word_boundary(self):
        cases = {
            "Garden weekly: water the beds and check the hose": "Garden weekly",
            "Buy a bike lock (U-lock, not cable) — the old one was stolen": "Buy a bike lock (U-lock, not cable)",
            "Short enough to stand alone": "Short enough to stand alone",
            "Compare the three used road bikes listed this week at the campus co-op store":
                "Compare the three used road bikes listed this week at the…",
            "Return the overdue library books to the front desk (maths, physics, history)":
                "Return the overdue library books to the front desk…",     # never ends inside a bracket
        }
        for text, want in cases.items():
            self.assertEqual(due.short_title(text), want, text)
        self.assertLessEqual(len(due.short_title("x" * 200)), due.TITLE_MAX + 1)   # a word with no break
        self.assertFalse(due.short_title("Word " * 40).endswith(" …"))             # no space before the ellipsis

    def test_a_task_prefers_its_own_title(self):
        area = write_area(self.root, "home", [
            "- [ ] A very long piece of text that nobody wants to read in a list | title:Ring the bank | id:aaa1",
            "- [ ] No title here, so one is derived from the text of the task | id:aaa2",
        ])
        by_id = {t.id: t for t in due.parse_area(area).tasks}
        self.assertEqual(by_id["aaa1"].title, "Ring the bank")
        self.assertEqual(by_id["aaa1"].text, "A very long piece of text that nobody wants to read in a list")
        self.assertEqual(by_id["aaa2"].title, due.short_title(by_id["aaa2"].text))

    def test_an_over_long_title_warns(self):
        area = write_area(self.root, "home", ["- [ ] Text | title:" + "x" * 80 + " | id:aaa1"])
        parsed = due.parse_area(area)
        self.assertTrue(any("title is longer than" in w for w in parsed.warnings), parsed.warnings)
        self.assertEqual(parsed.tasks[0].title, "x" * 80)          # kept, but flagged

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


def write_area(root, name, lines):
    (root / name).mkdir(exist_ok=True)
    path = root / name / "tasks.md"
    path.write_text("# Tasks — " + name + chr(10) + "Last reviewed: 2099-01-01" + chr(10)
                    + chr(10).join(lines) + chr(10), encoding="utf-8")
    return path


if __name__ == "__main__":
    unittest.main()
