"""
    python -m capabilities list
    python -m capabilities invoke lookup_member_balance --args '{"member_id": "10001"}'
    python -m capabilities invoke open_sub_account --confirmed-by-review \\
        --args '{"member_id": "10002", "nickname": "Rainy Day", "initial_deposit": 100}'

`list` prints the tool definitions an agent would be given. `invoke` is what
the agent's tool call does; it prints the ReplayResult as JSON.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from capabilities.catalog import CapabilityNotFound, invoke, list_tools

load_dotenv()


def main():
    parser = argparse.ArgumentParser(prog="python -m capabilities",
                                     description="The agent-facing catalog of approved capabilities.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="Tool definitions for every approved capability (JSON).")
    p = sub.add_parser("invoke", help="Call a capability by name with typed arguments.")
    p.add_argument("name")
    p.add_argument("--args", default="{}", help="Arguments as a JSON object, typed per the tool's input_schema.")
    p.add_argument("--confirmed-by-review", action="store_true",
                   help="Let an irreversible step run on the artifact's approval instead of pausing for a person.")
    p.add_argument("--escalation-timeout", type=float, default=60)
    p.add_argument("--evidence-root", default="evidence/replay")
    args = parser.parse_args()

    if args.command == "list":
        print(json.dumps(list_tools(), indent=2))
        return

    try:
        arguments = json.loads(args.args)
        if not isinstance(arguments, dict):
            raise ValueError("must be a JSON object")
    except ValueError as e:
        sys.exit(f"--args is not a JSON object: {e}")
    try:
        result = invoke(args.name, arguments, confirmed_by_review=args.confirmed_by_review,
                        escalation_timeout_s=args.escalation_timeout, evidence_root=Path(args.evidence_root))
    except CapabilityNotFound as e:
        sys.exit(str(e))
    print(json.dumps(result.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    main()
