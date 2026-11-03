"""AWS Inventory: the inventory database that scripts/aws_inventory.py writes, read for the app.

The scripts sync the accounts (scripts/aws_inventory.py) and write inventory/inventory.db
(scripts/aws_db.py); this app reads that database and the scripts' settings. What it changes,
it changes by running the scripts: a sync (followed through inventory/_runs/live.json and
stopped with a Ctrl+C), and the permissions/enable/disable commands. The inventory folder is
the scripts' output_dir (scripts/aws_accounts.json), or OWL_AWS_INVENTORY_DIR.
"""

import json
from contextlib import closing
import os
import re
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter(prefix="/api/aws-inventory")

CODE = Path(__file__).resolve().parents[3] / "scripts"  # aws_db.py, the scripts' database code


def scripts_folder():
    """Where the scripts keep aws_accounts.json (OWL_AWS_SCRIPTS_DIR, else the repo's scripts folder)."""
    return Path(os.environ.get("OWL_AWS_SCRIPTS_DIR") or CODE)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def saved():
    """The scripts' aws_accounts.json: settings and remembered accounts."""
    data = read_json(scripts_folder() / "aws_accounts.json", {}) or {}
    return data.get("settings") or {}, data.get("accounts") or {}


def folder():
    settings, _ = saved()
    if os.environ.get("OWL_AWS_INVENTORY_DIR"):
        return Path(os.environ["OWL_AWS_INVENTORY_DIR"])
    return (scripts_folder() / settings.get("output_dir", "inventory")).resolve()


def database_path():
    settings, _ = saved()
    return folder() / settings.get("db_file", "inventory.db")


def aws_db():
    """scripts/aws_db.py: the same database code the scripts use."""
    if str(CODE) not in sys.path:
        sys.path.insert(0, str(CODE))
    import aws_db as module

    return module


def open_db():
    path = database_path()
    if not path.exists():
        return None
    db = sqlite3.connect(path)  # the app only reads; a read-only open can fail on a WAL database
    db.row_factory = sqlite3.Row
    return db


def folder_bytes():
    """Disk space of the inventory folder (files, database, history), for OWL's storage page."""
    base = folder()
    if not base.is_dir():
        return 0
    return sum(p.stat().st_size for p in base.rglob("*") if p.is_file())


def not_ready():
    return {"ready": False, "folder": str(folder()),
            "message": "No inventory database yet. Sync accounts with scripts/aws_inventory.py; it writes the database last."}


