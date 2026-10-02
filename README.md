# pa — a personal-assistant skill for Claude Code

A `/pa` skill that tracks life areas as plain Markdown task files and gives a daily briefing:
overdue, due soon, status unknown, follow-ups due, waiting on someone.

**This repo holds code only, and nothing about any particular area.** Your tasks, notes, settings — and any tools
or pages a single area needs — live in your own data folders, which are never committed. The skill only knows how
to find areas and what an area may add.

## Layout

```
~/.claude/skills/pa/          ← this repo
  SKILL.md                    the procedure Claude follows
  tools/due.py                scans every area's tasks.md and prints the briefing
  tools/areas.py              finds the areas and reads what each one declares (area.json)
  tools/pa_widget.py, …       the widget, the schedule, the done-log reader
  examples/area-template/     a small working area: a reading list with a script, a page and a handler
  tests/                      regression tests on synthetic fixtures
  config.example.json         copy to config.json (gitignored) — just data_root
  pa.local.example.json       copy to <data_root>/pa.local.json — every other setting

<data_root>/                  ← NOT in git
  pa.local.json               the list of areas (in display order) and settings
  <local_rules>               your paths, people, follow-up rules, sensitive-data rules
  index.md                    human-readable overview of the areas
  <area>/about.md             ≤40-line orientation
  <area>/tasks.md             open tasks, one per line
  <area>/done.md              append-only log
  <area>/area.json            optional: what this area adds (instructions, scripts, pages, links, a handler)
```

## Setup

1. Clone into `~/.claude/skills/pa`.
2. `cp config.example.json config.json` and set `data_root`.
3. Copy `pa.local.example.json` to `<data_root>/pa.local.json`, list your areas and adjust the settings.
4. Create `<data_root>/<local_rules>` and each area's folder with at least a `tasks.md` (format: `SKILL.md` §2).
   To see what an area can add, copy `examples/area-template` in as an area.
5. `python tools/due.py` should print a briefing. Then run `/pa` in Claude Code.

## Areas

An area is a folder with `about.md`, `tasks.md` and `done.md`. Anything else it needs is its own business, declared
in `<area>/area.json` (all keys optional; reference in `tools/areas.py`):

- `instructions` — a file Claude reads when the area comes up (e.g. `AREA.md`);
- `on_load` / `review` — Python scripts inside the area that Claude runs before a briefing / in the weekly review;
- `pages`, `links` — widget pages served at `/area/<folder>/<id>`, and Quick links (a page, a local file, or a URL);
- `handler` — a Python file with `handle(request)` that answers the area's pages at `/area/<folder>/api/...`;
- `settings` — free-form values for the area's code.

Everything named there must be inside the area's folder. Handlers are loaded once when the widget server starts, and
a failing one only affects its own area. `python tools/areas.py list` shows what every area declares.

## Widget

A local page with a tickbox per task, a click-open panel (detail, comments) and snooze. It never edits
`tasks.md`: ticks, comments and snoozes go to an inbox that `/pa` applies at its next run (see `SKILL.md` §8).

```bash
python tools/open_widget.py            # start the server (127.0.0.1, access-key protected) and open the window
python tools/open_widget.py --status   # or --restart / --stop
```

A theme switch (Auto / Light / Dark) and a `?` shortcut panel sit in the top bar; the running notes fold into one
line. The page is titled and branded with `assistant_name` (an ibis mark, drawn inline and in the app icon), coloured by
`accent` / `accent_dark`. Window mode is `widget_browser` in `pa.local.json`: `default`, `firefox`, `shortcut` (run `widget_shortcut`),
`edge-app`, or `none`. Outside links open in `link_browser` (`default`, `firefox`, `edge`, or `app`).

