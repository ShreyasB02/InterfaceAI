"""
Regenerates the evidence set under evidence/ by running the real CLIs in
order, checks each run ended the way it should, and writes evidence/INDEX.md
mapping every scenario to its run folder.

Nothing here is simulated except where a row says "scripted operator": the
discovery runs call a real model with your API key, and every replay drives
a real browser against the target app.

Before running:  python target_app/app.py   (in another terminal)
                 an LLM provider key in .env

    python scripts/make_evidence.py --reviewer "Your Name"

Discovery output is a draft, and replay refuses drafts. This script prints
each recorded artifact (`artifacts.review show`) and then approves it under
the reviewer name you pass: running it is you reviewing and approving them.

Options:
    --sections lookup,open,handoff,catalog   run only some sections (default: all)
    --skip-discovery                 reuse the latest approved artifacts
    --evidence-root DIR              write somewhere other than evidence/
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PY = sys.executable
TARGET = os.environ.get("TARGET_APP_BASE_URL", "http://127.0.0.1:5055")
rows: list[dict] = []

MEMBER_ID_DESC = "member_id=The member's ID as shown in the servicer console, e.g. 10001."
NICKNAME_DESC = "nickname=Display name for the new sub-account."


class Unexpected(Exception):
    pass


def sh(args: list[str], env: dict | None = None) -> tuple[int, str]:
    proc = subprocess.run([PY, *args], cwd=ROOT, env={**os.environ, **(env or {})},
                          capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def record(scenario: str, shows: str, outcome: str, folder: Path, by: str = "") -> None:
    rows.append({"scenario": scenario, "shows": shows, "outcome": outcome,
                 "folder": str(folder.relative_to(ROOT)) if folder.is_relative_to(ROOT) else str(folder),
                 "by": by})
    print(f"  ok   {scenario:<44} {outcome}")


def params_args(params: dict) -> list[str]:
    return [a for k, v in params.items() for a in ("--param", f"{k}={v}")]


# -- discovery ---------------------------------------------------------------

def discover(args, scenario: str, shows: str, name: str, goal: str, params: dict, outputs: list[str],
             extra: list[str], validate, by: str, attempts: int = 3):
    from artifacts import repository

    for attempt in range(1, attempts + 1):
        cmd = ["-m", "agent.run_discovery", "--capability-name", name, "--goal", goal,
               "--entry-path", "/members/search", *params_args(params),
               *[a for o in outputs for a in ("--output", o)],
               "--evidence-root", str(args.evidence_root / "discovery"), *extra]
        code, out = sh(cmd)
        found = re.search(r"(?:Evidence|Partial evidence is at):? (\S+)", out)
        folder = (ROOT / found.group(1)).resolve() if found else args.evidence_root / "discovery"
        if code == 0:
            artifact = repository.load(name)
            problem = validate(artifact)
            if not problem:
                record(scenario, shows, f"completed, v{artifact.version} (draft)", folder, by)
                return artifact
            print(f"  ..   {scenario}: attempt {attempt} recorded a flow this script can't use ({problem}); retrying")
            # An unusable recording stays in the store as a draft nobody approves.
        else:
            print(f"  ..   {scenario}: attempt {attempt} did not finish; retrying\n{out[-600:]}")
    raise Unexpected(f"{scenario}: discovery did not produce a usable artifact in {attempts} attempts")


def augment_and_approve(args, name: str):
    from artifacts import repository

    code, out = sh(["-m", "artifacts.profile", "apply", name])
    if code != 0:
        raise Unexpected(f"applying the vendor outcome profile to {name} failed:\n{out[-800:]}")
    _, shown = sh(["-m", "artifacts.review", "show", name])
    print("\n" + "\n".join("       " + line for line in shown.splitlines() if "Warning" not in line) + "\n")
    code, out = sh(["-m", "artifacts.review", "approve", name, "--reviewer", args.reviewer,
                    "--notes", "Reviewed while generating the evidence set (scripts/make_evidence.py)."])
    if code != 0:
        raise Unexpected(f"approve {name} failed:\n{out[-800:]}")
    artifact = repository.load(name, approved_only=True)
    print(f"  ok   approved {name} v{artifact.version} as {args.reviewer}")
    return artifact


# -- replay ------------------------------------------------------------------

def replay(args, scenario: str, shows: str, name: str, params: dict, expect: str, extra: list[str] | None = None,
           env: dict | None = None, check=None, by: str = ""):
    cmd = ["-m", "replay.run_replay", "--capability-name", name, *params_args(params),
           "--evidence-root", str(args.evidence_root / "replay"), *(extra or [])]
    code, out = sh(cmd, env)
    found = re.search(r"\(run (\S+)\)", out)
    if not found:
        raise Unexpected(f"{scenario}: replay did not start:\n{out[-800:]}")
    folder = args.evidence_root / "replay" / found.group(1)
    result_file = folder / "result.json"
    if not result_file.exists():
        raise Unexpected(f"{scenario}: no result.json in {folder}:\n{out[-800:]}")
    result = json.loads(result_file.read_text())
    outcome = result["outcome"]
    detail = (result.get("business_outcome") or {}).get("code") or (result.get("failure") or {}).get("step_id")
    if outcome != expect:
        raise Unexpected(f"{scenario}: expected {expect}, got {outcome} ({detail}). See {folder}")
    if check:
        problem = check(result)
        if problem:
            raise Unexpected(f"{scenario}: {problem}. See {folder}")
    record(scenario, shows, outcome + (f": {detail}" if detail and outcome != "success" else ""), folder, by)
    return result


# -- sections ----------------------------------------------------------------

def section_lookup(args):
    name = "lookup_member_balance"
    print("\nRead-only capability: lookup_member_balance")
    if not args.skip_discovery:
        discover(
            args, "discovery: lookup_member_balance", "A real LLM-driven run that completes the goal and records "
            "the artifact", name, "Look up member 10001 and read their current savings balance.",
            {"member_id": "10001"},
            ["member_name:string:Member's full name", "savings_balance:number:Current savings balance in USD"],
            ["--param-desc", MEMBER_ID_DESC],
            validate=lambda a: None if {s.output_name for s in a.steps} >= {"member_name", "savings_balance"}
            else "missing an extract step", by="real model")
        augment_and_approve(args, name)

    replay(args, "replay: success", "Deterministic replay, no model; typed outputs returned", name,
           {"member_id": "10001"}, "success",
           check=lambda r: None if r["outputs"].get("savings_balance") == 8150.32 else f"outputs {r['outputs']}")
    replay(args, "replay: success, different member", "The artifact is not tied to the record it was recorded on",
           name, {"member_id": "10002"}, "success",
           check=lambda r: None if r["outputs"].get("member_name") == "Ben Okafor" else f"outputs {r['outputs']}")
    replay(args, "replay: business outcome (not found)", "'No such member' is an answer, not a failure", name,
           {"member_id": "99999"}, "business_outcome")
    replay(args, "replay: recoverable condition", "A session-expired interstitial is detected, recovered, and "
           "reported in recovered_steps", name, {"member_id": "10004"}, "success",
           check=lambda r: None if r["recovered_steps"] else "expected a recovered step")
    replay(args, "replay: slow load", "A slow page is waited for within the step timeout", name,
           {"member_id": "10005"}, "success")
    replay(args, "replay: hard failure (app unreachable)", "Structured failure with step/expected/observed and a "
           "screenshot; no traceback", name, {"member_id": "10001"}, "failure",
           extra=["--target-base-url", "http://127.0.0.1:5999"], env={"ALLOWLIST_DOMAINS": "127.0.0.1:5999"},
           check=lambda r: None if r["failure"]["step_id"] == "session_bootstrap" else f"failed at {r['failure']}")
    replay(args, "replay: blocked by policy (action type)", "A read-only policy stops the run at the fill step, "
           "before it acts", name, {"member_id": "10001"}, "failure",
           env={"ALLOWLIST_ACTIONS": "navigate,click,wait_for,extract"},
           check=lambda r: None if "not permitted" in r["failure"]["observed"] else f"failure {r['failure']}")
    replay(args, "replay: operator interrupt", "An operator stops the run; reported as interrupted, not failure",
           name, {"member_id": "10001"}, "interrupted", extra=["--simulate-interrupt-after", "0"],
           by="scripted operator")

    from artifacts import repository
    drafts = [v for v in repository.versions(name) if repository.load(name, v).status.value == "draft"]
    if drafts:
        replay(args, "replay: refused (unreviewed draft)", "A draft is refused for unattended replay before a "
               "browser opens", name, {"member_id": "10001"}, "refused", extra=["--version", drafts[-1]])


def section_open(args):
    name = "open_sub_account"
    print("\nWrite capability with an irreversible step: open_sub_account")

    def usable(a):
        if any(s.origin == "human" for s in a.steps):
            return "a human-performed step"
        if not any(s.value_param == "initial_deposit" for s in a.steps):
            return "the deposit was not bound to its input"
        if sum(s.requires_confirmation and s.action.value == "click" for s in a.steps) != 1:
            return "expected exactly one irreversible click"
        return None

    if not args.skip_discovery:
        discover(
            args, "discovery: open_sub_account", "Multi-field form and a native confirmation dialog; the confirm "
            "click is recorded as irreversible from what actually happened", name,
            "Open a new sub-account for member 10001 with nickname 'Vacation Fund' and an initial deposit of 150, "
            "and reach the confirmation screen.",
            {"member_id": "10001", "nickname": "Vacation Fund", "initial_deposit": "150"},
            ["new_account_number:string:The new sub-account's number"],
            ["--param-type", "initial_deposit=number", "--allow-irreversible", "--param-desc", MEMBER_ID_DESC,
             "--param-desc", NICKNAME_DESC,
             "--param-desc", "initial_deposit=Opening deposit in USD; the app enforces a minimum."],
            validate=usable, by="real model")
        augment_and_approve(args, name)

    ok = {"member_id": "10002", "nickname": "Rainy Day", "initial_deposit": "100"}
    replay(args, "replay: irreversible step, approved artifact", "With --auto-approve the artifact's review stands "
           "as confirmation; runs unattended", name, ok, "success", extra=["--auto-approve"],
           check=lambda r: None if str(r["outputs"].get("new_account_number", "")).startswith("SUB-10002-")
           else f"outputs {r['outputs']}")
    replay(args, "replay: business outcome (permission denied)", "A compliance hold is returned as a named outcome",
           name, {**ok, "member_id": "10003"}, "business_outcome", extra=["--auto-approve"])
    replay(args, "replay: business outcome (validation)", "A deposit below the minimum is an answer to the caller's "
           "input, not a failure", name, {**ok, "initial_deposit": "5"}, "business_outcome", extra=["--auto-approve"])

    def observed_confirm(r):
        actions = (r.get("escalation") or {}).get("human_actions", [])
        if not any(a.get("source") == "observed" and a.get("kind") == "click" for a in actions):
            return f"no observed operator click in {actions}"
        return None

    replay(args, "replay: escalation, operator confirms", "The irreversible step pauses; an operator attaches to the "
           "same live session over CDP, confirms, and the run resumes. Their actions are observed off the page",
           name, ok, "success", extra=["--simulate-operator", "click:Confirm & Open Account"],
           check=observed_confirm, by="scripted operator")
    replay(args, "replay: escalation cancelled", "An operator gives up on the pending request; reported as a "
           "controlled failure, not a hang", name, ok, "failure", extra=["--simulate-cancel-after", "2"],
           by="scripted operator")


def section_handoff(args):
    name = "open_sub_account_supervised"
    print("\nDiscovery that needs a human: open_sub_account_supervised")
    if args.skip_discovery:
        return
    discover(
        args, "discovery: stuck -> human -> resume", "The goal withholds a decision, so the model calls "
        "request_human; the operator acts on the same session; their actions become artifact steps", name,
        "Open a new sub-account for member 10001. The nickname and the deposit amount are a supervisor's decision; "
        "do not choose them yourself.",
        {"member_id": "10001", "nickname": "Vacation Fund"},
        ["new_account_number:string:The new sub-account's number"],
        ["--allow-irreversible", "--param-desc", MEMBER_ID_DESC, "--param-desc", NICKNAME_DESC,
         "--simulate-operator", "fill:nickname=Vacation Fund",
         "--simulate-operator", "fill:initial_deposit=150", "--simulate-operator", "click:Continue"],
        validate=lambda a: None if a.provenance.human_interventions and any(s.origin == "human" for s in a.steps)
        else "the model did not ask for a human", by="real model, scripted operator")


def section_catalog(args):
    """The agent-facing interface: list the approved capabilities as tools,
    then call one by name, the way an agent's tool call would."""
    print("\nAgent-facing catalog")
    code, out = sh(["-m", "capabilities", "list"])
    if code != 0:
        raise Unexpected(f"capabilities list failed:\n{out[-800:]}")
    listing = out[out.index("["):out.rindex("]") + 1]
    tools = json.loads(listing)
    args.evidence_root.mkdir(parents=True, exist_ok=True)
    (args.evidence_root / "capability_catalog.json").write_text(json.dumps(tools, indent=2) + "\n")
    print(f"  ok   catalog lists {[t['name'] for t in tools]}")

    def call(scenario, shows, name, arguments, expect, extra=()):
        _, out = sh(["-m", "capabilities", "invoke", name, "--args", json.dumps(arguments),
                     "--evidence-root", str(args.evidence_root / "replay"), *extra])
        try:
            result = json.loads(out[out.index("{"):out.rindex("}") + 1])
        except ValueError:
            raise Unexpected(f"{scenario}: no result returned:\n{out[-800:]}")
        if result["outcome"] != expect:
            raise Unexpected(f"{scenario}: expected {expect}, got {result['outcome']}")
        record(scenario, shows, expect, args.evidence_root / "replay" / result["run_id"])

    call("catalog: agent invokes by name", "An approved capability called by name with typed JSON arguments; "
         "returns the result contract", "lookup_member_balance", {"member_id": "10002"}, "success")
    call("catalog: irreversible call, confirmed by review", "The artifact's approval stands as confirmation for "
         "its irreversible step", "open_sub_account",
         {"member_id": "10002", "nickname": "Catalog Demo", "initial_deposit": 75}, "success",
         extra=["--confirmed-by-review"])
    call("catalog: mistyped argument", "A wrong argument type is input_error before a browser opens",
         "open_sub_account", {"member_id": "10002", "nickname": "Catalog Demo", "initial_deposit": "lots"},
         "input_error", extra=["--confirmed-by-review"])