@router.get("/overview")
def overview():
    db = open_db()
    if db is None:
        return not_ready()
    settings, remembered = saved()
    with closing(db):
        runs = db.execute("SELECT run, summary FROM runs ORDER BY run DESC LIMIT 1").fetchone()
        last = json.loads(runs["summary"]) if runs else None
        counts = {r["account_id"]: r["n"] for r in db.execute("SELECT account_id, COUNT(*) n FROM resources GROUP BY account_id")}
        regions = db.execute("SELECT COUNT(*) FROM (SELECT DISTINCT account_id, region FROM resources)").fetchone()[0]
        denied = {r["account_id"]: r["n"] for r in db.execute(
            "SELECT account_id, COUNT(*) n FROM type_status WHERE status = 'no_permission' GROUP BY account_id")}
        denied_details = {r["account_id"]: r["n"] for r in db.execute(
            "SELECT account_id, COUNT(*) n FROM type_status WHERE denied_details IS NOT NULL GROUP BY account_id")}
        changes = {}
        if last:
            for r in db.execute("SELECT account_id, action, COUNT(*) n FROM changes WHERE run = ? GROUP BY account_id, action",
                                (last["run"],)):
                changes.setdefault(r["account_id"], {})[r["action"]] = r["n"]
        coverage = {r["account_id"]: json.loads(r["report"]) for r in db.execute("SELECT account_id, report FROM coverage")}
        accounts = {r["account_id"]: dict(r) for r in db.execute(
            "SELECT account_id, profile, alias, last_sync, last_full_sweep, connectivity, regions FROM accounts")}
        recent = [dict(r) for r in db.execute(
            "SELECT c.run, c.account_id, a.profile, c.region, c.type, c.resource_id, c.action, r.id AS pk, r.name "
            "FROM changes c LEFT JOIN accounts a ON a.account_id = c.account_id "
            "LEFT JOIN resources r ON r.account_id = c.account_id AND r.region = c.region AND r.type = c.type "
            "AND r.resource_id = c.resource_id ORDER BY c.run DESC, c.rowid DESC LIMIT 5")]

    rows, by_id = [], {}
    for profile, entry in sorted(remembered.items()):
        account_id = entry.get("account_id")
        by_id.setdefault(account_id, []).append(profile)
        row = accounts.get(account_id) or {}
        ran = (last or {}).get("accounts", {}).get(profile) or {}
        status = (ran.get("connectivity") or {}).get("status") or row.get("connectivity") or ("ok" if row else "never synced")
        if (last or {}).get("not_ready", {}).get(profile):
            status = last["not_ready"][profile].get("status", status)
        rows.append({
            "account_id": account_id, "profile": profile, "alias": row.get("alias"),
            "regions": entry.get("regions") or settings.get("regions") or ["us-east-1"],
            "enabled": entry.get("enabled", True), "status": status, "last_sync": row.get("last_sync"),
            "average_seconds": entry.get("avg_seconds"), "resources": counts.get(account_id, 0),
            "no_permission": denied.get(account_id, 0) + denied_details.get(account_id, 0),
            "changes": changes.get(account_id, {}), "retry": entry.get("retry_permissions"),
        })
    for account_id, row in accounts.items():  # in the database but no longer remembered
        if account_id not in by_id:
            rows.append({"account_id": account_id, "profile": row.get("profile"), "alias": row.get("alias"),
                         "regions": json.loads(row.get("regions") or "[]") or [], "enabled": False, "status": row.get("connectivity"),
                         "last_sync": row.get("last_sync"), "average_seconds": None, "resources": counts.get(account_id, 0),
                         "no_permission": denied.get(account_id, 0) + denied_details.get(account_id, 0),
                         "changes": changes.get(account_id, {}), "retry": None})

    gaps = {a: c for a, c in coverage.items() if c.get("missed") or c.get("config_types_not_in_commands")}
    suggested = sorted({t for c in coverage.values() for t in c.get("suggested_commands") or []})
    issues = []
    for profile, conn in ((last or {}).get("not_ready") or {}).items():
        issues.append({"kind": "sign-in", "title": f"{profile} could not sign in",
                       "detail": f"{conn.get('status')}: {conn.get('message') or conn.get('error') or ''}".strip(": "),
                       "account_id": next((a for a, ps in by_id.items() if profile in ps), None)})
    if suggested:
        issues.append({"kind": "coverage", "title": f"{len(suggested)} type(s) not in aws_commands.json",
                       "detail": ", ".join(suggested[:4]) + (" …" if len(suggested) > 4 else "")})
    for account_id, c in list(gaps.items())[:3]:
        issues.append({"kind": "coverage", "title": f"AWS lists {c.get('missed', 0)} resource(s) {accounts.get(account_id, {}).get('profile') or account_id} lacks",
                       "detail": ", ".join(f"{k} {v.get('count')}" for k, v in list((c.get("missed_types") or {}).items())[:3]),
                       "account_id": account_id})
    total_denied = sum(denied.values()) + sum(denied_details.values())
    if total_denied:
        issues.append({"kind": "permission", "title": f"{total_denied:,} type(s) or detail calls skipped for no permission",
                       "detail": "Remembered per account; flag them for a retry when the permission is granted.",
                       "accounts": sum(1 for a in set(denied) | set(denied_details))})

    totals = (last or {}).get("totals") or {}
    return {
        "ready": True, "folder": str(folder()), "database": str(database_path()),
        "database_bytes": database_path().stat().st_size,
        "totals": {
            "accounts": len(rows), "ready": sum(1 for r in rows if r["status"] == "ok"),
            "resources": sum(counts.values()), "regions": regions,
            "no_permission": total_denied, "no_permission_accounts": len(set(denied) | set(denied_details)),
            "coverage_gaps": len(gaps), "coverage_missed": sum(c.get("missed", 0) for c in gaps.values()),
        },
        "last_run": None if not last else {
            "run": last["run"], "started": last.get("started"), "finished": last.get("finished"),
            "seconds": last.get("seconds"), "stopped": last.get("stopped"),
            "accounts": len(last.get("accounts") or {}), "not_ready": len(last.get("not_ready") or {}),
            "added": totals.get("added", 0), "modified": totals.get("modified", 0), "removed": totals.get("removed", 0),
            "skipped_empty": sum(a.get("skipped_types", 0) for a in (last.get("accounts") or {}).values()),
            "skipped_denied": sum(a.get("denied_skipped", 0) for a in (last.get("accounts") or {}).values()),
            "errors": sum(len(a.get("errors", [])) + len(a.get("timeout", [])) for a in (last.get("accounts") or {}).values()),
        },
        "issues": issues, "recent_changes": recent, "accounts": rows,
    }


@router.get("/filters")
def filters():
    """Accounts, types and regions to narrow a search by."""
    db = open_db()
    if db is None:
        return not_ready()
    with closing(db):
        return {
            "ready": True,
            "accounts": [dict(r) for r in db.execute("SELECT account_id, profile FROM accounts ORDER BY profile")],
            "types": [dict(r) for r in db.execute("SELECT type, COUNT(*) AS count FROM resources GROUP BY type ORDER BY type")],
            "regions": [r[0] for r in db.execute("SELECT DISTINCT region FROM resources ORDER BY region")],
        }


