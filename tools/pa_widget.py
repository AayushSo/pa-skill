#!/usr/bin/env python3
"""Local server for the /pa widget. Standard library only.

    python pa_widget.py            serve on 127.0.0.1:<widget_port> (default 8765)
    python pa_widget.py --port 0   pick a free port (tests)

Security: binds to 127.0.0.1 only; every /api call must carry the access key (X-PA-Key header),
and the Host header must be 127.0.0.1/localhost (blocks DNS-rebinding). No CORS headers are sent,
so other websites cannot read responses or send the custom header.

Areas add their own pieces through <area>/area.json (see areas.py):
    /area/<slug>/<page>       a page the area declared (no key needed to load the HTML; its data calls need it)
    /area/<slug>/api/<path>   handed to the area's declared handler — handle(request) -> (status, json)
    /doc/<slug>/<id>?k=<key>  a local document the area linked (opened in a new window, so the key comes as ?k=)
Handlers are loaded once, when the server starts; a failing handler takes down only its own area's routes.
Nothing outside what the manifests declare is reachable.

The page always reads tasks.md live, so edits made by /pa (or by hand) show up on the next refresh.
The widget never edits tasks.md: ticks, comments, snoozes and quick-added tasks go to the inbox for /pa to apply.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import webbrowser
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import areas as AR  # noqa: E402
import donelog  # noqa: E402
import due  # noqa: E402
import schedule as sch  # noqa: E402
from widget_store import EVENT_TYPES, Store  # noqa: E402

HTML = Path(__file__).resolve().parent / "widget.html"
MAX_BODY = 20_000
# The page's own accent values; both are swapped for the configured ones when it is served.
PAGE_ACCENT, PAGE_ACCENT_DARK = "#2f5d8a", "#8db8e0"


def helper_name(cfg: dict) -> str:
    return cfg.get("assistant_name") or "Assistant"


def accent(cfg: dict, dark: bool = False) -> str:
    """The accent colour as #rrggbb (config "accent" / "accent_dark"), falling back to the page's own."""
    raw = str(cfg.get("accent_dark" if dark else "accent") or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", raw):
        return raw.lower()
    return PAGE_ACCENT_DARK if dark else PAGE_ACCENT


def manifest(cfg: dict) -> dict:
    """Web app manifest so browsers offer "install as app" (own window + own taskbar icon). The app is named
    after the assistant. It deliberately holds no access key: the installed app reuses the one in localStorage."""
    name = helper_name(cfg)
    return {
        "name": name, "short_name": name, "start_url": "/", "scope": "/", "display": "standalone",
        "background_color": "#161615", "theme_color": accent(cfg), "description": f"Tasks, kept by {name}",
        "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}],
    }


def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == dy == 0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5


def _beak(s: float) -> list[tuple[float, float, float, float, float]]:
    """The beak as (ax, ay, bx, by, radius) segments — built once per size, not per pixel."""
    p0, p1, p2, steps = (34, 21), (49, 26), (53, 49), 24
    pts = []
    for i in range(steps + 1):
        t, u = i / steps, 1 - i / steps
        pts.append(((u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0]) * s,
                    (u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]) * s, (3.4 - 2.7 * t) * s))
    return [(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], pts[i][2]) for i in range(len(pts) - 1)]


def _ibis(px: float, py: float, s: float, beak: list) -> bool:
    """Inside the ibis head (the shape the page draws as SVG): skull, eye, neck, curved tapering beak."""
    if ((px - 29 * s) ** 2 + (py - 19.5 * s) ** 2) ** 0.5 <= 1.9 * s:
        return False                                              # the eye
    if ((px - 26 * s) ** 2 + (py - 22 * s) ** 2) ** 0.5 <= 9.2 * s:
        return True                                               # skull
    if _seg_dist(px, py, 25 * s, 27 * s, 21 * s, 48 * s) <= 4.1 * s:
        return True                                               # neck
    if 29 * s <= px <= 58 * s and 17 * s <= py <= 54 * s:         # only near the beak is it worth looking
        return any(_seg_dist(px, py, ax, ay, bx, by) <= r for ax, ay, bx, by, r in beak)
    return False


def icon_png(size: int, colour: str = PAGE_ACCENT) -> bytes:
    """A rounded accent square with the ibis head in white, drawn with the standard library only."""
    import struct
    import zlib

    rgb = bytes(int(colour[i:i + 2], 16) for i in (1, 3, 5))
    s = size / 64.0
    radius, beak = 14 * s, _beak(s)
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            px, py = x + 0.5, y + 0.5
            qx = min(max(px, radius), size - radius)
            qy = min(max(py, radius), size - radius)
            inside = (px - qx) ** 2 + (py - qy) ** 2 <= radius * radius
            if not inside:
                rows += b"\x00\x00\x00\x00"
            elif _ibis(px, py, s, beak):
                rows += b"\xff\xff\xff\xff"
            else:
                rows += rgb + b"\xff"

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


