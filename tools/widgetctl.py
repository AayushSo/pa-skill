#!/usr/bin/env python3
"""/pa's side of the widget: read what the user did, acknowledge it, set focus and details.

    python widgetctl.py inbox                 pending widget events, grouped by task, with the task's current line
    python widgetctl.py ack <eid> [<eid>...]  mark events handled (after applying them to tasks.md / docs)
    python widgetctl.py ack --all             mark every pending event handled
        Closing a task that has a yields: field needs --outcome "what it says · what changes · next step";
        the text is what belongs in done.md. Without it, ack refuses.
    python widgetctl.py focus <id> [<id>...]  replace today's focus list (task ids, in order)
    python widgetctl.py detail <id> "text"    set the 1–3 line detail shown when a task is expanded
    python widgetctl.py plan <id> <block> [--date D]   place a task in a schedule block (block ids: schedulectl.py show)
    python widgetctl.py plan --clear [--date D]        remove the day's plan
    python widgetctl.py note <id> "text"      leave a comment on a task for /pa to read at its next run
        For sessions that are not running /pa: it queues a note instead of editing tasks.md.
    python widgetctl.py nudge <id> [<id>...]  record that the briefing raised these tasks today (one count per day)
    python widgetctl.py prune                 drop details, nudges and past plans of tasks that are no longer open
    python widgetctl.py url                   print the widget URL with the access key masked
    python widgetctl.py url --show-key        print it with the key (a secret — for the user, not chat)

Takes the same --config / --root options as due.py.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import due  # noqa: E402
from widget_store import Store  # noqa: E402


def snooze_instruction(task, event: dict) -> str:
    """Exactly what /pa should write for a snooze, so start: never ends up after due:."""
    until = event.get("until")
    if task is None:
        return "task gone — tell the user, then ack"
    if "waiting" in task.fields and not task.when:
        # "Don't remind me until D" on a wait is a new chase date; start: would hide it and bring it back silently.
        return f"waiting task: set chase:{until} (not start:)"
    field ="due" if task.fields.get("due") else ("next" if task.fields.get("next") else None)
    current = task.fields.get(field) if field else None
    if not current or current >= until:
        return f"set start:{until}"
    if event.get("moves_date"):
        return f"user confirmed moving the date: set start:{until} and {field}:{until}"
    return f"{field} is {current}, before {until}, and the move was not confirmed: ASK the user (never set start after {field})"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--root")
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("inbox")
    p = sub.add_parser("ack"); p.add_argument("eids", nargs="*"); p.add_argument("--all", action="store_true")
    p.add_argument("--outcome", help="what the task yielded (required when it has a yields: field)")
    p = sub.add_parser("focus"); p.add_argument("ids", nargs="*")
    p = sub.add_parser("detail"); p.add_argument("id"); p.add_argument("text")
    p = sub.add_parser("plan"); p.add_argument("id", nargs="?"); p.add_argument("block", nargs="?")
    p.add_argument("--date", type=due.date.fromisoformat); p.add_argument("--clear", action="store_true")
    p = sub.add_parser("note"); p.add_argument("id"); p.add_argument("text")
    p = sub.add_parser("nudge"); p.add_argument("ids", nargs="+")
    sub.add_parser("prune")
    p = sub.add_parser("url"); p.add_argument("--show-key", action="store_true")
    args = ap.parse_args()

    cfg, _ = due.load_config(args.config)
    root = due.resolve_root(args.root, cfg)
    if not root:
        print("No data root: set data_root in config.json or pass --root.")
        return 2
    cfg = due.settings(root, cfg)
    store = Store(root, cfg.get("widget_dir", "_widget"))
    tasks = {t.id: t for a in due.load_areas(root, cfg) for t in a.tasks if t.id}

    if args.cmd == "inbox":
        done = store.processed_ids()
        pending = [e for e in store.events() if e["eid"] not in done]
        if not pending:
            print("Widget inbox: nothing pending.")
            return 0
        by_task: dict[str, list[dict]] = {}
        for e in pending:
            by_task.setdefault(e.get("task_id", "?"), []).append(e)
        print(f"Widget inbox: {len(pending)} pending event(s) on {len(by_task)} task(s)\n")
        for tid, evs in by_task.items():
            t = tasks.get(tid)
            where = f"{t.folder}/tasks.md:{t.line}  {t.text}" if t else f"(no open task with id {tid}) last seen as: {evs[-1].get('text', '')}"
            print(f"[{tid}] {where}")
            for e in evs:
                what = {"comment": f"comment: {e.get('comment', '')}"}.get(e["type"], e["type"])
                owed = e.get("yields") or (t.fields.get("yields") if t else None)
                if e["type"] == "done" and owed:
                    what = f"done — OWES AN OUTCOME: {owed}\n        (close it with: ack {e['eid']} --outcome \"…\")"
                if e["type"] == "snooze":
                    what = f"snooze until {e.get('until')} → {snooze_instruction(t, e)}"
                print(f"    {e['eid']}  {e['ts']}  {what}")
            print()
        print("Net effect per task: the LAST done/undone and snooze/unsnooze wins. Ack every eid once handled.")
        return 0

    theirs = [i for i in getattr(args, "ids", None) or [getattr(args, "id", None)]
              if i in tasks and tasks[i].owner == "assistant"]
    if args.cmd in ("nudge", "focus", "plan") and theirs and not getattr(args, "clear", False):
        # The user's focus, schedule and reminders are for the user's work; the assistant just does its own.
        print(f"owner:assistant — not the user's to {args.cmd}: {', '.join(theirs)}. "
              "Do these yourself on this run instead.")
        if args.cmd != "nudge":
            return 1
        args.ids = [i for i in args.ids if i not in theirs]

    if args.cmd == "note":
        task = tasks.get(args.id)
        if not task:
            print(f"Not an open task id: {args.id}. Run due.py --json to see the ids.")
            return 1
        text = args.text.strip()
        if not text:
            print("Nothing to note.")
            return 2
        event = store.append_event({"type": "comment", "task_id": args.id, "area": task.area,
                                    "text": task.text, "comment": text[:5000]})
        print(f"Noted on {args.id} ({task.area}): {text[:80]}")
        print(f"  task: {task.text[:90]}")
        print(f"  the assistant reads it at its next run (event {event['eid']})")
        return 0

    if args.cmd == "nudge":
        unknown = [i for i in args.ids if i not in tasks]
        if unknown:
            print(f"Not open task ids (skipped): {', '.join(unknown)}")
        today_iso = due.date.today().isoformat()
        for i in args.ids:
            if i in tasks:
                n = store.record_nudge(i, due.nudge_date(tasks[i]), today_iso)
                hint = "  ← ask to re-date, drop or keep; don't just repeat it" if n >= 2 else ""
                print(f"{i}: nudged {n}×{hint}")
        return 0

    if args.cmd == "ack":
        events = {e["eid"]: e for e in store.events()}
        eids = list(events) if args.all else args.eids
        if not args.outcome:
            owing = []
            for eid in eids:
                e = events.get(eid)
                if not e or e["type"] != "done" or eid in store.processed_ids():
                    continue
                t = tasks.get(e.get("task_id", ""))
                owed = e.get("yields") or (t.fields.get("yields") if t else None)
                if owed:
                    owing.append(f"  {eid}  {e.get('text', '')[:60]}\n      owes: {owed}")
            if owing:
                print("Not acknowledged. These closed tasks owe an outcome — decide what the result means, "
                      "tell the user, write it to done.md, then repeat with --outcome \"…\":\n" + "\n".join(owing))
                return 1
        n = store.ack(eids)
        print(f"Acknowledged {n} event(s)." + (f"\nOutcome recorded: {args.outcome}\n"
              "(put this in the area's done.md and say it in the briefing)" if args.outcome else ""))
        return 0

    data = store.details()
    if args.cmd == "focus":
        unknown = [i for i in args.ids if i not in tasks]
        if unknown:
            print(f"Not open task ids: {', '.join(unknown)}")
            return 1
        data["focus"] = args.ids
        store.save_details(data)
        print(f"Focus set: {', '.join(args.ids) or '(cleared)'}")
    elif args.cmd == "detail":
        if args.id not in tasks:
            print(f"Not an open task id: {args.id}")
            return 1
        data.setdefault("details", {})[args.id] = {"text": args.text.strip(), "updated": due.date.today().isoformat()}
        store.save_details(data)
        print(f"Detail set for {args.id}.")
    elif args.cmd == "plan":
        day = (args.date or due.date.today()).isoformat()
        plans = data.setdefault("plan", {})
        if args.clear:
            plans.pop(day, None)
            store.save_details(data)
            print(f"Plan cleared for {day}.")
            return 0
        if not (args.id and args.block):
            print("Usage: widgetctl.py plan <task id> <block id> [--date D]  (or --clear)")
            return 2
        if args.id not in tasks:
            print(f"Not an open task id: {args.id}")
            return 1
        if not cfg.get("schedule_file"):
            print("No schedule_file in config.json.")
            return 2
        import schedule as sch
        blocks = {b["id"]: b for b in sch.day(sch.load(root / cfg["schedule_file"]), due.date.fromisoformat(day))["blocks"]}
        if args.block not in blocks:
            print(f"No block '{args.block}' on {day}. Blocks that day: {', '.join(blocks) or '(none)'}")
            return 1
        for ids in plans.setdefault(day, {}).values():  # a task sits in one block per day
            if args.id in ids:
                ids.remove(args.id)
        plans[day].setdefault(args.block, []).append(args.id)
        plans[day] = {k: v for k, v in plans[day].items() if v}
        store.save_details(data)
        b = blocks[args.block]
        print(f"Planned {args.id} into {b['start']}–{b['end']} {b['title']} on {day}.")
    elif args.cmd == "prune":
        before = len(data.get("details", {}))
        data["details"] = {k: v for k, v in data.get("details", {}).items() if k in tasks}
        data["focus"] = [i for i in data.get("focus", []) if i in tasks]
        today_iso = due.date.today().isoformat()
        data["plan"] = {d: {b: [i for i in ids if i in tasks] for b, ids in blocks.items()}
                        for d, blocks in data.get("plan", {}).items() if d >= today_iso}
        store.save_details(data)
        gone = store.prune_nudges(set(tasks))
        print(f"Pruned {before - len(data['details'])} detail(s) and {gone} nudge record(s).")
    elif args.cmd == "url":
        port = int(cfg.get('widget_port', 8765))
        print(f"http://127.0.0.1:{port}/?k={store.key() if args.show_key else '…'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