@router.get("/search")
def search(q: str = Query("", max_length=500), account: str = "", type: str = "", region: str = "",
           limit: int = Query(50, ge=1, le=500)):
    if not q.strip():
        return {"ready": True, "query": q, "hits": []}
    db = open_db()
    if db is None:
        return not_ready()
    module = aws_db()
    with closing(db):
        hits = module.search(db, q, account or None, type or None, region or None, limit)
        mode = module.search_mode(db)
    return {
        "ready": True, "query": q, "limit": limit, "mode": mode,
        "hits": [{"profile": p, "account_id": a, "region": reg, "type": t, "name": name, "resource_id": rid,
                  "where": where, "id": pk} for p, a, reg, t, name, rid, where, pk in hits],
    }


@router.get("/resources/{resource}")
def resource(resource: int):
    db = open_db()
    if db is None:
        return not_ready()
    with closing(db):
        row = db.execute("SELECT r.*, a.profile, a.alias FROM resources r LEFT JOIN accounts a ON a.account_id = r.account_id "
                         "WHERE r.id = ?", (resource,)).fetchone()
        if not row:
            raise HTTPException(404, "No such resource in the inventory database.")
        rules = [dict(r) for r in db.execute(
            "SELECT kind, direction, rule_number, action, protocol, ports, peer_type, peer, description FROM network_rules "
            "WHERE resource = ? ORDER BY direction, rule_number", (resource,))]
        uses = [dict(r) for r in db.execute(
            "SELECT l.kind, l.target_id, t.id AS pk, t.name, t.type FROM resource_links l "
            "LEFT JOIN resources t ON t.account_id = ? AND t.region = ? AND t.resource_id = l.target_id WHERE l.resource = ?",
            (row["account_id"], row["region"], resource))]
        used_by = [dict(r) for r in db.execute(
            "SELECT DISTINCT r.id AS pk, r.type, r.name, r.resource_id, r.region, l.kind FROM resource_links l "
            "JOIN resources r ON r.id = l.resource WHERE l.target_id = ? AND r.account_id = ? AND r.region = ? "
            "ORDER BY r.type LIMIT 200", (row["resource_id"], row["account_id"], row["region"]))]
        history = [dict(r) for r in db.execute(
            "SELECT run, action FROM changes WHERE account_id = ? AND region = ? AND type = ? AND resource_id = ? ORDER BY run DESC",
            (row["account_id"], row["region"], row["type"], row["resource_id"]))]
        dns = [dict(r) for r in db.execute(
            "SELECT name, type, ttl, record_values, alias_target, lookup_status, lookup_addresses FROM dns_records "
            "WHERE resource = ? ORDER BY name", (resource,))]
    item = dict(row)
    data = json.loads(item.pop("data"))
    for record in dns:
        record["record_values"] = json.loads(record["record_values"] or "[]")
        record["lookup_addresses"] = json.loads(record["lookup_addresses"] or "[]")
    return {"ready": True, "resource": item, "data": data, "rules": rules, "uses": uses, "used_by": used_by,
            "history": history, "dns": dns}


# ---------------------------------------------------------------------------- accounts

def saved_profile(name):
    """(profile, saved entry) for a saved profile or account number; 404 when neither."""
    _, remembered = saved()
    if name in remembered:
        return name, remembered[name]
    for profile, entry in sorted(remembered.items()):
        if entry.get("account_id") == name:
            return profile, entry
    raise HTTPException(404, f"{name} is not a saved profile or account number.")


