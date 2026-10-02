#!/usr/bin/env python3
"""Local snapshots of the user's data, and a check that finished work was not erased or rewritten.

The data folder is deliberately outside git, so this is its only history. Snapshots are zip files in
<data_root>/<snapshot_dir> (default "_snapshots") and never leave the machine.

    python snapshot.py weekly            if the last snapshot is older than snapshot_every_days (default 7):
                                         check against it, take a new one, prune old ones. Otherwise one line.
    python snapshot.py take [--reason bulk|manual]   take one now (always do this before a bulk or scripted edit)
    python snapshot.py check [--against FILE]        compare the data with a snapshot (default: the newest)
    python snapshot.py list
    python snapshot.py diff FILE PATH    unified diff of one file: snapshot → now (PATH as listed in the zip)
    python snapshot.py restore FILE PATH writes PATH.restored next to the current file; never overwrites anything

What goes in: text files (.md .json .jsonl .txt .csv .html .py .yaml .yml .toml) under the data root and every listed area,
up to 5 MB each. Left out: dot-files (credentials and tool state), the widget's access key and log, binary files,
the snapshot and export folders, and anything matching "snapshot_exclude" (glob patterns) in pa.local.json.

What `check` reports, comparing a snapshot with now:
  - a done.md whose earlier lines changed or disappeared (done.md only ever grows at the end);
  - a task that was open then and is gone now without a done.md line carrying its id (or its text);
  - a task whose due date moved, or whose next: moved other than forward on a recurring task;
  - a task whose text changed (listed, so a blind rewrite is visible).
Settings (pa.local.json): snapshot_dir, snapshot_every_days (7), snapshot_keep (26 scheduled + 10 others),
snapshot_exclude ([]).
"""
from __future__ import annotations

import argparse
import difflib
import fnmatch
import re
import sys
import zipfile
from datetime import date, datetime
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import areas as AR  # noqa: E402
import due  # noqa: E402

TEXT = {".md", ".json", ".jsonl", ".txt", ".csv", ".html", ".htm", ".py", ".yaml", ".yml", ".toml"}
MAX_BYTES = 5_000_000
NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4}(?:\d{2})?)_([a-z]+)(?:-\d+)?\.zip$")
EXTERNAL = "_external"


def snap_dir(root: Path, cfg: dict) -> Path:
    return root / cfg.get("snapshot_dir", "_snapshots")


def sources(root: Path, cfg: dict) -> list[tuple[Path, str]]:
    """(file, name inside the zip) for everything a snapshot holds."""
    widget = cfg.get("widget_dir", "_widget")
    skip_dirs = {cfg.get("snapshot_dir", "_snapshots"), cfg.get("export_dir", "_exports"), "__pycache__", ".git"}
    patterns = list(cfg.get("snapshot_exclude") or [])
    bases = [(root, "")]
    for a in AR.area_dirs(root, cfg)[0]:
        try:
            a.path.resolve().relative_to(root.resolve())
        except ValueError:  # an area that lives outside the data root
            bases.append((a.path, f"{EXTERNAL}/{a.slug}/"))
    out = []
    for base, prefix in bases:
        for p in sorted(base.rglob("*")):
            rel = p.relative_to(base)
            name = prefix + rel.as_posix()
            if not p.is_file() or p.suffix.lower() not in TEXT:
                continue
            if any(part.startswith(".") for part in rel.parts) or skip_dirs & set(rel.parts):
                continue
            if not prefix and rel.parts[0] == widget and p.name in ("key", "server.log", "server.log.1", "server.pid"):
                continue
            if any(fnmatch.fnmatch(name, pat) for pat in patterns):
                continue
            try:
                if p.stat().st_size > MAX_BYTES:
                    continue
            except OSError:
                continue
            out.append((p, name))
    return out


def snapshots(root: Path, cfg: dict) -> list[Path]:
    d = snap_dir(root, cfg)
    return sorted((p for p in d.glob("*.zip") if NAME_RE.match(p.name)), key=lambda p: p.name) if d.is_dir() else []


