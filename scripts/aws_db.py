#!/usr/bin/env python3
"""Write the inventory that aws_inventory.py saved into one SQLite database.

aws_inventory.py runs this as its last step, after listing, saving, the coverage check
and the merged files are done. It can also run on its own:

    python3 aws_db.py                      # bring the database up to date with the saved files
    python3 aws_db.py search 10.20.1.15    # find anything: name, id, ARN, IP, CIDR, tag, DNS name, policy text
    python3 aws_db.py search               # asks "Search:" again and again
    python3 aws_db.py search web --account prod-profile --type ec2/security_groups
    python3 aws_db.py query "SELECT * FROM v_open_to_world LIMIT 20"
    python3 aws_db.py --rebuild            # start the database again from the saved files

The database is inventory/inventory.db (settings db_file). Only what changed is written:
each resource's hash is compared with the database, so new and changed resources are
written and removed ones deleted; each account is one transaction, so a stop (Ctrl+C)
keeps every account written before it.

Tables (see SCHEMA below):
    accounts        one row per account: profile, alias, last sync, sign-in, regions
    resources       one row per resource, with its full JSON in data (use json_extract)
    network_rules   security group and network ACL rules, one row per rule and peer
    resource_links  the security groups, subnets and VPC each resource uses
    dns_records     Route 53 records with their nslookup answers
    type_status     each type per account and region: ok, no_permission, timeout, error
    changes         every added / modified / removed resource per run
    coverage        what AWS lists that the inventory lacks, per account
    runs            one row per sync
Views: v_open_to_world, v_no_permission, v_counts.
Search: resource_search, a full-text index (SQLite FTS5) of every resource's text, matched
anywhere inside words (trigram; plain words where this SQLite lacks trigram, and a slower
text scan where it lacks FTS5). Each hit says where in the resource it matched.
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path


HERE = Path(__file__).resolve().parent
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    profile TEXT,
    alias TEXT,
    last_sync TEXT,
    last_full_sweep TEXT,
    connectivity TEXT,
    regions TEXT,                 -- JSON list
    written_at TEXT
);

CREATE TABLE IF NOT EXISTS resources (
    id INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL,
    region TEXT NOT NULL,         -- a region, or "global"
    service TEXT NOT NULL,
    type TEXT NOT NULL,           -- service/type, as in aws_commands.json
    resource_id TEXT NOT NULL,
    name TEXT,
    arn TEXT,
    vpc_id TEXT,
    parent_id TEXT,
    hash TEXT,
    last_changed TEXT,
    last_seen TEXT,
    data TEXT NOT NULL,           -- the resource as JSON, with _details, _network, _rules
    UNIQUE (account_id, region, type, resource_id)
);
CREATE INDEX IF NOT EXISTS resources_type ON resources (type);
CREATE INDEX IF NOT EXISTS resources_vpc ON resources (vpc_id);
CREATE INDEX IF NOT EXISTS resources_name ON resources (name);
CREATE INDEX IF NOT EXISTS resources_arn ON resources (arn);

CREATE TABLE IF NOT EXISTS network_rules (
    resource INTEGER NOT NULL REFERENCES resources (id) ON DELETE CASCADE,
    kind TEXT NOT NULL,           -- security_group or network_acl
    direction TEXT NOT NULL,      -- inbound or outbound
    rule_number INTEGER,          -- network ACLs
    action TEXT,                  -- network ACLs: allow / deny
    protocol TEXT,
    ports TEXT,
    peer_type TEXT,               -- cidr, cidr6, prefix_list, security_group
    peer TEXT,
    description TEXT
);
CREATE INDEX IF NOT EXISTS network_rules_resource ON network_rules (resource);
CREATE INDEX IF NOT EXISTS network_rules_peer ON network_rules (peer);

CREATE TABLE IF NOT EXISTS resource_links (
    resource INTEGER NOT NULL REFERENCES resources (id) ON DELETE CASCADE,
    kind TEXT NOT NULL,           -- security_group, subnet, vpc
    target_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS resource_links_resource ON resource_links (resource);
CREATE INDEX IF NOT EXISTS resource_links_target ON resource_links (target_id);

CREATE TABLE IF NOT EXISTS dns_records (
    resource INTEGER NOT NULL REFERENCES resources (id) ON DELETE CASCADE,
    zone_id TEXT,
    zone_name TEXT,
    private_zone INTEGER,
    name TEXT,
    type TEXT,
    ttl INTEGER,
    record_values TEXT,           -- JSON list
    alias_target TEXT,
    lookup_status TEXT,           -- ok, nxdomain, timeout, ... (nslookup)
    lookup_addresses TEXT         -- JSON list
);
CREATE INDEX IF NOT EXISTS dns_records_resource ON dns_records (resource);
CREATE INDEX IF NOT EXISTS dns_records_name ON dns_records (name);

CREATE TABLE IF NOT EXISTS type_status (
    account_id TEXT NOT NULL,
    region TEXT NOT NULL,
    type TEXT NOT NULL,
    status TEXT,
    error TEXT,
    count INTEGER,
    last_run TEXT,
    denied_details TEXT,          -- JSON: detail calls without permission
    PRIMARY KEY (account_id, region, type)
);

CREATE TABLE IF NOT EXISTS changes (
    run TEXT NOT NULL,
    account_id TEXT NOT NULL,
    region TEXT,
    type TEXT,
    resource_id TEXT,
    action TEXT,
    file TEXT
);
CREATE INDEX IF NOT EXISTS changes_account_run ON changes (account_id, run);

CREATE TABLE IF NOT EXISTS coverage (
    account_id TEXT PRIMARY KEY,
    checked_at TEXT,
    checked INTEGER,
    missed INTEGER,
    report TEXT                   -- the whole _coverage.json
);

CREATE TABLE IF NOT EXISTS runs (
    run TEXT PRIMARY KEY,
    started TEXT,
    finished TEXT,
    stopped INTEGER,
    seconds REAL,
    accounts INTEGER,
    resources INTEGER,
    added INTEGER,
    modified INTEGER,
    removed INTEGER,
    summary TEXT                  -- the whole summary.json
);

CREATE VIEW IF NOT EXISTS v_open_to_world AS
    SELECT r.account_id, r.region, r.type, r.resource_id, r.name, n.kind, n.protocol, n.ports, n.peer, n.description
    FROM network_rules n JOIN resources r ON r.id = n.resource
    WHERE n.direction = 'inbound' AND n.peer IN ('0.0.0.0/0', '::/0') AND COALESCE(n.action, 'allow') = 'allow';

CREATE VIEW IF NOT EXISTS v_no_permission AS
    SELECT a.profile, t.account_id, t.region, t.type, t.error, t.denied_details
    FROM type_status t LEFT JOIN accounts a ON a.account_id = t.account_id
    WHERE t.status = 'no_permission' OR t.denied_details IS NOT NULL;

CREATE VIEW IF NOT EXISTS v_counts AS
    SELECT a.profile, r.account_id, r.region, r.type, COUNT(*) AS resources
    FROM resources r LEFT JOIN accounts a ON a.account_id = r.account_id
    GROUP BY r.account_id, r.region, r.type;
"""