def script(name, *args, timeout=60, ok_codes=(0,)):
    """Run one of the scripts (aws_inventory.py, aws_compare.py, aws_project.py, aws_db.py) and wait;
    the finished process. A failure is a 400 with what the script said."""
    try:
        done = subprocess.run([sys.executable, str(scripts_folder() / name), *args], cwd=scripts_folder(),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired:
        raise HTTPException(504, f"{name} took longer than {timeout // 60} minutes; run it in a terminal instead.")
    except OSError as e:
        raise HTTPException(500, f"Could not run {name}: {e}")
    if done.returncode not in ok_codes:
        said = [line for line in (done.stderr or done.stdout).strip().splitlines() if not line.startswith(("DB ", "Writing "))]
        raise HTTPException(400, (said[-1] if said else "") or f"{name} exited with {done.returncode}")
    return done


def run_script(*args):
    """One of the inventory script's quick commands (permissions, enable, disable, config); its output."""
    if sync_status()["running"]:
        raise HTTPException(409, "A sync is running; it saves the account list when it ends. Try again after it.")
    return script("aws_inventory.py", *args).stdout.strip()


@router.get("/accounts/{name}")
def account(name: str):
    """One saved account: its settings, what it had no permission for, coverage and changes."""
    profile, entry = saved_profile(name)
    settings, _ = saved()
    account_id = entry.get("account_id")
    base = {"ready": True, "profile": profile, "account_id": account_id,
            "regions": entry.get("regions") or settings.get("regions") or ["us-east-1"],
            "enabled": entry.get("enabled", True), "retry": entry.get("retry_permissions"),
            "average_seconds": entry.get("avg_seconds"), "last_seconds": entry.get("last_seconds"),
            "durations": entry.get("durations") or [], "last_sync": entry.get("last_sync"),
            "synced": False, "alias": None, "connectivity": None, "by_region": [], "by_type": [],
            "no_permission": [], "coverage": None, "changes": []}
    db = open_db()
    if db is None or not account_id:
        return base
    with closing(db):
        row = db.execute("SELECT * FROM accounts WHERE account_id = ?", (account_id,)).fetchone()
        if not row:
            return base
        by_region = [dict(r) for r in db.execute(
            "SELECT region, COUNT(*) AS count FROM resources WHERE account_id = ? GROUP BY region ORDER BY region", (account_id,))]
        by_type = [dict(r) for r in db.execute(
            "SELECT type, COUNT(*) AS count FROM resources WHERE account_id = ? GROUP BY type ORDER BY count DESC, type",
            (account_id,))]
        denied = []
        for r in db.execute("SELECT region, type, status, error, denied_details, last_run FROM type_status "
                            "WHERE account_id = ? AND (status = 'no_permission' OR denied_details IS NOT NULL) "
                            "ORDER BY type, region", (account_id,)):
            if r["status"] == "no_permission":
                denied.append({"type": r["type"], "detail": None, "region": r["region"], "error": r["error"], "last_run": r["last_run"]})
            for detail, code in (json.loads(r["denied_details"] or "{}") or {}).items():
                denied.append({"type": r["type"], "detail": detail, "region": r["region"], "error": code, "last_run": r["last_run"]})
        coverage = db.execute("SELECT checked_at, report FROM coverage WHERE account_id = ?", (account_id,)).fetchone()
        changes = [dict(r) for r in db.execute(
            "SELECT c.run, c.region, c.type, c.resource_id, c.action, r.id AS pk, r.name FROM changes c "
            "LEFT JOIN resources r ON r.account_id = c.account_id AND r.region = c.region AND r.type = c.type "
            "AND r.resource_id = c.resource_id WHERE c.account_id = ? ORDER BY c.run DESC, c.rowid DESC LIMIT 25", (account_id,))]
    # One row per type or detail call, with the regions it was denied in.
    grouped = {}
    for d in denied:
        g = grouped.setdefault((d["type"], d["detail"]), {**d, "regions": []})
        g["regions"].append(d["region"])
    return {**base, "synced": True, "alias": row["alias"], "connectivity": row["connectivity"],
            "last_sync": row["last_sync"] or base["last_sync"], "last_full_sweep": row["last_full_sweep"],
            "by_region": by_region, "by_type": by_type,
            "no_permission": [{k: v for k, v in g.items() if k != "region"} for g in grouped.values()],
            "coverage": {"checked_at": coverage["checked_at"], **json.loads(coverage["report"])} if coverage else None,
            "changes": changes}


class Retry(BaseModel):
    types: list[str] = []  # empty: everything the account had no permission for


@router.post("/accounts/{name}/retry")
def retry(name: str, body: Retry):
    """Flag the account's no-permission items to be tried on the next sync."""
    profile, _ = saved_profile(name)
    if any(not re.fullmatch(r"[\w./-]+", t) for t in body.types):
        raise HTTPException(400, "Types look like s3/buckets, or a service like ec2.")
    message = run_script("permissions", "--retry", profile, *(["--types", *body.types] if body.types else []))
    return {"ok": True, "message": message, "retry": saved()[1].get(profile, {}).get("retry_permissions")}


@router.delete("/accounts/{name}/retry")
def clear_retry(name: str):
    profile, _ = saved_profile(name)
    return {"ok": True, "message": run_script("permissions", "--clear", profile), "retry": None}


class Enabled(BaseModel):
    enabled: bool


@router.put("/accounts/{name}/enabled")
def set_enabled(name: str, body: Enabled):
    """Keep syncing this account, or keep it saved but leave it out of the syncs."""
    profile, _ = saved_profile(name)
    run_script("enable" if body.enabled else "disable", profile)
    return {"ok": True, "enabled": saved()[1].get(profile, {}).get("enabled", True)}


# ---------------------------------------------------------------------------- changes

RUN_ID = re.compile(r"\d{8}T\d{6}Z")


@router.get("/changes")
def changes(run: str = "", account: str = "", action: str = "", type: str = "", q: str = Query("", max_length=200),
            offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    """What one sync (the latest by default) added, modified or removed, narrowed by account, action, type or text."""
    db = open_db()
    if db is None:
        return not_ready()
    with closing(db):
        runs = [dict(r) for r in db.execute(
            "SELECT run, finished, stopped, seconds, accounts, added, modified, removed FROM runs ORDER BY run DESC LIMIT 60")]
        logged = [r[0] for r in db.execute("SELECT DISTINCT run FROM changes ORDER BY run DESC LIMIT 60")]
        known = {r["run"] for r in runs}
        runs += [{"run": r} for r in logged if r not in known]  # changes from runs the database has no summary of
        runs.sort(key=lambda r: r["run"], reverse=True)
        chosen = run or (runs[0]["run"] if runs else "")
        where, params = ["c.run = ?"], [chosen]
        if account:
            where.append("c.account_id = ?")
            params.append(account)
        if type:
            where.append("(c.type = ? OR c.type LIKE ?)")
            params += [type, type + "/%"]
        if q.strip():
            where.append("(c.resource_id LIKE ? OR r.name LIKE ? OR c.type LIKE ?)")
            params += [f"%{q.strip()}%"] * 3
        join = ("FROM changes c LEFT JOIN accounts a ON a.account_id = c.account_id "
                "LEFT JOIN resources r ON r.account_id = c.account_id AND r.region = c.region AND r.type = c.type "
                "AND r.resource_id = c.resource_id WHERE " + " AND ".join(where))
        counts = {r["action"]: r["n"] for r in db.execute(f"SELECT c.action, COUNT(*) n {join} GROUP BY c.action", params)}
        accounts = [dict(r) for r in db.execute(
            f"SELECT c.account_id, a.profile, COUNT(*) AS count {join} GROUP BY c.account_id ORDER BY count DESC", params)]
        if action:
            where.append("c.action = ?")
            params.append(action)
            join += " AND c.action = ?"
        total = db.execute(f"SELECT COUNT(*) {join}", params).fetchone()[0]
        rows = [dict(r) for r in db.execute(
            f"SELECT c.account_id, a.profile, c.region, c.type, c.resource_id, c.action, r.id AS pk, r.name {join} "
            "ORDER BY a.profile, c.account_id, c.type, c.resource_id LIMIT ? OFFSET ?", params + [limit, offset])]
    return {"ready": True, "runs": runs, "run": chosen, "counts": counts, "accounts": accounts,
            "total": total, "offset": offset, "limit": limit, "changes": rows}


@router.get("/changes/diff")
def change_diff(run: str, account: str, region: str, type: str, id: str):
    """The fields a modified resource changed in that run, from _runs/<run>/changes.json."""
    if not RUN_ID.fullmatch(run):
        raise HTTPException(400, "Not a run id.")
    entries = read_json(folder() / "_runs" / run / "changes.json")
    if entries is None:
        raise HTTPException(404, "That run's changes.json is gone (old runs are cleaned up), so its field changes are unknown.")
    for c in entries:
        if (c.get("account_id"), c.get("region"), c.get("type"), str(c.get("id"))) == (account, region, type, id):
            return {"action": c.get("action"), "diff": c.get("diff") or []}
    raise HTTPException(404, "This change is not in that run's changes.json.")


# ---------------------------------------------------------------------------- sync

PROCESS = None  # the sync this backend started (subprocess.Popen)
RUNNING = {"sign-in", "sync", "database"}


def status_path():
    return folder() / "_runs" / "live.json"


def log_path():
    return folder() / "_runs" / "app-sync.log"


def app_pid():
    """The process id of the last sync started from the app (its output is app-sync.log)."""
    try:
        return int((folder() / "_runs" / "app-sync.pid").read_text())
    except (FileNotFoundError, ValueError):
        return None


def alive(pid):
    if PROCESS is not None and PROCESS.pid == pid:
        return PROCESS.poll() is None  # also reaps it once it ended
    if not pid:
        return False
    if os.name == "nt":
        return True  # os.kill(pid, 0) would end it on Windows; trust the status file
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sync_status():
    status = read_json(status_path(), {}) or {}
    running = status.get("phase") in RUNNING and alive(status.get("pid"))
    if status.get("phase") in RUNNING and not running:
        status = {**status, "phase": "ended",
                  "message": "The sync ended without saying how: it was closed or crashed. See its output below."}
    return {"running": running, "status": status}


def tail(path, lines=60):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 64 * 1024))
            text = f.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return []
    return [re.sub(r"\x1b\[[0-9;]*[A-Za-z]|\r", "", line) for line in text.splitlines()[-lines:]]


