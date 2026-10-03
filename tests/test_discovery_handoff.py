"""
Exercises discovery's human handoff end to end, with a scripted stand-in for
the model's decisions and a scripted operator that reattaches to the SAME
live browser over CDP (escalation/simulated_operator.py). Everything between
those two scripts is the real code path: the intervention request, the
control transfer, the observation of what the operator does, the conversion
of those actions into artifact steps, and the resume.

  1. The model asks for a human (request_human) at a decision it isn't
     allowed to make. The operator fills two fields and continues.
  2. The harness itself notices the model is stuck (repeated failing calls)
     and escalates. The operator performs the search. The resulting
     artifact — part model, part human — then replays deterministically.

No API key needed. Run: python3 tests/test_discovery_handoff.py
"""
import json
import os
import re
import shutil
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ALLOWLIST_DOMAINS", "127.0.0.1:5055")

from agent.discovery_loop import DiscoveryRun  # noqa: E402
from agent.llm import LLMResponse, TextBlock, ToolUseBlock  # noqa: E402
from artifacts.review import approve  # noqa: E402
from artifacts.schema import ReplayOutcome, RiskLevel  # noqa: E402
from escalation.simulated_operator import simulate_operator_takeover  # noqa: E402
from replay.executor import ReplayExecutor  # noqa: E402

EVIDENCE_ROOT = Path("/tmp/cua_handoff_test_evidence")
BASE_URL = "http://127.0.0.1:5055"


class ScriptedLLM:
    """Replays a fixed list of decisions. `("click", "Search")` and
    `("fill", "member_id", value)` are resolved to an element index by
    reading the latest observation, the way a model would."""

    model = "fake-model-scripted"

    def __init__(self, script):
        self.script = list(script)
        self.notes_seen: list[str] = []

    @staticmethod
    def _latest_observation(messages) -> str:
        content = messages[-1]["content"]
        return content if isinstance(content, str) else content[-1]["content"]

    @staticmethod
    def _index_of(observation: str, needle: str) -> int:
        for line in observation.splitlines():
            if needle in line:
                return int(re.search(r"index=(\d+)", line).group(1))
        raise AssertionError(f"{needle!r} not found in observation:\n{observation}")

    def decide(self, system_prompt, messages, image_bytes=None, image_mime_type="image/png"):
        observation = self._latest_observation(messages)
        if "A human operator took control" in observation:
            self.notes_seen.append(observation)
        entry = self.script.pop(0)
        kind = entry[0]
        if kind == "click":
            name, tool_input = "click", {"index": self._index_of(observation, f"text={entry[1]!r}")}
            if len(entry) > 2:
                tool_input["on_dialog"] = entry[2]
        elif kind == "fill":
            name, tool_input = "fill", {"index": self._index_of(observation, f"name={entry[1]!r}"), "value": entry[2]}
        else:
            name, tool_input = kind, entry[1]
        return LLMResponse([TextBlock(f"scripted: {name}"), ToolUseBlock(f"toolu_{len(self.script)}", name, tool_input)],
                           provider="scripted", model=self.model)


def _operator(actions):
    def on_escalation(control, ctx):
        threading.Thread(target=simulate_operator_takeover, args=(control,),
                         kwargs={"actions": actions, "reaction_delay_s": 0.3}, daemon=True).start()
    return on_escalation


def _log_events(run) -> list[dict]:
    return [json.loads(line) for line in (run.evidence_dir / "log.jsonl").read_text().splitlines()]


