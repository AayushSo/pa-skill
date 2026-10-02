"""Read the per-area done.md logs, so finished work can be shown alongside the schedule.

A line looks like:

    - 2026-09-15 | Task text | What came of it | added:2026-09-13 | owner:assistant | id:ab12

The date may carry an approximation marker the agent used when the real day was unknown
(`≤2026-09-12`, `~2026-09-07`); those are kept and flagged rather than dropped. The outcome and the
trailing `added:` / `owner:` / `id:` fields are optional — older entries have neither. A line whose outcome begins
with "dropped" is a task that was abandoned rather than completed.

done.md is append-only and written by /pa; nothing here ever writes to it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

LINE_RE = re.compile(r"^\s*-\s*(?P<approx>[≤<~]?)\s*(?P<date>\d{4}-\d{2}-\d{2})\s*\|\s*(?P<rest>.+?)\s*$")
FIELD_RE = re.compile(r"^(added|id|owner):(.+)$", re.IGNORECASE)


@dataclass
class Entry:
    area: str
    folder: str
    day: date
    approx: bool          # the date was recorded as "on or before" / "about"
    text: str
    outcome: str
    dropped: bool
    added: date | None
    task_id: str | None
    line: int
    owner: str = "me"

    @property
    def took(self) -> int | None:
        return (self.day - self.added).days if self.added else None

    def as_json(self) -> dict:
        return {"area": self.area, "folder": self.folder, "date": self.day.isoformat(), "approx": self.approx,
                "text": self.text, "outcome": self.outcome, "dropped": self.dropped,
                "added": self.added.isoformat() if self.added else None, "task_id": self.task_id,
                "took": self.took, "line": self.line, "owner": self.owner}


def parse_file(path: Path, area: str, warnings: list[str] | None = None) -> list[Entry]:
    from due import read_tasks_file  # same decoding rules as tasks.md (BOM, UTF-16, ANSI)
    warns: list[str] = [] if warnings is None else warnings
    out: list[Entry] = []
    folder = path.parent.name
    for i, raw in enumerate(read_tasks_file(path, warns).splitlines(), 1):
        if not raw.lstrip().startswith("- "):
            continue  # headings and the format note at the top of each file
        m = LINE_RE.match(raw)
        if not m:
            warns.append(f"{folder}/done.md:{i} no date at the start — not counted as finished work")
            continue
        parts = [p.strip() for p in m.group("rest").split(" | ")]
        text, outcome, added, task_id, owner = parts[0], "", None, None, "me"
        for p in parts[1:]:
            f = FIELD_RE.match(p)
            if not f:
                outcome = (outcome + " | " + p).strip(" |") if outcome else p
                continue
            key, val = f.group(1).lower(), f.group(2).strip()
            if key == "id":
                task_id = val
            elif key == "owner":
                owner = val.lower()
            else:
                try:
                    added = date.fromisoformat(val)
                except ValueError:
                    warns.append(f"{folder}/done.md:{i} bad added date {val!r}")
        try:
            day = date.fromisoformat(m.group("date"))
        except ValueError:
            continue
        low = outcome.lower().lstrip("*_ ")
        out.append(Entry(area=area, folder=folder, day=day, approx=bool(m.group("approx")), text=text,
                         outcome=outcome, dropped=low.startswith("dropped"), added=added,
                         task_id=task_id, line=i, owner=owner))
    return out


def load_all(root: Path, area_names: dict[str, str] | None = None, warnings: list[str] | None = None,
             folders: list[Path] | None = None) -> list[Entry]:
    """Every area's done.md, newest first. area_names maps folder → display name.

    `folders` lists the area folders (from the registry); without it, every <root>/*/done.md is read.
    """
    entries: list[Entry] = []
    paths = [f / "done.md" for f in folders] if folders is not None else sorted(root.glob("*/done.md"))
    for path in paths:
        if not path.is_file():
            continue
        folder = path.parent.name
        entries += parse_file(path, (area_names or {}).get(folder, folder), warnings)
    entries.sort(key=lambda e: (e.day, e.area), reverse=True)
    return entries


def between(entries: list[Entry], start: date, end: date) -> list[Entry]:
    return [e for e in entries if start <= e.day <= end]