def last_date(root: Path, cfg: dict) -> date | None:
    snaps = snapshots(root, cfg)
    return date.fromisoformat(NAME_RE.match(snaps[-1].name).group(1)) if snaps else None


def due_notice(root: Path, cfg: dict, today: date) -> str | None:
    """One line for the briefing when a scheduled snapshot + check is due; None otherwise."""
    every = int(cfg.get("snapshot_every_days", 7))
    if every <= 0:
        return None
    last = last_date(root, cfg)
    if last and (today - last).days < every:
        return None
    when = f"last {last.isoformat()}" if last else "none yet"
    return f"Snapshot + integrity check due ({when}): python {Path(__file__).resolve()} weekly"


def take(root: Path, cfg: dict, reason: str, now: datetime | None = None) -> tuple[Path, int]:
    now = now or datetime.now()
    d = snap_dir(root, cfg)
    d.mkdir(parents=True, exist_ok=True)
    stem = f"{now:%Y-%m-%d_%H%M%S}_{reason}"
    path, n = d / f"{stem}.zip", 2
    while path.exists():
        path, n = d / f"{stem}-{n}.zip", n + 1
    tmp = path.with_suffix(".tmp")
    files = sources(root, cfg)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for p, name in files:
            try:
                z.write(p, name)
            except OSError:
                continue
    tmp.replace(path)
    return path, len(files)


def prune(root: Path, cfg: dict) -> list[Path]:
    """Keep the newest `snapshot_keep` scheduled snapshots and the newest 10 others."""
    keep = int(cfg.get("snapshot_keep", 26))
    snaps = snapshots(root, cfg)
    scheduled = [p for p in snaps if NAME_RE.match(p.name).group(3) == "weekly"]
    others = [p for p in snaps if p not in scheduled]
    gone = scheduled[:-keep] if keep > 0 else []
    gone += others[:-10]
    for p in gone:
        p.unlink(missing_ok=True)
    return gone


# ---------- check ----------
def _text(data: bytes) -> str:
    return data.decode("utf-8-sig", errors="replace").replace("\r", "")  # line endings are not a change


def _current_name_map(root: Path, cfg: dict) -> dict[str, Path]:
    return {name: p for p, name in sources(root, cfg)}


