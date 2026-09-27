"""
Exercises the replay engine against every runtime condition the target app
is seeded to reproduce: happy path, a named business outcome, a recoverable
interstitial, and an escalation that gets resolved by a simulated operator
reattaching to the live session via CDP. None of this needs an LLM or an
API key — replay never calls one.

Run: python3 tests/test_replay_scenarios.py
"""
import shutil
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os  # noqa: E402
os.environ.setdefault("ALLOWLIST_DOMAINS", "127.0.0.1:5055")

from tests.fixtures.example_artifact import (  # noqa: E402
    build_lookup_member_balance_fixture,
    build_open_sub_account_fixture,
)
from replay.executor import ReplayExecutor  # noqa: E402
from escalation.simulated_operator import simulate_operator_takeover  # noqa: E402
from artifacts.schema import ReplayOutcome  # noqa: E402

EVIDENCE_ROOT = Path("/tmp/cua_replay_test_evidence")


def fresh_evidence_root():
    if EVIDENCE_ROOT.exists():
        shutil.rmtree(EVIDENCE_ROOT)
    return EVIDENCE_ROOT


def test_happy_path():
    artifact = build_lookup_member_balance_fixture()
    ex = ReplayExecutor(artifact, fresh_evidence_root(), headless=True)
    result = ex.run({"member_id": "10001"})
    assert result.outcome == ReplayOutcome.SUCCESS, result.model_dump()
    assert result.outputs["member_name"] == "Alice Rivera", result.outputs
    assert result.outputs["savings_balance"] == 8150.32, result.outputs
    print("PASS: happy path ->", result.outcome, result.outputs)


def test_business_outcome_not_found():
    artifact = build_lookup_member_balance_fixture()
    ex = ReplayExecutor(artifact, fresh_evidence_root(), headless=True)
    result = ex.run({"member_id": "99999"})
    assert result.outcome == ReplayOutcome.BUSINESS_OUTCOME, result.model_dump()
    assert result.business_outcome.code == "member_not_found", result.business_outcome
    print("PASS: business outcome (not found) ->", result.outcome, result.business_outcome)


def test_recoverable_flaky_session():
    artifact = build_lookup_member_balance_fixture()
    ex = ReplayExecutor(artifact, fresh_evidence_root(), headless=True)
    result = ex.run({"member_id": "10004"})
    assert result.outcome == ReplayOutcome.SUCCESS, result.model_dump()
    assert len(result.recovered_steps) == 1, result.recovered_steps
    assert "session" in result.recovered_steps[0].condition.lower()
    assert result.outputs["member_name"] == "Dana Kim"
    print("PASS: recoverable session-expired ->", result.outcome, result.recovered_steps)


def test_business_outcome_permission_denied():
    artifact = build_open_sub_account_fixture()
    ex = ReplayExecutor(artifact, fresh_evidence_root(), headless=True)
    result = ex.run({"member_id": "10003", "nickname": "Test", "initial_deposit": "100"})
    assert result.outcome == ReplayOutcome.BUSINESS_OUTCOME, result.model_dump()
    assert result.business_outcome.code == "permission_denied", result.business_outcome
    print("PASS: business outcome (permission denied) ->", result.outcome, result.business_outcome)


def test_business_outcome_invalid_deposit():
    artifact = build_open_sub_account_fixture()
    ex = ReplayExecutor(artifact, fresh_evidence_root(), headless=True)
    result = ex.run({"member_id": "10001", "nickname": "Test", "initial_deposit": "5"})
    assert result.outcome == ReplayOutcome.BUSINESS_OUTCOME, result.model_dump()
    assert result.business_outcome.code == "invalid_deposit_amount", result.business_outcome
    print("PASS: business outcome (invalid deposit) ->", result.outcome, result.business_outcome)


def test_escalation_resolved_by_simulated_operator():
    artifact = build_open_sub_account_fixture()
    ex = ReplayExecutor(artifact, fresh_evidence_root(), headless=True)

    def on_escalation(control, ctx):
        t = threading.Thread(
            target=simulate_operator_takeover,
            args=(control, "Confirm & Open Account"),
            kwargs={"accept_dialog": True, "reaction_delay_s": 0.5},
            daemon=True,
        )
        t.start()

    result = ex.run(
        {"member_id": "10001", "nickname": "Vacation Fund", "initial_deposit": "150"},
        auto_approve=False,
        escalation_timeout_s=15,
        on_escalation=on_escalation,
    )
    assert result.outcome == ReplayOutcome.SUCCESS, result.model_dump()
    assert result.escalation is not None, "expected escalation to be recorded even though it resolved"
    assert result.escalation.step_id == "s9"
    assert result.outputs["new_account_number"].startswith("SUB-10001-")
    print("PASS: escalation resolved by simulated operator ->", result.outcome, result.escalation, result.outputs)


def test_escalation_auto_approve_bypass():
    artifact = build_open_sub_account_fixture()
    ex = ReplayExecutor(artifact, fresh_evidence_root(), headless=True)
    result = ex.run(
        {"member_id": "10002", "nickname": "Auto Approved", "initial_deposit": "75"},
        auto_approve=True,
    )
    assert result.outcome == ReplayOutcome.SUCCESS, result.model_dump()
    assert result.escalation is None
    print("PASS: auto-approve bypass ->", result.outcome, result.outputs)


if __name__ == "__main__":
    test_happy_path()
    test_business_outcome_not_found()
    test_recoverable_flaky_session()
    test_business_outcome_permission_denied()
    test_business_outcome_invalid_deposit()
    test_escalation_resolved_by_simulated_operator()
    test_escalation_auto_approve_bypass()
    print("\nALL REPLAY SCENARIO TESTS PASSED")
