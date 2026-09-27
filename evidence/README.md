# /evidence/

This directory holds the record of real runs: one discovery run (LLM-driven,
against the live target app) and at least one replay run, per the
assignment brief. It's empty in this checkout on purpose — these are
populated by actually running the system, not checked in as static fixtures.

## What must land here before submission

Run these in order (see the top-level README for full setup):

```bash
# 1. Discover the read-only lookup capability
python -m agent.run_discovery \
  --capability-name lookup_member_balance \
  --goal "Look up member 10001 and read their current savings balance." \
  --entry-path /members/search \
  --param member_id=10001 \
  --output member_name:string:"Member's full name" \
  --output savings_balance:number:"Current savings balance in USD"

# 2. Discover the richer write capability (multi-field form + confirmation dialog)
python -m agent.run_discovery \
  --capability-name open_sub_account \
  --goal "Open a new sub-account for member 10001 with nickname 'Vacation Fund' and an initial deposit of 150, and reach the confirmation screen." \
  --entry-path /members/search \
  --param member_id=10001 --param nickname="Vacation Fund" --param initial_deposit=150 \
  --param-type initial_deposit=number \
  --output new_account_number:string:"The newly created sub-account's account number"

# 3. Add the known_outcomes / recoverable_patterns second authoring pass
# (see agent/augment_artifact.py's docstring for why this is a separate step)
python -m agent.augment_artifact --capability-name lookup_member_balance
python -m agent.augment_artifact --capability-name open_sub_account

# 4. Replay: happy path
python -m replay.run_replay --capability-name lookup_member_balance --param member_id=10001

# 5. Replay: a genuine error/exceptional state (member doesn't exist -> business outcome,
# not a crash)
python -m replay.run_replay --capability-name lookup_member_balance --param member_id=99999

# 6. Replay hitting the escalation path for real (reattaches via CDP, doesn't fake it)
python -m replay.run_replay --capability-name open_sub_account \
  --param member_id=10002 --param nickname="Rainy Day" --param initial_deposit=100 \
  --simulate-operator "Confirm & Open Account"
```

Each command writes a timestamped run folder under `discovery/` or `replay/`
containing `log.jsonl` (structured, redacted trace of what happened and why),
`screenshots/`, and `artifact.json` / `result.json`.

## What's here instead, for now

`tests/test_discovery_dry_run.py` and `tests/test_replay_scenarios.py`
exercise the exact same code paths end-to-end with a scripted stand-in for
the LLM (discovery only — replay never calls one), so the harness itself is
verified without needing an API key. Their output goes to `/tmp`, not here,
so it's never mistaken for the graded evidence.