@router.get("/sync")
def sync():
    """Whether a sync is running (started here or in a terminal), how far it is, and its output."""
    settings, remembered = saved()
    state = sync_status()
    started_here = state["status"].get("pid") is not None and state["status"].get("pid") == app_pid()
    return {"ready": True, **state, "started_here": bool(started_here),
            "log": tail(log_path()) if started_here else [],
            "accounts": [{"profile": p, "account_id": a.get("account_id"), "enabled": a.get("enabled", True),
                          "average_seconds": a.get("avg_seconds"), "retry": bool(a.get("retry_permissions"))}
                         for p, a in sorted(remembered.items())],
            "average_seconds": read_json(scripts_folder() / "aws_accounts.json", {}).get("average_seconds"),
            "workers": settings.get("max_parallel_calls", "auto")}


class Start(BaseModel):
    profiles: list[str] = []  # empty: every account that is not left out
    full: bool = False   # repeat every detail call
    sweep: bool = False  # list every type, also those empty before (not those without permission)


@router.post("/sync")
def start_sync(body: Start):
    """Start `aws_inventory.py sync --yes` in the background; its output goes to _runs/app-sync.log."""
    global PROCESS
    if sync_status()["running"]:
        raise HTTPException(409, "A sync is already running.")
    _, remembered = saved()
    unknown = [p for p in body.profiles if p not in remembered]
    if unknown:
        raise HTTPException(400, f"Not saved profiles: {', '.join(unknown)}")
    if not remembered:
        raise HTTPException(400, "No saved accounts. Add some first: python scripts/aws_inventory.py")
    command = [sys.executable, "-u", str(scripts_folder() / "aws_inventory.py"), "sync", "--yes"]
    command += (["--profiles", *body.profiles] if body.profiles else []) + ["--full"] * body.full + ["--sweep"] * body.sweep
    log_path().parent.mkdir(parents=True, exist_ok=True)
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True})
    with open(log_path(), "w", encoding="utf-8") as log:
        try:
            PROCESS = subprocess.Popen(command, cwd=scripts_folder(), stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, env={**os.environ, "PYTHONIOENCODING": "utf-8"}, **options)
        except OSError as e:
            raise HTTPException(500, f"Could not start aws_inventory.py: {e}")
    (folder() / "_runs" / "app-sync.pid").write_text(str(PROCESS.pid))
    # Until the script writes its own status.
    status = {"phase": "sign-in", "pid": PROCESS.pid, "command": command[3:],
              "accounts": {"done": 0, "total": len(body.profiles) or sum(1 for a in remembered.values() if a.get("enabled", True))}}
    write_status(status)
    return {"ok": True, "pid": PROCESS.pid}


