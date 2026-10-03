"""
Shared pytest setup. `pytest` from the repo root runs everything with no API
key and no manual steps: the model is scripted, and the mock target app is
started here if it isn't already running.
"""
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TARGET_URL = "http://127.0.0.1:5055"
os.environ.setdefault("ALLOWLIST_DOMAINS", "127.0.0.1:5055")
os.environ.setdefault("TARGET_APP_BASE_URL", TARGET_URL)


def _target_app_is_up() -> bool:
    try:
        urllib.request.urlopen(TARGET_URL + "/login", timeout=1)
        return True
    except OSError:
        return False


@pytest.fixture(scope="session", autouse=True)
def target_app():
    """Use a target app that is already running, or start one for the session."""
    if _target_app_is_up():
        yield
        return
    proc = subprocess.Popen([sys.executable, str(ROOT / "target_app" / "app.py")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            if _target_app_is_up():
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("target_app/app.py did not come up on port 5055")
        yield
    finally:
        proc.terminate()
        proc.wait(timeout=10)
