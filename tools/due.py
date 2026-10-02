#!/usr/bin/env python3
"""Scan every area's tasks.md and print what needs attention.

Used by the /pa skill as its first step, so the model reads ~50 lines instead of every file.
config.json (in the skill folder) names the data root; everything else local — which areas exist and where,
the schedule, widget settings — is in <data_root>/pa.local.json (see areas.py and pa.local.example.json).
Nothing about a particular user's setup lives in this file.

Task line format (one task per line, fields separated by " | "):

    - [ ] Text of the task | due:2026-09-16 | ref:notes.md | id:k3x9
    - [?] Status unknown — ask the user | ref:...
    - [ ] Recurring thing | every:mon | next:2026-09-14
    - [ ] Blocked thing | waiting:reply from a contact

Fields: due, start (hidden until this date), every (daily | mon..sun | Nd | Nw), next
(next occurrence of a recurring task), waiting (external blocker), chase (on a waiting task: the day to follow up
if nothing has come back — from then on it is listed under FOLLOW UP), ref (where detail lives),
id (stable identifier used by the widget; assigned by --assign-ids, never edited by hand),
at (HH:MM — a fixed time on the task's date, e.g. a call; shown in the widget's timeline),
yields (what the task owes you when it is done — a conclusion, a decision, a number and what it implies;
present only on tasks whose point is the information, not the doing),
added (the day the task was written — used for "how long did this take" once it reaches done.md),
owner (who does it: "me" — the default — or "assistant", for work the agent does itself when /pa runs),
title (a short name for the task — what the widget shows until the task is expanded; without one a short form is
derived from the text).

Usage:
    python due.py                     briefing: overdue, next 7 days, unconfirmed, follow up, waiting
    python due.py --days 14           widen the look-ahead window
    python due.py --area <name>       every open task in one area
    python due.py --json              machine-readable: every open task with its bucket
    python due.py --assign-ids        add id: to open tasks that lack one (edits tasks.md)
    python due.py --today 2026-09-20  pretend it is another day (testing)
    python due.py --root <dir>        scan <dir> instead of config data_root (testing)

Config lookup: --config PATH, else $PA_CONFIG, else config.json in the skill folder; then
<data_root>/pa.local.json is merged over it (its keys win).

Context file (config "context_file", default "context.md" in the data root): the few things the user must keep in
mind right now, one per line, each with an end date:

    - 2026-09-14 → 2026-09-20 | maintenance week; freed hours go to the closest paper

Active entries are printed at the top of the briefing; expired ones are flagged for removal.
Nudges (recorded by `widgetctl.py nudge`) show as "nudged N×" next to a task until it is done or re-dated.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
KNOWN_KEYS = {"due", "start", "every", "next", "waiting", "chase", "ref", "id", "at", "yields", "added", "owner", "title"}
CHASE_DAYS = 14  # default follow-up interval for a waiting task, when pa.local.json sets no chase_days
TITLE_MAX = 60
OWNERS = {"me", "assistant"}
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
TASK_RE = re.compile(r"^\s*- \[( |x|X|\?)\]\s+(.*)$")
EVERY_RE = re.compile(r"daily|mon|tue|wed|thu|fri|sat|sun|[1-9]\d*[dw]")
ID_RE = re.compile(r"[a-z0-9]{3,12}")
AT_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
ID_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o/1/l/i
FIELD_IN_TEXT_RE = re.compile(r"\b(due|start|next|every|waiting|chase):\S")
CONTEXT_RE = re.compile(r"^\s*-\s*(\d{4}-\d{2}-\d{2})\s*(?:→|->|–|—|to)\s*(\d{4}-\d{2}-\d{2})\s*\|\s*(.+?)\s*$")
SPLIT_RE = re.compile(r"\s+\|\s+|\s*\|\s*(?=(?:due|start|every|next|waiting|chase|ref|id|at|yields|added|owner|title)\s*:)", re.IGNORECASE)


@dataclass(eq=False)
class Task:
    area: str
    text: str
    mark: str  # " ", "x", "?"
    line: int
    fields: dict[str, str] = field(default_factory=dict)
    folder: str = ""

    def date_field(self, key: str) -> date | None:
        raw = self.fields.get(key)
        return date.fromisoformat(raw) if raw else None

    @property
    def when(self) -> date | None:
        return self.date_field("due") or self.date_field("next")

    @property
    def chase(self) -> date | None:
        """When to follow up on a waiting task if nothing has come back. Only meaningful with waiting:."""
        return self.date_field("chase") if "waiting" in self.fields else None

    @property
    def id(self) -> str | None:
        return self.fields.get("id")

    @property
    def owner(self) -> str:
        return self.fields.get("owner", "me")

    @property
    def title(self) -> str:
        """The short name to show: the task's own `title:`, else a short form of its text."""
        return self.fields.get("title") or short_title(self.text)


