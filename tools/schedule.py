"""Weekly schedule model for /pa: resolve which blocks apply on a date, spot clashes, render a table.

The schedule is a JSON file in the user's data root (path from config "schedule_file"):

{
  "categories": {"<cat>": {"label": "...", "color": "#rrggbb", "fixed": true|false, "areas": ["<area folder>", ...]}},
  "week_kinds": {"cycle": ["a", "b"], "anchor": "YYYY-MM-DD (a Monday)", "anchor_kind": "a",
                 "overrides": {"YYYY-MM-DD (Monday)": "b"}},
  "phases": [{"from": "YYYY-MM-DD", "until": "YYYY-MM-DD", "label": "...", "note": "..."}],
  "blocks": [{"id": "unique", "days": ["mon", ...], "start": "HH:MM", "end": "HH:MM", "title": "...",
              "category": "<cat>", "short": "compact name for narrow views", "note": "...", "location": "...",
              "from": "YYYY-MM-DD", "until": "YYYY-MM-DD", "week_kind": "a"}],
  "events": [{"id": "unique", "date": "YYYY-MM-DD", "until": "YYYY-MM-DD", "start": "HH:MM", "end": "HH:MM",
              "title": "...", "category": "<cat>", "all_day": true, "cancels": ["<cat>" | "*"]}]
}

- A block applies on a date when its weekday is in "days", the date is within from/until (both optional,
  inclusive), and its week_kind (optional) matches that week's kind. Phase-specific versions of a slot are
  simply separate blocks with different from/until ranges.
- "fixed" categories (job, classes, meals) are commitments shown as context; the rest are blocks to fill.
- A category's "areas" lets the clash check know which tasks belong in its blocks.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


def minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def validate(s: dict) -> list[str]:
    """Structural problems, as human-readable warnings. An empty list means the file is usable."""
    out: list[str] = []
    cats = s.get("categories", {})
    seen: set[str] = set()
    for kind, items in (("block", s.get("blocks", [])), ("event", s.get("events", []))):
        for i, b in enumerate(items):
            where = f"schedule {kind} {b.get('id', f'#{i}')}"
            if not b.get("id"):
                out.append(f"{where}: missing id")
            elif b["id"] in seen:
                out.append(f"{where}: duplicate id")
            seen.add(b.get("id", ""))
            if not b.get("title"):
                out.append(f"{where}: missing title")
            if b.get("category") and b["category"] not in cats:
                out.append(f"{where}: unknown category '{b['category']}'")
            timed = kind == "block" or not b.get("all_day")
            if timed:
                for k in ("start", "end"):
                    if not TIME_RE.fullmatch(str(b.get(k, ""))):
                        out.append(f"{where}: bad {k} '{b.get(k)}' (HH:MM)")
                if all(TIME_RE.fullmatch(str(b.get(k, ""))) for k in ("start", "end")) and minutes(b["end"]) <= minutes(b["start"]):
                    out.append(f"{where}: end {b['end']} is not after start {b['start']}")
            if kind == "block":
                bad_days = [d for d in b.get("days", []) if d not in DAYS]
                if not b.get("days") or bad_days:
                    out.append(f"{where}: days must be a non-empty list of mon..sun")
            else:
                if not b.get("date"):
                    out.append(f"{where}: missing date")
            try:
                f, u = _d(b.get("from") or b.get("date")), _d(b.get("until"))
                if f and u and u < f:
                    out.append(f"{where}: until is before from/date")
            except ValueError as exc:
                out.append(f"{where}: bad date ({exc})")
    wk = s.get("week_kinds")
    if wk:
        try:
            anchor = date.fromisoformat(wk["anchor"])
            if anchor.weekday() != 0:
                out.append("schedule week_kinds.anchor must be a Monday")
            if wk.get("anchor_kind") not in wk.get("cycle", []):
                out.append("schedule week_kinds.anchor_kind must be one of cycle")
            for k, v in wk.get("overrides", {}).items():
                if date.fromisoformat(k).weekday() != 0:
                    out.append(f"schedule week_kinds.overrides key {k} must be a Monday")
                if v not in wk.get("cycle", []):
                    out.append(f"schedule week_kinds.overrides {k}: '{v}' is not in cycle")
        except (KeyError, ValueError) as exc:
            out.append(f"schedule week_kinds: {exc}")
    return out


def week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_kind(s: dict, d: date) -> str | None:
    wk = s.get("week_kinds")
    if not wk:
        return None
    monday = week_monday(d)
    if monday.isoformat() in wk.get("overrides", {}):
        return wk["overrides"][monday.isoformat()]
    cycle = wk["cycle"]
    n = (monday - date.fromisoformat(wk["anchor"])).days // 7
    return cycle[(cycle.index(wk["anchor_kind"]) + n) % len(cycle)]


def _in_range(item: dict, d: date, start_key: str = "from") -> bool:
    f, u = _d(item.get(start_key)), _d(item.get("until"))
    if start_key == "date" and not u:  # an event without "until" is a single day, not "from then on"
        u = f
    return (not f or f <= d) and (not u or d <= u)


def short_title(title: str, limit: int = 14) -> str:
    """Fallback short name: the part before a ' — ', ' · ', ' (' or ' - ', trimmed to `limit` characters."""
    head = re.split(r"\s+[—–·(-]\s*", title, maxsplit=1)[0].strip() or title
    return head if len(head) <= limit else head[:limit - 1].rstrip() + "…"


def _decorate(b: dict, s: dict) -> dict:
    cat = s.get("categories", {}).get(b.get("category", ""), {})
    return {"id": b["id"], "start": b.get("start"), "end": b.get("end"), "title": b["title"],
            "category": b.get("category"), "label": cat.get("label", b.get("category") or ""),
            "short": b.get("short") or short_title(b["title"]),
            "color": cat.get("color", "#888888"), "fixed": bool(cat.get("fixed")),
            "note": b.get("note"), "location": b.get("location")}


def day(s: dict, d: date) -> dict:
    """Everything that applies on one date: timed blocks (incl. timed events), all-day events, phases."""
    kind = week_kind(s, d)
    events = [e for e in s.get("events", []) if _in_range(e, d, "date")]
    cancelled = {c for e in events for c in e.get("cancels", [])}
    blocks = []
    for b in s.get("blocks", []):
        if DAYS[d.weekday()] not in b.get("days", []) or not _in_range(b, d):
            continue
        if b.get("week_kind") and b["week_kind"] != kind:
            continue
        if "*" in cancelled or b.get("category") in cancelled:
            continue
        blocks.append(_decorate(b, s))
    blocks += [_decorate(e, s) | {"event": True} for e in events if not e.get("all_day")]
    blocks.sort(key=lambda b: (minutes(b["start"]), minutes(b["end"])))
    return {
        "date": d.isoformat(), "weekday": DAYS[d.weekday()], "week_kind": kind, "blocks": blocks,
        "all_day": [_decorate(e, s) for e in events if e.get("all_day")],
        "phases": [p for p in s.get("phases", []) if _in_range(p, d)],
    }


def days(s: dict, start: date, n: int) -> list[dict]:
    return [day(s, start + timedelta(days=i)) for i in range(n)]


def overlaps(blocks: list[dict]) -> list[tuple[dict, dict]]:
    out = []
    for i, a in enumerate(blocks):
        for b in blocks[i + 1:]:
            if minutes(b["start"]) < minutes(a["end"]) and minutes(a["start"]) < minutes(b["end"]):
                out.append((a, b))
    return out


def clashes(s: dict, tasks: list, today: date, n: int = 7) -> list[str]:
    """Overlapping blocks in the next n days, and tasks due before any block of their category comes round.

    `tasks` are due.Task objects (open ones); a task belongs to a category when its area folder is listed in
    that category's "areas".
    """
    out: list[str] = []
    window = days(s, today, n)
    for dd in window:
        for a, b in overlaps(dd["blocks"]):
            out.append(f"{dd['date']} ({dd['weekday'].title()}): '{a['title']}' {a['start']}–{a['end']} overlaps "
                       f"'{b['title']}' {b['start']}–{b['end']}")
    area_cats: dict[str, list[str]] = {}
    for cid, c in s.get("categories", {}).items():
        for area in c.get("areas", []):
            area_cats.setdefault(area, []).append(cid)
    lookahead = days(s, today, 60)
    for t in tasks:
        cats = area_cats.get(t.folder)
        due = t.date_field("due")
        if not cats or not due or not (today <= due <= today + timedelta(days=n)) or t.fields.get("waiting"):
            continue
        before = [dd for dd in lookahead if dd["date"] <= due.isoformat()
                  and any(b["category"] in cats for b in dd["blocks"])]
        if not before:
            nxt = next((dd for dd in lookahead if any(b["category"] in cats for b in dd["blocks"])), None)
            label = s["categories"][cats[0]].get("label", cats[0])
            when = f"the next {label} block is {nxt['date']} ({nxt['weekday'].title()})" if nxt else f"no {label} block in 60 days"
            out.append(f"'{t.text[:60]}' is due {due} but {when}")
    return out


def render_table(s: dict, ref: date) -> str:
    """Markdown hour grid for the week containing `ref` (regular blocks only; events are left out)."""
    monday = week_monday(ref)
    week = []
    for i in range(7):
        dd = day(s, monday + timedelta(days=i))
        week.append([b for b in dd["blocks"] if not b.get("event")])
    starts = sorted({minutes(b["start"]) for blocks in week for b in blocks})
    head = "| | " + " | ".join(d.title() for d in DAYS) + " |\n|---|" + "---|" * 7 + "\n"
    rows = []
    for t in starts:
        cells = []
        for blocks in week:
            here = next((b for b in blocks if minutes(b["start"]) == t), None)
            if here:
                cells.append(here["title"].replace("|", "/"))
            elif any(minutes(b["start"]) < t < minutes(b["end"]) for b in blocks):
                cells.append("″")
            else:
                cells.append("—")
        rows.append(f"| **{t // 60:02d}:{t % 60:02d}** | " + " | ".join(cells) + " |")
    kind = week_kind(s, ref)
    title = f"Week of {monday.isoformat()}" + (f" · {kind} week" if kind else "")
    return f"_{title} — generated from the schedule file; edit that, not this table._\n\n" + head + "\n".join(rows) + "\n"