NAME_KEYS = ("Name", "name", "GroupName", "FunctionName", "RoleName", "UserName", "PolicyName", "LoadBalancerName",
             "TargetGroupName", "DBInstanceIdentifier", "DBClusterIdentifier", "TableName", "ClusterName",
             "repositoryName", "logGroupName", "StackName", "AutoScalingGroupName", "AlarmName", "DomainName",
             "Identifier")


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    if search_mode(db) is None:
        create_search(db)
    db.commit()
    return db


def search_mode(db):
    row = db.execute("SELECT value FROM meta WHERE key='search'").fetchone()
    return row[0] if row else None


def create_search(db):
    """The full-text index: trigram (any part of a word) where SQLite has it, else words, else none."""
    for mode, tokenizer in (("trigram", "trigram"), ("words", "unicode61")):
        try:
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS resource_search USING fts5("
                       f"name, resource_id, text, tokenize='{tokenizer}')")
            break
        except sqlite3.OperationalError:
            continue
    else:
        mode = "scan"
    db.execute("INSERT OR REPLACE INTO meta VALUES ('search', ?)", (mode,))
    if mode != "scan":
        # Resources written before the index existed.
        for pk, name, rid, data in db.execute("SELECT id, name, resource_id, data FROM resources").fetchall():
            db.execute("INSERT INTO resource_search (rowid, name, resource_id, text) VALUES (?,?,?,?)",
                       (pk, name, rid, search_text(json.loads(data))))