def test_model_requests_human_for_a_decision():
    script = [
        ("fill", "member_id", "10001"),
        ("click", "Search"),
        ("click", "View Member"),
        ("click", "Open Sub-Account"),
        ("request_human", {"reason": "The nickname and deposit amount are a supervisor's decision."}),
        ("click", "Confirm & Open Account", "accept"),
        ("extract_field", {"label": "New Account Number", "output_name": "new_account_number"}),
        ("finish_success", {"summary": "Opened a sub-account.", "checkpoint_description": "Success banner shown."}),
    ]
    llm = ScriptedLLM(script)
    run = DiscoveryRun(
        capability_name="open_sub_account_handoff", base_url=BASE_URL, entry_path="/members/search",
        goal="Open a new sub-account for member 10001. The nickname and deposit are a supervisor's decision.",
        params={"member_id": "10001", "nickname": "Vacation Fund"},
        outputs=[{"name": "new_account_number", "type": "string", "description": "New sub-account number."}],
        evidence_root=EVIDENCE_ROOT, headless=True, llm=llm, escalation_timeout_s=30,
        on_escalation=_operator([("fill", "nickname", "Vacation Fund"), ("fill", "initial_deposit", "173.25"),
                                 ("click", "Continue")]),
    )
    artifact = run.run()

    human = [s for s in artifact.steps if s.origin == "human"]
    assert [s.action.value for s in human] == ["fill", "fill", "click"], [(s.action, s.intent) for s in human]
    nickname, deposit, cont = human
    assert nickname.value_param == "nickname" and not nickname.requires_confirmation
    # The deposit isn't a declared input: its value is never stored, and the
    # step is handed to a human at replay instead.
    assert deposit.value_param is None and deposit.value_literal is None
    assert deposit.requires_confirmation and deposit.risk_level == RiskLevel.RISKY
    assert cont.target.strategies[0].role_name == "Continue"
    assert artifact.provenance.human_interventions == 1
    assert "173.25" not in artifact.model_dump_json(), "a human-typed, undeclared value must not be persisted"

    # Automation resumed on the same session and finished the goal itself.
    assert artifact.steps[-1].output_name == "new_account_number"
    assert llm.notes_seen and "Clicked 'Continue' button" in llm.notes_seen[0], llm.notes_seen

    events = _log_events(run)
    requested = next(e for e in events if e["event"] == "escalation_requested")
    resumed = next(e for e in events if e["event"] == "escalation_resumed")
    assert requested["kind"] == "stuck" and "supervisor" in requested["reason"]
    observed = [a["description"] for a in resumed["human_actions"] if a["source"] == "observed"]
    assert "Set the 'Sub-Account Nickname' field to the supplied nickname" in observed, observed
    assert not any("173.25" in d for d in observed), observed
    control = json.loads((run.evidence_dir / "control.json").read_text())
    assert control["status"] == "automation" and control["intervention_request"]["goal"] == run.goal
    print("PASS: model requested a human; operator's actions observed and recorded as steps ->", observed)


def test_harness_detects_stuck_and_human_steps_replay():
    doomed = ("wait_for_text", {"text": "Text That Never Appears", "timeout_ms": 300})
    script = [doomed, doomed, doomed,
              ("click", "View Member"),
              ("extract_field", {"label": "Name", "output_name": "member_name"}),
              ("extract_field", {"label": "Savings", "output_name": "savings_balance"}),
              ("finish_success", {"summary": "Read the balance.", "checkpoint_description": "Detail page shown."})]
    run = DiscoveryRun(
        capability_name="lookup_member_balance_handoff", base_url=BASE_URL, entry_path="/members/search",
        goal="Look up member 10001 and read their current savings balance.",
        params={"member_id": "10001"},
        outputs=[{"name": "member_name", "type": "string", "description": "Member's full name."},
                 {"name": "savings_balance", "type": "number", "description": "Savings balance in USD."}],
        evidence_root=EVIDENCE_ROOT, headless=True, llm=ScriptedLLM(script), escalation_timeout_s=30,
        on_escalation=_operator([("fill", "member_id", "10001"), ("click", "Search")]),
    )
    artifact = run.run()

    requested = next(e for e in _log_events(run) if e["event"] == "escalation_requested")
    assert requested["kind"] == "stuck" and "No progress" in requested["reason"], requested
    human = [s for s in artifact.steps if s.origin == "human"]
    assert [(s.action.value, s.value_param) for s in human] == [("fill", "member_id"), ("click", None)]

    # The part-human artifact is an ordinary artifact: review it, replay it.
    result = ReplayExecutor(approve(artifact, "test"), EVIDENCE_ROOT / "replay", headless=True).run(
        {"member_id": "10002"})
    assert result.outcome == ReplayOutcome.SUCCESS, result.model_dump()
    assert result.outputs["member_name"] == "Ben Okafor", result.outputs
    print("PASS: harness detected stuck, human searched, the recorded artifact replays ->", result.outputs)


if __name__ == "__main__":
    if EVIDENCE_ROOT.exists():
        shutil.rmtree(EVIDENCE_ROOT)
    test_model_requests_human_for_a_decision()
    test_harness_detects_stuck_and_human_steps_replay()
    print("\nALL DISCOVERY HANDOFF TESTS PASSED")