def check(root: Path, cfg: dict, snap: Path) -> list[str]:
    problems: list[str] = []
    now_files = _current_name_map(root, cfg)
    with zipfile.ZipFile(snap) as z:
        old = {n: z.read(n) for n in z.namelist()}

    # done.md only ever grows at the end
    for name, data in old.items():
        if PurePosixPath(name).name != "done.md":
            continue
        before = _text(data).rstrip("\n")
        cur = now_files.get(name)
        if cur is None:
            problems.append(f"{name}: was in the snapshot, now missing")
            continue
        after = _text(cur.read_bytes())
        if not after.startswith(before):
            b, a = before.split("\n"), after.split("\n")
            i = next((k for k in range(len(b)) if k >= len(a) or b[k] != a[k]), len(b))
            was = b[i] if i < len(b) else ""
            now = a[i] if i < len(a) else "(gone)"
            problems.append(f"{name}:{i + 1} changed above the end — was: {was[:90]!r} now: {now[:90]!r}")

    # tasks: vanished without a record, re-dated, reworded
    def tasks_of(name: str, text: str):
        return {t.id: t for t in due.parse_area(Path(name), text).tasks if t.id and t.mark != "x"}

    done_now = " \n".join(_text(p.read_bytes()) for n, p in now_files.items() if PurePosixPath(n).name == "done.md")
    done_ids = set(re.findall(r"\|\s*id:([a-z0-9]{3,12})\b", done_now, re.IGNORECASE))
    for name, data in old.items():
        if PurePosixPath(name).name != "tasks.md":
            continue
        before = tasks_of(name, _text(data))
        cur = now_files.get(name)
        after = tasks_of(name, _text(cur.read_bytes())) if cur else {}
        for tid, t in before.items():
            n = after.get(tid)
            if n is None:
                if tid not in done_ids and t.text[:60] not in done_now:
                    problems.append(f"{name}: task {tid} disappeared without a done.md record — {t.text[:80]!r}")
                continue
            od, nd = t.fields.get("due"), n.fields.get("due")
            if od != nd:
                problems.append(f"{name}: task {tid} due {od or '(none)'} → {nd or '(none)'} — {n.text[:60]!r}")
            on, nn = t.fields.get("next"), n.fields.get("next")
            if on != nn and not ("every" in n.fields and on and nn and nn > on):
                problems.append(f"{name}: task {tid} next {on or '(none)'} → {nn or '(none)'} — {n.text[:60]!r}")
            if t.text != n.text:
                problems.append(f"{name}: task {tid} reworded — was {t.text[:60]!r}")
    return problems


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--root"); ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("weekly")
    p = sub.add_parser("take"); p.add_argument("--reason", default="manual", choices=["manual", "bulk"])
    p = sub.add_parser("check"); p.add_argument("--against")
    sub.add_parser("list")
    p = sub.add_parser("diff"); p.add_argument("file"); p.add_argument("path")
    p = sub.add_parser("restore"); p.add_argument("file"); p.add_argument("path")
    args = ap.parse_args()
    cfg, _ = due.load_config(args.config)
    root = due.resolve_root(args.root, cfg)
    if not root:
        print("No data root: set data_root in config.json or pass --root.")
        return 2
    cfg = due.settings(root, cfg)
    snaps = snapshots(root, cfg)

    def pick(name: str | None) -> Path | None:
        if not name:
            return snaps[-1] if snaps else None
        p = Path(name)
        return p if p.is_file() else next((s for s in snaps if s.name == name), None)

    if args.cmd == "weekly":
        notice = due_notice(root, cfg, date.today())
        if not notice:
            print(f"Snapshot not due (last {last_date(root, cfg)}).")
            return 0
        problems = check(root, cfg, snaps[-1]) if snaps else []
        path, n = take(root, cfg, "weekly")
        gone = prune(root, cfg)
        print(f"Snapshot {path.name}: {n} files." + (f" Pruned {len(gone)} old one(s)." if gone else ""))
        if snaps:
            print(f"Checked against {snaps[-1].name}: " + (f"{len(problems)} finding(s)" if problems else "nothing unexpected."))
            for pr in problems:
                print(f"  ! {pr}")
            if problems:
                print(f"  Compare with: python {Path(__file__).resolve()} diff {snaps[-1].name} <path>")
        return 0
    if args.cmd == "take":
        path, n = take(root, cfg, args.reason)
        prune(root, cfg)
        print(f"Snapshot {path.name}: {n} files.")
        return 0
    if args.cmd == "list":
        for s in snaps:
            print(f"{s.name}  {round(s.stat().st_size / 1024)} KB")
        if not snaps:
            print("No snapshots yet.")
        return 0
    snap = pick(getattr(args, "against", None) if args.cmd == "check" else args.file)
    if not snap:
        print("No such snapshot." if snaps else "No snapshots yet.")
        return 1
    if args.cmd == "check":
        problems = check(root, cfg, snap)
        print(f"Against {snap.name}: " + (f"{len(problems)} finding(s)" if problems else "nothing unexpected."))
        for pr in problems:
            print(f"  ! {pr}")
        return 0
    with zipfile.ZipFile(snap) as z:
        if args.path not in z.namelist():
            print(f"{args.path} is not in {snap.name}.")
            return 1
        data = z.read(args.path)
    target = _current_name_map(root, cfg).get(args.path)
    if target is None:
        rel = PurePosixPath(args.path)
        if rel.parts[0] == EXTERNAL:
            print("That file belongs to an area outside the data root that is no longer listed; restore it by hand.")
            return 1
        target = root.joinpath(*rel.parts)
    if args.cmd == "diff":
        now = _text(target.read_bytes()).splitlines(keepends=True) if target.is_file() else []
        sys.stdout.writelines(difflib.unified_diff(_text(data).splitlines(keepends=True), now,
                                                   f"{snap.name}:{args.path}", f"now:{args.path}"))
        return 0
    out = target.with_name(target.name + ".restored")
    if out.exists():
        print(f"{out} already exists — move it away first.")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"Wrote {out}. Compare it with the current file and copy over what should come back.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
