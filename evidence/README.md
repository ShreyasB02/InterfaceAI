# Evidence

Output of real runs. Each run folder holds `log.jsonl` (the structured,
redacted trace of what happened and why), `screenshots/`, and
`artifact.json` (discovery) or `result.json` (replay).

## What is here

| Folder | What it is |
|---|---|
| `discovery/lookup_member_balance_20260927T115235_5cb009/` | A genuine LLM-driven discovery run (`gemini-3.8-flash`) that completed the goal and produced the 6-step `lookup_member_balance` artifact |
| `replay/replay_lookup_member_balance_20260927T115337_79e702/` | Deterministic replay of that artifact, `member_id=10001`: `success`, with typed outputs |
| `replay/replay_lookup_member_balance_20260927T115355_5eadbf/` | Replay with `member_id=99999`: `business_outcome`, `member_not_found` |

The other folders under `discovery/` are earlier attempts that stopped
before finishing, kept as they happened.

These runs predate later changes to the log and result formats (provider
and model on every decision, structured failures with DOM snapshots, the
review gate, observed human actions). The commands to regenerate the full
set, including the handoff runs, are in the top-level
[`README.md`](../README.md).
