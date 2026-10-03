# CLAUDE.md

Guidance for Claude Code working in this repo. Human-facing docs are
[`README.md`](./README.md) (setup, demo path) and [`REPORT.md`](./REPORT.md)
(design rationale, trade-offs, cuts). This file is the fast-orientation and
conventions layer.

## What this is

Goal -> LLM-driven discovery -> reviewed capability artifact -> deterministic
replay, against a deliberately legacy mock banking console (`target_app/`).
An interface.ai take-home; the brief's seven REPORT headings and the
`/README.md`, `/REPORT.md`, `/evidence/` paths are fixed requirements.

## Commands

```bash
pip install -r requirements.txt && python -m playwright install chromium
pytest                         # 50 tests, no API key; starts target_app itself if needed
python target_app/app.py       # needed for the CLIs below (port 5055)

python -m agent.run_discovery --capability-name <name> --goal "..." \
  --entry-path /members/search --param member_id=10001 --output name:string:"..."
python -m agent.augment_artifact --capability-name <name>
python -m artifacts.review list | show <name> | approve <name> --reviewer <who>
python -m replay.run_replay --capability-name <name> --param member_id=10001
python -m escalation.operator_console      # port 5056
```

Discovery needs one provider key in `.env` (`.env.example` lists them).

## Map

| Path | What it is |
|---|---|
| `target_app/` | Mock legacy console: Flask, server-rendered, no test ids, seeded error members |
| `agent/` | Discovery: `browser_surface.py` (perceive/act), `locator_inference.py`, `discovery_loop.py`, `tools.py` |
| `agent/llm/` | Provider-agnostic model access: `base.py` (neutral contract), `gemini.py`, `openai_compat.py`, `router.py` (retry, failover) |
| `artifacts/schema/` | `CapabilityArtifact` and `ReplayResult` contracts. Read the module docstrings before changing either |
| `artifacts/repository.py`, `review.py` | Immutable per-version store; the draft -> approved gate and content hash |
| `replay/` | `locator_resolver.py`, `executor.py` (the engine; handoff logic is `_handoff`) |
| `guardrails/` | `allowlist.py` (policy), `network.py` (on-the-wire enforcement), `risk_policy.py`, `redaction.py`, `tokenizer.py`, `credentials.py` |
| `escalation/` | `transport.py` (interface), `control_channel.py` (file-based), `recorder.py` (observes the human), `inpage.py` (hand-back bar in headed runs), `notify.py`, `operator_console.py`, `simulated_operator.py` |

## Conventions that will bite if skipped

- **Replay never imports `agent/`.** No model client may become reachable
  from `replay/`.
- **Login is bootstrapped outside recorded steps** (`_bootstrap_session` in
  both `discovery_loop.py` and `replay/executor.py`), using
  `guardrails/credentials.py`. Don't record login as steps.
- **Stored artifact versions are immutable.** `repository.save()` refuses
  to overwrite; only a review-state change passes `allow_overwrite=True`.
  Changing what an artifact does means a new version, which starts as a
  draft.
- **Tests that edit a fixture must re-`approve()` it.** Fixtures are
  approved, and approval is bound to a content hash; an edited artifact is
  `REFUSED` otherwise.
- **`RunInterrupted` subclasses `BaseException` on purpose**, so the broad
  `except Exception` backstops can't swallow an operator's stop. Keep any
  new broad handler from catching it.
- **Escalation code depends on `ControlTransport`**, not on
  `ControlChannel`'s file details. The file writes are atomic (temp file +
  rename); keep them so.
- **While a human holds the session, wait with `recorder.pump()`**, not
  `time.sleep()`. Sync Playwright only delivers the recorder's callbacks
  (and the network guard's) while the owning thread is inside a Playwright
  call.
- **Call `guard.begin()` before an action and `guard.raise_if_blocked()`
  after it.** `begin(allow_risky=True)` only when a human or an approved
  artifact has cleared that action.
- **Redaction and tokenization are different mechanisms.** `redaction.py`
  is one-way, for what is written to disk. `tokenizer.py` is two-way, for
  what reaches the model. Ordinary ids such as `member_id` are deliberately
  touched by neither; don't broaden the patterns to "anything numeric".
- **A human-typed or sensitive-looking value that isn't a declared input is
  never stored** in an artifact; the step is flagged for a human instead.
- **Test output goes to `/tmp`, real runs to `evidence/`.** Never point a
  test at `evidence/` or at the real `artifacts/store/`.
- **Tests never call a real model.** Inject a scripted client through
  `DiscoveryRun(llm=...)`.
