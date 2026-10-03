"""
Exercises the artifact store and review gate with no browser and no LLM:
one immutable file per version, numeric version ordering, approved-only
resolution, and approval being bound to the reviewed content.

Run: pytest tests/test_artifact_store.py
"""
import pytest  # noqa: F401
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artifacts import repository  # noqa: E402
from artifacts.review import approval_is_valid, approve, reject  # noqa: E402
from artifacts.schema import ArtifactStatus  # noqa: E402
from tests.fixtures.example_artifact import build_lookup_member_balance_fixture  # noqa: E402


def _draft(version: str):
    artifact = build_lookup_member_balance_fixture()
    artifact.version, artifact.status, artifact.review = version, ArtifactStatus.DRAFT, None
    return artifact


@pytest.fixture(scope="module", autouse=True)
def _temp_store(tmp_path_factory):
    """These tests build on each other's saved versions, in file order, in
    a throwaway store — never the repo's real artifacts/store/."""
    original = repository.STORE_DIR
    repository.STORE_DIR = tmp_path_factory.mktemp("store")
    yield
    repository.STORE_DIR = original


def test_versions_are_kept_and_immutable():
    repository.save(_draft("1.0.0"))
    repository.save(_draft("1.1.0"))
    assert repository.versions("lookup_member_balance") == ["1.0.0", "1.1.0"]
    try:
        repository.save(_draft("1.1.0"))
    except repository.VersionExists:
        pass
    else:
        raise AssertionError("saving over an existing version must be refused")
    print("PASS: every version is kept; a stored version can't be overwritten")


def test_latest_is_numeric_not_lexicographic():
    repository.save(_draft("1.9.0"))
    repository.save(_draft("1.10.0"))
    assert repository.load("lookup_member_balance").version == "1.10.0"
    assert repository.load("lookup_member_balance", "1.9").version == "1.9.0"
    assert repository.load("lookup_member_balance", "1").version == "1.10.0"
    assert repository.next_major_version("lookup_member_balance") == "2.0.0"
    assert repository.next_major_version("never_recorded") == "1.0.0"
    print("PASS: 1.10.0 sorts after 1.9.0; prefix and next-major resolution")


def test_unattended_callers_get_the_approved_version():
    try:
        repository.load("lookup_member_balance", approved_only=True)
    except FileNotFoundError as e:
        assert "No approved version" in str(e)
    else:
        raise AssertionError("expected no approved version yet")

    reviewed = approve(repository.load("lookup_member_balance", "1.1.0"), reviewer="alice", notes="read it")
    repository.save(reviewed, allow_overwrite=True)
    # 1.9.0 and 1.10.0 are newer but unreviewed: they must not be picked.
    picked = repository.load("lookup_member_balance", approved_only=True)
    assert picked.version == "1.1.0" and picked.review.reviewed_by == "alice"
    print("PASS: approved-only resolution ignores newer drafts")


def test_approval_is_bound_to_content():
    artifact = approve(_draft("3.0.0"), reviewer="alice")
    assert approval_is_valid(artifact)
    artifact.target.base_url = "https://tenant-b.example"  # another tenant's host: same flow
    assert approval_is_valid(artifact), "dispatching to a different host must not void approval"
    artifact.steps[1].value_param = "something_else"
    assert not approval_is_valid(artifact), "changing what a step does must void approval"
    assert not approval_is_valid(reject(_draft("3.0.0"), reviewer="alice", notes="wrong flow"))
    print("PASS: approval survives a base_url change, not a step change")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
