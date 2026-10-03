"""
Kept as the command name earlier docs and scripts use. The per-capability
augmentation code that lived here is gone: known outcomes and recoverable
conditions are now declared once per vendor app and applied to any artifact
recorded on it — see artifacts/profile.py.

    python -m agent.augment_artifact --capability-name lookup_member_balance
is the same as
    python -m artifacts.profile apply lookup_member_balance
"""
from __future__ import annotations

import argparse

from artifacts import profile


def main():
    parser = argparse.ArgumentParser(description="Apply the vendor outcome profile to a discovered artifact.")
    parser.add_argument("--capability-name", required=True)
    parser.add_argument("--version", default=None)
    args = parser.parse_args()
    profile.main(["apply", args.capability_name, *(["--version", args.version] if args.version else [])])


if __name__ == "__main__":
    main()
