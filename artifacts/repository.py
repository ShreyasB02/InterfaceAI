"""
Flat-file artifact store. Artifacts are small, reviewable JSON documents —
a directory of files is the appropriately-simple choice here (see REPORT.md
"Architecture" for why this isn't a database).

Filename convention: <name>.v<version>.json — e.g. lookup_member_balance.v1.json.
This alone gives free human-browsable versioning: `ls artifacts/store/` shows
every capability and every version that's ever been recorded.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from artifacts.schema import CapabilityArtifact

STORE_DIR = Path(__file__).parent / "store"


def _filename(name: str, version: str) -> str:
    major = version.split(".")[0]
    return f"{name}.v{major}.json"


def save(artifact: CapabilityArtifact) -> Path:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    path = STORE_DIR / _filename(artifact.name, artifact.version)
    path.write_text(artifact.model_dump_json(indent=2, exclude_none=False))
    return path


def load(name: str, version: Optional[str] = None) -> CapabilityArtifact:
    if version is not None:
        path = STORE_DIR / _filename(name, version)
        if not path.exists():
            raise FileNotFoundError(f"No artifact '{name}' version '{version}' in store.")
        return CapabilityArtifact.model_validate_json(path.read_text())

    # No version pinned -> highest major version present for this name.
    candidates = sorted(STORE_DIR.glob(f"{name}.v*.json"))
    if not candidates:
        raise FileNotFoundError(f"No artifact named '{name}' in store.")
    return CapabilityArtifact.model_validate_json(candidates[-1].read_text())


def list_artifacts() -> list[dict]:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for path in sorted(STORE_DIR.glob("*.json")):
        try:
            art = CapabilityArtifact.model_validate_json(path.read_text())
            out.append({
                "name": art.name,
                "version": art.version,
                "status": art.status,
                "description": art.description,
                "path": str(path),
            })
        except Exception as e:  # pragma: no cover - defensive, for a corrupt file
            out.append({"path": str(path), "error": str(e)})
    return out
