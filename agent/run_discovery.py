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
from pathlib import Path

from dotenv import load_dotenv

from agent.discovery_loop import DiscoveryFailed, DiscoveryRun
from agent.llm import LLMClient, LLMConfigError
from artifacts import repository
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
    print(f"\nSuccess. Artifact saved to {path}")
    print(f"Steps recorded: {len(artifact.steps)}")
    print(f"Evidence: {run.evidence_dir}")


if __name__ == "__main__":
    main()
