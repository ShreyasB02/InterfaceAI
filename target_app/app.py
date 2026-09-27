"""
Mock legacy core-banking servicer console — the target application for the
computer-use automation system.

Deliberately legacy in shape (this is the point, not an accident):
  - server-rendered, full-page POST/redirect/GET, no JS framework
  - table-based layout, no semantic <label for=...>, no id/data-testid attrs
  - one native browser confirm() dialog on the irreversible step
  - a handful of seeded "chaos" members that deterministically reproduce the
    runtime conditions replay has to handle: not-found, permission denial,
    validation error, a one-time session interstitial (recoverable), and an
    artificially slow response (transient slowness).

This is NOT a real banking app and holds no real data — every account below
is fabricated for this exercise.
"""
import time
import random
import string
from flask import Flask, render_template, request, session, redirect, url_for

app = Flask(__name__)
app.secret_key = "dev-only-not-a-real-secret"  # mock app; never used for real auth

EMPLOYEE_ID = "EMP001"
PASSCODE = "demo1234"

MIN_INITIAL_DEPOSIT = 25.00

# --- Seed data -------------------------------------------------------------
# Each member has a "behavior" flag that drives one specific runtime
# condition, so discovery/replay evidence can be reproduced on demand
# instead of hoping a live third-party site happens to misbehave.
MEMBERS = {
    "10001": {
        "name": "Alice Rivera",
        "dob": "1985-03-12",
        "address": "412 Willow St, Riverside, CA",
        "behavior": "normal",
        "accounts": [
            {"type": "Checking", "number": "CHK-10001-01", "balance": 2400.00},
            {"type": "Savings", "number": "SAV-10001-01", "balance": 8150.32},
        ],
    },
    "10002": {
        "name": "Ben Okafor",
        "dob": "1991-07-22",
        "address": "88 Court Ave, Fontana, CA",
        "behavior": "normal",
        "accounts": [
            {"type": "Checking", "number": "CHK-10002-01", "balance": 530.10},
            {"type": "Savings", "number": "SAV-10002-01", "balance": 12000.00},
        ],
    },
    "10003": {
        "name": "Carla Nguyen",
        "dob": "1978-11-02",
        "address": "19 Cedar Ln, Moreno Valley, CA",
        "behavior": "restricted",  # viewable, but opening a sub-account is blocked
        "accounts": [
            {"type": "Checking", "number": "CHK-10003-01", "balance": 1120.44},
            {"type": "Savings", "number": "SAV-10003-01", "balance": 300.00},
        ],
    },
    "10004": {
        "name": "Dana Kim",
        "dob": "1995-01-30",
        "address": "5 Birch Ct, Pomona, CA",
        "behavior": "flaky",  # first view this session -> session-expired interstitial
        "accounts": [
            {"type": "Checking", "number": "CHK-10004-01", "balance": 75.20},
            {"type": "Savings", "number": "SAV-10004-01", "balance": 940.00},
        ],
    },
    "10005": {
        "name": "Evan Zhou",
        "dob": "1988-09-14",
        "address": "233 Palm Dr, San Bernardino, CA",
        "behavior": "slow",  # artificial latency on detail load
        "accounts": [
            {"type": "Checking", "number": "CHK-10005-01", "balance": 4020.75},
            {"type": "Savings", "number": "SAV-10005-01", "balance": 500.00},
        ],
    },
}


def _require_login():
    return session.get("logged_in") is True


@app.route("/", methods=["GET"])
def index():
    if not _require_login():
        return redirect(url_for("login"))
    return redirect(url_for("search"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        emp = request.form.get("employee_id", "")
        code = request.form.get("passcode", "")
        if emp == EMPLOYEE_ID and code == PASSCODE:
            session.clear()
            session["logged_in"] = True
            session["employee_id"] = emp
            session["flaky_seen"] = []
            return redirect(url_for("search"))
        error = "Invalid employee ID or passcode."
    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/members/search", methods=["GET", "POST"])
def search():
    if not _require_login():
        return redirect(url_for("login"))

    result = None
    not_found_id = None
    queried_id = None

    if request.method == "POST":
        queried_id = request.form.get("member_id", "").strip()
        member = MEMBERS.get(queried_id)
        if member:
            result = {"id": queried_id, **member}
        else:
            not_found_id = queried_id

    return render_template(
        "search.html", result=result, not_found_id=not_found_id, queried_id=queried_id
    )


@app.route("/members/<member_id>", methods=["GET"])
def member_detail(member_id):
    if not _require_login():
        return redirect(url_for("login"))

    member = MEMBERS.get(member_id)
    if not member:
        return render_template("search.html", not_found_id=member_id, queried_id=member_id), 404

    if member["behavior"] == "slow":
        time.sleep(3.0)

    if member["behavior"] == "flaky":
        seen = session.get("flaky_seen", [])
        if member_id not in seen:
            seen.append(member_id)
            session["flaky_seen"] = seen
            return render_template("session_expired.html", member_id=member_id)

    return render_template("member_detail.html", member_id=member_id, member=member)


@app.route("/members/<member_id>/open-subaccount", methods=["GET", "POST"])
def open_subaccount(member_id):
    if not _require_login():
        return redirect(url_for("login"))

    member = MEMBERS.get(member_id)
    if not member:
        return render_template("search.html", not_found_id=member_id, queried_id=member_id), 404

    if request.method == "GET":
        return render_template("open_subaccount_form.html", member_id=member_id, member=member)

    # POST — validate first, regardless of restriction, so validation errors
    # are reproducible independent of which member is used.
    nickname = request.form.get("nickname", "").strip()
    deposit_raw = request.form.get("initial_deposit", "").strip()

    validation_error = None
    try:
        deposit = float(deposit_raw)
        if deposit < MIN_INITIAL_DEPOSIT:
            validation_error = f"Initial deposit must be at least ${MIN_INITIAL_DEPOSIT:.2f}."
    except ValueError:
        deposit = None
        validation_error = "Initial deposit must be a number."

    if not nickname:
        validation_error = "Sub-account nickname is required."

    if validation_error:
        return render_template(
            "open_subaccount_form.html",
            member_id=member_id,
            member=member,
            error=validation_error,
            prefill_nickname=nickname,
            prefill_deposit=deposit_raw,
        )

    if member["behavior"] == "restricted":
        return render_template("permission_denied.html", member_id=member_id, member=member)

    # Stash pending request in session for the confirmation step.
    session["pending_subaccount"] = {
        "member_id": member_id,
        "nickname": nickname,
        "deposit": deposit,
    }
    return render_template(
        "open_subaccount_confirm.html", member_id=member_id, member=member,
        nickname=nickname, deposit=deposit,
    )


@app.route("/members/<member_id>/open-subaccount/confirm", methods=["POST"])
def open_subaccount_confirm(member_id):
    if not _require_login():
        return redirect(url_for("login"))

    pending = session.get("pending_subaccount")
    if not pending or pending.get("member_id") != member_id:
        return redirect(url_for("member_detail", member_id=member_id))

    member = MEMBERS.get(member_id)
    suffix = "".join(random.choices(string.digits, k=2))
    new_account_number = f"SUB-{member_id}-{suffix}"
    member["accounts"].append(
        {"type": "Sub-Savings", "number": new_account_number, "balance": pending["deposit"]}
    )
    session.pop("pending_subaccount", None)

    return render_template(
        "open_subaccount_success.html",
        member_id=member_id,
        member=member,
        account_number=new_account_number,
        nickname=pending["nickname"],
    )


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5055))
    app.run(host="127.0.0.1", port=port, debug=False)
