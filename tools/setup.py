#!/usr/bin/env python3
"""First-run setup for /pa: check what a setup has, and create what it is missing. Never overwrites a file.

The interview itself is done by /pa (SKILL.md, "First-run setup"); this script does the file work, so no settings
file is ever written by hand.

    python setup.py check [--json]
        What is present, missing or broken: config.json, the data folder, pa.local.json, the local rules file, each
        area's tasks.md / done.md / about.md, <data_root>/CLAUDE.md and index.md. Exit 0 when nothing required is
        missing, 1 otherwise.

    python setup.py init --data-root DIR [--area FOLDER[=PURPOSE] ...] [--assistant-name NAME] [--chase-days N]
                         [--widget-browser default|firefox|edge-app|none] [--dry-run]
        Creates only what does not exist yet:
          <skill>/config.json                 {"data_root": DIR}  (an existing one must already point at DIR)
          DIR/pa.local.json                   areas in the order given, and the other settings
          DIR/pa-local.md                     the local rules, as headings for /pa to fill in from the interview
          DIR/CLAUDE.md                       tells other sessions the records are read-only
          DIR/index.md                        a table of the areas
          DIR/<area>/tasks.md, done.md, about.md
        An existing pa.local.json is left alone; the areas it does not list yet are printed to add by hand.
        The data folder must be outside the skill folder, so user data can never end up in git.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import due  # noqa: E402
from areas import LOCAL_FILE, area_dirs, expand, load_local  # noqa: E402

SKILL_DIR = due.SKILL_DIR
EXAMPLE = SKILL_DIR / "examples" / "area-template"
FOLDER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
BROWSERS = ("default", "firefox", "edge-app", "none")

RULES = """# Local rules for /pa

Read by /pa on every run (`local_rules` in pa.local.json). These extend and override the skill's generic judgment.
Keep them short; facts about one area belong in that area's about.md.

