# Computer-Use Automation System

A goal → LLM-driven discovery → reusable artifact → deterministic replay
system, built against a deliberately legacy, server-rendered mock
core-banking console (see `target_app/`). Full design rationale, trade-offs
and what was cut is in [`REPORT.md`](./REPORT.md).

The working thread: a natural-language goal is accomplished once by an LLM
driving a real browser session; that successful run is recorded as a typed,
versioned **capability artifact**; the artifact then replays deterministically
— no model in the loop — with typed inputs/outputs, a three-way result
contract (success / named business outcome / hard failure), and a real
human-handoff path for anything irreversible.

## Setup

Requires Python 3.11+.

```bash
pip install -r requirements.txt
python -m playwright install chromium

cp .env.example .env    # then fill in ANTHROPIC_API_KEY for discovery runs
```

Start the target app (a separate terminal, or backgrounded — it needs to be
running for every command below):

```bash
python target_app/app.py
```

It serves on `http://127.0.0.1:5055`. Log in at `/login` with
`EMP001` / `demo1234` (hardcoded — see `target_app/app.py`'s module docstring;
this is a mock app with no real data). Seeded members and their behaviors
are listed there too: `10001`/`10002` are normal, `10003` is
permission-restricted, `10004` shows a one-time session-expired interstitial,
`10005` is artificially slow, and `99999` (or anything unseeded) doesn't exist.

**Replay never needs an API key** — it's pure browser automation against the
artifact's recorded steps. **Discovery does** — it's a real LLM tool-use loop
(Anthropic API; see `.env.example`).

## Demo path

```bash
# 1. Discovery: a real LLM-driven run that produces a capability artifact
python -m agent.run_discovery \
  --capability-name lookup_member_balance \
  --goal "Look up member 10001 and read their current savings balance." \
  --entry-path /members/search \
  --param member_id=10001 \
  --output member_name:string:"Member's full name" \
  --output savings_balance:number:"Current savings balance in USD"

# 2. A second authoring pass adds known error/recovery conditions this
# app is seeded to reproduce (why this is separate: agent/discovery_loop.py's
# module docstring)
python -m agent.augment_artifact --capability-name lookup_member_balance

# 3. Replay: deterministic, no LLM call
python -m replay.run_replay --capability-name lookup_member_balance --param member_id=10001

# 4. Replay hitting a real exceptional state — a member that doesn't exist
# is a named business outcome, not a crash
python -m replay.run_replay --capability-name lookup_member_balance --param member_id=99999
```

Each run prints its outcome and writes a full evidence trail (structured
log, screenshots, the artifact/result JSON) under `evidence/discovery/<run_id>/`
or `evidence/replay/<run_id>/`. See [`evidence/README.md`](./evidence/README.md)
for the full command set, including the richer `open_sub_account` capability
(multi-field form + a native confirmation dialog) and the human-escalation
demo.

### Running without live services

Nothing here calls out to the internet except the Anthropic API during
discovery. To exercise the whole harness with **no API key at all**,
including the escalation/handoff mechanism:

```bash
python3 tests/test_discovery_dry_run.py    # scripted stand-in for the LLM
python3 tests/test_replay_scenarios.py     # replay never needs an LLM anyway
```

These assert against the real code paths (locator inference, step recording,
the three-way outcome taxonomy, CDP-based session handoff) — only the
model's decisions are scripted. Their output goes to `/tmp`, never to
`/evidence/`, so it can't be mistaken for the graded run.

## Repo structure

```
target_app/          the mock legacy servicer console (Flask, server-rendered,
                      no test IDs, seeded error-injection knobs)
artifacts/schema/     the capability artifact contract (Pydantic) + the
                      replay result contract
artifacts/repository.py   flat-file artifact store (artifacts/store/)
agent/                discovery: browser surface, locator inference,
                      LLM tool-use loop, artifact assembly, augmentation pass
replay/               deterministic replay: locator resolution, the executor
guardrails/           allowlist, redaction, risk/escalation policy
escalation/           control-channel handoff, mock operator console,
                      the CDP-reattachment "simulated operator"
evidence/             where real run output lands (empty until you run it)
tests/                fixtures + the two no-API-key integration tests above
```

## A note on scope

This assignment allows (and expects) picking one concrete surface and
designing — not building — the rest. `REPORT.md` §4 covers the heterogeneity
and multi-tenant story; §7 says what was deliberately cut and why.
