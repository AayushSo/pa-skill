#!/usr/bin/env python3
"""Where the user's areas live, and what each one adds to /pa and the widget.

Everything here is read from the user's own files; nothing about a particular setup is built in.

Registry — `<data_root>/pa.local.json` (see pa.local.example.json), key "areas", in display order:

    "areas": [{"path": "admin"}, {"path": "D:/elsewhere/taxes"}]

A relative path is inside the data root. Without an "areas" list, every folder under the data root that
holds a tasks.md is an area. An area's short name (its "slug", used in URLs) is its folder name.

Per-area manifest — `<area>/area.json`, optional; every key is optional:

    {
      "instructions": "AREA.md",                  what /pa reads when this area comes up (besides about.md)
      "on_load": [{"script": "tools/x.py", "args": ["--brief"], "why": "one line"}],   /pa runs before a briefing
      "review":  [{"script": "tools/x.py", "args": [], "why": "..."}],                 /pa runs in the weekly review
      "pages":   {"board": "pages/board.html"},   served at /area/<slug>/<page id>
      "links":   [{"label": "Board", "page": "board"},            the widget's Quick links
                  {"label": "Plan", "file": "plan.html"},         a local document, opened in a new window
                  {"label": "Portal", "url": "https://..."}],     an outside site, opened in the system browser
      "handler": "tools/widget_api.py",           answers /area/<slug>/api/...; loaded once when the server starts
      "settings": {}                              free-form; handed to the handler and printed by `show`
    }

Guardrails: scripts, the handler, pages and linked files must resolve to files inside the area folder, and scripts
and handlers must be .py files. Commands are built from script + args and run with the area folder as the working
directory — never from a shell string. /pa never adds or changes `on_load`, `review` or `handler` without asking.

    python areas.py list                 every area, where it lives and what its manifest declares
    python areas.py hooks on_load        commands /pa runs before a briefing (also: hooks review)
    python areas.py show <slug>          one area's manifest, resolved
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

LOCAL_FILE = "pa.local.json"
HOOKS = ("on_load", "review")
MANIFEST_KEYS = {"instructions", "on_load", "review", "pages", "links", "handler", "settings"}


@dataclass
class AreaDir:
    slug: str
    path: Path
    manifest: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def inside(self, rel, suffixes: tuple[str, ...] | None = None) -> Path | None:
        """`rel` resolved inside this area folder, or None (outside, missing, or wrong type)."""
        if not isinstance(rel, str) or not rel.strip():
            return None
        base = self.path.resolve()
        p = (base / rel).resolve()
        if base not in p.parents or not p.is_file():
            return None
        if suffixes and p.suffix.lower() not in suffixes:
            return None
        return p

    @property
    def tasks_file(self) -> Path:
        return self.path / "tasks.md"


def load_local(root: Path) -> dict:
    """The user's local settings file in the data root, or {} when there is none."""
    path = root / LOCAL_FILE
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{LOCAL_FILE} must hold a JSON object")
    return data


def expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p)))


def read_manifest(folder: Path) -> tuple[dict, list[str]]:
    path = folder / "area.json"
    if not path.is_file():
        return {}, []
    label = f"{folder.name}/area.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return {}, [f"{label} unreadable ({exc}) — ignored"]
    if not isinstance(data, dict):
        return {}, [f"{label} must hold a JSON object — ignored"]
    warns = [f"{label} unknown key '{k}'" for k in data if k not in MANIFEST_KEYS]
    for key, kind in (("on_load", list), ("review", list), ("links", list), ("pages", dict), ("settings", dict)):
        if key in data and not isinstance(data[key], kind):
            warns.append(f"{label} '{key}' should be a {kind.__name__} — ignored")
            data.pop(key)
    return data, warns