def write_status(status):
    path = status_path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(status), encoding="utf-8")
    os.replace(tmp, path)


@router.post("/sync/stop")
def stop_sync():
    """A Ctrl+C for the running sync: finished accounts are kept and accounts in progress keep their
    finished regions. Pressed again while it is stopping, it quits at once (as a second Ctrl+C)."""
    state = sync_status()
    if not state["running"]:
        raise HTTPException(409, "No sync is running.")
    pid = state["status"]["pid"]
    ours = PROCESS is not None and PROCESS.pid == pid
    try:
        if os.name == "nt":
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        elif ours:
            os.killpg(pid, signal.SIGINT)  # the script and its aws commands, as Ctrl+C in a terminal
        else:
            os.kill(pid, signal.SIGINT)
    except (ProcessLookupError, PermissionError, OSError) as e:
        raise HTTPException(500, f"Could not stop the sync (process {pid}): {e}")
    return {"ok": True}


# ---------------------------------------------------------------------------- compare and projects

REPORT = re.compile(r"[\w.-]+\.json")
_summaries = {}  # (path, mtime) -> summary, so a list of big reports is read once


def reports(kind, summarize, limit=30):
    """The newest reports in inventory/_compare or _projects, each summed up."""
    base = folder() / kind
    files = sorted((p for p in base.glob("*.json") if p.name not in ("pairs.json", "last.json")
                    and not p.name.endswith(".tad.json")), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    rows = []
    for path in files:
        key = (str(path), path.stat().st_mtime)
        if key not in _summaries:
            data = read_json(path)
            if not isinstance(data, dict):
                continue
            _summaries[key] = {"name": path.name, "bytes": path.stat().st_size, **summarize(data)}
        rows.append(_summaries[key])
    return rows


def report_file(kind, name):
    if not REPORT.fullmatch(name) or name in ("pairs.json", "last.json"):
        raise HTTPException(400, "Not a report name.")
    path = folder() / kind / name
    data = read_json(path)
    if data is None:
        raise HTTPException(404, "No such report; it may have been deleted.")
    return path, data


def report_name(stdout, suffix=""):
    """The report the script wrote, from its last "Report: <path>" line."""
    for line in reversed(stdout.splitlines()):
        if line.startswith("Report: "):
            return Path(line[len("Report: "):].split()[0]).name
    return None


def compare_summary(r):
    return {"source": r["source"], "target": r["target"], "since": r.get("since"), "compared_at": r.get("compared_at"),
            "pairs": r.get("pairs", 0), "same": r.get("same", 0), "missing": len(r.get("missing", [])),
            "extra": len(r.get("extra", [])), "different": len(r.get("different", [])),
            "expected": len(r.get("expected", [])), "not_compared": len(r.get("not_compared", [])),
            "blind_spots": len(r.get("blind_spots", []))}


@router.get("/compare")
def compare_page():
    """The accounts that can be compared, the environment words used before for each pair, and past reports."""
    db = open_db()
    if db is None:
        return not_ready()
    with closing(db):
        accounts = [dict(r) for r in db.execute(
            "SELECT a.account_id, a.profile, a.last_sync, COUNT(r.id) AS resources FROM accounts a "
            "LEFT JOIN resources r ON r.account_id = a.account_id GROUP BY a.account_id ORDER BY a.profile")]
    config = read_json(scripts_folder() / "aws_compare.json", {}) or {}
    pairs = read_json(folder() / "_compare" / "pairs.json", {}) or {}
    return {"ready": True, "accounts": accounts, "env_words": config.get("env_words", []),
            "pairs": [{"source": k.split("|")[0], "target": k.split("|")[-1], **v} for k, v in pairs.items()],
            "reports": reports("_compare", compare_summary)}


class CompareRun(BaseModel):
    source: str
    target: str
    env_source: list[str] = []  # empty: remembered for this pair, else what the names show
    env_target: list[str] = []
    since: str = ""             # YYYY-MM-DD: only what changed in the source since then
    services: list[str] = []
    regions: list[str] = []


WORD = re.compile(r"[\w./-]+")


@router.post("/compare")
def run_compare(body: CompareRun):
    """aws_compare.py with --yes; the report it wrote."""
    if sync_status()["status"].get("phase") == "database" and sync_status()["running"]:
        raise HTTPException(409, "A sync is writing the database; compare when it is done.")
    values = [body.source, body.target, *body.env_source, *body.env_target, *body.services, *body.regions]
    if not all(WORD.fullmatch(v) for v in values) or (body.since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", body.since)):
        raise HTTPException(400, "Accounts, words, services and regions are single words; the date is YYYY-MM-DD.")
    args = [body.source, body.target, "--yes", "--show", "0"]
    args += ["--env-source", ",".join(body.env_source)] * bool(body.env_source)
    args += ["--env-target", ",".join(body.env_target)] * bool(body.env_target)
    args += ["--since", body.since] * bool(body.since)
    args += (["--services", *body.services] if body.services else []) + (["--regions", *body.regions] if body.regions else [])
    done = script("aws_compare.py", *args, timeout=900, ok_codes=(0, 1))
    name = report_name(done.stdout)
    if not name:
        said = (done.stderr or done.stdout).strip().splitlines()
        raise HTTPException(400, said[-1] if said else "aws_compare.py wrote no report.")
    return {"ok": True, "name": name}


@router.get("/compare/reports/{name}")
def compare_report(name: str):
    _, data = report_file("_compare", name)
    return {"ready": True, "name": name, "report": data}


def project_summary(r):
    paths = r.get("paths", [])
    return {"project": r.get("project"), "keywords": r.get("keywords", []), "generated": r.get("generated"),
            "applications": [a.get("profile") or a.get("account") for a in r["accounts"]["application"]],
            "shared": [a.get("profile") or a.get("account") for a in r["accounts"]["shared"]],
            "eks": len(r.get("eks", [])), "paths": len(paths),
            "pass": sum(p["verdict"] == "PASS" for p in paths), "fail": sum(p["verdict"] == "FAIL" for p in paths),
            "unknown": sum(p["verdict"] == "UNKNOWN" for p in paths), "open": len(r.get("open", []))}


@router.get("/projects")
def projects_page():
    db = open_db()
    if db is None:
        return not_ready()
    with closing(db):
        accounts = [dict(r) for r in db.execute("SELECT account_id, profile, alias FROM accounts ORDER BY profile")]
    config = read_json(scripts_folder() / "aws_project.json", {}) or {}
    last = read_json(folder() / "_projects" / "last.json", {}) or {}
    return {"ready": True, "accounts": accounts, "last": last, "shared_accounts": config.get("shared_accounts", []),
            "shared_words": config.get("shared_words", []), "reports": reports("_projects", project_summary)}


class ProjectRun(BaseModel):
    keywords: list[str]
    shared: list[str] = []  # empty: aws_project.json's, the ones used last time and the shared_words matches
    name: str = ""


@router.post("/projects")
def run_project(body: ProjectRun):
    """aws_project.py with --yes; the report it wrote."""
    if sync_status()["status"].get("phase") == "database" and sync_status()["running"]:
        raise HTTPException(409, "A sync is writing the database; map the project when it is done.")
    values = [*body.keywords, *body.shared] + ([body.name] if body.name else [])
    if not body.keywords or not all(WORD.fullmatch(v) for v in values):
        raise HTTPException(400, "Give at least one keyword; keywords and accounts are single words.")
    args = [*body.keywords, "--yes", "--show", "0"] + (["--shared", *body.shared] if body.shared else []) + (["--name", body.name] if body.name else [])
    done = script("aws_project.py", *args, timeout=900, ok_codes=(0, 1))
    name = report_name(done.stdout)
    if not name:
        said = (done.stderr or done.stdout).strip().splitlines()
        raise HTTPException(400, said[-1] if said else "aws_project.py wrote no report.")
    return {"ok": True, "name": name}


@router.get("/projects/reports/{name}")
def project_report(name: str):
    """The project report without the shared resources' full data (each links to its resource instead),
    with its TAD model and Mermaid text."""
    path, data = report_file("_projects", name)
    base = path.with_suffix("")
    model = read_json(Path(f"{base}.tad.json"), {"nodes": [], "edges": []})
    try:
        mmd = Path(f"{base}.mmd").read_text(encoding="utf-8")
    except FileNotFoundError:
        mmd = ""
    shared = [{k: v for k, v in r.items() if k != "data"} for r in data.get("shared_resources", [])]
    db = open_db()
    if db is not None:
        with closing(db):
            for r in shared:
                row = db.execute("SELECT id, name FROM resources WHERE account_id=? AND region=? AND type=? AND resource_id=?",
                                 (r["account"], r["region"], r["type"], r["id"])).fetchone()
                r["pk"], r["name"] = (row["id"], row["name"]) if row else (None, None)
    return {"ready": True, "name": name, "report": {**data, "shared_resources": shared}, "tad": model, "mermaid": mmd}


# ---------------------------------------------------------------------------- settings

SETTINGS = {  # what the app may change through `aws_inventory.py config`
    "regions", "global_region", "account_workers", "max_parallel_calls", "region_workers", "type_workers",
    "detail_workers", "connect_timeout", "read_timeout", "call_timeout", "timeout_retries", "detail_refresh_hours",
    "smart_skip", "full_sweep_days", "dns_lookup", "dns_match", "dns_workers", "dns_timeout", "add_commands",
    "logs", "keep_history", "write_db", "status_seconds", "eta_window", "aws_cli", "verify_ssl",
}


@router.get("/settings")
def settings_page():
    settings = json.loads(script("aws_inventory.py", "config").stdout)
    commands = read_json(scripts_folder() / "aws_commands.json", {}) or {}
    services = {}
    for r in commands.get("resources", []):
        services[r["service"]] = services.get(r["service"], 0) + 1
    db = {"exists": database_path().exists(), "path": str(database_path())}
    if db["exists"]:
        db["bytes"] = sum(Path(str(database_path()) + x).stat().st_size for x in ("", "-wal") if Path(str(database_path()) + x).exists())
        with closing(open_db()) as conn:
            db.update(accounts=conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0],
                      resources=conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0],
                      search=(conn.execute("SELECT value FROM meta WHERE key = 'search'").fetchone() or [None])[0])
    compare = read_json(scripts_folder() / "aws_compare.json", {}) or {}
    project = read_json(scripts_folder() / "aws_project.json", {}) or {}
    return {"ready": True, "settings": settings, "editable": sorted(SETTINGS), "folder": str(folder()),
            "files": {name: str(scripts_folder() / name) for name in
                      ("aws_accounts.json", "aws_commands.json", "aws_compare.json", "aws_project.json")},
            "commands": {"types": len(commands.get("resources", [])), "services": dict(sorted(services.items()))},
            "database": db, "folder_bytes": folder_bytes(),
            "compare": {"env_words": compare.get("env_words", [])},
            "project": {"shared_accounts": project.get("shared_accounts", []), "shared_words": project.get("shared_words", [])}}