def search_text(resource):
    """Every string and number in the resource, one per line, for the search index."""
    parts = []

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, (str, int, float)) and not isinstance(v, bool):
            parts.append(str(v))

    walk(resource)
    return "\n".join(parts)[:200_000]


def resource_name(resource):
    for field in ("Tags", "tags", "TagList"):
        tags = resource.get(field)
        if isinstance(tags, dict) and isinstance(tags.get("Name"), str):
            return tags["Name"]
        for t in tags if isinstance(tags, list) else []:
            if isinstance(t, dict) and (t.get("Key") or t.get("key")) == "Name":
                return t.get("Value") or t.get("value")
    for key in NAME_KEYS:
        if isinstance(resource.get(key), str):
            return resource[key]
    return None


def vpc_of(resource):
    queue = [{k: v for k, v in resource.items() if k not in ("_network", "_details")}]
    while queue:
        node = queue.pop(0)
        if isinstance(node, dict):
            for key in ("VpcId", "vpcId", "VPCId"):
                if isinstance(node.get(key), str) and node[key].startswith("vpc-"):
                    return node[key]
            queue += [v for v in node.values() if isinstance(v, (dict, list))]
        elif isinstance(node, list):
            queue += [v for v in node if isinstance(v, (dict, list))]
    return None


def delete_resource(db, account_id, label, key, rid):
    row = db.execute("SELECT id FROM resources WHERE account_id=? AND region=? AND type=? AND resource_id=?",
                     (account_id, label, key, rid)).fetchone()
    if row:
        if search_mode(db) != "scan":
            db.execute("DELETE FROM resource_search WHERE rowid=?", (row[0],))
        db.execute("DELETE FROM resources WHERE id=?", (row[0],))