def area_dirs(root: Path, cfg: dict) -> tuple[list[AreaDir], list[str]]:
    """The user's areas in display order, each with its manifest and any problems found reading it."""
    warnings: list[str] = []
    listed = cfg.get("areas")
    if listed is None:
        folders = [p.parent for p in sorted(root.glob("*/tasks.md"))]
    else:
        folders = []
        for i, entry in enumerate(listed if isinstance(listed, list) else []):
            raw = entry.get("path") if isinstance(entry, dict) else entry
            if not isinstance(raw, str) or not raw.strip():
                warnings.append(f"{LOCAL_FILE}: areas[{i}] has no path — skipped")
                continue
            p = expand(raw)
            folders.append(p if p.is_absolute() else root / p)
        if not isinstance(listed, list):
            warnings.append(f"{LOCAL_FILE}: 'areas' should be a list — no areas loaded")
    out: list[AreaDir] = []
    seen: set[str] = set()
    for folder in folders:
        slug = folder.name
        if not folder.is_dir():
            warnings.append(f"{LOCAL_FILE}: area folder {folder} does not exist — skipped")
            continue
        if slug in seen:
            warnings.append(f"{LOCAL_FILE}: two areas share the folder name '{slug}' — the second is skipped")
            continue
        seen.add(slug)
        manifest, warns = read_manifest(folder)
        a = AreaDir(slug=slug, path=folder, manifest=manifest, warnings=warns)
        if not a.tasks_file.is_file():
            a.warnings.append(f"{slug}: no tasks.md — the area shows no tasks")
        out.append(a)
    return out, warnings


def commands(a: AreaDir, hook: str) -> tuple[list[dict], list[str]]:
    """Runnable steps for a hook: [{"argv", "cwd", "why"}]. Anything outside the rules is refused, with a reason."""
    steps, problems = [], []
    for i, step in enumerate(a.manifest.get(hook) or []):
        where = f"{a.slug}/area.json {hook}[{i}]"
        if not isinstance(step, dict):
            problems.append(f"{where} should be an object with 'script' — skipped")
            continue
        script = a.inside(step.get("script"), (".py",))
        args = step.get("args", [])
        if not script:
            problems.append(f"{where} script must be a .py file inside the area folder — skipped")
            continue
        if not isinstance(args, list) or not all(isinstance(x, (str, int, float)) for x in args):
            problems.append(f"{where} args must be a list of strings — skipped")
            continue
        steps.append({"argv": [sys.executable, str(script), *map(str, args)], "cwd": str(a.path),
                      "why": str(step.get("why", ""))})
    return steps, problems


def page_file(a: AreaDir, page_id: str) -> Path | None:
    rel = (a.manifest.get("pages") or {}).get(page_id)
    return a.inside(rel, (".html", ".htm"))


def links(areas: list[AreaDir], root: Path, cfg: dict) -> tuple[list[dict], list[str]]:
    """Quick links from every area's manifest, then the global ones in the local settings ("links")."""
    out, problems = [], []
    owners = [(a, a.manifest.get("links") or [], f"{a.slug}/area.json") for a in areas]
    owners.append((AreaDir(slug="_", path=root), cfg.get("links") or [], LOCAL_FILE))
    for owner, items, label in owners:
        for i, l in enumerate(items):
            if not isinstance(l, dict) or not l.get("label"):
                problems.append(f"{label} links[{i}] needs a label — skipped")
                continue
            lid = str(l.get("id") or i)
            base = {"id": f"{owner.slug}/{lid}", "label": str(l["label"]), "area": owner.slug}
            if l.get("page"):
                if owner.slug == "_" or not page_file(owner, str(l["page"])):
                    problems.append(f"{label} links[{i}] page '{l['page']}' is not a declared page — skipped")
                    continue
                out.append({**base, "kind": "page", "href": f"/area/{owner.slug}/{l['page']}"})
            elif l.get("file"):
                path = owner.inside(l["file"])
                if not path:
                    problems.append(f"{label} links[{i}] file '{l['file']}' is missing or outside the folder — skipped")
                    continue
                out.append({**base, "kind": "doc", "href": f"/doc/{owner.slug}/{lid}", "path": path})
            elif l.get("url"):
                from urllib.parse import urlsplit
                if urlsplit(str(l["url"])).scheme not in ("http", "https"):
                    problems.append(f"{label} links[{i}] url must be http(s) — skipped")
                    continue
                out.append({**base, "kind": "url", "href": str(l["url"])})
            else:
                problems.append(f"{label} links[{i}] needs page, file or url — skipped")
    return out, problems