def write_index(args) -> Path:
    lines = ["# Evidence index", "",
             "Generated by `scripts/make_evidence.py`. Each folder holds `log.jsonl`, `screenshots/`, and",
             "`artifact.json` (discovery) or `result.json` (replay).", "",
             "| # | Scenario | What it shows | Outcome | Driven by | Folder |", "|---|---|---|---|---|---|"]
    def link(folder: str) -> str:
        """Relative to INDEX.md when the evidence root is inside the repo."""
        if args.evidence_root.is_relative_to(ROOT):
            return str(Path(folder).relative_to(args.evidence_root.relative_to(ROOT)))
        return folder

    for n, row in enumerate(rows, 1):
        lines.append(f"| {n} | {row['scenario']} | {row['shows']} | `{row['outcome']}` | "
                     f"{row['by'] or 'no model, no human'} | [`{Path(row['folder']).name}`]({link(row['folder'])}/) |")
    index = args.evidence_root / "INDEX.md"
    index.write_text("\n".join(lines) + "\n")
    return index


def main():
    parser = argparse.ArgumentParser(description="Regenerate the evidence set.")
    parser.add_argument("--reviewer", required=True, help="Your name: recorded as the reviewer who approved the artifacts.")
    parser.add_argument("--sections", default="lookup,open,handoff,catalog")
    parser.add_argument("--skip-discovery", action="store_true")
    parser.add_argument("--evidence-root", default=str(ROOT / "evidence"))
    args = parser.parse_args()
    args.evidence_root = Path(args.evidence_root).resolve()

    try:
        urllib.request.urlopen(TARGET + "/login", timeout=2)
    except OSError:
        sys.exit(f"The target app is not running at {TARGET}. Start it first: python target_app/app.py")

    sections = {"lookup": section_lookup, "open": section_open, "handoff": section_handoff,
                "catalog": section_catalog}
    try:
        for key in [s.strip() for s in args.sections.split(",") if s.strip()]:
            sections[key](args)
    except Unexpected as e:
        print(f"\nSTOPPED: {e}", file=sys.stderr)
        if rows:
            print(f"Index of what did run: {write_index(args)}", file=sys.stderr)
        sys.exit(1)

    print(f"\nAll {len(rows)} scenarios ended as expected. Index: {write_index(args)}")


if __name__ == "__main__":
    main()
