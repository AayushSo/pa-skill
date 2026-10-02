"""Shared storage for the /pa widget: inbox events, focus + details, access key.

All files live in <data_root>/<widget_dir>/ (default "_widget"), i.e. with the user's data —
never in the skill repo.

    inbox.jsonl      append-only events written by the widget (done / undone / comment / snooze / unsnooze)
    processed.txt    event ids /pa has handled (append-only; the inbox itself is never rewritten)
    details.json     {"updated": ts, "focus": [task ids], "details": {task id: {"text", "updated"}}, "plan": {...}}
    nudges.json      {task id: {"when": the task's date when nudged, "dates": [days /pa raised it]}}
    key              random access key the widget page must send with every API call
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime
from pathlib import Path

EVENT_TYPES = {"done", "undone", "comment", "snooze", "unsnooze"}
_lock = threading.Lock()


class Store:
    def __init__(self, root: Path, widget_dir: str = "_widget"):
        self.dir = root / widget_dir
        self.inbox = self.dir / "inbox.jsonl"
        self.processed = self.dir / "processed.txt"
        self.details_path = self.dir / "details.json"
        self.nudges_path = self.dir / "nudges.json"
        self.key_path = self.dir / "key"

    def ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---- access key ---------------------------------------------------------------------------
    def key(self) -> str:
        self.ensure()
        if not self.key_path.exists():
            self.key_path.write_text(secrets.token_urlsafe(24), encoding="utf-8")
        return self.key_path.read_text(encoding="utf-8").strip()

    # ---- inbox --------------------------------------------------------------------------------
    def append_event(self, event: dict) -> dict:
        if event.get("type") not in EVENT_TYPES:
            raise ValueError(f"bad event type {event.get('type')!r}")
        self.ensure()
        event = {"eid": "e" + secrets.token_hex(6), "ts": datetime.now().isoformat(timespec="seconds"), **event}
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with _lock, open(self.inbox, "a", encoding="utf-8") as f:
            f.write(line)
        return event

    def events(self) -> list[dict]:
        if not self.inbox.exists():
            return []
        out = []
        for line in self.inbox.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn line never blocks the rest
        return out

    def processed_ids(self) -> set[str]:
        if not self.processed.exists():
            return set()
        return {l.strip() for l in self.processed.read_text(encoding="utf-8").splitlines() if l.strip()}

    def ack(self, eids: list[str]) -> int:
        known = {e["eid"] for e in self.events()}
        new = [e for e in eids if e in known and e not in self.processed_ids()]
        if new:
            self.ensure()
            with _lock, open(self.processed, "a", encoding="utf-8") as f:
                f.write("".join(e + "\n" for e in new))
        return len(new)

    def task_states(self) -> dict[str, dict]:
        """Per task id: pending done / snooze (net of undo), and every comment with its filed status."""
        done_seen = self.processed_ids()
        states: dict[str, dict] = {}
        for e in self.events():
            tid = e.get("task_id")
            if not tid:
                continue
            s = states.setdefault(tid, {"done": False, "snooze": None, "comments": [], "pending": []})
            acked = e["eid"] in done_seen
            if not acked:
                s["pending"].append(e["eid"])
            if e["type"] == "comment":
                s["comments"].append({"eid": e["eid"], "ts": e["ts"], "text": e.get("comment", ""), "filed": acked})
            elif not acked and e["type"] in ("done", "undone"):
                s["done"] = e["type"] == "done"
            elif not acked and e["type"] in ("snooze", "unsnooze"):
                s["snooze"] = e.get("until") if e["type"] == "snooze" else None
        return states

    # ---- focus + details ----------------------------------------------------------------------
    def details(self) -> dict:
        if not self.details_path.exists():
            return {"updated": None, "focus": [], "details": {}}
        return json.loads(self.details_path.read_text(encoding="utf-8"))

    def save_details(self, data: dict) -> None:
        self.ensure()
        data["updated"] = datetime.now().isoformat(timespec="seconds")
        self._write_json(self.details_path, data)

    def _write_json(self, path: Path, data: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    # ---- nudges: how many days /pa has raised a task that is still not done ---------------------
    def nudges(self) -> dict:
        if not self.nudges_path.exists():
            return {}
        try:
            return json.loads(self.nudges_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def record_nudge(self, task_id: str, when: str | None, today: str) -> int:
        """Count one nudge per day. Re-dating the task (a different when) starts the count again."""
        self.ensure()
        with _lock:
            data = self.nudges()
            entry = data.get(task_id)
            if not entry or entry.get("when") != when:
                entry = {"when": when, "dates": []}
            if today not in entry["dates"]:
                entry["dates"].append(today)
            data[task_id] = entry
            self._write_json(self.nudges_path, data)
        return len(entry["dates"])

    def nudge_counts(self, current_when: dict[str, str | None]) -> dict[str, int]:
        """Counts for tasks whose date is unchanged since they were nudged; re-dated tasks count 0."""
        return {tid: len(e.get("dates", [])) for tid, e in self.nudges().items()
                if tid in current_when and e.get("when") == current_when[tid]}

    def prune_nudges(self, open_ids: set[str]) -> int:
        data = self.nudges()
        kept = {k: v for k, v in data.items() if k in open_ids}
        if len(kept) != len(data):
            self.ensure()
            self._write_json(self.nudges_path, kept)
        return len(data) - len(kept)
