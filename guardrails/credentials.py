"""
Where the session bootstrap gets the operator login from. One place, so
neither discovery nor replay holds a credential of its own, and so a real
deployment has a single seam to swap for a per-tenant secret store.

The credential never enters an artifact, a log line, or the model's context:
login is driven directly by the harness before anything is observed or
recorded.

The fallback values are the mock target app's own hardcoded demo login
(target_app/app.py) — not a secret, and useless anywhere else.
"""
from __future__ import annotations

import os

_MOCK_EMPLOYEE_ID = "EMP001"
_MOCK_PASSCODE = "demo1234"


def operator_credentials() -> tuple[str, str]:
    return (os.environ.get("TARGET_APP_EMPLOYEE_ID") or _MOCK_EMPLOYEE_ID,
            os.environ.get("TARGET_APP_PASSCODE") or _MOCK_PASSCODE)