_ICONS: dict[tuple[int, str], bytes] = {}


def icon(size: int, colour: str = PAGE_ACCENT) -> bytes:
    if (size, colour) not in _ICONS:
        _ICONS[(size, colour)] = icon_png(size, colour)
    return _ICONS[(size, colour)]


def build_data(root: Path, cfg: dict, store: Store, today: date | None = None,
               extra_warnings: list[str] | None = None) -> dict:
    today = today or date.today()
    days = int(cfg.get("widget_days", 7))
    warnings: list[str] = [cfg["_settings_error"]] if cfg.get("_settings_error") else []
    areas = due.load_areas(root, cfg, warnings)
    b = due.classify(areas, today, today + timedelta(days=days))
    data = due.to_json(areas, b, today, days, due.week_number(today, root, cfg),
                       int(cfg.get("stale_after_days", 7)),
                       warnings + list(extra_warnings or []) + due.duplicate_id_warnings(areas)
                       + due.chain_warnings(areas),
                       frozenset(cfg.get("no_stale_check") or []))
    states = store.task_states()
    det = store.details()
    for t in data["tasks"]:
        s = states.get(t["id"] or "", {})
        t["pending_done"] = s.get("done", False)
        t["pending_snooze"] = s.get("snooze")
        t["comments"] = s.get("comments", [])
        d = det.get("details", {}).get(t["id"] or "")
        t["detail"] = d["text"] if d else None
        t["detail_updated"] = d["updated"] if d else None
    data["captures"] = store.captures()
    open_ids = {t["id"] for t in data["tasks"] if t["id"]}
    mine = {t["id"] for t in data["tasks"] if t["id"] and t["owner"] != "assistant"}
    data["focus"] = [i for i in det.get("focus", []) if i in mine]
    data["plan"] = {d: {b: [i for i in ids if i in mine] for b, ids in blocks.items()}
                    for d, blocks in det.get("plan", {}).items() if d >= today.isoformat()}
    # The assistant's own tasks: how each open one last went, and one-offs it finished in the last week.
    data["assistant_name"] = helper_name(cfg)
    data["accent"] = accent(cfg)
    names = {a.path.parent.name: a.name for a in areas}
    entries = donelog.load_all(root, names, [], [a.path.parent for a in areas])
    theirs = {t["id"] for t in data["tasks"] if t["id"] and t["owner"] == "assistant"}
    last: dict[str, dict] = {}
    for e in entries:  # newest first
        if e.task_id in theirs and e.task_id not in last:
            last[e.task_id] = e.as_json()
    data["assistant_runs"] = last
    week_ago = today - timedelta(days=7)
    data["assistant_done"] = [e.as_json() for e in entries
                              if e.owner == "assistant" and e.day >= week_ago and e.task_id not in open_ids]
    data["schedule"] = None
    if cfg.get("schedule_file"):
        try:
            s = sch.load(root / cfg["schedule_file"])
            problems = sch.validate(s)
            open_tasks = [t for a in areas for t in a.tasks if t.mark != "x"]
            data["schedule"] = {
                "days": sch.days(s, today, 7),
                "fixed_labels": [c.get("label", k) for k, c in s.get("categories", {}).items() if c.get("fixed")],
                "problems": problems,
                "clashes": [] if problems else sch.clashes(s, open_tasks, today, days),
            }
        except (OSError, ValueError, KeyError) as exc:
            data["schedule"] = {"days": [], "problems": [f"schedule file unreadable: {exc}"], "clashes": []}
    ctx = due.load_context(root, cfg, today)
    data["context"] = [{"from": f.isoformat(), "until": u.isoformat(), "note": n} for f, u, n in ctx.active]
    data["warnings"] += ctx.warnings + [f"context.md: expired {u.isoformat()} — remove: {n[:70]}" for _, u, n in ctx.expired]
    nudges = due.nudge_counts(root, cfg, areas)
    for t in data["tasks"]:
        t["nudged"] = nudges.get(t["id"] or "", 0)
    dirs, _ = AR.area_dirs(root, cfg)
    links, link_problems = AR.links(dirs, root, cfg)
    data["links"] = [{k: l[k] for k in ("id", "label", "href", "kind", "area")} for l in links]
    data["warnings"] += link_problems
    data["pa_updated"] = det.get("updated")
    data["missing_ids"] = sum(1 for t in data["tasks"] if not t["id"])
    return data


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def external_url(raw) -> str | None:
    """The URL if it is a plain outside http(s) link, else None. Never app pages, never other schemes."""
    if not isinstance(raw, str) or not raw or len(raw) > 4096:
        return None
    if any(c.isspace() or ord(c) < 32 for c in raw):
        return None
    try:
        u = urlsplit(raw)
        host = u.hostname
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not host or host in LOCAL_HOSTS:
        return None
    return raw


