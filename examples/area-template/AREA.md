# Area instructions — reading list

Read this when the reading area comes up. It extends the generic /pa procedure.

**What lives here:** `items.json` is the list. The user triages it on the widget page (`pages/list.html`,
served through `tools/widget_api.py`); you add to it and read it back with `tools/readinglist.py`:

```bash
python tools/readinglist.py summary            # counts; run before a briefing (declared in area.json)
python tools/readinglist.py list [--status unread]
python tools/readinglist.py add --title T --url U
```

**Division of labour**
- The user moves items between unread / reading / done / dropped on the page.
- When an item reaches `done`, add a task to write its takeaway into `notes.md` (with `yields:`), unless the
  user already wrote it.
- Mention the list in the briefing only when something needs the user: more than 10 unread, or an item stuck in
  `reading` for two weeks.
