#!/usr/bin/env python3
"""Export every task — open and finished — as one CSV table to read in a spreadsheet.

    python export_csv.py                      all tasks → <data_root>/<export_dir>/tasks-YYYY-MM-DD.csv
    python export_csv.py --open               only open tasks
    python export_csv.py --done --since 2026-01-01
    python export_csv.py --area fitness
    python export_csv.py --out path.csv       write somewhere else ("-" prints to stdout)

One row per task, newest work last: open tasks first (by area, then by the date they are due — or the next
occurrence for a recurring task, whose Due column is empty), then finished ones by the day they closed. Written as UTF-8 with a byte-order mark so Excel shows dashes and accents correctly; dates stay ISO
(YYYY-MM-DD) so they sort. A "Closed" date starting with "≈" was recorded as approximate ("on or before", "about").

This is a one-way reference copy. Nothing reads it back: edits made in the spreadsheet are never imported, since
tasks.md and done.md are the only record (see SKILL.md). Exports are left out of snapshots.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import donelog  # noqa: E402
import due  # noqa: E402

COLUMNS = ["Area", "Status", "Title", "Task", "Due", "Start", "Repeats", "Next", "Waiting on", "Chase", "Owner", "Owes",
           "Added", "Closed", "Days", "Outcome", "Details in", "Where", "Id"]
STATUS = {"overdue": "overdue", "today": "due today", "soon": "due soon", "later": "later", "undated": "undated",
          "waiting": "waiting", "chase": "follow up", "deferred": "not started", "unknown": "status unknown"}


def days_between(a: str | None, b: str | None) -> str:
    try:
        return str((date.fromisoformat(b) - date.fromisoformat(a)).days)
    except (TypeError, ValueError):
        return ""


def open_rows(root: Path, cfg: dict, today: date, area: str | None) -> list[list[str]]:
    areas = due.load_areas(root, cfg)
    buckets = due.classify(areas, today, today + timedelta(days=int(cfg.get("widget_days", 7))))
    rows, order = [], []
    for a in areas:
        if area and area.lower() not in (a.name.lower(), a.path.parent.name.lower()):
            continue
        for t in a.tasks:
            if t.mark == "x":
                continue
            f = t.fields
            when = f.get("due") or f.get("next") or ""      # a recurring task sorts by its next occurrence
            order.append((a.name.lower(), when == "", when, t.text.lower()))
            rows.append([
                a.name, STATUS.get(due.bucket_of(t, buckets, today), "open"), t.title, t.text,
                f.get("due", ""), f.get("start", ""), f.get("every", ""), f.get("next", ""), f.get("waiting", ""),
                f.get("chase", ""), t.owner, f.get("yields", ""), f.get("added", ""), "",
                days_between(f.get("added"), today.isoformat()), "", f.get("ref", ""),
                f"{a.path.parent.name}/tasks.md:{t.line}", t.id or "",
            ])
    return [r for _, r in sorted(zip(order, rows), key=lambda pair: pair[0])]


def done_rows(root: Path, cfg: dict, area: str | None, since: date | None) -> list[list[str]]:
    areas = due.load_areas(root, cfg)
    names = {a.path.parent.name: a.name for a in areas}
    entries = donelog.load_all(root, names, [], [a.path.parent for a in areas])
    rows = []
    for e in sorted(entries, key=lambda e: (e.day, e.area)):
        if area and area.lower() not in (e.area.lower(), e.folder.lower()):
            continue
        if since and e.day < since:
            continue
        rows.append([
            e.area, "dropped" if e.dropped else "done", due.short_title(e.text), e.text,
            "", "", "", "", "", "", e.owner, "",
            e.added.isoformat() if e.added else "",
            ("≈ " if e.approx else "") + e.day.isoformat(),
            "" if e.took is None else str(e.took), e.outcome, "",
            f"{e.folder}/done.md:{e.line}", e.task_id or "",
        ])
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root"); ap.add_argument("--config")
    ap.add_argument("--open", action="store_true", help="only open tasks")
    ap.add_argument("--done", action="store_true", help="only finished tasks")
    ap.add_argument("--area")
    ap.add_argument("--since", type=date.fromisoformat, help="finished on or after this date")
    ap.add_argument("--today", type=date.fromisoformat, default=date.today())
    ap.add_argument("--out", help='file to write ("-" for stdout)')
    args = ap.parse_args()
    cfg, _ = due.load_config(args.config)
    root = due.resolve_root(args.root, cfg)
    if not root:
        print("No data root: set data_root in config.json or pass --root.")
        return 2
    cfg = due.settings(root, cfg)

    rows = []
    if not args.done:
        rows += open_rows(root, cfg, args.today, args.area)
    if not args.open:
        rows += done_rows(root, cfg, args.area, args.since)

    if args.out == "-":
        w = csv.writer(sys.stdout, lineterminator="\n")
        w.writerow(COLUMNS)
        w.writerows(rows)
        return 0
    if args.out:
        path = Path(args.out)
    else:
        path = root / cfg.get("export_dir", "_exports") / f"tasks-{args.today.isoformat()}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        w.writerows(rows)
    kinds = sum(1 for r in rows if r[1] in ("done", "dropped"))
    print(f"{path}: {len(rows)} row(s) — {len(rows) - kinds} open, {kinds} finished.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
