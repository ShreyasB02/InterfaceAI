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

Tier 2 #7's other two exit paths can be demoed the same way, without a
physically present operator:

    # cancel a stuck escalation instead of resolving it -> reported as FAILURE
    python -m replay.run_replay --capability-name open_sub_account \\
        --param member_id=10001 --param nickname="Vacation Fund" --param initial_deposit=100 \\
        --simulate-cancel-after 2

    # interrupt the run outright, mid-automation -> ReplayOutcome.INTERRUPTED
    python -m replay.run_replay --capability-name lookup_member_balance \\
        --param member_id=10001 --simulate-interrupt-after 0.5

    # take over on a step that was never flagged risky, then hand back
    python -m replay.run_replay --capability-name lookup_member_balance \\
        --param member_id=10001 --simulate-takeover-after 0
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

from artifacts import repository
from artifacts.validate import validate_required_params
from escalation.control_channel import ControlChannel
from escalation.simulated_operator import parse_action, simulate_operator_takeover
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
    parser.add_argument("--version", default=None,
                         help="Exact version (1.1.0) or prefix (1). Defaults to the latest approved "
                              "version, or the latest of any status with --attended.")
    parser.add_argument("--attended", action="store_true",
                         help="A person is running this to validate the artifact. Allows a draft to "
                              "run; its risky steps still escalate. Without this flag only an "
                              "approved artifact replays.")
    parser.add_argument("--param", action="append", default=[], type=_parse_param, dest="params")
    parser.add_argument("--target-base-url", default=os.environ.get("TARGET_APP_BASE_URL", "http://127.0.0.1:5055"))
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--auto-approve", action="store_true",
                         help="Don't escalate irreversible steps: the review that approved the "
                              "artifact stands as their confirmation. Refused for a draft.")
    parser.add_argument("--escalate-on-failure", action="store_true",
                         help="When a step fails and replay can't recover, hand the live session to a "
                              "human instead of returning FAILURE straight away.")
    parser.add_argument("--simulate-operator", action="append", default=None, metavar="ACTION",
                         help="If an escalation is hit, reattach via CDP and perform this action on the "
                              "same live session: 'click:<button label>' or 'fill:<field name>=<value>' "
                              "(a bare label means click). Repeatable. Proves the handoff without a "
                              "human physically present.")
    parser.add_argument("--operator-leaves-step", action="store_true",
                         help="With --simulate-operator: the operator hands the paused step back for "
                              "automation to run, instead of having performed it themselves.")
    parser.add_argument("--simulate-cancel-after", type=float, default=None, metavar="SECONDS",
                         help="Tier 2 #7's 'cancel': once an escalation is hit, wait this many "
                              "seconds and then cancel it instead of resolving it — the run reports "
                              "a FAILURE rather than hanging or timing out. Mutually exclusive with "
                              "--simulate-operator / --simulate-takeover-after (only one on-escalation "
                              "behavior can be simulated per run).")
    parser.add_argument("--simulate-takeover-after", type=float, default=None, metavar="SECONDS",
                         help="Tier 2 #7's 'takeover': wait this many seconds from run start, then "
                              "request manual control on whatever step runs next — even one never "
                              "flagged risky — then auto-resume shortly after, proving automation "
                              "picks the same step back up rather than skipping it. Mutually "
                              "exclusive with --simulate-operator / --simulate-cancel-after.")
    parser.add_argument("--simulate-interrupt-after", type=float, default=None, metavar="SECONDS",
                         help="Tier 2 #7's 'interrupt': wait this many seconds from run start, then "
                              "end the run outright, whether or not an escalation is pending. "
                              "Can be combined with any of the other --simulate-* flags.")
    parser.add_argument("--escalation-timeout", type=float, default=None,
                         help="Seconds to wait for a human before returning ESCALATED instead of blocking forever.")
    parser.add_argument("--evidence-root", default="evidence/replay")
    args = parser.parse_args()

    exclusive = [args.simulate_operator, args.simulate_cancel_after, args.simulate_takeover_after]
    if sum(1 for x in exclusive if x is not None) > 1:
        print("Only one of --simulate-operator / --simulate-cancel-after / --simulate-takeover-after "
              "may be used at a time (they each install a different on-escalation behavior). "
              "--simulate-interrupt-after may be combined with any of them.", file=sys.stderr)
        sys.exit(1)

    try:
        artifact = repository.load(args.capability_name, args.version,
                                   approved_only=not args.attended and args.version is None)
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
        try:
            operator_actions = [parse_action(a if ":" in a else f"click:{a}") for a in args.simulate_operator]
        except ValueError as e:
            print(e, file=sys.stderr)
            sys.exit(1)

        def on_escalation(control, ctx):  # noqa: ANN001
            t = threading.Thread(
                target=simulate_operator_takeover,
                args=(control,),
                kwargs={"actions": operator_actions, "accept_dialog": True,
                        "step_done": False if args.operator_leaves_step else None},
                daemon=True,
            )
            t.start()
    elif args.simulate_cancel_after is not None:
        def on_escalation(control, ctx):  # noqa: ANN001
            def _cancel():
                time.sleep(args.simulate_cancel_after)
                control.cancel(reason=f"Simulated operator gave up after {args.simulate_cancel_after}s.")
            threading.Thread(target=_cancel, daemon=True).start()
    elif args.simulate_takeover_after is not None:
        def on_escalation(control, ctx):  # noqa: ANN001
            def _auto_resume():
                time.sleep(0.5)  # models a brief look-around before handing back
                control.mark_human_active()
                control.record_human_action("Simulated operator looked around, nothing needed; handing back control.")
                control.signal_resume()
            threading.Thread(target=_auto_resume, daemon=True).start()

    if args.simulate_takeover_after is not None:
        # Unlike the other --simulate-* flags, this one has to act BEFORE
        # the escalation exists (it's what causes one), on a control
        # channel pointed at the same evidence dir the executor will use
        # internally — same file, same cross-process coordination a real
        # operator console or CDP simulator relies on.
        control_for_takeover = ControlChannel(executor.run_id, executor.evidence_dir)

        def _request_takeover():
            time.sleep(args.simulate_takeover_after)
            control_for_takeover.request_takeover(
                reason=f"Simulated operator requested takeover {args.simulate_takeover_after}s into the run."
            )
        threading.Thread(target=_request_takeover, daemon=True).start()

    if args.simulate_interrupt_after is not None:
        control_for_interrupt = ControlChannel(executor.run_id, executor.evidence_dir)

        def _request_interrupt():
            time.sleep(args.simulate_interrupt_after)
            control_for_interrupt.interrupt(
                reason=f"Simulated operator interrupted the run {args.simulate_interrupt_after}s in."
            )
        threading.Thread(target=_request_interrupt, daemon=True).start()

    print(f"Replaying {artifact.name} v{artifact.version} (run {executor.run_id})")
    try:
        result = executor.run(
            params,
            base_url=args.target_base_url,
            auto_approve=args.auto_approve,
            attended=args.attended,
            escalate_on_failure=args.escalate_on_failure,
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

    print(f"\nOutcome: {result.outcome.value}")
    print(json.dumps(result.model_dump(mode="json"), indent=2, default=str))


if __name__ == "__main__":
    main()
