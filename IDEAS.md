# Ideas not yet built

A parking place for things worth doing later, so they are not re-derived. Nothing here is committed to.

## From a survey of similar projects (2026-09-17)

Looked at Claude Code assistant skill packs ([claude-ea-skills](https://github.com/jenn-creator/claude-ea-skills),
[skill-daily-briefing](https://github.com/milesskorpen/skill-daily-briefing)), markdown-vault second brains
([second-brain-open](https://github.com/liams975/second-brain-open),
[open-second-brain](https://github.com/itechmeat/open-second-brain), [Khoj](https://github.com/khoj-ai/khoj)) and
mature task systems with an LLM layer ([Obsidian Tasks](https://github.com/obsidian-tasks-group/obsidian-tasks),
Taskwarrior, [tasknotes-skill](https://github.com/vanillaflava/tasknotes-skill)). None enforces that a task owes a
conclusion, keeps the assistant's own tasks, or checks the agent's edits, but several do one part better:

1. **Test the procedure, not only the tools.** Static tests (structure, no model) plus behavioural tests that run the
   skill headless and snapshot the output. Every bug so far has been in the procedure, not the scripts.
2. **`until:` on a task** — it expires by itself (Taskwarrior). Already wanted by hand ("this task lapses on <date>").
3. **Recurrence from completion**, not only from a fixed date (Obsidian Tasks): "4 weeks after I actually do it"
   versus "every 4th Monday". `every:` only does the second.
4. **Promote repeated corrections into rules** (open-second-brain's "dream pass"): deterministic counters, so the
   third time the user corrects the same thing the agent proposes a standing rule for the local rules file, and
   rules nothing uses are retired. Nudges already count; this is the same idea for corrections.
5. **Write receipts with rollback** (open-second-brain): a per-write log and an undo. Fold into auto-filing (below).
6. **A first-run setup interview** (skill-daily-briefing): the skill asks about areas and settings and writes its own
   config, instead of hand-edited JSON.
7. **Later, maybe:** an MCP server over these tools so other clients (phone, desktop) share one store; a priority or
   urgency score for ranking.

Rejected on purpose: git as the data store (private data stays out of git; snapshots cover history), and any
dashboard reachable beyond 127.0.0.1.

## Scripting what does not need reasoning (2026-09-17)

In rough order of payoff: one startup command that prints everything a run needs; a `taskctl` for structured task
edits (add / close / defer / drop / recur) so the model stops hand-editing files; auto-applying unambiguous widget
events; recording nudges from the scanner's own list; a weekly-review runner; suggested schedule placement; job
triage task skeletons. The model keeps conclusions, fit notes, choosing the day, reading the user's messages,
spotting drift and deciding what is worth interrupting for.

## Auto-filing ticked tasks, with review (2026-09-17, designed not built)

Plain ticks (no `yields:`) are filed to done.md immediately by the server — no model, no waiting — and recorded in
the widget inbox as already applied, with hints for the review: another task was waiting on this one, the area's
follow-up rule applies, it was recurring, it freed a scheduled block. The agent reviews at the next run or when the
chat opens, raises anything of consequence and acknowledges the rest. A tick on a task that owes a conclusion asks
the user inline for the outcome instead. Undo re-adds the task and appends a `reopened:` line rather than editing
done.md. Open questions: undo window, whether the filed list shows in the widget.

## Claude in the widget (2026-09-17, feasibility done)

Feasible by spawning the Claude Code CLI headless (`--print --output-format stream-json --resume`), which reuses the
existing subscription and skill; an API-key route only matters if this ships to other people. Phases: questions with
no tools, then approved `taskctl` operations, then the whole briefing. One session per day, started by the morning
run and reset when the date changes, with a size cap that summarises and restarts. Care needed on permissions,
prompt injection from listing and email text, and one agent writing at a time.