def write_resource(db, account_id, label, key, rid, doc, entry):
    """Replace one resource and its rules, links, DNS records and search text."""
    meta, resource = doc.get("_meta", {}), doc.get("resource") or {}
    service = key.split("/")[0]
    delete_resource(db, account_id, label, key, rid)
    cur = db.execute(
        "INSERT INTO resources (account_id, region, service, type, resource_id, name, arn, vpc_id, parent_id, hash,"
        " last_changed, last_seen, data) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (account_id, label, service, key, rid, resource_name(resource), meta.get("arn"), vpc_of(resource),
         resource.get("_parent_id"), entry.get("raw_hash") or entry.get("hash"), meta.get("last_changed"),
         entry.get("last_seen"), json.dumps(resource, default=str)))
    pk = cur.lastrowid
    if search_mode(db) != "scan":
        db.execute("INSERT INTO resource_search (rowid, name, resource_id, text) VALUES (?,?,?,?)",
                   (pk, resource_name(resource), rid, search_text(resource)))

    rules = resource.get("_rules") or {}
    kind = "network_acl" if key == "ec2/network_acls" else "security_group"
    db.executemany(
        "INSERT INTO network_rules VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(pk, kind, direction, r.get("rule"), r.get("action"), r.get("protocol"), r.get("ports"),
          r.get("peer_type") or ("cidr" if r.get("cidr") else None), r.get("peer") or r.get("cidr"), r.get("description"))
         for direction in ("inbound", "outbound") for r in rules.get(direction) or []])

    network = resource.get("_network") or {}
    links = [("security_group", sg["id"]) for sg in network.get("security_groups") or []]
    links += [("subnet", s["id"]) for s in network.get("subnets") or []]
    vpc = vpc_of(resource)
    if vpc and key != "ec2/vpcs":
        links.append(("vpc", vpc))
    db.executemany("INSERT INTO resource_links VALUES (?,?,?)", [(pk, k, t) for k, t in links])

    if key == "route53/hosted_zones":
        zone = resource
        rows = []
        for rec in (resource.get("_details") or {}).get("Records") or []:
            if not isinstance(rec, dict):
                continue
            lookup = rec.get("_lookup") or {}
            rows.append((pk, rid, zone.get("Name"), int(bool((zone.get("Config") or {}).get("PrivateZone"))),
                         rec.get("Name"), rec.get("Type"), rec.get("TTL"),
                         json.dumps([v.get("Value") for v in rec.get("ResourceRecords") or []]),
                         (rec.get("AliasTarget") or {}).get("DNSName"), lookup.get("status"),
                         json.dumps(lookup.get("addresses") or [])))
        db.executemany("INSERT INTO dns_records VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)


def account_plan(db, acct_dir):
    """What the database lacks for one account: [(label, type, id, manifest entry)] to write and
    [(label, type, id)] to delete."""
    have = {(r[0], r[1], r[2]): r[3] for r in db.execute(
        "SELECT region, type, resource_id, hash FROM resources WHERE account_id=?", (acct_dir.name,))}
    write, seen = [], set()
    for path in sorted(acct_dir.glob("*/_manifest.json")):
        label = path.parent.name
        for key, m in (read_json(path, {}) or {}).items():
            for rid, entry in m.get("items", {}).items():
                k = (label, key, rid)
                seen.add(k)
                h = entry.get("raw_hash") or entry.get("hash")
                if k not in have or h is None or have[k] != h:
                    write.append((label, key, rid, entry))
    return write, [k for k in have if k not in seen]


def write_account(db, acct_dir, plan, tick):
    """One account in one transaction: its resources, statuses, changes, coverage and row."""
    account_id = acct_dir.name
    write, delete = plan
    with db:
        for label, key, rid in delete:
            delete_resource(db, account_id, label, key, rid)
            tick()
        for label, key, rid, entry in write:
            doc = read_json(acct_dir / entry["file"])
            if doc is not None:
                write_resource(db, account_id, label, key, rid, doc, entry)
            tick()

        db.execute("DELETE FROM type_status WHERE account_id=?", (account_id,))
        for path in sorted(acct_dir.glob("*/_manifest.json")):
            for key, m in (read_json(path, {}) or {}).items():
                db.execute("INSERT INTO type_status VALUES (?,?,?,?,?,?,?,?)",
                           (account_id, path.parent.name, key, m.get("status"), m.get("error"), m.get("count"),
                            m.get("last_run") or m.get("last_error"),
                            json.dumps(m["denied_details"]) if m.get("denied_details") else None))

        last = db.execute("SELECT MAX(run) FROM changes WHERE account_id=?", (account_id,)).fetchone()[0] or ""
        log = acct_dir / "_changelog.jsonl"
        if log.exists():
            with open(log, encoding="utf-8") as f:
                rows = []
                for line in f:
                    if line.strip():
                        c = json.loads(line)
                        if c.get("run", "") > last:
                            rows.append((c["run"], account_id, c.get("region"), c.get("type"), c.get("id"),
                                         c.get("action"), c.get("file")))
                db.executemany("INSERT INTO changes VALUES (?,?,?,?,?,?,?)", rows)

        coverage = read_json(acct_dir / "_coverage.json")
        if coverage:
            db.execute("INSERT OR REPLACE INTO coverage VALUES (?,?,?,?,?)",
                       (account_id, coverage.get("checked_at"), int(bool(coverage.get("checked"))),
                        coverage.get("missed"), json.dumps(coverage, default=str)))

        a = read_json(acct_dir / "_account.json", {}) or {}
        db.execute("INSERT OR REPLACE INTO accounts VALUES (?,?,?,?,?,?,?,?)",
                   (account_id, a.get("profile"), a.get("alias"), a.get("last_sync"), a.get("last_full_sweep"),
                    (a.get("connectivity") or {}).get("status"), json.dumps(a.get("regions")),
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))


def stale_accounts(db, out):
    """Accounts whose saved files are newer than what the database has."""
    known = {r[0]: r[1] for r in db.execute("SELECT account_id, last_sync FROM accounts")}
    stale = []
    for acct_dir in sorted(p for p in out.iterdir() if p.is_dir() and p.name.isdigit()):
        a = read_json(acct_dir / "_account.json", {}) or {}
        if acct_dir.name not in known or (a.get("last_sync") or "") > (known[acct_dir.name] or ""):
            stale.append(acct_dir)
    return stale


class Bar:
    """One console line for this step: DB [###---] done/total, accounts, elapsed, ETA."""

    def __init__(self, total, accounts):
        self.total, self.done, self.accounts, self.accounts_done = total, 0, accounts, 0
        self.started = time.monotonic()
        self.tty = sys.stderr.isatty()
        self.shown = 0.0

    def tick(self):
        self.done += 1
        if time.monotonic() - self.shown >= (0.5 if self.tty else 30):
            self.show()

    def show(self, end=False):
        self.shown = time.monotonic()
        elapsed = self.shown - self.started
        share = self.done / self.total if self.total else 1
        eta = 0 if self.done >= self.total else (elapsed / self.done * (self.total - self.done) if self.done else None)
        clock = lambda s: f"{int(s) // 3600:02d}:{int(s) % 3600 // 60:02d}:{int(s) % 60:02d}"
        text = (f"{self.done:,}/{self.total:,} {share:5.0%}  accounts {self.accounts_done}/{self.accounts}"
                f"  elapsed {clock(elapsed)}  ETA {clock(eta) if eta is not None else 'estimating'}")
        columns = shutil.get_terminal_size((120, 20)).columns - 1
        size = max(10, min(32, columns - len(text) - 9))
        line = f"DB    [{'#' * int(size * share)}{'-' * (size - int(size * share))}]  {text}"[:columns]
        if self.tty:
            sys.stderr.write("\r\x1b[2K" + line + ("\n" if end else ""))
        else:
            sys.stderr.write(line + "\n")
        sys.stderr.flush()


def update(db_path, out, accounts=None, run=None, rebuild=False, only=False, quiet=False):
    """Bring the database up to date for these account folders and every stale one (only: just
    these), and record the run. quiet: say nothing when nothing changed. Returns (accounts
    written, resources written or deleted)."""
    if rebuild and db_path.exists():
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
    db = connect(db_path)
    try:
        dirs = {} if only else {d.name: d for d in stale_accounts(db, out)}
        dirs.update({d.name: d for d in accounts or []})
        dirs = [dirs[k] for k in sorted(dirs)]
        plans = [(d, account_plan(db, d)) for d in dirs]
        bar = Bar(sum(len(w) + len(x) for _, (w, x) in plans), len(plans))
        if bar.total:
            print(f"Writing {bar.total:,} change(s) for {len(plans)} account(s) to {os.path.relpath(db_path)}. "
                  "Ctrl+C to stop.", file=sys.stderr)
            bar.show()
        for acct_dir, plan in plans:
            write_account(db, acct_dir, plan, bar.tick)  # statuses, history and coverage even with no resource changes
            bar.accounts_done += 1
        if bar.total:
            bar.show(end=True)
        elif not quiet:
            print(f"Database {os.path.relpath(db_path)}: nothing changed.", file=sys.stderr)
        if run:
            totals = run.get("totals", {})
            with db:
                db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (run["run"], run.get("started"), run.get("finished"), int(bool(run.get("stopped"))),
                            run.get("seconds"), len(run.get("accounts", {})), totals.get("resources"),
                            totals.get("added"), totals.get("modified"), totals.get("removed"),
                            json.dumps(run, default=str)))
        return len(plans), bar.total
    finally:
        db.close()


