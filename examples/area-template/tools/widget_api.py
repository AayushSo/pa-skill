"""The reading list's widget handler: the /pa widget server passes /area/<folder>/api/... here.

Contract (see the skill's tools/areas.py):
    handle(request) -> (http status, JSON-serialisable body)
    request = {"method": "GET" | "POST", "path": "<after api/>", "query": {name: [values]},
               "body": dict | None, "area_dir": Path, "settings": dict (area.json "settings"), "today": date}

Loaded once when the server starts. Import your helper modules at the top of this file (the server forgets them
after loading, so another area's module of the same name never replaces yours). Keep it small: the logic lives
in readinglist.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import readinglist as RL  # noqa: E402


def handle(req):
    path = RL.items_path(req["settings"])
    if req["method"] == "GET" and req["path"] == "items":
        return 200, RL.load(path)
    body = req["body"] or {}
    if req["method"] == "POST" and req["path"] == "items":
        item, problem = RL.change(path, lambda d: RL.add(d, body.get("title"), body.get("url"),
                                                          req["today"].isoformat()))
        return (400, {"error": problem}) if problem else (200, {"item": item})
    if req["method"] == "POST" and req["path"] == "status":
        problem = RL.change(path, lambda d: RL.set_status(d, str(body.get("id")), str(body.get("status"))))
        return (400, {"error": problem}) if problem else (200, {"ok": True})
    return 404, {"error": f"no route {req['method']} {req['path']}"}