def launch_url(url: str, how: str) -> str:
    """Open an outside link in the user's browser (config "link_browser"). Returns what was used."""
    if how in ("firefox", "edge"):
        from open_widget import find_exe
        if exe := find_exe(how):
            subprocess.Popen([exe, url])
            if how == "firefox":
                threading.Thread(target=raise_window, args=("MozillaWindowClass",), daemon=True).start()
            return how
    if hasattr(os, "startfile"):
        os.startfile(url)  # type: ignore[attr-defined]  # the system default browser
        if default_is_firefox():
            threading.Thread(target=raise_window, args=("MozillaWindowClass",), daemon=True).start()
    else:
        webbrowser.open(url)
    return "default"


def default_is_firefox() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\Shell\Associations"
                            r"\UrlAssociations\https\UserChoice") as k:
            return str(winreg.QueryValueEx(k, "ProgId")[0]).startswith("FirefoxURL")
    except OSError:
        return False


def raise_window(win_class: str, wait: float = 2.0) -> bool:
    """Bring the browser's front window forward once the link has landed in it.

    Windows refuses SetForegroundWindow from a background process (this server), so the browser opens the tab
    but stays behind the widget. Attaching to the foreground window's input queue lifts that lock for the call.
    """
    if os.name != "nt":
        return False
    import ctypes
    import time
    from ctypes import wintypes
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32.GetForegroundWindow.restype = wintypes.HWND
    u32.FindWindowW.restype = wintypes.HWND
    u32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    for arg in ("SetForegroundWindow", "ShowWindow", "IsIconic", "BringWindowToTop"):
        getattr(u32, arg).argtypes = [wintypes.HWND] + ([ctypes.c_int] if arg == "ShowWindow" else [])
    time.sleep(0.4)                                   # let the browser take the URL first
    deadline = time.monotonic() + wait
    while (hwnd := u32.FindWindowW(win_class, None)) is None and time.monotonic() < deadline:
        time.sleep(0.2)                               # a cold start has no window yet
    if not hwnd:
        return False
    if u32.IsIconic(hwnd):
        u32.ShowWindow(hwnd, 9)                       # SW_RESTORE
    fg_thread = u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), None)
    me = k32.GetCurrentThreadId()
    attached = bool(fg_thread and fg_thread != me and u32.AttachThreadInput(me, fg_thread, True))
    try:
        u32.BringWindowToTop(hwnd)
        return bool(u32.SetForegroundWindow(hwnd))
    finally:
        if attached:
            u32.AttachThreadInput(me, fg_thread, False)