def matched_paths(resource, terms, path="", out=None):
    """Where in the resource the search terms are (e.g. _network.inbound[2].peer)."""
    out = [] if out is None else out
    if len(out) >= 3:
        return out
    if isinstance(resource, dict):
        for k, v in resource.items():
            matched_paths(v, terms, f"{path}.{k}" if path else str(k), out)
    elif isinstance(resource, list):
        for i, v in enumerate(resource):
            matched_paths(v, terms, f"{path}[{i}]", out)
    elif isinstance(resource, (str, int, float)) and not isinstance(resource, bool):
        text = str(resource).lower()
        if any(t in text for t in terms):
            value = str(resource)
            out.append(f"{path} = {value if len(value) <= 60 else value[:59] + '…'}")
    return out


def search(db, text, account=None, type_=None, region=None, limit=50):
    """Resources whose text holds every word of the search, best matches first:
    [(profile, account, region, type, name, id, [where it matched], database row id)]."""
    terms = [t for t in re.split(r"\s+", text.strip()) if t]
    if not terms:
        return []
    mode = search_mode(db)
    filters, params = [], []
    if account:
        filters.append("(a.profile = ? OR r.account_id = ?)")
        params += [account, account.replace("-", "")]
    if type_:
        filters.append("(r.type = ? OR r.service = ?)")
        params += [type_, type_]
    if region:
        filters.append("r.region = ?")
        params.append(region)
    # Trigram needs 3 characters; shorter words are checked on the text instead.
    short = [t for t in terms if mode == "trigram" and len(t) < 3]
    indexed = [t for t in terms if t not in short] if mode != "scan" else []
    if indexed:
        match = " AND ".join('"' + t.replace('"', '""') + '"' for t in indexed)
        sql = ("SELECT a.profile, r.account_id, r.region, r.type, r.name, r.resource_id, r.data, r.id "
               "FROM resource_search s JOIN resources r ON r.id = s.rowid "
               "LEFT JOIN accounts a ON a.account_id = r.account_id WHERE resource_search MATCH ?")
        params = [match] + params
        order = " ORDER BY s.rank"
    else:
        sql = ("SELECT a.profile, r.account_id, r.region, r.type, r.name, r.resource_id, r.data, r.id "
               "FROM resources r LEFT JOIN accounts a ON a.account_id = r.account_id WHERE 1=1")
        order = ""
    for t in (short if indexed else terms):
        filters.append("r.data LIKE ?")
        params.append(f"%{t}%")
    if filters:
        sql += " AND " + " AND ".join(filters)
    rows = db.execute(sql + order + " LIMIT ?", params + [limit]).fetchall()
    lowered = [t.lower() for t in terms]
    return [(p, a, reg, typ, name, rid, matched_paths(json.loads(data), lowered), pk)
            for p, a, reg, typ, name, rid, data, pk in rows]


