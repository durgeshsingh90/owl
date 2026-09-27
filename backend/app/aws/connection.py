"""AWS CLI connectivity: `aws sts get-caller-identity` checks and `aws sso login` sessions.

An SSO approval lasts 12 hours. The approval time is stored so the countdown
survives page reloads and backend restarts.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time

from app.core.database import connection
from app.core.logging import event

SESSION = 12 * 60 * 60
LOGIN_TIMEOUT = 10 * 60
CHECK_TIMEOUT = 30
PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
URL = re.compile(r"https://\S+")
CODE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b")
_lock = threading.Lock()
_login = None  # The running `aws sso login` process, owned by this backend.


def _aws():
    path = shutil.which("aws")
    if not path:
        raise FileNotFoundError(
            "AWS CLI not found. Install AWS CLI v2 and make sure `aws` is on PATH."
        )
    return path


def _options():
    options = {"env": {**os.environ, "AWS_PAGER": ""}, "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    return options


def _last_line(text):
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return (lines[-1] if lines else "AWS CLI returned no output.")[:500]


def run_sts(profile):
    """Return (identity, None) when the profile works, else (None, error)."""
    try:
        result = subprocess.run(
            [_aws(), "sts", "get-caller-identity", "--profile", profile, "--output", "json"],
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT,
            **_options(),
        )
    except FileNotFoundError as error:
        return None, str(error)
    except subprocess.TimeoutExpired:
        return None, f"`aws sts get-caller-identity` did not respond within {CHECK_TIMEOUT} seconds."
    if result.returncode:
        return None, _last_line(result.stderr or result.stdout)
    try:
        return json.loads(result.stdout), None
    except ValueError:
        return None, "AWS CLI returned an unexpected response."


def _update(**fields):
    with connection() as db:
        db.execute(
            "UPDATE aws_connection SET "
            + ",".join(f"{name}=?" for name in fields)
            + " WHERE id=1",
            tuple(fields.values()),
        )


def _row():
    with connection() as db:
        return dict(db.execute("SELECT * FROM aws_connection WHERE id=1").fetchone())


def _record(identity, error, *, approved=False):
    now = time.time()
    row = _row()
    if identity is None:
        _update(status="disconnected", error=error, checked_at=now)
        event("aws.connection_failed", profile=row["profile"])
        return
    fields = {"status": "connected", "identity": json.dumps(identity), "error": "", "checked_at": now}
    if approved:
        fields.update(approved_at=now, expires_at=now + SESSION, source="login")
    elif not row["expires_at"] or row["expires_at"] <= now:
        # Signed in outside OWL (for example from a terminal); start the clock now.
        fields.update(approved_at=now, expires_at=now + SESSION, source="detected")
    _update(**fields)
    event("aws.connection_ok", profile=row["profile"], source=fields.get("source", row["source"]))


def check():
    _record(*run_sts(_row()["profile"]))
    return state()


def state():
    row = _row()
    with _lock:
        running = _login is not None and _login.poll() is None
    if row["login_status"] == "pending" and not running:
        # The backend restarted while a login was waiting for approval.
        _update(login_status="failed", error="Login was interrupted. Start it again.")
        row = _row()
    row["identity"] = json.loads(row["identity"]) if row["identity"] else None
    row["session_seconds"] = SESSION
    row["now"] = time.time()
    return row


def reset():
    """Forget the saved session and profile; used by Delete all."""
    with _lock:
        if _login is not None and _login.poll() is None:
            raise ValueError("Wait for the current AWS login to finish.")
    with connection() as db:
        db.execute("DELETE FROM aws_connection WHERE id=1")
        db.execute("INSERT INTO aws_connection(id) VALUES(1)")


def set_profile(profile):
    if not PROFILE.match(profile):
        raise ValueError("Profile names may contain letters, digits, '.', '_' and '-'.")
    with _lock:
        if _login is not None and _login.poll() is None:
            raise ValueError("Wait for the current login to finish.")
    _update(
        profile=profile, status="unknown", identity=None, error="", checked_at=None,
        approved_at=None, expires_at=None, source="", login_status="idle",
        login_url=None, login_code=None,
    )


def _watch(process, profile):
    expired = threading.Event()

    def expire():
        expired.set()
        process.kill()

    timer = threading.Timer(LOGIN_TIMEOUT, expire)
    timer.start()
    output = []
    try:
        for line in process.stdout:
            output.append(line)
            url, code = URL.search(line), CODE.search(line)
            if url:
                _update(login_url=url.group(0))
            if code:
                _update(login_code=code.group(0))
        process.wait()
    finally:
        timer.cancel()
        process.stdout.close()
    if process.returncode:
        message = (
            "Login was not approved within 10 minutes."
            if expired.is_set()
            else _last_line("".join(output))
        )
        _update(login_status="failed", status="disconnected", error=message)
        event("aws.login_failed", profile=profile, returncode=process.returncode)
        return
    identity, error = run_sts(profile)
    _record(identity, error, approved=True)
    _update(login_status="approved" if identity else "failed")
    event("aws.login_finished", profile=profile, approved=identity is not None)


def login():
    """Start `aws sso login`; the CLI opens the approval page in a browser tab."""
    global _login
    profile = _row()["profile"]
    with _lock:
        if _login is not None and _login.poll() is None:
            return state()
        process = subprocess.Popen(
            [_aws(), "sso", "login", "--profile", profile],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            **_options(),
        )
        _login = process
    _update(login_status="pending", login_started=time.time(), login_url=None, login_code=None, error="")
    event("aws.login_started", profile=profile)
    threading.Thread(target=_watch, args=(process, profile), daemon=True).start()
    return state()
