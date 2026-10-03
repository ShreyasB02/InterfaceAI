"""
The agent-facing interface: approved capabilities listed as callable tools
with typed arguments, and invoked by name. Replay only — no model.

Run: pytest tests/test_capability_catalog.py
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import capabilities  # noqa: E402
from artifacts import repository  # noqa: E402
from artifacts.schema import ArtifactStatus, ReplayOutcome  # noqa: E402
from tests.fixtures.example_artifact import (  # noqa: E402
    build_lookup_member_balance_fixture,
    build_open_sub_account_fixture,
)

EVIDENCE = Path("/tmp/cua_catalog_test_evidence")


@pytest.fixture(scope="module", autouse=True)
def _store(tmp_path_factory):
    """Two approved capabilities, a newer unreviewed draft of one of them,
    and a capability that only exists as a draft."""
    original = repository.STORE_DIR
    repository.STORE_DIR = tmp_path_factory.mktemp("store")
    repository.save(build_lookup_member_balance_fixture())
    repository.save(build_open_sub_account_fixture())

    newer_draft = build_lookup_member_balance_fixture()
    newer_draft.version, newer_draft.status, newer_draft.review = "2.0.0", ArtifactStatus.DRAFT, None
    newer_draft.description = "UNREVIEWED rewrite"
    repository.save(newer_draft)

    draft_only = build_lookup_member_balance_fixture()
    draft_only.name, draft_only.status, draft_only.review = "close_account", ArtifactStatus.DRAFT, None
    repository.save(draft_only)
    yield
    repository.STORE_DIR = original


def test_catalog_lists_only_approved_capabilities_as_tools():
    tools = {t["name"]: t for t in capabilities.list_tools()}
    assert set(tools) == {"lookup_member_balance", "open_sub_account"}, "a draft-only capability is not offered"

    lookup = tools["lookup_member_balance"]
    assert lookup["version"] == "1.0.0" and "UNREVIEWED" not in lookup["description"], \
        "a newer draft must not replace the approved version"
    assert lookup["input_schema"]["required"] == ["member_id"]
    assert lookup["input_schema"]["properties"]["member_id"]["type"] == "string"
    assert lookup["input_schema"]["additionalProperties"] is False
    assert lookup["output_schema"]["properties"]["savings_balance"]["type"] == "number"
    assert [o["code"] for o in lookup["outcomes"]] == ["member_not_found"]
    assert lookup["requires_human_confirmation"] is False

    opener = tools["open_sub_account"]
    assert opener["input_schema"]["properties"]["initial_deposit"]["type"] == "number"
    assert opener["requires_human_confirmation"] is True
    print("PASS: catalog exposes approved capabilities with typed schemas ->", sorted(tools))


def test_agent_invokes_by_name_with_typed_arguments():
    result = capabilities.invoke("lookup_member_balance", {"member_id": "10002"}, evidence_root=EVIDENCE)
    assert result.outcome == ReplayOutcome.SUCCESS, result.model_dump()
    assert result.outputs == {"member_name": "Ben Okafor", "savings_balance": 12000.0}
    assert result.artifact_version == "1.0.0"

    answer = capabilities.invoke("lookup_member_balance", {"member_id": "99999"}, evidence_root=EVIDENCE)
    assert answer.outcome == ReplayOutcome.BUSINESS_OUTCOME and answer.business_outcome.code == "member_not_found"

    # A JSON number for a number param, and the artifact's review standing
    # as confirmation for its irreversible step.
    opened = capabilities.invoke(
        "open_sub_account", {"member_id": "10002", "nickname": "Rainy Day", "initial_deposit": 100},
        confirmed_by_review=True, evidence_root=EVIDENCE)
    assert opened.outcome == ReplayOutcome.SUCCESS, opened.model_dump()
    assert opened.outputs["new_account_number"].startswith("SUB-10002-")
    print("PASS: invoked by name ->", result.outputs, "|", answer.business_outcome.code, "|", opened.outputs)


def test_bad_calls_come_back_as_input_error_without_opening_a_browser():
    for arguments, needle in (
        ({}, "Missing required input param 'member_id'"),
        ({"member_id": "10001", "ssn": "x"}, "Unexpected input param(s) ['ssn']"),
        ({"member_id": ["10001"]}, "must be a string"),
    ):
        result = capabilities.invoke("lookup_member_balance", arguments, evidence_root=EVIDENCE)
        assert result.outcome == ReplayOutcome.INPUT_ERROR and needle in result.failure.observed, result.failure
        assert result.steps_executed == 0

    wrong_type = capabilities.invoke(
        "open_sub_account", {"member_id": "10002", "nickname": "X", "initial_deposit": "a lot"},
        confirmed_by_review=True, evidence_root=EVIDENCE)
    assert wrong_type.outcome == ReplayOutcome.INPUT_ERROR and "must be a number" in wrong_type.failure.observed
    print("PASS: missing, unexpected and mistyped arguments are input_error")


def test_irreversible_call_without_confirmation_returns_escalated_not_a_hang():
    result = capabilities.invoke(
        "open_sub_account", {"member_id": "10002", "nickname": "Rainy Day", "initial_deposit": 100},
        escalation_timeout_s=1.5, evidence_root=EVIDENCE)
    assert result.outcome == ReplayOutcome.ESCALATED, result.model_dump()
    assert result.escalation.kind == "risk_confirmation" and result.escalation.resolution == "timed_out"
    print("PASS: an unconfirmed irreversible call comes back as escalated ->", result.escalation.step_id)


def test_unknown_or_unapproved_capability_cannot_be_called():
    for name in ("no_such_capability", "close_account"):
        with pytest.raises(capabilities.CapabilityNotFound):
            capabilities.invoke(name, {"member_id": "10001"}, evidence_root=EVIDENCE)
    print("PASS: unknown and draft-only capabilities are not callable")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
