#!/usr/bin/env python3
"""/pa's side of the schedule.

    python schedulectl.py show [--date D] [--days N]   blocks per day (ids included, for `widgetctl.py plan`)
    python schedulectl.py check [--days N]             validate the file + overlaps + tasks due before their block
    python schedulectl.py week-kind <kind> [--week-of D]  set this (or D's) week's kind, e.g. cook / maintenance
    python schedulectl.py render [--date D]            rewrite the table between the markers in the schedule doc

The schedule file is config "schedule_file" (relative to data_root). The doc for `render` is config
"schedule_doc"; the table goes between <!-- schedule:begin --> and <!-- schedule:end -->.
Takes the same --config / --root options as due.py.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import due  # noqa: E402
import schedule as sch  # noqa: E402

BEGIN, END = "<!-- schedule:begin -->", "<!-- schedule:end -->"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--root"); ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("show"); p.add_argument("--date", type=date.fromisoformat); p.add_argument("--days", type=int, default=1)
    p = sub.add_parser("check"); p.add_argument("--days", type=int, default=7); p.add_argument("--today", type=date.fromisoformat)
    p = sub.add_parser("week-kind"); p.add_argument("kind"); p.add_argument("--week-of", type=date.fromisoformat)
    p = sub.add_parser("render"); p.add_argument("--date", type=date.fromisoformat)
    args = ap.parse_args()

    cfg, _ = due.load_config(args.config)
    root = due.resolve_root(args.root, cfg)
    cfg = due.settings(root, cfg) if root else cfg
    if not root or not cfg.get("schedule_file"):
        print("No schedule: set data_root in config.json and schedule_file in pa.local.json.")
        return 2
    path = root / cfg["schedule_file"]
    s = sch.load(path)

    if args.cmd == "show":
        start = args.date or date.today()
        for dd in sch.days(s, start, args.days):
            kind = f" · {dd['week_kind']} week" if dd["week_kind"] else ""
            print(f"{dd['date']} {dd['weekday'].title()}{kind}")
            for p in dd["phases"]:
                print(f"  [phase] {p['label']}")
            for e in dd["all_day"]:
                print(f"  [all day] {e['title']}")
            for b in dd["blocks"]:
                tag = "fixed" if b["fixed"] else "open "
                print(f"  {b['start']}–{b['end']}  {tag}  {b['title']}  ({b['label']})  id:{b['id']}")
            print()
        return 0

    if args.cmd == "check":
        problems = sch.validate(s)
        if not problems:
            today = args.today or date.today()
            tasks = [t for a in due.load_areas(root, cfg) for t in a.tasks if t.mark != "x"]
            problems = sch.clashes(s, tasks, today, args.days)
        print("\n".join(f"! {p}" for p in problems) if problems else "Schedule OK: no structural problems or clashes.")
        return 1 if problems else 0

    if args.cmd == "week-kind":
        wk = s.get("week_kinds")
        if not wk or args.kind not in wk.get("cycle", []):
            print(f"Unknown week kind '{args.kind}'. Known: {', '.join((wk or {}).get('cycle', [])) or '(none configured)'}")
            return 1
        monday = sch.week_monday(args.week_of or date.today())
        wk.setdefault("overrides", {})[monday.isoformat()] = args.kind
        # keep the file tidy: an override equal to what the cycle already gives is dropped
        without = dict(wk["overrides"]); without.pop(monday.isoformat())
        if sch.week_kind({"week_kinds": wk | {"overrides": without}}, monday) == args.kind:
            wk["overrides"] = without
        due.atomic_write(path, (json.dumps(s, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
        print(f"Week of {monday}: {args.kind}.")
        return 0

    if args.cmd == "render":
        doc_rel = cfg.get("schedule_doc")
        if not doc_rel:
            print("No schedule_doc in pa.local.json.")
            return 2
        doc = root / doc_rel
        text = doc.read_text(encoding="utf-8")
        if BEGIN not in text or END not in text:
            print(f"Markers {BEGIN} / {END} not found in {doc}.")
            return 1
        problems = sch.validate(s)
        if problems:
            print("\n".join(f"! {p}" for p in problems))
            return 1
        table = sch.render_table(s, args.date or date.today())
        head, rest = text.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
        due.atomic_write(doc, (head + BEGIN + "\n" + table + END + tail).encode("utf-8"))
        print(f"Rendered the schedule table into {doc_rel}.")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
