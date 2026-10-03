"""
CLI entry point for a discovery run.

Example (after the target app is running — see README):

    python -m agent.run_discovery \\
        --capability-name lookup_member_balance \\
        --goal "Look up member 10001 and read their current savings balance." \\
        --entry-path /members/search \\
        --param member_id=10001 \\
        --output member_name:string:"Member's full name" \\
        --output savings_balance:number:"Current savings balance in USD"
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv

from agent.discovery_loop import DiscoveryFailed, DiscoveryRun
from agent.llm import LLMClient, LLMConfigError
from artifacts import repository
from escalation.notify import announce_pause
from escalation.simulated_operator import parse_action, simulate_operator_takeover
from escalation.transport import RunInterrupted
from guardrails.allowlist import AllowlistViolation

load_dotenv()


def _parse_param(s: str) -> tuple[str, str]:
    name, _, value = s.partition("=")
    if not _:
        raise argparse.ArgumentTypeError(f"--param must be name=value, got {s!r}")
    return name, value


def _parse_output(s: str) -> dict:
    parts = s.split(":", 2)
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(f"--output must be name:type:description, got {s!r}")
    name, type_, description = parts
    if type_ not in ("string", "number", "boolean"):
        raise argparse.ArgumentTypeError(f"--output type must be string|number|boolean, got {type_!r}")
    return {"name": name, "type": type_, "description": description}


def main():
    parser = argparse.ArgumentParser(description="Run a real LLM-driven discovery run against the target app.")
    parser.add_argument("--capability-name", required=True)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--entry-path", required=True)
    parser.add_argument("--target-base-url", default=os.environ.get("TARGET_APP_BASE_URL", "http://127.0.0.1:5055"))
    parser.add_argument("--param", action="append", default=[], type=_parse_param, dest="params")
    parser.add_argument("--param-type", action="append", default=[], type=_parse_param, dest="param_types",
                         help="Override a param's inferred type, e.g. initial_deposit=number. "
                              "Params default to string (safer for IDs that merely look numeric).")
    parser.add_argument("--output", action="append", default=[], type=_parse_output, dest="outputs")
    parser.add_argument("--headed", action="store_true", help="Show the browser window instead of running headless.")
    parser.add_argument("--no-vision", action="store_true",
                         help="Text-only observation, no screenshot attached to the model's context each "
                              "turn (see agent/llm/). Needs a vision-capable model. Vision is on by "
                              "default.")
    parser.add_argument("--evidence-root", default="evidence/discovery")
    parser.add_argument("--escalation-timeout", type=float, default=600,
                         help="Seconds to wait for a human when the run escalates (the model calls "
                              "request_human, or the harness sees it stuck) before giving up. Resolve it "
                              "from the operator console: python -m escalation.operator_console.")
    parser.add_argument("--allow-irreversible", action="store_true",
                         help="Let the model perform actions it declares irreversible without a human "
                              "approving each one first. Only for a sandbox target where nothing real "
                              "can be committed. Without it, such a click escalates for approval.")
    parser.add_argument("--simulate-operator", action="append", default=None, metavar="ACTION",
                         help="If the run escalates, reattach via CDP and perform this action on the same "
                              "live session: 'click:<label>' or 'fill:<field name>=<value>'. Repeatable. "
                              "For repeatable evidence without a human physically present.")
    args = parser.parse_args()

    params = dict(args.params)
    param_types = dict(args.param_types)

    # Built before the run so a missing key or unknown provider name fails
    # here with a clear message, before an evidence directory is created.
    try:
        llm = LLMClient.from_env()
    except LLMConfigError as e:
        print(f"LLM configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    def on_escalation(control, ctx):  # noqa: ANN001
        announce_pause(control, ctx, headed=args.headed)

    if args.simulate_operator:
        try:
            operator_actions = [parse_action(a) for a in args.simulate_operator]
        except ValueError as e:
            print(e, file=sys.stderr)
            sys.exit(1)

        def on_escalation(control, ctx):  # noqa: ANN001
            threading.Thread(target=simulate_operator_takeover, args=(control,),
                             kwargs={"actions": operator_actions}, daemon=True).start()

    run = DiscoveryRun(
        capability_name=args.capability_name,
        goal=args.goal,
        base_url=args.target_base_url,
        entry_path=args.entry_path,
        params=params,
        param_types=param_types,
        outputs=args.outputs,
        evidence_root=Path(args.evidence_root),
        headless=not args.headed,
        vision=not args.no_vision,
        llm=llm,
        escalation_timeout_s=args.escalation_timeout,
        on_escalation=on_escalation,
        risk_gate=not args.allow_irreversible,
        # Re-discovering an existing capability records the next major
        # version; earlier versions stay in the store.
        artifact_version=repository.next_major_version(args.capability_name),
    )

    print(f"Starting discovery run {run.run_id}")
    print(f"  goal: {args.goal}")
    print(f"  llm: {llm.describe()}")
    print(f"  evidence: {run.evidence_dir}")

    try:
        artifact = run.run()
    except DiscoveryFailed as e:
        print(f"\nDiscovery run did not complete: {e.reason}", file=sys.stderr)
        print(f"Partial evidence is at {run.evidence_dir}", file=sys.stderr)
        sys.exit(1)
    except RunInterrupted as e:
        print(f"\nDiscovery run was stopped by an operator: {e.reason}", file=sys.stderr)
        print(f"Partial evidence is at {run.evidence_dir}", file=sys.stderr)
        sys.exit(1)
    except AllowlistViolation as e:
        # Deliberately not caught inside the discovery loop itself (see
        # discovery_loop.py's tool-execution try/except: an allowlist
        # breach is re-raised rather than fed back to the model as
        # something to route around) — but the CLI boundary still owes the
        # person running this a clean message, not a raw traceback.
        print(f"\nDiscovery run stopped by the allowlist guardrail: {e}", file=sys.stderr)
        print(f"Partial evidence is at {run.evidence_dir}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        # Backstop for anything genuinely unexpected (a Playwright launch
        # failure, the target app not running at all, ...). Every failure
        # mode we know about by name is already caught above with a more
        # specific, actionable message; this is only the safety net.
        print(f"\nDiscovery run crashed unexpectedly: {type(e).__name__}: {e}", file=sys.stderr)
        print(f"Partial evidence is at {run.evidence_dir}", file=sys.stderr)
        sys.exit(1)

    path = repository.save(artifact)
    print(f"\nSuccess. Artifact v{artifact.version} saved to {path} as a DRAFT.")
    print(f"Review it:  python -m artifacts.review show {artifact.name}")
    print(f"Approve it: python -m artifacts.review approve {artifact.name} --reviewer <you>")
    print(f"Steps recorded: {len(artifact.steps)}"
          + (f" ({sum(s.origin == 'human' for s in artifact.steps)} performed by a human operator)"
             if run.interventions else ""))
    print(f"Evidence: {run.evidence_dir}")


if __name__ == "__main__":
    main()
