# Computer-Use Automation System

Gives an AI agent a way to operate a legacy back-office app that has no API.
An LLM drives the real UI once to work out how to do something (**discovery**).
That run is recorded as a typed, versioned **capability artifact**. Once a
person has reviewed and approved it, the same flow runs again and again with
no model in the loop (**deterministic replay**), returning typed outputs, a
named business outcome, or a debuggable failure. When a run can't safely
proceed, a human takes over the same live browser session and hands it back.

The target is a deliberately legacy mock core-banking console
(`target_app/`): server-rendered, table layout, no ids or test ids, a native
confirmation dialog, and seeded members that reproduce runtime conditions on
demand.

- Design write-up: [`REPORT.md`](./REPORT.md)
- Evidence from real runs: [`evidence/`](./evidence/)

## Where each requirement is met

Row numbers refer to [`evidence/INDEX.md`](./evidence/INDEX.md).

| Brief | Requirement | Implemented in | Evidence |
|---|---|---|---|
| 3.1 | Goal-driven agent loop | `agent/discovery_loop.py`, `agent/tools.py`, `agent/llm/` | rows 1, 11, 17: real LLM runs, each decision logged with its reason |
| 3.2 | Structured, versioned artifact | `artifacts/schema/artifact.py`, `artifacts/store/`, `artifacts/review.py` | the store itself; `python -m artifacts.review show <name>` |
| 3.3 | Deterministic replay with an outcome taxonomy | `replay/executor.py`, `artifacts/schema/result.py` | rows 2–9, 12–14: success, business outcomes, recovery, hard failure |
| 3.4 | Safety and policy guardrails | `guardrails/` | rows 8 and 10; `tests/test_guardrails.py` |
| 3.5 | Evidence and observability | every run folder: `log.jsonl`, screenshots | row 7: failure with screenshot and DOM snapshot |
| 3.6 | Human escalation and live-session handoff | `escalation/`, `replay/executor.py` (`_handoff`) | rows 15–17, and three runs resolved by a person ([`evidence/README.md`](./evidence/README.md)) |
| 3.7 | Heterogeneity and multi-tenant design | `replay/surface.py`, `artifacts/profiles/`; [`REPORT.md`](./REPORT.md) §4 | `tests/test_surface_seam.py`: the engine replaying on a non-browser surface |
| 8 | Stretch: approval gate; agent-facing catalog | `artifacts/review.py`, `capabilities/` | row 10; rows 18–20 |

## Setup

Requires Python 3.11+.

```bash
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
```

Replay and the test suite need no API key. Discovery needs one LLM provider
key in `.env`.

Start the target app and leave it running (`http://127.0.0.1:5055`):

```bash
python target_app/app.py
```

Seeded members: `10001` and `10002` are normal, `10003` is
permission-restricted, `10004` shows a one-time session-expired
interstitial, `10005` loads slowly, and anything else does not exist. The
login is the mock app's own demo credential; nothing here is real data.

### LLM providers

Discovery is provider-agnostic (`agent/llm/`): Gemini natively, and anything
that speaks the OpenAI Chat Completions format through one adapter
(OpenRouter, OpenAI, Groq, or any endpoint via `LLM_BASE_URL`).

```bash
LLM_PROVIDERS=openrouter,gemini      # order to try; unset = every provider with a key
OPENROUTER_API_KEY=...
GEMINI_API_KEY=...
```

A rate limit, overload or timeout is retried with backoff, then the run
fails over to the next provider and continues the same conversation. The
model must support tool calling; pass `--no-vision` if it can't take images.

## Demo path

```bash
# 1. Discovery: a real LLM-driven run that records a capability artifact
python -m agent.run_discovery \
  --capability-name lookup_member_balance \
  --goal "Look up member 10001 and read their current savings balance." \
  --entry-path /members/search \
  --param member_id=10001 \
  --output member_name:string:"Member's full name" \
  --output savings_balance:number:"Current savings balance in USD"

# 2. Apply the vendor app's outcome profile: the known outcomes and
#    recoverable conditions a successful run never sees, declared once per
#    app in artifacts/profiles/ (saved as the next minor version)
python -m artifacts.profile apply lookup_member_balance

# 3. Review: discovery only produces a DRAFT, and a draft is refused for
#    unattended replay. Read what it will do, then approve it.
python -m artifacts.review show lookup_member_balance
python -m artifacts.review approve lookup_member_balance --reviewer "$USER"

# 4. Replay: deterministic, no LLM
python -m replay.run_replay --capability-name lookup_member_balance --param member_id=10001
```

Replay with other inputs to see each outcome class:

| Input | Outcome |
|---|---|
| `--param member_id=10001` | `success`, with `member_name` and `savings_balance` |
| `--param member_id=99999` | `business_outcome`: `member_not_found` |
| `--param member_id=10004` | `success`, with the session-expired recovery listed in `recovered_steps` |
| target app stopped | `failure` at `session_bootstrap`, with a screenshot and DOM snapshot |
| before step 3 (still a draft) | `refused`; add `--attended` to validate a draft by hand |

