"""AWS Inventory: the inventory database that scripts/aws_inventory.py writes, read for the app.

The scripts sync the accounts (scripts/aws_inventory.py) and write inventory/inventory.db
(scripts/aws_db.py); this app only reads that database and the scripts' settings. The
inventory folder is the scripts' output_dir (scripts/aws_accounts.json), or
OWL_AWS_INVENTORY_DIR.
"""

import json
from contextlib import closing
import os
import sqlite3
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

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