**Own taskbar icon:** open the widget once in a normal Edge/Chrome window (the URL with `?k=` saves the key),
install it as an app from the address-bar install icon, pin it, then copy the app's shortcut somewhere stable and set
`widget_browser: "shortcut"` + `widget_shortcut` to it. Turn off the browser's own "run on login" for the app so it
doesn't open before the server. Use `127.0.0.1`, not `localhost` — HTTPS-only browser modes upgrade `localhost`.
To start it at login on Windows, point a logon scheduled task at `pythonw.exe tools\open_widget.py`.

## Schedule (optional)

Point `schedule_file` at a JSON weekly schedule in the data root (format documented in `tools/schedule.py`) and the
widget gains a Now/Next card, a Today timeline with tasks placed into blocks, and a Week tab that steps through
calendar weeks — deadlines and clashes ahead, and for days already past, what was finished or dropped that day.
`tools/schedulectl.py` shows, checks and renders it (into `schedule_doc`, between `<!-- schedule:begin -->` and
`<!-- schedule:end -->`). Tasks can carry `at:HH:MM` to appear at a fixed time. Blocks can carry a `short` name for
the compact views.

**Layout:** below 760 px one column (sticky NOW/NEXT bar, "Areas" button); from 760 px one left rail, always visible:
today's mini schedule on top, the area filter below it, then Quick links from the areas' `links` and from
`links` in `pa.local.json`. Local documents open in a new window (served at `/doc/<area>/<id>?k=…`, restricted to
files inside that area's folder); area pages replace the widget window.

## The assistant's own tasks

A task with `owner:assistant` is work the agent does itself on its next run (a weekly import, a lookup). The briefing
lists those first and the agent does them before briefing; the widget shows them in a collapsible section at the
top — no tickbox, with each one's last run and result from `done.md`. The agent's on-screen name is
`assistant_name` in `pa.local.json`.

## Reasoning, not just logging

A task whose point is the information carries a `yields:` field naming what it owes (a conclusion, a decision, a
number and what it implies). Recording the data does not close such a task: `widgetctl.py ack` refuses it without
`--outcome "what it says · what changes · next step"`, and the briefing ends with "What this changes".

## Waiting and follow-ups

A task blocked on someone else carries `waiting:` and a `chase:` date — the day to follow up if nothing has come back
(default `chase_days`, 14). It stays listed under "Waiting on someone" until then; from that day the briefing and the
widget list it under **Follow up** and ask: chase them, keep waiting, or close it. Snoozing a waiting task in the
widget sets its chase date.

## Memory between sessions

No session log. Two small, structured pieces instead: `<data_root>/context.md` holds the few important things in
force right now, with end dates (shown as an alert at the top of the widget, flagged when expired), and `widgetctl.py nudge` counts how many days a
task has been raised without being done (shown as `nudged N×`, reset when the task is re-dated).

## Export

`tools/export_csv.py` writes all tasks, open and finished, as one CSV for reading in a spreadsheet
(`<data_root>/_exports/tasks-<date>.csv`, UTF-8 with a BOM so Excel is happy; `--open`, `--done`, `--area`,
`--since`, `--out -`). It is one-way: nothing is ever imported back from the file.

## Other sessions

`<data_root>/CLAUDE.md` and a line in each `tasks.md` / `done.md` header tell sessions that are not running the
skill to treat the records as read-only: read them for context, but leave changes to the skill. Such a session can
queue a comment with `widgetctl.py note <task id> "…"` instead of editing a task.

## Snapshots

The data folder is never in git, so `tools/snapshot.py` keeps local zip snapshots of its text files in
`<data_root>/_snapshots/` (dot-files, the widget key and binaries are left out). About once a week the briefing says
one is due; `snapshot.py weekly` then compares the data with the previous snapshot — finished work edited or removed,
tasks gone without a record, dates moved, tasks reworded — takes a new one and prunes old ones. `diff` and `restore`
(which writes a `.restored` copy, never overwriting) help undo damage.

## Tests

```bash
python -m unittest discover -s tests
```

Standard library only (Python 3.10+).