def print_hits(hits, limit):
    if not hits:
        print("  nothing found")
        return
    for profile, account, region, type_, name, rid, where, _ in hits:
        label = name if name and name != rid else ""
        print(f"  {profile or account} ({account})  {region:12} {type_:34} {label}")
        print(f"      id: {rid}")
        for w in where:
            print(f"      {w}")
    if len(hits) >= limit:
        print(f"  ... showing the first {limit}; add words or --account / --type / --region to narrow it")


def settings():
    data = read_json(HERE / "aws_accounts.json", {}) or {}
    s = data.get("settings", {})
    out = (HERE / s.get("output_dir", "inventory")).resolve()
    return out, out / s.get("db_file", "inventory.db")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=["update", "search", "query"], default="update",
                        help="update (default): write what changed; search: find resources; query: run SQL")
    parser.add_argument("text", nargs="*", help="search: the words to find; query: the SQL")
    parser.add_argument("--account", help="search: only this profile or account number")
    parser.add_argument("--type", help="search: only this type or service, e.g. ec2/instances or ec2")
    parser.add_argument("--region", help="search: only this region (or global)")
    parser.add_argument("--limit", type=int, default=50, help="rows to show")
    parser.add_argument("--rebuild", action="store_true", help="update: delete the database and write every account again")
    args = parser.parse_args()
    out, db_path = settings()
    try:
        if args.command == "query":
            db = connect(db_path)
            try:
                cur = db.execute(" ".join(args.text))
            except sqlite3.Error as e:
                sys.exit(f"SQL error: {e}")
            names = [d[0] for d in cur.description or []]
            print("\t".join(names))
            for row in cur.fetchmany(args.limit):
                print("\t".join("" if v is None else str(v) for v in row))
            return 0
        if args.command == "search":
            if not db_path.exists():
                sys.exit(f"No database yet ({db_path}); run aws_inventory.py or aws_db.py first.")
            db = connect(db_path)
            if args.text:
                print_hits(search(db, " ".join(args.text), args.account, args.type, args.region, args.limit), args.limit)
                return 0
            print("Search names, ids, ARNs, IPs, CIDRs, tags, DNS names, policies. Enter on its own to quit.")
            while True:
                try:
                    text = input("Search: ").strip()
                except EOFError:
                    return 0
                if not text:
                    return 0
                print_hits(search(db, text, args.account, args.type, args.region, args.limit), args.limit)
        if not out.is_dir():
            sys.exit(f"No inventory in {out}; run aws_inventory.py first.")
        count, changed = update(db_path, out, rebuild=args.rebuild)
        print(f"Database up to date: {count} account(s) written, {changed:,} change(s).", file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        if args.command == "update":
            print("\nStopped: the accounts written before this one are in the database; "
                  "the next run continues.", file=sys.stderr)
        else:
            print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