def _forget_area_modules(folder: Path, before: set[str]) -> None:
    """Drop helper modules the handler imported from its own folder from the import cache.

    The handler keeps its references; forgetting them stops a second area with a helper of the same name
    (say, both have tools/model.py) from silently getting the first area's copy.
    """
    base = folder.resolve()
    for name in set(sys.modules) - before:
        f = getattr(sys.modules.get(name), "__file__", None)
        try:
            if f and base in Path(f).resolve().parents:
                del sys.modules[name]
        except (OSError, ValueError):
            continue


def load_handler(a: AreaDir):
    """(module, None) for a declared handler, (None, problem) if it fails, (None, None) if none is declared.

    Called once per area when the server starts; a failing handler only takes its own area's routes down.
    """
    rel = a.manifest.get("handler")
    if not rel:
        return None, None
    path = a.inside(rel, (".py",))
    if not path:
        return None, f"{a.slug}/area.json handler must be a .py file inside the area folder — not loaded"
    before = set(sys.modules)
    try:
        spec = importlib.util.spec_from_file_location(f"pa_area_{a.slug.replace('-', '_')}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:  # the area's own code: report, never take the server down
        return None, f"{a.slug}: handler failed to load — {type(exc).__name__}: {exc}"
    finally:
        _forget_area_modules(a.path, before)
    if not callable(getattr(mod, "handle", None)):
        return None, f"{a.slug}: handler has no handle(request) function — not loaded"
    return mod, None


def _cli_roots(args) -> tuple[Path, dict]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import due
    cfg, _ = due.load_config(args.config)
    root = due.resolve_root(args.root, cfg)
    if not root:
        raise SystemExit("No data root: set data_root in config.json or pass --root.")
    return root, due.settings(root, cfg)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--root"); ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("hooks"); p.add_argument("hook", choices=HOOKS)
    p = sub.add_parser("show"); p.add_argument("slug")
    args = ap.parse_args()
    root, cfg = _cli_roots(args)
    found, warnings = area_dirs(root, cfg)

    if args.cmd == "list":
        for a in found:
            m = a.manifest
            extras = [k for k in ("instructions", "on_load", "review", "pages", "links", "handler") if m.get(k)]
            print(f"{a.slug:<22} {a.path}" + (f"   [{', '.join(extras)}]" if extras else ""))
    elif args.cmd == "hooks":
        any_step = False
        for a in found:
            steps, problems = commands(a, args.hook)
            warnings += problems
            for s in steps:
                any_step = True
                print(f"# {a.slug}: {s['why'] or '(no reason given)'}")
                argv = [Path(x).as_posix() if i < 2 else x for i, x in enumerate(s["argv"])]
                print(f"(cd {shlex.quote(Path(s['cwd']).as_posix())} && {shlex.join(argv)})")
        if not any_step:
            print(f"(no {args.hook} steps declared)")
    else:
        a = next((x for x in found if x.slug == args.slug), None)
        if not a:
            print(f"No area '{args.slug}'. Known: {', '.join(x.slug for x in found)}")
            return 1
        print(f"{a.slug}: {a.path}")
        instr = a.inside(a.manifest.get("instructions"))
        print(f"instructions: {instr or '(none)'}")
        for hook in HOOKS:
            steps, problems = commands(a, hook)
            warnings += problems
            for s in steps:
                print(f"{hook}: {shlex.join(s['argv'][1:])}  — {s['why']}")
        for pid in a.manifest.get("pages") or {}:
            print(f"page: /area/{a.slug}/{pid} -> {page_file(a, pid) or '(missing)'}")
        if a.manifest.get("handler"):
            print(f"handler: {a.manifest['handler']}")
        if a.manifest.get("settings"):
            print(f"settings: {json.dumps(a.manifest['settings'], ensure_ascii=False)}")
        warnings += a.warnings
    for a in found:
        if args.cmd != "show":
            warnings += a.warnings
    for w in warnings:
        print(f"  ! {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
