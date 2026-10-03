"""
The vendor outcome profile: error handling declared once per vendor app and
attached to any artifact recorded on it, by anchoring each rule to a
control instead of to one capability. No browser, no LLM.

Run: pytest tests/test_outcome_profile.py
"""
import sys
from pathlib import Path

import pytest  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artifacts.profile import ProfileNotFound, apply_profile, load_profile  # noqa: E402
from tests.fixtures.example_artifact import (  # noqa: E402
    build_lookup_member_balance_fixture,
    build_open_sub_account_fixture,
)

VENDOR = "ACME Core Servicer Terminal"


def _bare(artifact):
    """As discovery leaves it: steps only, no outcome or recovery rules."""
    artifact.known_outcomes, artifact.recoverable_patterns = [], []
    return artifact


def _click_step(artifact, name):
    return next(s.step_id for s in artifact.steps if s.target and s.target.strategies[0].role_name == name)


def test_rules_attach_where_the_flow_uses_their_anchor():
    profile = load_profile(VENDOR)

    lookup, attached = apply_profile(_bare(build_lookup_member_balance_fixture()), profile)
    assert {(o.code, o.after_step) for o in lookup.known_outcomes} == {
        ("member_not_found", _click_step(lookup, "Search"))}, attached
    assert [p.after_step for p in lookup.recoverable_patterns] == [_click_step(lookup, "View Member")]

    # A different capability on the same app gets its handling from the same
    # file: the search outcome it shares, plus the two that follow Continue.
    opener, _ = apply_profile(_bare(build_open_sub_account_fixture()), profile)
    cont = _click_step(opener, "Continue")
    assert {(o.code, o.after_step) for o in opener.known_outcomes} == {
        ("member_not_found", _click_step(opener, "Search")),
        ("permission_denied", cont), ("invalid_deposit_amount", cont)}
    print("PASS: one profile, two capabilities, rules land on the right steps")


def test_rule_without_its_anchor_is_skipped():
    lookup, _ = apply_profile(_bare(build_lookup_member_balance_fixture()), load_profile(VENDOR))
    codes = {o.code for o in lookup.known_outcomes}
    assert "invalid_deposit_amount" not in codes and "permission_denied" not in codes, codes
    print("PASS: a lookup never reaches Continue, so it gets no deposit outcomes")


def test_applying_twice_adds_nothing():
    profile = load_profile(VENDOR)
    artifact, first = apply_profile(_bare(build_open_sub_account_fixture()), profile)
    counts = (len(artifact.known_outcomes), len(artifact.recoverable_patterns))
    artifact, second = apply_profile(artifact, profile)
    assert first and not second
    assert (len(artifact.known_outcomes), len(artifact.recoverable_patterns)) == counts
    print("PASS: applying a profile is idempotent")


def test_unknown_vendor_is_an_error_not_a_silent_no_op():
    for vendor in ("Some Other Core", None):
        with pytest.raises(ProfileNotFound):
            load_profile(vendor)
    print("PASS: no profile for the vendor product is reported, not ignored")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
