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

cp .env.example .env    # then add at least one LLM provider key for discovery runs
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
artifact's recorded steps. **Discovery does** — it's a real LLM tool-use loop.

### LLM providers

Discovery is provider-agnostic (`agent/llm/`). Gemini is supported natively,
and anything that speaks the OpenAI Chat Completions format works through one
adapter: OpenRouter, OpenAI, Groq, or any other compatible endpoint
(`LLM_BASE_URL`).

Set one or more keys in `.env` and, optionally, an order:

```bash
LLM_PROVIDERS=openrouter,gemini
OPENROUTER_API_KEY=...
GEMINI_API_KEY=...
```

A rate limit, overload (503) or timeout is retried with backoff
(`LLM_MAX_RETRIES`, default 3), then the run fails over to the next provider
and carries on with the same conversation. Every retry and failover is
written to the run's evidence log, and each `decide` event records which
provider and model answered. The model must support tool calling; pass
`--no-vision` if it can't take images.

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

# 3. Review: discovery only ever produces a DRAFT, and a draft is refused
# for unattended replay. Read what it will do, then approve it. Approval is
# bound to a hash of the reviewed content; editing the artifact voids it.
python -m artifacts.review show lookup_member_balance
python -m artifacts.review approve lookup_member_balance --reviewer "$USER"

# 4. Replay: deterministic, no LLM call
python -m replay.run_replay --capability-name lookup_member_balance --param member_id=10001

# 5. Replay hitting a real exceptional state — a member that doesn't exist
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

Nothing here calls out to the internet except the configured LLM provider during
discovery. To exercise the whole harness with **no API key at all**,
including the escalation/handoff mechanism:

```bash
python3 tests/test_discovery_dry_run.py    # scripted stand-in for the LLM
python3 tests/test_discovery_handoff.py     # discovery stuck -> human on the live session -> resume
python3 tests/test_artifact_store.py        # versioning and the review gate (no browser)
python3 tests/test_llm_providers.py         # provider adapters, retry and failover (no network)
python3 tests/test_replay_scenarios.py     # replay never needs an LLM anyway
```

These assert against the real code paths (locator inference, step recording,
the three-way outcome taxonomy, CDP-based session handoff) — only the
model's decisions are scripted. Their output goes to `/tmp`, never to
`/evidence/`, so it can't be mistaken for the graded run.

## Guardrails

Policy is configuration (`.env`, see `.env.example`), enforced the same way
in discovery and replay:

| Setting | Effect |
|---|---|
| `ALLOWLIST_DOMAINS` | hosts the session may talk to; empty blocks everything |
| `ALLOWLIST_ROUTES` | path globs allowed on those hosts, e.g. `/,/login,/members/*` |
| `ALLOWLIST_ACTIONS` | action types allowed at all (omit `fill` for a read-only agent) |
| `RISKY_ROUTES` | path globs where a non-GET request is irreversible, e.g. `*/confirm` |

A request that leaves the allowlist is aborted before it is sent, not
noticed afterwards. An irreversible action needs a human: discovery pauses
for approval when the model declares one (`--allow-irreversible` disables
this for a sandbox), and replay escalates the step unless the artifact is
approved and run with `--auto-approve`.

```bash
python3 tests/test_guardrails.py    # policy, on-the-wire enforcement, redaction
```

## Human handoff

A run hands its live browser session to a person when the model asks for
one, when the harness sees it stuck, when a replay step needs confirmation,
or (with `--escalate-on-failure`) when a replay step fails. To resolve one
by hand:

```bash
python -m escalation.operator_console          # http://127.0.0.1:5056
# open /console?run_dir=<the run's evidence folder>
```

The console shows why the run stopped, a screenshot, and the CDP endpoint.
Open `chrome://inspect`, add that endpoint under "Discover network targets"
and click "inspect" to drive the same page (or pass `--headed` and use the
window). Your clicks and field edits are recorded automatically; then hand
back from the console.

Without a person present, `--simulate-operator` reattaches over CDP and
performs scripted actions on the same session (replay takes the same flag;
see [`evidence/README.md`](./evidence/README.md)):

```bash
# discovery: the goal leaves a decision to a supervisor, so the model calls request_human
python -m agent.run_discovery --capability-name open_sub_account \
  --goal "Open a new sub-account for member 10001. The nickname and the deposit amount are a supervisor's decision; do not choose them yourself." \
  --entry-path /members/search --param member_id=10001 --param nickname="Vacation Fund" \
  --output new_account_number:string:"The new sub-account's number" \
  --simulate-operator "fill:nickname=Vacation Fund" --simulate-operator "fill:initial_deposit=150" \
  --simulate-operator "click:Continue"

```

## Repo structure

```
target_app/          the mock legacy servicer console (Flask, server-rendered,
                      no test IDs, seeded error-injection knobs)
artifacts/schema/     the capability artifact contract (Pydantic) + the
                      replay result contract
artifacts/repository.py   flat-file artifact store (artifacts/store/), one immutable
                          file per version
artifacts/review.py       the draft -> approved | rejected gate
agent/                discovery: browser surface, locator inference,
                      LLM tool-use loop, artifact assembly, augmentation pass
replay/               deterministic replay: locator resolution, the executor
guardrails/           allowlist policy + on-the-wire enforcement, redaction,
                      tokenizer, risk policy, credential seam
escalation/           control transport, human-action recorder, mock operator
                      console, the CDP-reattachment "simulated operator"
evidence/             where real run output lands (empty until you run it)
tests/                fixtures + the two no-API-key integration tests above
```

## A note on scope

This assignment allows (and expects) picking one concrete surface and
designing — not building — the rest. `REPORT.md` §4 covers the heterogeneity
and multi-tenant story; §7 says what was deliberately cut and why.
