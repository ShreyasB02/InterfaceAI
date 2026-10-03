"""
Integration test for the discovery harness that does NOT call the real
Gemini API — it substitutes a scripted stub in place of the LLM client
so the rest of the machinery (browser control, step recording, locator
inference, artifact assembly, evidence writing) can be verified
end-to-end without needing an API key. The actual real-model discovery
run used for the graded evidence is separate (see /evidence/discovery/)
and is what satisfies "the discovery run has to be real".

Run: python3 tests/test_discovery_dry_run.py
"""
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# LLMClient() is constructed inside DiscoveryRun.__init__ before this test
# swaps run.llm for ScriptedLLMClient below — genai.Client() validates that
# an API key is *present* at construction time (unlike the old Anthropic
# client, which only checked at call time), so a syntactically-plausible
# fake key is still required even though no real call is ever made.
os.environ.setdefault("GEMINI_API_KEY", "fake-key-for-dry-run-only")
os.environ.setdefault("ALLOWLIST_DOMAINS", "127.0.0.1:5055")

from agent.discovery_loop import DiscoveryRun  # noqa: E402


class FakeBlock:
    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class FakeResponse:
    def __init__(self, content):
        self.content = content


class ScriptedLLMClient:
    """Replays a fixed sequence of tool calls, ignoring the actual prompt
    content (the assertions on the harness's OWN behavior — recorded steps,
    locator strategies, extracted values, artifact shape — are what this
    test is checking, not model reasoning)."""

    def __init__(self, script):
        self.script = list(script)
        self.model = "fake-model-scripted"
        self._i = 0
        self.saw_image_bytes = False

    def decide(self, system_prompt, messages, image_bytes=None, image_mime_type="image/png"):
        # Records whether a real screenshot was attached this call, so the
        # test can assert the vision wiring actually produced bytes rather
        # than silently passing None every turn (see assertion below).
        self.saw_image_bytes = self.saw_image_bytes or bool(image_bytes)
        name, tool_input, text = self.script[self._i]
        self._i += 1
        blocks = []
        if text:
            blocks.append(FakeBlock("text", text=text))
        blocks.append(FakeBlock("tool_use", id=f"toolu_{self._i}", name=name, input=tool_input))
        return FakeResponse(blocks)


SCRIPT = [
    ("fill", {"index": 1, "value": "10001"}, "Filling in the member ID."),
    ("click", {"index": 2}, "Submitting the search."),
    ("click", {"index": 3}, "Opening the found member's detail page."),
    ("extract_field", {"label": "Name", "output_name": "member_name"}, "Reading the member's name."),
    ("extract_field", {"label": "Savings", "output_name": "savings_balance"}, "Reading the savings balance."),
    ("finish_success", {
        "summary": "Looked up member 10001 and read their savings balance.",
        "checkpoint_description": "Member detail page is showing with the Savings row visible.",
    }, "Goal achieved."),
]


def main():
    evidence_root = Path("/tmp/cua_dry_run_evidence")
    if evidence_root.exists():
        shutil.rmtree(evidence_root)

    run = DiscoveryRun(
        capability_name="lookup_member_balance_dryrun",
        goal="Look up member 10001 and read their current savings balance.",
        base_url="http://127.0.0.1:5055",
        entry_path="/members/search",
        params={"member_id": "10001"},
        outputs=[
            {"name": "member_name", "type": "string", "description": "Member's full name."},
            {"name": "savings_balance", "type": "number", "description": "Current savings balance in USD."},
        ],
        evidence_root=evidence_root,
        headless=True,
    )
    run.llm = ScriptedLLMClient(SCRIPT)

    artifact = run.run()

    # -- assertions -----------------------------------------------------
    assert artifact.name == "lookup_member_balance_dryrun"
    assert len(artifact.steps) == 6, f"expected 6 steps, got {len(artifact.steps)}: {[s.action for s in artifact.steps]}"
    assert artifact.steps[0].action == "navigate"
    assert artifact.steps[1].action == "fill" and artifact.steps[1].value_param == "member_id"
    assert artifact.steps[2].action == "click"
    assert artifact.steps[3].action == "click"
    assert artifact.steps[4].action == "extract" and artifact.steps[4].output_name == "member_name"
    assert artifact.steps[5].action == "extract" and artifact.steps[5].output_name == "savings_balance"
    assert "{member_id}" in artifact.checkpoint.value, artifact.checkpoint.value
    assert (run.evidence_dir / "log.jsonl").exists()
    assert (run.evidence_dir / "artifact.json").exists()
    assert (run.evidence_dir / "screenshots").exists()
    n_shots = len(list((run.evidence_dir / "screenshots").glob("*.png")))
    assert n_shots > 0, "expected at least one screenshot"
    assert run.vision is True, "vision should default to True"
    assert run.llm.saw_image_bytes, "expected at least one decide() call to receive real screenshot bytes"

    print("ALL ASSERTIONS PASSED")
    print(f"steps: {[(s.step_id, s.action) for s in artifact.steps]}")
    print(f"checkpoint: {artifact.checkpoint.value}")
    print(f"evidence dir: {run.evidence_dir} ({n_shots} screenshots)")


if __name__ == "__main__":
    main()