@dataclass
class Area:
    name: str
    path: Path
    reviewed: date | None
    tasks: list[Task]
    warnings: list[str]


def read_tasks_file(path: Path, warnings: list[str]) -> str:
    """Decode whatever Notepad / Notepad++ saved: UTF-8 (± BOM), UTF-16 (± BOM), or ANSI."""
    data = path.read_bytes()
    label = f"{path.parent.name}/tasks.md"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")  # the BOM tells the codec the byte order
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    if b"\x00" in data:  # UTF-16 without a BOM: ASCII chars carry a zero byte
        le = data[1::2].count(0) > data[0::2].count(0)
        warnings.append(f"{label} looks like UTF-16 without a byte-order mark — decoded as UTF-16; re-save as UTF-8")
        return data.decode("utf-16-le" if le else "utf-16-be", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        warnings.append(f"{label} is not UTF-8 (saved as ANSI?) — read as cp1252; re-save as UTF-8")
        return data.decode("cp1252", errors="replace")


CLAUSE_RE = re.compile(r"^(.{8,%d}?)(?:: | — | – | -- | → |; |\. )" % TITLE_MAX)


def short_title(text: str, limit: int = TITLE_MAX) -> str:
    """A short name derived from a task's text: its first clause, else a word-boundary trim."""
    text = text.strip()
    m = CLAUSE_RE.match(text)
    if m:
        return m.group(1).rstrip(" ,;:-—–")
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    if cut.count("(") > cut.count(")"):          # never end inside a bracket
        cut = cut[:cut.rfind("(")]
    cut = cut.rstrip(" ,;:-—–(")
    return (cut or text[:limit]) + "…"


def atomic_write(path: Path, data: bytes) -> None:
    """Write via a temporary file and a rename, so a crash never leaves a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def split_fields(body: str) -> list[str]:
    """Split on ' | ' always; on a bare '|' only when a field name follows ('A|B designs' stays text)."""
    return [p.strip() for p in SPLIT_RE.split(body)]


def parse_area(path: Path, content: str | None = None) -> Area:
    """Parse one tasks.md. `content` parses text from elsewhere (a snapshot) as if it were at `path`."""
    name = path.parent.name
    reviewed = None
    tasks: list[Task] = []
    warnings: list[str] = []
    if content is None:
        content = read_tasks_file(path, warnings)
    for i, raw in enumerate(content.splitlines(), 1):
        if raw.startswith("# Tasks"):
            m = re.search(r"[—-]\s*(\S+)\s*$", raw)
            if m:
                name = m.group(1)
        if raw.lower().startswith("last reviewed:"):
            m = re.search(r"\d{4}-\d{2}-\d{2}", raw)
            reviewed = date.fromisoformat(m.group(0)) if m else None
        m = TASK_RE.match(raw)
        if not m:
            continue
        where = f"{path.parent.name}/tasks.md:{i}"
        parts = split_fields(m.group(2))
        task = Task(area=name, text=parts[0], mark=m.group(1).lower(), line=i, folder=path.parent.name)
        if FIELD_IN_TEXT_RE.search(task.text):
            warnings.append(f"{where} field-like text with no ' | ' before it — task treated as undated")
        for part in parts[1:]:
            key, sep, val = part.partition(":")
            key = key.strip().lower()
            if not sep or key not in KNOWN_KEYS:
                warnings.append(f"{where} unknown field '{part}'")
                continue
            if not val.strip():
                warnings.append(f"{where} empty {key}: value")
                continue
            task.fields[key] = val.strip()
        if "at" in task.fields and not AT_RE.fullmatch(task.fields["at"]):
            warnings.append(f"{where} bad at '{task.fields['at']}' (HH:MM, 24-hour)")
            task.fields.pop("at")
        if "id" in task.fields and not ID_RE.fullmatch(task.fields["id"]):
            warnings.append(f"{where} bad id '{task.fields['id']}' (lowercase letters/digits, 3–12 chars)")
            task.fields.pop("id")
        if "title" in task.fields and len(task.fields["title"]) > TITLE_MAX:
            warnings.append(f"{where} title is longer than {TITLE_MAX} characters — shorten it")
        if "owner" in task.fields:
            task.fields["owner"] = task.fields["owner"].lower()
            if task.fields["owner"] not in OWNERS:
                warnings.append(f"{where} bad owner '{task.fields['owner']}' (use me or assistant) — treated as yours")
                task.fields.pop("owner")
        if "every" in task.fields and not EVERY_RE.fullmatch(task.fields["every"].lower()):
            warnings.append(f"{where} bad every '{task.fields['every']}' (use daily, mon..sun, Nd, Nw with N≥1)")
            task.fields.pop("every")
        for key in ("due", "start", "next", "added", "chase"):
            try:
                task.date_field(key)
            except ValueError:
                warnings.append(f"{where} bad {key} date '{task.fields[key]}'")
                task.fields.pop(key)
        if "every" in task.fields and "next" not in task.fields:
            warnings.append(f"{where} recurring task has no next: date")
        every, nxt = task.fields.get("every", "").lower(), task.date_field("next")
        if every in WEEKDAYS and nxt and nxt.weekday() != WEEKDAYS.index(every):
            warnings.append(f"{where} next {nxt} is a {nxt.strftime('%a')}, but every:{every}")
        if "chase" in task.fields and "waiting" not in task.fields:
            warnings.append(f"{where} chase: without waiting: — a chase date only applies while waiting on someone")
        start, due = task.date_field("start"), task.date_field("due")
        if start and due and start > due:
            warnings.append(f"{where} start {start} is after due {due}")
        tasks.append(task)
    for t in tasks:
        t.area = name
    return Area(name=name, path=path, reviewed=reviewed, tasks=tasks, warnings=warnings)


@dataclass
class Context:
    active: list[tuple[date, date, str]] = field(default_factory=list)
    upcoming: list[tuple[date, date, str]] = field(default_factory=list)
    expired: list[tuple[date, date, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def load_context(root: Path, cfg: dict, today: date) -> Context:
    ctx = Context()
    path = root / cfg.get("context_file", "context.md")
    if not path.is_file():
        return ctx
    for i, raw in enumerate(read_tasks_file(path, ctx.warnings).splitlines(), 1):
        if not raw.lstrip().startswith("- "):
            continue  # headings, blank lines and prose are allowed
        m = CONTEXT_RE.match(raw)
        try:
            start, until = (date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))) if m else (None, None)
        except ValueError:
            start = None
        if not m or not start or until < start:
            ctx.warnings.append(f"{path.name}:{i} not 'YYYY-MM-DD → YYYY-MM-DD | note' (or end before start)")
            continue
        entry = (start, until, m.group(3))
        (ctx.expired if until < today else ctx.upcoming if start > today else ctx.active).append(entry)
    return ctx


def nudge_date(t: Task) -> str | None:
    """The date a nudge count belongs to: the task's due/next date, else its chase date. Moving it resets the count."""
    d = t.when or t.chase
    return d.isoformat() if d else None


def nudge_counts(root: Path, cfg: dict, areas: list[Area]) -> dict[str, int]:
    """Nudge counts from the widget store, only for tasks not re-dated since. Empty if nothing recorded."""
    from widget_store import Store  # same folder; imported lazily so due.py stays usable on its own
    store = Store(root, cfg.get("widget_dir", "_widget"))
    if not store.nudges_path.exists():
        return {}
    when = {t.id: nudge_date(t) for a in areas for t in a.tasks if t.id and t.mark != "x"}
    return store.nudge_counts(when)


def last_runs(root: Path, cfg: dict, areas: list[Area]):
    """Newest done.md entry per task id — how a recurring task last went. {id: donelog.Entry}"""
    import donelog
    out = {}
    for e in donelog.load_all(root, None, [], [a.path.parent for a in areas]):  # newest first
        if e.task_id and e.task_id not in out:
            out[e.task_id] = e
    return out


def load_areas(root: Path, cfg: dict | None = None, warnings: list[str] | None = None) -> list[Area]:
    """Areas in the order the registry lists them (or every <root>/*/tasks.md when there is no list).

    Problems with the registry go to `warnings`; problems with one area's area.json go to that area's warnings.
    """
    from areas import area_dirs  # same folder; lazy so due.py's parsing stays importable on its own
    dirs, problems = area_dirs(root, cfg or {})
    if warnings is not None:
        warnings.extend(problems)
    out = []
    for d in dirs:
        if not d.tasks_file.is_file():
            if warnings is not None:
                warnings.extend(d.warnings)
            continue
        a = parse_area(d.tasks_file)
        a.warnings = d.warnings + a.warnings
        out.append(a)
    return out


def duplicate_id_warnings(areas: list[Area]) -> list[str]:
    seen: dict[str, str] = {}
    out = []
    for a in areas:
        for t in a.tasks:
            if not t.id:
                continue
            here = f"{a.path.parent.name}/tasks.md:{t.line}"
            if t.id in seen:
                out.append(f"{here} duplicate id '{t.id}' (also at {seen[t.id]})")
            else:
                seen[t.id] = here
    return out


WAIT_TASK_RE = re.compile(r"task[:\s]+([a-z0-9]{3,12})", re.IGNORECASE)


def chain_warnings(areas: list[Area]) -> list[str]:
    """A task marked `waiting:task <id>` whose task is no longer open: the thing it waited for has happened."""
    open_ids = {t.id for a in areas for t in a.tasks if t.id and t.mark != "x"}
    out = []
    for a in areas:
        for t in a.tasks:
            m = WAIT_TASK_RE.fullmatch(t.fields.get("waiting", "").strip())
            if m and t.mark != "x" and m.group(1).lower() not in open_ids:
                out.append(f"{a.path.parent.name}/tasks.md:{t.line} waits on task {m.group(1)}, which is no longer "
                           f"open — unblock it: remove waiting:, then date or finish it")
    return out


def next_after(every: str, after: date) -> date | None:
    """First occurrence of a recurrence strictly after `after`."""
    every = every.lower()
    if every == "daily":
        return after + timedelta(days=1)
    if every in WEEKDAYS:
        delta = (WEEKDAYS.index(every) - after.weekday()) % 7 or 7
        return after + timedelta(days=delta)
    m = re.fullmatch(r"(\d+)([dw])", every)
    if m and int(m.group(1)) > 0:  # 0d would never advance → infinite loop
        n = int(m.group(1)) * (7 if m.group(2) == "w" else 1)
        return after + timedelta(days=n)
    return None


def suggested_next(task: Task, today: date) -> date | None:
    """For an overdue recurring task: the first occurrence after today."""
    when = task.when
    if "every" not in task.fields or not when or when >= today:
        return None
    nxt = when
    while nxt and nxt <= today:
        nxt = next_after(task.fields["every"], nxt)
    return nxt


def load_config(explicit: str | None) -> tuple[dict, Path | None]:
    """Return (config, path). An absent config is fine only when --root is given."""
    candidates = [explicit, os.environ.get("PA_CONFIG"), SKILL_DIR / "config.json"]
    for c in candidates:
        if c and Path(c).is_file():
            return json.loads(Path(c).read_text(encoding="utf-8-sig")), Path(c)
    return {}, None


def resolve_root(root_arg: str | None, cfg: dict) -> Path | None:
    root_str = root_arg or cfg.get("data_root")
    return Path(os.path.expandvars(os.path.expanduser(root_str))) if root_str else None


def settings(root: Path, cfg: dict) -> dict:
    """config.json with <root>/pa.local.json merged over it. A broken local file is reported, not fatal."""
    from areas import load_local
    try:
        local = load_local(root)
    except (OSError, ValueError) as exc:
        return {**cfg, "_settings_error": f"pa.local.json unreadable: {exc}"}
    return {**cfg, **local} if local else cfg  # same dict when there is nothing to merge


def week_number(today: date, root: Path, cfg: dict) -> str | None:
    """Optional 'Term week N': config "week_numbering" ({"assumed_week1_monday", "verified"}) given inline,
    or read from the JSON file named by "week_numbering_file" (same object under the key "week_numbering")."""
    wn = cfg.get("week_numbering")
    rel = cfg.get("week_numbering_file")
    if not wn and not rel:
        return None
    try:
        if not wn:
            wn = json.loads((root / rel).read_text(encoding="utf-8"))["week_numbering"]
        week1 = date.fromisoformat(wn["assumed_week1_monday"])
    except (OSError, KeyError, ValueError, TypeError):
        return None
    week = (today - week1).days // 7 + 1
    note = "" if wn.get("verified") else " (week-1 date unverified)"
    return f"{cfg.get('week_label', 'Week')} week {week}{note}"


@dataclass
class Buckets:
    overdue: list[Task] = field(default_factory=list)
    soon: list[Task] = field(default_factory=list)       # today .. horizon
    unknown: list[Task] = field(default_factory=list)
    chase: list[Task] = field(default_factory=list)      # waiting, and its chase date has come
    waiting: list[Task] = field(default_factory=list)
    undated: list[Task] = field(default_factory=list)
    later: list[Task] = field(default_factory=list)      # dated beyond horizon
    deferred: list[Task] = field(default_factory=list)   # start in the future
    unlocked: list[Task] = field(default_factory=list)   # start passed in the last 7 days (not exclusive)
    done_left: list[Task] = field(default_factory=list)  # [x] still in tasks.md
    assistant: list[Task] = field(default_factory=list)  # owner:assistant, open (also in the lists above)


def classify(areas: list[Area], today: date, horizon: date) -> Buckets:
    b = Buckets()
    for area in areas:
        for t in area.tasks:
            if t.mark == "x":
                b.done_left.append(t)
                continue
            if t.owner == "assistant":
                b.assistant.append(t)
            start = t.date_field("start")
            when = t.when
            if start and start > today and not (when and when < today):  # never hide an overdue task
                b.deferred.append(t)
                continue
            if when and when < today:
                b.overdue.append(t)
            elif when and when <= horizon:
                b.soon.append(t)
            elif t.mark == "?":
                b.unknown.append(t)
            elif "waiting" in t.fields:
                (b.chase if t.chase and t.chase <= today else b.waiting).append(t)
            elif not when:
                b.undated.append(t)
            else:
                b.later.append(t)
            if (start and today - timedelta(days=7) <= start <= today and (not when or when > horizon)
                    and t not in b.unknown and t not in b.waiting and t not in b.chase):
                b.unlocked.append(t)
    return b


def assistant_due(t: Task, b: Buckets, today: date) -> bool:
    """An assistant task to do on this run: dated today or earlier, or undated and not deferred or blocked."""
    if t in b.deferred or "waiting" in t.fields:
        return False
    return t.when <= today if t.when else True


def bucket_of(t: Task, b: Buckets, today: date) -> str:
    for name in ("overdue", "soon", "unknown", "chase", "waiting", "undated", "later", "deferred"):
        if t in getattr(b, name):
            if name == "soon" and t.when == today:
                return "today"
            return name
    return "done"


def fmt(task: Task, today: date, show_area: bool = True, nudged: int = 0) -> str:
    when = task.when
    if when:
        delta = (when - today).days
        rel = "today" if delta == 0 else f"{delta:+d}d"
        head = f"{when.isoformat()} {when.strftime('%a')} {rel:>5}"
    else:
        head = " " * 20
    mark = "[?] " if task.mark == "?" else ""
    label = task.fields.get("title") or task.text        # the full text stays in --area and --json
    area = f"{task.area:<11}" if show_area else ""
    extra = []
    if "every" in task.fields:
        extra.append(f"every {task.fields['every']}")
        nxt = suggested_next(task, today)
        if nxt:
            extra.append(f"after done → next:{nxt.isoformat()}")
    if "waiting" in task.fields:
        extra.append(f"waiting: {task.fields['waiting']}")
        chase = task.chase
        if chase:
            ago = (today - chase).days
            extra.append(f"chase {chase.strftime('%a')} {chase.isoformat()}" + (
                "" if ago < 0 else " — today" if ago == 0 else f" — {ago}d ago"))
    if "yields" in task.fields:
        owed = task.fields["yields"]
        extra.append("owes: " + (owed if len(owed) <= 60 else owed[:59] + "…"))
    if nudged:
        extra.append(f"nudged {nudged}×" + (" — ask to re-date, drop or keep" if nudged >= 2 else ""))
    tail = f"  ({'; '.join(extra)})" if extra else ""
    return f"  {head}  {area}{mark}{label}{tail}"


def is_stale(a: Area, today: date, stale_after: int, exempt: frozenset = frozenset()) -> bool:
    """Not reviewed within stale_after days. Areas named in `no_stale_check` (folder names) never are."""
    if a.path.parent.name in exempt:
        return False
    return not a.reviewed or (today - a.reviewed).days > stale_after


def to_json(areas: list[Area], b: Buckets, today: date, horizon_days: int, week: str | None,
            stale_after: int, extra_warnings: list[str], exempt: frozenset = frozenset()) -> dict:
    tasks = []
    unlocked = set(map(id, b.unlocked))
    for a in areas:
        for t in a.tasks:
            if t.mark == "x":
                continue
            when = t.when
            nxt = suggested_next(t, today)
            tasks.append({
                "id": t.id, "area": t.area, "folder": t.folder, "line": t.line, "text": t.text, "owner": t.owner,
                "title": t.title, "has_title": "title" in t.fields,
                "unknown": t.mark == "?", "fields": t.fields, "bucket": bucket_of(t, b, today),
                "when": when.isoformat() if when else None,
                "days": (when - today).days if when else None,
                "newly_active": id(t) in unlocked,
                "suggested_next": nxt.isoformat() if nxt else None,
            })
    return {
        "today": today.isoformat(), "weekday": today.strftime("%A"), "week": week,
        "horizon_days": horizon_days, "generated": datetime.now().isoformat(timespec="seconds"),
        "areas": [{"name": a.name, "folder": a.path.parent.name,
                   "reviewed": a.reviewed.isoformat() if a.reviewed else None,
                   "stale": is_stale(a, today, stale_after, exempt)} for a in areas],
        "tasks": tasks,
        "done_left": len(b.done_left),
        "warnings": [w for a in areas for w in a.warnings] + extra_warnings,
    }


def assign_ids(areas: list[Area]) -> int:
    """Append ' | id:xxxx' to every open task without an id. Rewrites only files that change."""
    taken = {t.id for a in areas for t in a.tasks if t.id}
    added = 0
    for a in areas:
        missing = [t for t in a.tasks if t.mark != "x" and not t.id]
        if not missing:
            continue
        raw = a.path.read_bytes()
        content = read_tasks_file(a.path, [])
        newline = "\r\n" if "\r\n" in content else "\n"
        lines = content.splitlines()
        for t in missing:
            new = "".join(secrets.choice(ID_ALPHABET) for _ in range(4))
            while new in taken:
                new = "".join(secrets.choice(ID_ALPHABET) for _ in range(4))
            taken.add(new)
            lines[t.line - 1] = lines[t.line - 1].rstrip() + f" | id:{new}"
            t.fields["id"] = new
            added += 1
        text = newline.join(lines) + (newline if content.endswith(("\n", "\r")) else "")
        if raw.startswith(b"\xef\xbb\xbf"):
            text = "﻿" + text
        atomic_write(a.path, text.encode("utf-8"))
    return added


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--area")
    ap.add_argument("--today", type=date.fromisoformat, default=date.today())
    ap.add_argument("--root", help="data root to scan (overrides config data_root)")
    ap.add_argument("--config", help="path to config.json")
    ap.add_argument("--json", action="store_true", help="print every open task as JSON")
    ap.add_argument("--assign-ids", action="store_true", help="add id: to open tasks lacking one")
    args = ap.parse_args()
    today: date = args.today
    horizon = today + timedelta(days=args.days)

    cfg, _ = load_config(args.config)
    root = resolve_root(args.root, cfg)
    if not root:
        print(f"No data root. Copy config.example.json to {SKILL_DIR / 'config.json'} and set data_root, "
              "or pass --root.")
        return 2
    cfg = settings(root, cfg)
    stale_after = int(cfg.get("stale_after_days", 7))
    exempt = frozenset(cfg.get("no_stale_check") or [])

    registry_warnings: list[str] = [cfg["_settings_error"]] if cfg.get("_settings_error") else []
    areas = load_areas(root, cfg, registry_warnings)
    if not areas:
        print(f"No areas with a tasks.md found under {root}")
        for w in registry_warnings:
            print(f"! {w}")
        return 1

    if args.assign_ids:
        n = assign_ids(areas)
        print(f"Assigned {n} new id(s).")
        return 0

    week = week_number(today, root, cfg)
    helper = cfg.get("assistant_name") or "Assistant"
    dup_warnings = registry_warnings + duplicate_id_warnings(areas) + chain_warnings(areas)
    ctx = load_context(root, cfg, today)
    nudges = nudge_counts(root, cfg, areas)

    if args.json:
        b = classify(areas, today, horizon)
        out = to_json(areas, b, today, args.days, week, stale_after, dup_warnings + ctx.warnings, exempt)
        out["context"] = [{"from": f.isoformat(), "until": u.isoformat(), "note": n} for f, u, n in ctx.active]
        for t in out["tasks"]:
            t["nudged"] = nudges.get(t["id"] or "", 0)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0

    print(f"Today {today.isoformat()} {today.strftime('%A')}" + (f" · {week}" if week else ""))
    if ctx.active:
        print("\nIMPORTANT (applies across areas — say these to the user)")
        for f, u, n in sorted(ctx.active, key=lambda e: e[1]):
            print(f"  until {u.isoformat()} {u.strftime('%a')}  {n}")

    if args.area:
        match = [a for a in areas if args.area.lower() in (a.name.lower(), a.path.parent.name.lower())]
        if not match:
            print(f"No area '{args.area}'. Areas: {', '.join(a.name for a in areas)}")
            return 1
        area = match[0]
        print(f"\n{area.name} — {area.path}  (last reviewed {area.reviewed or 'never'})")
        open_tasks = [t for t in area.tasks if t.mark != "x"]
        open_tasks.sort(key=lambda t: (t.when is None, t.when or date.max))
        for t in open_tasks:
            start = t.date_field("start")
            hidden = f"  [starts {start.isoformat()}]" if start and start > today else ""
            print(fmt(t, today, show_area=False, nudged=nudges.get(t.id or "", 0)) + hidden)
        for w in area.warnings:
            print(f"  ! {w}")
        return 0

    b = classify(areas, today, horizon)
    mine = lambda items: [t for t in items if t.owner != "assistant"]
    undated: dict[str, int] = {}
    for t in mine(b.undated):
        undated[t.area] = undated.get(t.area, 0) + 1

    by_date = lambda t: (t.when or date.max, t.area)

    by_chase = lambda t: (t.chase or date.max, t.area)

    def section(title: str, items: list[Task], key=by_date) -> None:
        items = mine(items)
        if items:
            print(f"\n{title} ({len(items)})")
            for t in sorted(items, key=key):
                print(fmt(t, today, nudged=nudges.get(t.id or "", 0)))

    if b.assistant:
        todo = [t for t in b.assistant if assistant_due(t, b, today)]
        later = sorted((t for t in b.assistant if t not in todo), key=by_date)
        last = last_runs(root, cfg, areas)
        print(f"\nFOR {helper.upper()} — owner:assistant, do these before the briefing ({len(todo)})")
        for t in sorted(todo, key=by_date):
            print(fmt(t, today))
            run = last.get(t.id or "")
            if run:
                print(f"      last run {run.day.isoformat()}: {run.outcome[:110] or '(no outcome recorded)'}")
        if later:
            nxt = later[0].when
            print(f"{helper}, later: {len(later)}" + (f" (next {nxt.isoformat()})" if nxt else ""))

    section("OVERDUE", b.overdue)
    section(f"DUE IN NEXT {args.days} DAYS", b.soon)
    section("STATUS UNKNOWN — confirm with user", b.unknown)
    section("NEWLY ACTIVE (start date passed this week)", b.unlocked)
    section("FOLLOW UP — still waiting at the chase date: chase them, keep waiting (new chase:) or close",
            b.chase, by_chase)
    section("WAITING ON SOMEONE", b.waiting, by_chase)

    print()
    if undated:
        print("Open, undated: " + " · ".join(f"{k} {v}" for k, v in sorted(undated.items())))
    unchased: dict[str, int] = {}
    for t in mine(b.waiting):
        if not t.chase and not WAIT_TASK_RE.fullmatch(t.fields["waiting"].strip()):  # chains unblock on their own
            unchased[t.area] = unchased.get(t.area, 0) + 1
    if unchased:
        chase_days = int(cfg.get("chase_days", CHASE_DAYS))
        print(f"Waiting with no chase date: {sum(unchased.values())} ("
              + " · ".join(f"{k} {v}" for k, v in sorted(unchased.items()))
              + f") — give each a chase: (default +{chase_days}d → {(today + timedelta(days=chase_days)).isoformat()})")
    print(f"Dated beyond {args.days} days: {len(mine(b.later))} · Not yet started (future start:): {len(mine(b.deferred))}")
    stale = [a for a in areas if is_stale(a, today, stale_after, exempt)]
    for a in stale:
        age = f"{(today - a.reviewed).days}d ago" if a.reviewed else "never"
        print(f"! {a.name}: tasks.md last reviewed {age} — review with the user")
    if b.done_left:
        print(f"! {len(b.done_left)} checked-off task(s) still in tasks.md — move to done.md")
    no_id = [t for a in areas for t in a.tasks if t.mark != "x" and not t.id]
    if no_id:
        print(f"! {len(no_id)} open task(s) have no id (the widget cannot show them properly) — run: "
              f"python {Path(__file__).resolve()} --assign-ids")
    for a in areas:
        for w in a.warnings:
            print(f"! {w}")
    for w in dup_warnings:
        print(f"! {w}")
    if ctx.upcoming:
        print(f"Context: {len(ctx.upcoming)} upcoming entr{'y' if len(ctx.upcoming) == 1 else 'ies'}")
    from snapshot import due_notice
    notice = due_notice(root, cfg, today)
    if notice:
        print(notice)
    from areas import area_dirs
    hooked = [d.slug for d in area_dirs(root, cfg)[0] if d.manifest.get("on_load")]
    if hooked:
        print(f"Area steps to run before the briefing ({', '.join(hooked)}): "
              f"python {Path(__file__).resolve().parent / 'areas.py'} hooks on_load")
    for f, u, n in ctx.expired:
        print(f"! context.md: expired {u.isoformat()} — remove: {n[:70]}")
    for w in ctx.warnings:
        print(f"! {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
