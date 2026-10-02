#!/usr/bin/env python3
"""Start the /pa widget server if needed, then open the widget window.

    python open_widget.py              ensure server + open window (what the login task runs) — unless a widget
                                       window is already open (pages check in with the server; see widget_store)
    python open_widget.py --new-window open one anyway
    python open_widget.py --no-browser ensure server only
    python open_widget.py --restart    stop the running server first (after updating the code)
    python open_widget.py --stop       stop the server
    python open_widget.py --status     say whether it is running (the URL is printed with the key masked)

Window mode comes from "widget_browser" (pa.local.json):
    "default"   system default browser (a normal tab)
    "firefox"   a new Firefox window — use its "Add tab to taskbar" button once to make it a web app
    "shortcut"  run the file in "widget_shortcut" (e.g. the Firefox web-app shortcut) — standalone window
    "edge-app"  Microsoft Edge --app window (standalone, no tabs)
    "none"      don't open anything
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import due  # noqa: E402
from widget_store import Store  # noqa: E402

BROWSER_PATHS = {
    "firefox": [r"%ProgramFiles%\Mozilla Firefox\firefox.exe", r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe",
                r"%LocalAppData%\Mozilla Firefox\firefox.exe"],
    "edge": [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe", r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"],
}


def find_exe(name: str) -> str | None:
    for p in BROWSER_PATHS[name]:
        p = os.path.expandvars(p)
        if os.path.isfile(p):
            return p
    return shutil.which(name)


def masked_url(port: int) -> str:
    """The widget URL with the access key hidden — safe to print into logs and chat."""
    return f"http://127.0.0.1:{port}/?k=…"


def ping(port: int, key: str) -> bool | None:
    """True = our server with this key; False = something answered but not us; None = nothing listening."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/ping", headers={"X-PA-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=1.5) as r:
            return bool(json.loads(r.read()).get("ok"))
    except urllib.error.HTTPError:
        return False
    except (urllib.error.URLError, OSError, ValueError):
        return None


def start_server(port: int, pid_file: Path, cfg_arg: list[str]) -> None:
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pyw if pyw.exists() else sys.executable)
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen([exe, str(HERE / "pa_widget.py"), "--port", str(port), *cfg_arg],
                            creationflags=flags, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True)
    pid_file.write_text(str(proc.pid), encoding="utf-8")


def stop_server(pid_file: Path) -> bool:
    if not pid_file.exists():
        return False
    pid = pid_file.read_text(encoding="utf-8").strip()
    if os.name == "nt":
        ok = subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True).returncode == 0
    else:
        try:
            os.kill(int(pid), 15); ok = True
        except OSError:
            ok = False
    pid_file.unlink(missing_ok=True)
    return ok


def open_window(mode: str, url: str, cfg: dict) -> str:
    if mode == "none":
        return "not opening a window (widget_browser = none)"
    if mode == "shortcut":
        target = os.path.expandvars(os.path.expanduser(cfg.get("widget_shortcut", "")))
        if target and os.path.exists(target):
            os.startfile(target)  # type: ignore[attr-defined]
            return f"opened {target}"
        mode = "firefox"  # fall back so the user still gets a window
    if mode == "firefox" and (exe := find_exe("firefox")):
        subprocess.Popen([exe, "-new-window", url])
        return "opened in a new Firefox window"
    if mode == "edge-app" and (exe := find_exe("edge")):
        subprocess.Popen([exe, f"--app={url}", "--window-size=560,860"])
        return "opened as an Edge app window"
    webbrowser.open(url)
    return "opened in the default browser"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure") and sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--root"); ap.add_argument("--config")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--new-window", action="store_true", help="open a window even if one is already open")
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    cfg, cfg_path = due.load_config(args.config)
    root = due.resolve_root(args.root, cfg)
    if not root:
        print("No data root: set data_root in config.json or pass --root.")
        return 2
    cfg = due.settings(root, cfg)
    store = Store(root, cfg.get("widget_dir", "_widget"))
    key, port = store.key(), int(cfg.get("widget_port", 8765))
    pid_file = store.dir / "server.pid"
    url = f"http://127.0.0.1:{port}/?k={key}"
    cfg_arg = (["--config", str(cfg_path)] if cfg_path else []) + (["--root", str(root)] if args.root else [])

    if args.stop or args.restart:
        print("stopped" if stop_server(pid_file) else "no server pid on record")
        if args.stop:
            return 0
        time.sleep(0.5)

    status = ping(port, key)
    if args.status:
        print({True: f"running · {masked_url(port)}", False: f"port {port} is used by something else", None: "not running"}[status])
        return 0
    if status is False:
        print(f"Port {port} is taken by another program (or a server with a different key). "
              f"Stop it, or set widget_port in pa.local.json.")
        return 1
    if status is None:
        start_server(port, pid_file, cfg_arg)
        for _ in range(30):
            time.sleep(0.2)
            if ping(port, key):
                break
        else:
            print("Server did not come up. Try: python pa_widget.py (to see the error).")
            return 1
        print(f"server started on port {port}")
    if not args.no_browser:
        live = store.open_pages()
        if live and not args.new_window:
            print(f"already open in {len(live)} window{'s' if len(live) > 1 else ''} — not opening another "
                  "(--new-window to open one anyway)")
        else:
            print(open_window(cfg.get("widget_browser", "default"), url, cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
