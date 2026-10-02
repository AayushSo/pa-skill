#!/usr/bin/env python3
"""The reading list's own logic and command line. Standard library only; lives in the area, not in /pa.

    python tools/readinglist.py summary
    python tools/readinglist.py list [--status unread]
    python tools/readinglist.py add --title T --url U
    python tools/readinglist.py set <id> --status done
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import threading
from datetime import date
from pathlib import Path

AREA = Path(__file__).resolve().parent.parent
STATUSES = ["unread", "reading", "done", "dropped"]
_lock = threading.Lock()


def items_path(settings: dict | None = None) -> Path:
    return AREA / (settings or {}).get("items_file", "items.json")


def load(path: Path) -> dict:
    if not path.exists():
        return {"items": []}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def change(path: Path, fn):
    """Read, modify and write under a lock (the page and /pa can both write)."""
    with _lock:
        data = load(path)
        result = fn(data)
        save(path, data)
        return result


def add(data: dict, title: str, url: str, today: str) -> tuple[dict | None, str | None]:
    title, url = (title or "").strip(), (url or "").strip()
    if not title or not url.startswith(("http://", "https://")):
        return None, "an item needs a title and an http(s) link"
    same = next((i for i in data["items"] if i["url"] == url), None)
    if same:
        return same, None
    item = {"id": "r-" + secrets.token_hex(3), "title": title[:300], "url": url, "status": "unread", "added": today}
    data["items"].append(item)
    return item, None


def set_status(data: dict, item_id: str, status: str) -> str | None:
    if status not in STATUSES:
        return f"status must be one of {', '.join(STATUSES)}"
    item = next((i for i in data["items"] if i["id"] == item_id), None)
    if not item:
        return "unknown item"
    item["status"] = status
    return None


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("summary")
    p = sub.add_parser("list"); p.add_argument("--status", choices=STATUSES)
    p = sub.add_parser("add"); p.add_argument("--title", required=True); p.add_argument("--url", required=True)
    p = sub.add_parser("set"); p.add_argument("id"); p.add_argument("--status", required=True)
    args = ap.parse_args()
    path = items_path()

    if args.cmd == "summary":
        items = load(path)["items"]
        counts = {s: sum(1 for i in items if i["status"] == s) for s in STATUSES}
        print("Reading list: " + " · ".join(f"{s} {n}" for s, n in counts.items()))
        return 0
    if args.cmd == "list":
        for i in load(path)["items"]:
            if not args.status or i["status"] == args.status:
                print(f"[{i['id']}] {i['status']:<8} {i['title']}  {i['url']}")
        return 0
    if args.cmd == "add":
        item, problem = change(path, lambda d: add(d, args.title, args.url, date.today().isoformat()))
        print(problem or f"added {item['id']}")
        return 1 if problem else 0
    problem = change(path, lambda d: set_status(d, args.id, args.status))
    print(problem or "updated")
    return 1 if problem else 0


if __name__ == "__main__":
    sys.exit(main())