def make_handler(root: Path, cfg: dict, store: Store, port_ref: list[int]):
    key = store.key()
    log_path = store.dir / "server.log"
    # Area handlers: only those declared in area.json, loaded once here (restart the server to pick up changes).
    handlers: dict[str, tuple] = {}
    handler_problems: list[str] = []
    for d in AR.area_dirs(root, cfg)[0]:
        mod, problem = AR.load_handler(d)
        if mod:
            handlers[d.slug] = (mod, d)
        if problem:
            handler_problems.append(problem)

    class Handler(BaseHTTPRequestHandler):
        server_version = "pa-widget"

        def log_message(self, fmt, *a):
            """Small request log for troubleshooting (never logs the key; capped at ~200 KB)."""
            try:
                store.ensure()
                if log_path.exists() and log_path.stat().st_size > 200_000:
                    log_path.replace(log_path.with_suffix(".log.1"))
                msg = re.sub(r"k=[^&\s\"]+", "k=<key>", fmt % a)
                ua = (self.headers.get("User-Agent", "-") if getattr(self, "headers", None) else "-")[:80]
                host = self.headers.get("Host", "-") if getattr(self, "headers", None) else "-"
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"{self.log_date_time_string()} {self.client_address[0]} host={host} {msg} ua={ua}\n")
            except OSError:
                pass

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            return bool(re.fullmatch(rf"(127\.0\.0\.1|localhost):{port_ref[0]}", host))

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _authorized(self) -> bool:
            return hmac.compare_digest(self.headers.get("X-PA-Key", ""), key)

        def _area_api(self, method: str, path: str, body):
            """/area/<slug>/api/<rest> → that area's handler. The handler's errors stay inside its own response."""
            parts = path.split("/", 4)  # ['', 'area', slug, 'api', rest]
            slug = parts[2]
            entry = handlers.get(slug)
            if not entry:
                return self._json(404, {"error": f"area '{slug}' has no widget handler"})
            mod, d = entry
            from urllib.parse import parse_qs
            req = {"method": method, "path": parts[4] if len(parts) > 4 else "",
                   "query": parse_qs(urlsplit(self.path).query), "body": body,
                   "area_dir": d.path, "settings": dict(d.manifest.get("settings") or {}), "today": date.today(),
                   "assistant": helper_name(cfg), "accent": accent(cfg), "accent_dark": accent(cfg, dark=True)}
            try:
                code, obj = mod.handle(req)
                payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            except Exception as exc:  # the area's own code
                return self._json(500, {"error": f"{slug} handler: {type(exc).__name__}: {exc}"})
            return self._send(int(code), payload, "application/json; charset=utf-8")

        def _is_area_api(self, path: str) -> bool:
            parts = path.split("/")
            return len(parts) >= 4 and parts[1] == "area" and bool(parts[2]) and parts[3] == "api"

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"bad host", "text/plain")
            path = self.path.split("?", 1)[0]
            if path == "/":
                page = HTML.read_bytes()
                for old, new in ((PAGE_ACCENT, accent(cfg)), (PAGE_ACCENT_DARK, accent(cfg, dark=True))):
                    page = page.replace(old.encode(), new.encode())
                return self._send(200, page, "text/html; charset=utf-8")
            if path == "/manifest.webmanifest":
                return self._send(200, json.dumps(manifest(cfg)).encode("utf-8"), "application/manifest+json")
            if path in ("/icon-192.png", "/icon-512.png", "/favicon.ico"):
                return self._send(200, icon(512 if "512" in path else 192, accent(cfg)), "image/png")
            if path == "/api/data":
                if not self._authorized():
                    return self._json(403, {"error": "missing or wrong key"})
                try:
                    return self._json(200, build_data(root, cfg, store, extra_warnings=handler_problems))
                except Exception as exc:  # show the problem in the widget instead of a blank page
                    return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            if path == "/api/week":
                if not self._authorized():
                    return self._json(403, {"error": "missing or wrong key"})
                from urllib.parse import parse_qs, urlsplit
                q = parse_qs(urlsplit(self.path).query)
                try:
                    start = date.fromisoformat(q.get("start", [""])[0])
                except ValueError:
                    start = sch.week_monday(date.today())
                end = start + timedelta(days=6)
                warnings: list[str] = []
                areas = due.load_areas(root, cfg, warnings)
                names = {a.path.parent.name: a.name for a in areas}
                done = donelog.between(donelog.load_all(root, names, warnings, [a.path.parent for a in areas]),
                                       start, end)
                days = []
                if cfg.get("schedule_file"):
                    try:
                        s = sch.load(root / cfg["schedule_file"])
                        days = sch.days(s, start, 7)
                    except (OSError, ValueError, KeyError) as exc:
                        warnings.append(f"schedule file unreadable: {exc}")
                return self._json(200, {"start": start.isoformat(), "end": end.isoformat(),
                                        "days": days, "done": [e.as_json() for e in done],
                                        "warnings": warnings, "today": date.today().isoformat()})
            if path.startswith("/doc/"):
                # opened as a new tab, which cannot send headers: the key comes as ?k=
                from urllib.parse import parse_qs, urlsplit
                given = parse_qs(urlsplit(self.path).query).get("k", [""])[0]
                if not hmac.compare_digest(given, key):
                    return self._send(403, b"missing or wrong key", "text/plain")
                wanted = path[len("/doc/"):]
                doc = next((l for l in AR.links(AR.area_dirs(root, cfg)[0], root, cfg)[0]
                            if l["kind"] == "doc" and l["id"] == wanted), None)
                if not doc:
                    return self._send(404, b"not found", "text/plain")
                ctype = "text/html; charset=utf-8" if doc["path"].suffix.lower() in (".html", ".htm") else "text/plain; charset=utf-8"
                return self._send(200, doc["path"].read_bytes(), ctype)
            if self._is_area_api(path):
                if not self._authorized():
                    return self._json(403, {"error": "missing or wrong key"})
                return self._area_api("GET", path, None)
            if path.startswith("/area/"):
                parts = path.split("/")
                if len(parts) == 4 and parts[3]:
                    d = next((x for x in AR.area_dirs(root, cfg)[0] if x.slug == parts[2]), None)
                    page = AR.page_file(d, parts[3]) if d else None
                    if page:
                        return self._send(200, page.read_bytes(), "text/html; charset=utf-8")
                return self._send(404, b"not found", "text/plain")
            if path == "/api/ping":
                return self._json(200 if self._authorized() else 403, {"ok": self._authorized()})
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            # Read the body before any rejection: answering early makes Windows reset the connection.
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                self.close_connection = True
                return self._json(413, {"error": "bad body size"})
            raw = self.rfile.read(length) if length else b""
            if not self._host_ok():
                return self._send(403, b"bad host", "text/plain")
            path = self.path.split("?", 1)[0]
            if path not in ("/api/event", "/api/open") and not self._is_area_api(path):
                return self._send(404, b"not found", "text/plain")
            if not self._authorized():
                return self._json(403, {"error": "missing or wrong key"})
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                return self._json(400, {"error": "bad json"})
            if not isinstance(body, dict):
                return self._json(400, {"error": "bad json"})
            if self._is_area_api(path):
                return self._area_api("POST", path, body)
            if self.path == "/api/open":
                # An installed Edge app keeps target=_blank links in Edge; route outside links to the chosen browser.
                how = cfg.get("link_browser", "default")
                if how == "app":
                    return self._json(409, {"error": "link_browser is 'app': the page opens links itself"})
                url = external_url(body.get("url"))
                if not url:
                    return self._json(400, {"error": "not an outside http(s) link"})
                try:
                    used = launch_url(url, how)
                except OSError as exc:
                    return self._json(500, {"error": f"could not open the browser: {exc}"})
                return self._json(200, {"ok": True, "browser": used})
            etype, tid = body.get("type"), body.get("task_id")
            if etype not in EVENT_TYPES:
                return self._json(400, {"error": "bad type"})
            if etype in ("capture", "uncapture"):   # quick add: a new task, queued in the user's words
                try:
                    if etype == "uncapture":
                        return self._json(200, store.uncapture(str(body.get("ref", ""))))
                    folders = {a.path.parent.name for a in due.load_areas(root, cfg)}
                    return self._json(200, store.capture(body.get("text"), body.get("area"), body.get("due"), folders))
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
            task = next((t for a in due.load_areas(root, cfg) for t in a.tasks if t.id and t.id == tid), None)
            if not task:
                return self._json(400, {"error": "unknown task id"})
            event = {"type": etype, "task_id": tid, "area": task.area, "text": task.text}
            if etype == "done" and task.fields.get("yields"):
                event["yields"] = task.fields["yields"]  # kept so the outcome is still required after the line is gone
            if etype == "comment":
                comment = str(body.get("comment", "")).strip()
                if not comment:
                    return self._json(400, {"error": "empty comment"})
                event["comment"] = comment[:5000]
            if etype == "snooze":
                try:
                    until = date.fromisoformat(str(body.get("until")))
                except ValueError:
                    return self._json(400, {"error": "bad until date"})
                event["until"] = until.isoformat()
                # Snoozing past the task's date only makes sense if that date moves too; the page asks first.
                if task.when and task.when < until:
                    if body.get("moves_date") is not True:
                        return self._json(409, {"error": f"task is dated {task.when} — confirm moving it to {until}"})
                    event["moves_date"] = "next" if task.date_field("due") is None else "due"
            return self._json(200, store.append_event(event))

        def do_PUT(self):
            self._send(405, b"method not allowed", "text/plain")

        do_DELETE = do_PATCH = do_OPTIONS = do_PUT

    return Handler


def make_server(root: Path, cfg: dict, port: int) -> ThreadingHTTPServer:
    cfg = due.settings(root, cfg)
    store = Store(root, cfg.get("widget_dir", "_widget"))
    port_ref = [port]
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(root, cfg, store, port_ref))
    port_ref[0] = srv.server_address[1]
    return srv


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root")
    ap.add_argument("--config")
    ap.add_argument("--port", type=int)
    args = ap.parse_args()
    cfg, _ = due.load_config(args.config)
    root = due.resolve_root(args.root, cfg)
    if not root:
        print("No data root: set data_root in config.json or pass --root.")
        return 2
    cfg = due.settings(root, cfg)
    port = args.port if args.port is not None else int(cfg.get("widget_port", 8765))
    srv = make_server(root, cfg, port)
    print(f"pa widget on http://127.0.0.1:{srv.server_address[1]}/  (data root {root})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