## Where things live
- (each area's detailed documents, if they live outside the area folder)

## People
- (who comes up often, and how to refer to them)

## Follow-up rules
- Waiting on someone: give every waiting: task a chase: date. Default {chase_days} days.
- (intervals that differ by kind, e.g. a job application 21 days, an email to a busy person 5 working days)
- (what to add after common events, e.g. an email sent → a follow-up task)

## Busy periods
- (dates or seasons that cannot take new work, e.g. exam weeks)

## Sensitive data
- Never print passwords, account numbers or ID numbers in a briefing or a note.
- (anything else to keep out of chat)

## Tone
- (how short or direct the briefing should be)
"""

CLAUDE = """# Records kept by the /pa assistant

This folder holds the user's task records, kept by the `/pa` skill. Any session started here loads this file.

**Read anything freely. Unless this session is running `/pa`, do not edit:** any `tasks.md`, `done.md`,
`context.md`, `pa.local.json`, the local rules file, or the `_widget` / `_snapshots` / `_exports` folders. The
assistant's ids, its append-only done.md and its weekly integrity check all depend on those files changing only
through it. Everything else in an area folder is ordinary working material.

To pass something on instead:

    python ~/.claude/skills/pa/tools/widgetctl.py note <task id> "what you found"
    python ~/.claude/skills/pa/tools/widgetctl.py capture "a new task" [--area <folder>]

Or tell the user, and suggest they run `/pa`.
"""

ABOUT = """# {name} — {purpose}
**Scope:** what is in and out of this area.
**Detailed docs:** (paths, canonical first)
## Key facts
- (dates, numbers, people, constraints needed to reason about tasks without opening the detailed docs)
## Standing decisions (do not re-litigate)
- (decisions already made, with a few words on why)
## Definition of done
- (what a finished piece of work in this area must produce)
"""


def area_header(kind: str, name: str, today: date) -> str:
    """The header of the example area's tasks.md / done.md (read-only notice included), for another area."""
    out = []
    for line in (EXAMPLE / kind).read_text(encoding="utf-8").splitlines():
        if line.startswith("- "):
            break                                     # the example's own entries stop here
        line = re.sub(r"^(# (?:Tasks|Done) — ).*$", lambda m: m.group(1) + name, line)
        line = re.sub(r"^Last reviewed: .*$", f"Last reviewed: {today.isoformat()}", line)
        out.append(line)
    return "\n".join(out).rstrip() + "\n"


def inside_skill(p: Path) -> bool:
    p, skill = p.resolve(), SKILL_DIR.resolve()
    return p == skill or skill in p.parents


# ---- check -----------------------------------------------------------------------------------------------------
def check(config_path: str | None = None) -> tuple[list[str], list[str], list[str]]:
    """(ok, problems, suggestions). Problems stop /pa working properly; suggestions are worth fixing."""
    ok: list[str] = []
    problems: list[str] = []
    hints: list[str] = []
    if config_path and not Path(config_path).is_file():     # an explicit path is the only one that counts
        return ok, [f"no config file at {config_path} — run /pa setup"], hints
    cfg, path = due.load_config(config_path)
    if not path:
        return ok, [f"no config.json in {SKILL_DIR} — run /pa setup"], hints
    root = due.resolve_root(None, cfg)
    if not root:
        return ok, [f"{path.name} has no data_root"], hints
    if not root.is_dir():
        return ok, [f"data folder {root} does not exist"], hints
    return check_data(root, cfg)


def check_data(root: Path, cfg: dict) -> tuple[list[str], list[str], list[str]]:
    """The data folder's part of `check` (the widget calls this: its config is already known to work)."""
    ok: list[str] = []
    problems: list[str] = []
    hints: list[str] = []
    if inside_skill(root):
        problems.append(f"data folder {root} is inside the skill folder — user data could end up in git; move it out")
    ok.append(f"data folder: {root}")
    try:
        local = load_local(root)
    except (OSError, ValueError) as exc:
        problems.append(f"{LOCAL_FILE} unreadable: {exc}")
        local = {}
    if (root / LOCAL_FILE).is_file() and local:
        ok.append(f"{LOCAL_FILE}: {len(local.get('areas') or [])} area(s) listed")
    elif not (root / LOCAL_FILE).is_file():
        hints.append(f"no {LOCAL_FILE} — every folder with a tasks.md counts as an area, with default settings")
    cfg = {**cfg, **local}
    rules = cfg.get("local_rules")
    if rules and not (root / rules).is_file():
        problems.append(f"local rules file {rules} (local_rules) is missing")
    elif not rules:
        hints.append("no local_rules file named in pa.local.json — follow-up intervals and people have nowhere to live")
    dirs, warns = area_dirs(root, cfg)
    problems += warns
    if not dirs:
        problems.append("no areas yet")
    for d in dirs:
        missing = [f for f in ("tasks.md", "done.md") if not (d.path / f).is_file()]
        if missing:
            problems.append(f"area {d.slug}: no {' or '.join(missing)}")
        if not (d.path / "about.md").is_file():
            hints.append(f"area {d.slug}: no about.md (scope, key facts, standing decisions)")
        if not missing:
            ok.append(f"area {d.slug}")
    if not (root / "CLAUDE.md").is_file():
        hints.append("no CLAUDE.md in the data folder — other sessions are not told the records are read-only")
    if not (root / "index.md").is_file():
        hints.append("no index.md (a human-readable list of the areas)")
    return ok, problems, hints


# ---- init ------------------------------------------------------------------------------------------------------
def parse_area(spec: str) -> tuple[str, str]:
    folder, _, purpose = spec.partition("=")
    folder = folder.strip()
    if not FOLDER_RE.fullmatch(folder):
        raise ValueError(f"area folder '{folder}': letters, digits, '.', '_' and '-' only")
    return folder, purpose.strip() or "(one-line purpose)"


def plan_init(data_root: Path, areas: list[tuple[str, str]], name: str, chase_days: int, browser: str,
              config_path: Path, today: date) -> tuple[list[tuple[Path, str]], list[str]]:
    """(files to create with their text, notes). Raises ValueError when setup cannot go ahead."""
    if inside_skill(data_root):
        raise ValueError(f"the data folder must be outside the skill folder ({SKILL_DIR}) so user data never "
                         "reaches git")
    notes: list[str] = []
    files: list[tuple[Path, str]] = []
    if config_path.is_file():
        current = due.resolve_root(None, json.loads(config_path.read_text(encoding="utf-8-sig")))
        if not current or current.resolve() != data_root.resolve():
            raise ValueError(f"{config_path} already points at {current}; edit it yourself if the data should move")
        notes.append(f"exists: {config_path}")
    else:
        files.append((config_path, json.dumps({"data_root": data_root.as_posix()}, indent=2) + "\n"))
    local_path = data_root / LOCAL_FILE
    if local_path.is_file():
        listed = {Path(str(e.get("path") if isinstance(e, dict) else e)).name
                  for e in (load_local(data_root).get("areas") or [])}
        todo = [f for f, _ in areas if f not in listed]
        notes.append(f"exists: {local_path}" + (f" — add these areas to its \"areas\" list yourself: {', '.join(todo)}"
                                                 if todo else ""))
    else:
        example = json.loads((SKILL_DIR / "pa.local.example.json").read_text(encoding="utf-8"))
        example.update({"areas": [{"path": f} for f, _ in areas], "assistant_name": name, "chase_days": chase_days,
                        "widget_browser": browser, "links": [],
                        "week_numbering": None, "week_label": None})
        files.append((local_path, json.dumps(example, indent=2, ensure_ascii=False) + "\n"))
    rules = data_root / "pa-local.md"
    files.append((rules, RULES.format(chase_days=chase_days)))
    files.append((data_root / "CLAUDE.md", CLAUDE))
    rows = "\n".join(f"| {f} | {p} |" for f, p in areas)
    files.append((data_root / "index.md", f"# Areas\n\n| Folder | Purpose |\n|---|---|\n{rows}\n"))
    for folder, purpose in areas:
        d = data_root / folder
        files.append((d / "tasks.md", area_header("tasks.md", folder, today)))
        files.append((d / "done.md", area_header("done.md", folder, today)))
        files.append((d / "about.md", ABOUT.format(name=folder, purpose=purpose)))
    create, kept = [], []
    for p, text in files:
        (kept if p.exists() else create).append((p, text))
    notes += [f"exists: {p}" for p, _ in kept if p != config_path]
    return create, notes


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="path to config.json (default: the skill folder's)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("init")
    p.add_argument("--data-root", required=True)
    p.add_argument("--area", action="append", default=[], help="FOLDER or FOLDER=one-line purpose; repeat, in order")
    p.add_argument("--assistant-name", default="Assistant")
    p.add_argument("--chase-days", type=int, default=due.CHASE_DAYS)
    p.add_argument("--widget-browser", choices=BROWSERS, default="default")
    p.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.cmd == "check":
        ok, problems, hints = check(args.config)
        if args.json:
            print(json.dumps({"ok": ok, "problems": problems, "suggestions": hints}, ensure_ascii=False, indent=1))
        else:
            for line in ok:
                print(f"  ok  {line}")
            for line in problems:
                print(f"  !!  {line}")
            for line in hints:
                print(f"  --  {line}")
            print("Setup complete." if not problems else f"{len(problems)} problem(s) — run /pa setup.")
        return 1 if problems else 0

    try:
        areas = [parse_area(a) for a in args.area]
        if len({f for f, _ in areas}) != len(areas):
            raise ValueError("the same area folder is listed twice")
        if args.chase_days < 1:
            raise ValueError("--chase-days must be at least 1")
        config_path = Path(args.config) if args.config else SKILL_DIR / "config.json"
        create, notes = plan_init(expand(args.data_root), areas, args.assistant_name.strip() or "Assistant",
                                  args.chase_days, args.widget_browser, config_path, date.today())
    except ValueError as exc:
        print(f"Not set up: {exc}")
        return 2
    for p, text in create:
        if args.dry_run:
            print(f"would create {p}")
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "x", encoding="utf-8", newline="\n") as f:   # "x": fails rather than overwrite
            f.write(text)
        print(f"created {p}")
    for n in notes:
        print(n)
    if not create:
        print("Nothing to create.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