class SettingsChange(BaseModel):
    values: dict


@router.put("/settings")
def change_settings(body: SettingsChange):
    unknown = sorted(set(body.values) - SETTINGS)
    if unknown:
        raise HTTPException(400, f"Not a setting the app changes: {', '.join(unknown)}")
    pairs = []
    for key, value in body.values.items():
        if key == "regions":
            value = "all" if value in ("all", ["all"]) else ",".join(value if isinstance(value, list) else str(value).split(","))
            if value != "all" and not re.fullmatch(r"[a-z0-9-]+(,[a-z0-9-]+)*", value):
                raise HTTPException(400, "Regions look like us-east-1,eu-west-1, or all.")
            pairs.append(f"regions={value}")
        else:
            pairs.append(f"{key}={json.dumps(value)}")
    if pairs:
        run_script("config", *pairs)
    return settings_page()


@router.post("/database/update")
def update_database():
    """aws_db.py update: write what the database is behind on (accounts synced while it was off or stopped)."""
    if sync_status()["running"]:
        raise HTTPException(409, "A sync is running; it writes the database at its end.")
    done = script("aws_db.py", "update", timeout=1800)
    said = [line for line in done.stderr.splitlines() if line.startswith("Database")]
    return {"ok": True, "message": said[-1] if said else "Database up to date."}
