# Example area: reading list

A minimal area showing everything an area can add to /pa. Copy this folder into your data root (or anywhere),
rename it, and list it in `<data_root>/pa.local.json`:

```json
{"areas": [{"path": "reading"}]}
```

| File | Role |
|---|---|
| `about.md`, `tasks.md`, `done.md` | the three files every area has |
| `area.json` | what this area adds: instructions, scripts /pa runs, a widget page, links, a handler |
| `AREA.md` | instructions /pa reads when this area comes up |
| `tools/readinglist.py` | the area's own logic and its command line (run by /pa) |
| `tools/widget_api.py` | `handle(request)` — answers the page's calls under `/area/<folder>/api/` |
| `pages/list.html` | the page, served at `/area/<folder>/list`; calls `api/...` relative to itself |
| `items.json`, `notes.md` | the area's data |

The handler is loaded when the widget server starts, so restart the server after changing it.
Everything in `area.json` must point at files inside this folder; scripts and handlers must be `.py` files.