Every run writes a structured log, screenshots, and its artifact or result
JSON under `evidence/discovery/<run_id>/` or `evidence/replay/<run_id>/`.

The committed evidence set (20 scenarios, indexed in
[`evidence/INDEX.md`](./evidence/INDEX.md)) is produced by one command that
runs these CLIs in order and checks each outcome:

```bash
python scripts/make_evidence.py --reviewer "$USER"
```

## Calling a capability as an agent would

Approved artifacts are exposed as a catalog of tools with typed arguments
(`capabilities/`). An agent sees names, JSON-Schema inputs and outputs, and
the named outcomes; it never sees steps or a browser, and drafts are not
listed.

```bash
python -m capabilities list
python -m capabilities invoke lookup_member_balance --args '{"member_id": "10002"}'
python -m capabilities invoke open_sub_account --confirmed-by-review \
  --args '{"member_id": "10002", "nickname": "Rainy Day", "initial_deposit": 100}'
```

`invoke` runs deterministic replay pinned to the approved version and
prints the result. Missing, unexpected or mistyped arguments come back as
`input_error` before a browser opens. A capability with an irreversible step
returns `escalated` after a timeout rather than blocking the caller, unless
`--confirmed-by-review` lets the artifact's approval stand as confirmation.
From Python: `capabilities.list_tools()` and `capabilities.invoke(name, args)`.

## Human handoff

A run hands its live browser session to a person when:

- the model asks for one (`request_human`), or the harness sees it stuck;
- the model is about to do something irreversible during discovery;
- a replay step is marked as needing a human;
- a replay step fails, if you passed `--escalate-on-failure`.

When a run pauses, the terminal prints why, where to act, and how to hand
back. The simplest way to resolve one by hand is a headed run:

```bash
python -m replay.run_replay --capability-name open_sub_account --attended --headed \
  --param member_id=10002 --param nickname="Rainy Day"
```

Act in the browser window that opens. A bar at the bottom of the page says
why the run stopped and has the hand-back buttons: "I completed this step",
"automation runs this step", or "Cancel". Your clicks and field edits on the
page are recorded automatically.

For a headless or remote run there is no window, so use the operator
console, which lists every run waiting on a human:

```bash
python -m escalation.operator_console          # http://127.0.0.1:5056
```

It shows the same request plus a screenshot and the run's CDP endpoint; open
`chrome://inspect`, add that endpoint under "Discover network targets" and
click "inspect" to drive the page, then hand back from the console.

Without a person present, `--simulate-operator` attaches to the same session
over CDP and performs scripted actions. Here the goal withholds a decision,
so the model has to ask:

```bash
python -m agent.run_discovery --capability-name open_sub_account \
  --goal "Open a new sub-account for member 10001. The nickname and the deposit amount are a supervisor's decision; do not choose them yourself." \
  --entry-path /members/search --param member_id=10001 --param nickname="Vacation Fund" \
  --output new_account_number:string:"The new sub-account's number" \
  --allow-irreversible \
  --simulate-operator "fill:nickname=Vacation Fund" \
  --simulate-operator "fill:initial_deposit=150" \
  --simulate-operator "click:Continue"
```

`--allow-irreversible` is there because one script can't answer two
different pauses; without it the run also stops for approval before the
final, irreversible confirm click.

## Guardrails

Policy is configuration (`.env`), enforced the same way in discovery and
replay:

| Setting | Effect |
|---|---|
| `ALLOWLIST_DOMAINS` | hosts the session may talk to; empty blocks everything |
| `ALLOWLIST_ROUTES` | path globs allowed on those hosts, e.g. `/,/login,/members/*` |
| `ALLOWLIST_ACTIONS` | action types allowed at all (omit `fill` for a read-only agent) |
| `RISKY_ROUTES` | path globs where a non-GET request is irreversible, e.g. `*/confirm` |

A request that leaves the allowlist is aborted before it is sent.
Irreversible actions go to a human. Logs, artifacts and DOM snapshots are
redacted; credentials never reach an artifact, a log, or the model.

## Tests

```bash
pytest
```

62 tests, about a minute, no API key and nothing to start first: the model's
decisions are scripted, the target app is started by the test session if it
isn't running, and output goes to `/tmp`, never to `evidence/`. Everything
else is the real code path, including the CDP handoff.

## Layout

```
target_app/     the mock legacy console (Flask, server-rendered)
agent/          discovery: browser surface, locator inference, the
                observe -> decide -> act loop, tool surface
agent/llm/      provider-agnostic model access: adapters, retry, failover
artifacts/      the artifact and result contracts (schema/), the versioned
                store, the review gate, and per-vendor outcome profiles
capabilities/   the agent-facing catalog: list tools, invoke by name
replay/         deterministic replay: the Surface interface, its Playwright
                implementation, and the executor (which imports no driver)
guardrails/     allowlist policy, on-the-wire enforcement, risk policy,
                redaction, tokenizer, credential seam
escalation/     control transport, human-action recorder, operator console,
                scripted operator
evidence/       output of real runs, with an index and a README
scripts/        make_evidence.py, which regenerates the evidence set
tests/          pytest suite and artifact fixtures
```
