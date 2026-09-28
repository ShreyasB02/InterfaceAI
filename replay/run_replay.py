"""
CLI entry point for a deterministic replay run — no LLM involved. Loads a
saved artifact from the store and executes it against live input params.

Examples:

    # happy path
    python -m replay.run_replay --capability-name lookup_member_balance --param member_id=10001

    # hits a business outcome (member doesn't exist)
    python -m replay.run_replay --capability-name lookup_member_balance --param member_id=99999

    # hits an irreversible step and escalates to a human; --simulate-operator
    # reattaches via CDP to the same live session and performs the click,
    # proving the handoff mechanism rather than mocking it
    python -m replay.run_replay --capability-name open_sub_account \\
        --param member_id=10001 --param nickname="Vacation Fund" --param initial_deposit=100 \\
        --simulate-operator "Confirm & Open Account"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv

from artifacts import repository
from artifacts.validate import validate_required_params
from escalation.simulated_operator import simulate_operator_takeover
from replay.executor import ReplayExecutor

load_dotenv()


def _parse_param(s: str) -> tuple[str, str]:
    name, _, value = s.partition("=")
    if not _:
        raise argparse.ArgumentTypeError(f"--param must be name=value, got {s!r}")
    return name, value


def main():
    parser = argparse.ArgumentParser(description="Deterministically replay a saved capability artifact.")
    parser.add_argument("--capability-name", required=True)
    parser.add_argument("--version", default=None, help="Artifact major version, defaults to latest.")
    parser.add_argument("--param", action="append", default=[], type=_parse_param, dest="params")
    parser.add_argument("--target-base-url", default=os.environ.get("TARGET_APP_BASE_URL", "http://127.0.0.1:5055"))
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--auto-approve", action="store_true",
                         help="Bypass human escalation for irreversible steps. For repeatable "
                              "testing/demo only — never appropriate for an unreviewed artifact "
                              "in real unattended production replay.")
    parser.add_argument("--simulate-operator", default=None, metavar="BUTTON_LABEL",
                         help="If escalation is hit, reattach via CDP and click this button, "
                              "proving the handoff mechanism without a human physically present.")
    parser.add_argument("--escalation-timeout", type=float, default=None,
                         help="Seconds to wait for a human before returning ESCALATED instead of blocking forever.")
    parser.add_argument("--evidence-root", default="evidence/replay")
    args = parser.parse_args()

    try:
        artifact = repository.load(args.capability_name, args.version)
    except FileNotFoundError as e:
        print(f"Could not load artifact: {e}", file=sys.stderr)
        sys.exit(1)

    # Same check ReplayExecutor.run() makes before opening a browser (see
    # artifacts/validate.py) — done again here so a bad call fails
    # immediately, before even creating an evidence directory, rather than
    # waiting to hit the identical check one layer down.
    params = dict(args.params)
    err = validate_required_params(artifact, params)
    if err:
        print(f"Input error: {err}", file=sys.stderr)
        sys.exit(1)

    executor = ReplayExecutor(artifact, Path(args.evidence_root), headless=not args.headed)

    on_escalation = None
    if args.simulate_operator:
        def on_escalation(control, ctx):  # noqa: ANN001
            t = threading.Thread(
                target=simulate_operator_takeover,
                args=(control, args.simulate_operator),
                kwargs={"accept_dialog": True},
                daemon=True,
            )
            t.start()

    print(f"Replaying {artifact.name} v{artifact.version} (run {executor.run_id})")
    try:
        result = executor.run(
            params,
            base_url=args.target_base_url,
            auto_approve=args.auto_approve,
            escalation_timeout_s=args.escalation_timeout,
            on_escalation=on_escalation,
        )
    except Exception as e:
        # ReplayExecutor.run() already catches the failure modes it expects
        # (LocatorResolutionError, AllowlistViolation) and turns them into a
        # proper FAILURE ReplayResult. This is the backstop for what it
        # doesn't expect — most commonly the target app not running at all,
        # so the very first page.goto() during session bootstrap raises a
        # raw Playwright connection error before that inner try block is
        # even reached. Never let that reach the caller as a bare traceback.
        print(f"\nReplay crashed unexpectedly: {type(e).__name__}: {e}", file=sys.stderr)
        print(f"Evidence (if any) is at {executor.evidence_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"\nOutcome: {result.outcome}")
    print(json.dumps(result.model_dump(mode="json"), indent=2, default=str))


if __name__ == "__main__":
    main()
