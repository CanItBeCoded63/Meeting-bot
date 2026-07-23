# Memory System — Reference for Future Enhancements

Written 2026-07-08. This branch (and the new `dev`/`main`) ship the **simple**
memory system (single meeting-link scoping, one `remember` tool, one
auto-generated summary per meeting). A separate, more advanced memory system
was built independently on the old `dev` branch (`feature/memory-improvements`,
merged via `a68f65a`) and was **deliberately not merged in** — it conflicts
structurally with the simple system's `memory.py`/`main.py` and would need
real reconciliation work, not a quick merge.

This doc summarizes what that branch has, so it can be selectively
reimplemented later without re-deriving it from scratch.

## Where the code still lives

Nothing was deleted. The full commit history is preserved on the local/pushed
branch `magent` (and `origin/magent` if pushed) — same content, just not on
`dev`/`main` anymore. To look at it directly:

```bash
git log --oneline magent-pre-dev-rebase..magent | grep -i memory
git show <commit> -- client/joinly_client/memory.py
```

Relevant commits (in `magent`'s history, not in `dedicatedprofile-v2`/new `dev`/`main`):

| Commit | What it does |
|---|---|
| `43d20ce` | Shared namespace, new scopes/types, recency retrieval, `retrieve_memory` tool (tagged M1,M2,M4,M6,M7,H1,H4) |
| `c1eb16e` | Post-meeting auto-extraction pipeline (M5) |
| `edccfad` | Mid-session prompt refresh + agent handoff (M3, H6) |
| `ded5a3b` | Improved prompt injection quality (H7) |
| `a68f65a` | Merge commit integrating the above into the old `dev` branch |
| `8df36e4` | Fix for `profile_dir` support that the above merge accidentally dropped |

## What it adds over the simple system

### Schema (`client/joinly_client/memory.py` — `MemoryEntry`)

- **New scope**: `meeting_series` (in addition to `agent`, `project`, `client`,
  `meeting`, `participant`, `global`).
- **Shared cross-agent scopes**: `global`, `client`, `participant`, `meeting`,
  `meeting_series` now partition under a shared `"shared:<scope>"` key instead
  of `"<agent_id>:<scope>"` — multiple agents can read/write the same
  memories instead of each agent having an isolated silo.
- **New memory types**: `follow_up`, `blocker` (in addition to `fact`,
  `preference`, `decision`, `action_item`, `participant_profile`,
  `relationship`).
- **New fields**: `canonical_participant_id` (resolved cross-platform identity
  — e.g. same person across Teams/Zoom), `tags` (list[str]), `superseded_by`
  (row_key of the entry that replaces this one — versioning/dedup), `item_status`
  (`pending`/`completed`/`blocked`/`delegated` — for action items/follow-ups).
- **Different TTL defaults**: e.g. `fact` now expires after 365 days (simple
  system: never expires), `decision` after 730 days, `follow_up` after 60 days.
  Worth deciding deliberately rather than inheriting — the simple system's
  "facts never expire" was a conscious choice for meeting-summary content.

### New capabilities

- **`retrieve_memory` tool** — lets the LLM explicitly query memory mid-conversation
  ("what did we decide about X last time?") instead of only getting whatever
  was pre-injected into the prompt at join time. The simple system has no
  equivalent; memory context is fixed once per meeting at prompt-build time.
- **Post-meeting auto-extraction pipeline** — parses the transcript after the
  meeting and derives multiple structured `MemoryEntry` rows (decisions,
  action items, follow-ups) automatically. The simple system only saves one
  free-text summary blob (via the LLM-generated meeting summary), not
  structured entries.
- **Mid-session prompt refresh** — re-injects updated memory context into the
  agent's system prompt *during* a live meeting (e.g. after a `remember` call),
  not just once at join. The simple system builds the prompt once and never
  updates it mid-meeting.
- **Improved prompt injection quality** (H7) — refinements to how the memory
  block is formatted/prioritized in the system prompt.

## Why it wasn't merged

Trial-merged and aborted on 2026-07-08 (no commit made). Real conflicts in
`client/joinly_client/main.py` and `memory.py` — not line-level conflicts, but
two different generations of the same subsystem that need deliberate design
reconciliation (e.g.: do we want shared cross-agent scopes now that multi-agent
via `agents.yaml` is centralized? does auto-extraction replace or complement
the single summary blob?). Four other conflicting files were trivial
(`.gitignore`, `deploy.sh`, `pyproject.toml`, `uv.lock` — regenerate, don't
hand-merge).

The simple system was chosen for `dev`/`main` because it's the one actually
verified live in both Azure dev and prod today (Alex recalling a real prior
meeting's summary, memory saved to Azure Table Storage, confirmed via direct
table query).

## Suggested approach if reviving this

1. Decide on the schema first (shared scopes vs per-agent, new memory types,
   TTL defaults) — this affects every other piece.
2. Port `retrieve_memory` tool and mid-session refresh independently; they're
   additive and don't require the schema change.
3. Auto-extraction pipeline is the biggest lift — needs its own eval (does it
   produce better structured data than the current single-summary approach,
   or just more noise?).
4. Re-test `client/joinly_client/main.py`'s chat-trigger and auto-leave logic
   carefully — `a68f65a`'s merge notes mention "COMPLEX MERGE" here with
   deletions/insertions on both sides.
