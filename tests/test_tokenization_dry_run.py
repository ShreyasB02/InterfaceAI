"""
Integration test for Tier 2 #6 (two-way tokenization) — verifies the whole
round trip through the real discovery harness, not just the Tokenizer
class in isolation: a value is tokenized before it ever reaches the
model's context, the model (scripted here, same technique as
test_discovery_dry_run.py) can only ever see and echo back the
placeholder, and discovery_loop.py detokenizes it back to the real value
at the one boundary where it drives an actual browser action.

No real LLM/API key is used — same ScriptedLLMClient substitution as
test_discovery_dry_run.py.

Run: python3 tests/test_tokenization_dry_run.py
"""
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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
    def __init__(self, script):
        self.script = list(script)
        self.model = "fake-model-scripted"
        self._i = 0
        self.first_system_prompt = None

    def decide(self, system_prompt, messages, image_bytes=None, image_mime_type="image/png"):
        if self.first_system_prompt is None:
            self.first_system_prompt = system_prompt
        name, tool_input, text = self.script[self._i]
        self._i += 1
        blocks = []
        if text:
            blocks.append(FakeBlock("text", text=text))
        blocks.append(FakeBlock("tool_use", id=f"toolu_{self._i}", name=name, input=tool_input))
        return FakeResponse(blocks)


def main():
    evidence_root = Path("/tmp/cua_tokenizer_dry_run_evidence")
    if evidence_root.exists():
        shutil.rmtree(evidence_root)

    run = DiscoveryRun(
        capability_name="lookup_member_balance_tokenizer_dryrun",
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
        llm=ScriptedLLMClient([]),  # script is filled in below, once the token is known
    )

    # member_id ("10001") doesn't match any of tokenizer.py's VALUE_PATTERNS
    # shapes on its own — it's an ordinary-looking ID, deliberately never
    # tokenized by accident (same design principle as redaction.py). To
    # actually exercise the tokenize/detokenize round trip end-to-end, opt
    # this specific value in via register_known_value(), the same escape
    # hatch a real caller would use for a value it knows is sensitive
    # regardless of shape. This mints the token deterministically (first
    # value registered on a fresh Tokenizer -> "[[TOK1]]"), which we then
    # reference directly in the scripted "model" response below — exactly
    # standing in for what a real model would echo back after reading it
    # tokenized in its own context.
    token = run.tokenizer.register_known_value("10001")
    assert token == "[[TOK1]]", f"expected deterministic first token, got {token!r}"

    SCRIPT = [
        ("fill", {"index": 1, "value": token}, f"Filling in the member ID using the placeholder {token}."),
        ("click", {"index": 2}, "Submitting the search."),
        ("click", {"index": 3}, "Opening the found member's detail page."),
        ("extract_field", {"label": "Name", "output_name": "member_name"}, "Reading the member's name."),
        ("extract_field", {"label": "Savings", "output_name": "savings_balance"}, "Reading the savings balance."),
        ("finish_success", {
            "summary": "Looked up member 10001 and read their savings balance.",
            "checkpoint_description": "Member detail page is showing with the Savings row visible.",
        }, "Goal achieved."),
    ]
    llm = run.llm
    llm.script = list(SCRIPT)

    artifact = run.run()

    # -- the model-facing side never saw the real value as a PARAM value --
    # (the goal sentence itself legitimately says "member 10001" — that's
    # author-provided task text, not a scraped/param value, and is
    # deliberately not in scope for tokenization; what must never appear
    # is the raw value in the *params* line the model reads values from.)
    assert token in llm.first_system_prompt, "expected the placeholder in the system prompt"
    assert f"member_id = '{token}'" in llm.first_system_prompt, (
        "expected the param line to show the placeholder in place of the real value"
    )
    assert "member_id = '10001'" not in llm.first_system_prompt, (
        "the raw value should not appear on the params line"
    )

    # -- but the real browser action, and the resulting artifact, got the
    # real value — proving detokenize() ran at the fill boundary. If it
    # hadn't, browser.fill() would have literally typed "[[TOK1]]" into the
    # member_id field, the search would have found nothing, and none of
    # the rest of this script's clicks/extracts would have succeeded.
    fill_step = artifact.steps[1]
    assert fill_step.action == "fill", artifact.steps
    assert fill_step.value_param == "member_id", (
        f"expected the FILLED value to resolve back to the member_id param (proving it was the real "
        f"value, not the literal token) — got value_param={fill_step.value_param!r}, "
        f"value_literal={fill_step.value_literal!r}"
    )
    assert artifact.steps[4].output_name == "member_name"
    assert artifact.steps[5].output_name == "savings_balance"

    print("ALL TOKENIZATION ASSERTIONS PASSED")
    print(f"token used: {token}")
    print(f"fill step resolved to value_param: {fill_step.value_param!r}")


if __name__ == "__main__":
    main()
