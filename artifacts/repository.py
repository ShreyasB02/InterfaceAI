"""
Flat-file artifact store. Artifacts are small, reviewable JSON documents —
a directory of files is the appropriately-simple choice here (see REPORT.md
"Architecture" for why this isn't a database).

One file per version: <name>.v<major.minor.patch>.json, e.g.
lookup_member_balance.v1.1.0.json. A version's content is immutable once
saved — `save()` refuses to overwrite it — so `ls artifacts/store/` is the
full history and a `git diff` between two files is the review of what
changed. The only in-place change allowed is a review-state transition
(draft -> approved/rejected), which artifacts/review.py makes through
`save(..., allow_overwrite=True)`.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from artifacts.schema import ArtifactStatus, CapabilityArtifact

STORE_DIR = Path(__file__).parent / "store"

_FILENAME = re.compile(r"^(?P<name>.+)\.v(?P<version>\d+\.\d+\.\d+)\.json$")


class VersionExists(Exception):
    pass


def _version_key(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(p) for p in version.split("."))
    return major, minor, patch


def _path(name: str, version: str) -> Path:
    return STORE_DIR / f"{name}.v{version}.json"


def versions(name: str) -> list[str]:
    """Every stored version of `name`, oldest first (numeric order, so
    1.10.0 sorts after 1.9.0)."""
    found = []
    for path in STORE_DIR.glob(f"{name}.v*.json"):
        m = _FILENAME.match(path.name)
        if m and m.group("name") == name:
            found.append(m.group("version"))
    return sorted(found, key=_version_key)


def next_major_version(name: str) -> str:
    """Version for a fresh discovery of `name`: 1.0.0 if it's new, otherwise
    the next major — a re-recorded flow is a new contract, not a patch."""
    existing = versions(name)
    if not existing:
        return "1.0.0"
    return f"{_version_key(existing[-1])[0] + 1}.0.0"


def save(artifact: CapabilityArtifact, allow_overwrite: bool = False) -> Path:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    _version_key(artifact.version)  # reject a malformed version before it becomes a filename
    path = _path(artifact.name, artifact.version)
    if path.exists() and not allow_overwrite:
        raise VersionExists(
            f"{artifact.name} v{artifact.version} already exists in the store. Stored versions are "
            "immutable — save the change as a new version instead."
        )
    path.write_text(artifact.model_dump_json(indent=2, exclude_none=False))
    return path


def load(name: str, version: Optional[str] = None, approved_only: bool = False) -> CapabilityArtifact:
    """Load one version of `name`.

    `version` may be exact ("1.1.0") or a prefix ("1" or "1.1"), which
    resolves to the highest stored version under it. With no version, the
    highest stored version overall. `approved_only` restricts that choice
    to approved versions — what an unattended caller should get, so a newer
    unreviewed draft never silently replaces the approved one.
    """
    candidates = versions(name)
    if not candidates:
        raise FileNotFoundError(f"No artifact named '{name}' in store.")

    if version is not None:
        prefix = version.split(".")
        candidates = [v for v in candidates if v.split(".")[:len(prefix)] == prefix]
        if not candidates:
            raise FileNotFoundError(f"No artifact '{name}' version '{version}' in store.")

    for v in reversed(candidates):
        artifact = CapabilityArtifact.model_validate_json(_path(name, v).read_text())
        if not approved_only or artifact.status == ArtifactStatus.APPROVED:
            return artifact
    raise FileNotFoundError(
        f"No approved version of '{name}'" + (f" matching '{version}'" if version else "")
        + f" in store (stored: {', '.join(candidates)}). Approve one with "
        f"`python -m artifacts.review approve {name} --reviewer <you>`."
    )


def list_artifacts() -> list[dict]:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for path in sorted(STORE_DIR.glob("*.json")):
        try:
            art = CapabilityArtifact.model_validate_json(path.read_text())
            out.append({
                "name": art.name,
                "version": art.version,
                "status": art.status.value,
                "reviewed_by": art.review.reviewed_by if art.review else None,
                "description": art.description,
                "path": str(path),
            })
        except Exception as e:  # pragma: no cover - defensive, for a corrupt file
            out.append({"path": str(path), "error": str(e)})
    return out
