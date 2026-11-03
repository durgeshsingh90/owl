#!/usr/bin/env python3
"""Compare two accounts' inventories the way they were meant to match, e.g. nonp and prod.

Reads the inventory database (inventory/inventory.db, written by aws_inventory.py and
aws_db.py), bringing it up to date for the two accounts first; with --files, or when the
database is turned off, it reads the saved files instead. Makes no AWS calls, so sync
both accounts first for a fresh answer.

    python3 aws_compare.py                                  # asks which two accounts and the environment words
    python3 aws_compare.py nonp-profile prod-profile        # profiles, account numbers or merged files
    python3 aws_compare.py nonp-profile prod-profile --since 2026-11-01   # only what changed in nonp since then
    python3 aws_compare.py 111111111111 222222222222 --env-source dev,tst --env-target mtf,prod --yes

Resources are paired by name (the Name tag, else a name field, else a key such as a
listener's port), inside their VPC or parent, with the environment words of each side
read as the same word: web-dev in one account pairs with web-mtf in the other. Once
paired, ids, ARNs and account numbers are read through the pairs, so a security group
rule that points to sg-<dev web> matches one that points to sg-<mtf web>.

The report (console, and inventory/_compare/<source>-vs-<target>-<time>.json) lists:
    missing     in the source, not in the target (the step not done yet)
    extra       in the target, not in the source
    different   paired, but configured differently (field by field)
    expected    differences in fields that usually differ (CIDRs, sizes, certificates)
    not compared types (or detail calls) one side had no permission for, timed out on, or
                 never listed; they are not counted as missing
Settings (environment words, ignored and expected fields) are in aws_compare.json; the
words chosen for each pair of accounts are remembered in inventory/_compare/pairs.json.
"""

import argparse
import collections
import datetime as dt
import ipaddress
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # aws_inventory.py sits next to this file
import aws_inventory as inv  # noqa: E402


CONFIG_FILE = inv.HERE / "aws_compare.json"

GENERATED = re.compile(r"^(?:[a-z]+(?:-[a-z]+)*-[0-9a-f]{8,17}|[0-9a-f-]{32,36}|[0-9a-f]{8,}|[A-Z0-9]{13,32})$")
EMBEDDED_ID = re.compile(
    r"\b(?:sg|subnet|vpc|rtb|acl|igw|eigw|nat|eni|vpce|vpce-svc|tgw|tgw-attach|tgw-rtb|pcx|i|vol|snap|ami|"
    r"eipalloc|eipassoc|lt|cvpn-endpoint|vgw|cgw|vpn|dopt|fl|pl|sgr|rtbassoc|aclassoc|lb|tg)-[0-9a-f]{8,17}\b")
ACCOUNT = re.compile(r"(?<!\d)\d{12}(?!\d)")
HEX = re.compile(r"(?<![0-9a-z])[0-9a-f]{8,}(?![0-9a-z])")
NOT_LISTED = "not listed"


def load_config():
    cfg = inv.read_json(CONFIG_FILE)
    if cfg is None:
        sys.exit(f"Missing {CONFIG_FILE}")
    return cfg


def last_segment(value):
    return str(value).rstrip("/").split("/")[-1].split(":")[-1]


# ---------------------------------------------------------------------------- one side

def tag(resource, key="Name"):
    for holder in (resource, resource.get("_details") or {}):
        for field in ("Tags", "tags", "TagList", "TagSet"):
            tags = holder.get(field) if isinstance(holder, dict) else None
            if isinstance(tags, dict) and isinstance(tags.get(key), str):
                return tags[key]
            for t in tags if isinstance(tags, list) else []:
                if isinstance(t, dict) and (t.get("Key") or t.get("key")) == key:
                    return t.get("Value") or t.get("value")
    return None


def values_at(obj, path):
    """The plain values at a dotted path; lists on the way are gone through (Associations.SubnetId)."""
    found = [obj]
    for step in path.split("."):
        found = [n[step] for node in found for n in (node if isinstance(node, list) else [node])
                 if isinstance(n, dict) and step in n]
    flat = [x for v in found for x in (v if isinstance(v, list) else [v])]
    return [v for v in flat if v not in (None, "") and not isinstance(v, (dict, list))]


def part_values(resource, part):
    """One pair_by part of a resource, as strings (see pair_by in aws_compare.json)."""
    if part == "main":
        return ["main"] if any(isinstance(a, dict) and a.get("Main") for a in resource.get("Associations") or []) else []
    if part == "default":
        return ["default"] if resource.get("IsDefault") is True else []
    if part.startswith("="):
        return [part[1:]]
    if part == "cidr_in_vpc":
        return [resource["CidrBlock"]] if resource.get("CidrBlock") else []
    return [str(v) for v in values_at(resource, part[4:] if part.startswith("ref:") else part)]


def cidr_in_vpc(cidr, vpc_cidrs):
    """Where a subnet sits inside its VPC: 10.1.4.0/24 in 10.1.0.0/16 -> block1+1024/24, the same as
    10.2.4.0/24 in 10.2.0.0/16."""
    try:
        net = ipaddress.ip_network(cidr, strict=False)
        for i, block in enumerate(vpc_cidrs, 1):
            vpc = ipaddress.ip_network(block, strict=False)
            if net.version == vpc.version and net.subnet_of(vpc):
                return f"block{i}+{int(net.network_address) - int(vpc.network_address)}/{net.prefixlen}"
    except ValueError:
        pass
    return "outside-vpc"


def describe(cfg, rt, label, key, rid, ref, resource):
    """What is needed to pair one resource, without keeping the resource."""
    name = tag(resource)
    for field in cfg["name_keys"]:
        if not name and isinstance(resource.get(field), str) and resource[field]:
            name = resource[field]
    fields = ",".join(f"{f}={resource[f]}" for f in cfg["key_fields"] if resource.get(f) not in (None, ""))
    if not name and not fields:
        # Last: the id itself, when it is a name (an SQS URL's queue, an EKS cluster) and not generated.
        candidate = last_segment(rid) if rid.startswith("arn:") or "://" in rid else rid
        if not GENERATED.match(candidate) and "#" not in candidate:
            name = candidate
    default = resource.get("IsDefault") is True or any(
        isinstance(a, dict) and a.get("Main") for a in resource.get("Associations") or [])
    own = {rid} | {str(resource[f]) for f in rt.get("id", []) if resource.get(f)}
    own |= {v for v in resource.values() if isinstance(v, str) and v.startswith("arn:")
            and last_segment(v) in {last_segment(rid), name}}
    spec = cfg.get("pair_by", {}).get(key)
    cidrs = [c["CidrBlock"] for c in resource.get("CidrBlockAssociationSet") or [] if c.get("CidrBlock")] or (
        [resource["CidrBlock"]] if resource.get("CidrBlock") else [])
    return {"label": label, "key": key, "rid": rid, "ref": ref, "name": name, "fields": fields,
            "default": default, "vpc": None if key == "ec2/vpcs" else inv.find_vpc_id(
                {k: v for k, v in resource.items() if k not in ("_network", "_details")}),
            "parent": resource.get("_parent_id"), "own": own, "cidrs": cidrs,
            # Unnamed: what it is attached to pairs it instead (pair_by in aws_compare.json).
            "parts": [(part, part_values(resource, part)) for part in spec] if spec and not name else None}


def load_side_db(db, account_id, cfg, catalog, keep):
    """Every resource of one account in the database (described, not kept), each type's status,
    and how to read one resource again."""
    entries, status = [], {}
    for label, key, state in db.execute("SELECT region, type, status FROM type_status WHERE account_id=?", (account_id,)):
        if keep(label, key):
            status[label, key] = state or "ok"
    for pk, label, key, rid, data in db.execute(
            "SELECT id, region, type, resource_id, data FROM resources WHERE account_id=?", (account_id,)):
        if keep(label, key):
            entries.append(describe(cfg, catalog.get(key, {}), label, key, rid, pk, json.loads(data)))

    def read(e):
        row = db.execute("SELECT data FROM resources WHERE id=?", (e["ref"],)).fetchone()
        return json.loads(row[0]) if row else None

    return entries, status, read


def load_side(acct_dir, cfg, catalog, keep):
    """Every saved resource of one account (described, not kept), each type's status, and how
    to read one resource again; from the files."""
    entries, status = [], {}
    for manifest_path in sorted(acct_dir.glob("*/_manifest.json")):
        label = manifest_path.parent.name
        for key, m in (inv.read_json(manifest_path, {}) or {}).items():
            if not keep(label, key):
                continue
            status[label, key] = m.get("status", "ok")
            for rid, item in m.get("items", {}).items():
                resource = (inv.read_json(acct_dir / item["file"]) or {}).get("resource")
                if resource is not None:
                    entries.append(describe(cfg, catalog.get(key, {}), label, key, rid, item["file"], resource))
    return entries, status, lambda e: (inv.read_json(acct_dir / e["ref"]) or {}).get("resource")


def env_pattern(words):
    words = sorted({w.strip().lower() for w in words if w.strip()}, key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(map(re.escape, words)) + r")(?![a-z0-9])", re.I) if words else None


def found_words(entries, cfg):
    """How often each known environment word appears in one side's names, and an example name each."""
    pattern = env_pattern(cfg["env_words"])
    counts, examples = collections.Counter(), {}
    for e in entries:
        if e["name"]:
            for w in {w.lower() for w in pattern.findall(e["name"])}:
                counts[w] += 1
                examples.setdefault(w, e["name"])
    return counts, examples


def main_words(counts):
    """The words most names use; a word in only a few names is more likely a naming mistake."""
    if not counts:
        return []
    top = counts.most_common(1)[0][1]
    return [w for w, n in counts.most_common() if n >= max(2, top * 0.2)]


def identities(entries, words, catalog):
    """Give each resource its pairing key: its name with the environment words, account
    numbers and generated ids taken out, inside its VPC or parent. Returns how this side's
    values are cleaned for comparing (accounts and environment words, case kept)."""
    pattern = env_pattern(words)

    def clean_value(text):
        text = ACCOUNT.sub("<account>", str(text))
        return pattern.sub("<env>", text) if pattern else text

    def clean(name):
        return clean_value(name).lower()

    by_rid = {(e["label"], e["key"], e["rid"]): e for e in entries}
    vpcs = {(e["label"], e["rid"]): e for e in entries if e["key"] == "ec2/vpcs"}
    by_id = {(e["label"], own): e for e in entries for own in e["own"]}

    def parts_ident(e):
        """An unnamed resource's pairing name from its pair_by parts."""
        pieces = []
        for part, values in e["parts"]:
            if part == "cidr_in_vpc":
                vpc = vpcs.get((e["label"], e["vpc"]))
                pieces += [cidr_in_vpc(v, vpc["cidrs"] if vpc else []) for v in values]
            elif part.startswith("ref:"):
                names = []
                for v in values:
                    target = by_id.get((e["label"], v))
                    names.append((ident(target) if target is not None and target is not e else None) or "?")
                pieces.append("+".join(sorted(names)))
            else:
                pieces.append("+".join(sorted(HEX.sub("*", EMBEDDED_ID.sub("*", clean(v))) for v in values)))
        pieces = [x for x in pieces if x]
        return "|".join(pieces) or None

    def ident(e):
        if "ident" in e:
            return e["ident"]
        e["ident"] = None  # guards against loops
        if e["name"]:
            base = clean(e["name"])
        elif e.get("parts"):
            base = parts_ident(e)
        else:
            base = e["fields"] or ("default" if e["default"] else None)
        parent_key = catalog.get(e["key"], {}).get("parent")
        if base and parent_key and e["parent"]:
            parent = by_rid.get((e["label"], parent_key, e["parent"]))
            base = f"{ident(parent)}/{base}" if parent and ident(parent) else None
        elif base and e["vpc"]:
            vpc = vpcs.get((e["label"], e["vpc"]))
            base = f"{(ident(vpc) if vpc else None) or 'vpc?'}/{base}"
        e["ident"] = base
        return base

    for e in entries:
        ident(e)
    return clean_value


# ---------------------------------------------------------------------------- compare

def normalizer(refs, clean_name, ignore, text_keys=()):
    """A copy of a resource with ids, ARNs, accounts and environment words read the same way on both
    sides; in free-text fields (text_keys, e.g. Description) generated hex is ignored too."""
    def value(v, key=None):
        if isinstance(v, dict):
            return {k: value(x, k) for k, x in v.items() if k not in ignore}
        if isinstance(v, str) and key in text_keys and v not in refs:
            return HEX.sub("*", clean_name(EMBEDDED_ID.sub(lambda m: f"<{refs.get(m.group(0), 'unpaired id')}>", v)))
        if isinstance(v, list):
            return [value(x) for x in v]
        if isinstance(v, str):
            if v in refs:
                return f"<{refs[v]}>"
            return clean_name(EMBEDDED_ID.sub(lambda m: f"<{refs.get(m.group(0), 'unpaired id')}>", v))
        return v
    return value


def diff(a, b, path=""):
    """Differences between two cleaned resources; lists compare as sets (order does not matter)."""
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            p = f"{path}.{k}" if path else str(k)
            if k not in b:
                out.append({"path": p, "source": a[k], "target": "(none)"})
            elif k not in a:
                out.append({"path": p, "source": "(none)", "target": b[k]})
            else:
                out += diff(a[k], b[k], p)
    elif isinstance(a, list) and isinstance(b, list):
        sa = {json.dumps(x, sort_keys=True, default=str): x for x in a}
        sb = {json.dumps(x, sort_keys=True, default=str): x for x in b}
        only_a = [sa[k] for k in sa if k not in sb]
        only_b = [sb[k] for k in sb if k not in sa]
        if only_a or only_b:
            out.append({"path": path, "only_in_source": only_a, "only_in_target": only_b})
    elif a != b:
        out.append({"path": path, "source": a, "target": b})
    return out


def split_details(a, b):
    """Drop detail calls that one side could not make (no permission, timeout); returns their names."""
    skipped = []
    da, db = a.get("_details") or {}, b.get("_details") or {}
    for key in set(da) | set(db):
        if any(isinstance(d.get(key), dict) and "_status" in d[key] for d in (da, db)):
            da.pop(key, None)
            db.pop(key, None)
            skipped.append(key)
    return skipped


def short(value, width=70):
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return text if len(text) <= width else text[:width - 1] + "…"


def describe_change(d):
    if "only_in_source" in d:
        parts = [f"only in source: {short(x)}" for x in d["only_in_source"][:2]]
        parts += [f"only in target: {short(x)}" for x in d["only_in_target"][:2]]
        more = len(d["only_in_source"]) + len(d["only_in_target"]) - len(parts)
        return f"{d['path']}: " + "; ".join(parts) + (f" (+{more})" if more > 0 else "")
    return f"{d['path']}: {short(d['source'], 40)} -> {short(d['target'], 40)}"


def compare(source, target, cfg, catalog, volatile, since_keys=None):
    """Pair, translate and compare; returns the report as a dict."""
    ignore = set(cfg["ignore_keys"]) | set(volatile)
    expected = set(cfg["expected_keys"])
    groups = collections.defaultdict(lambda: ([], []))
    for side, entries in ((0, source["entries"]), (1, target["entries"])):
        for e in entries:
            groups[e["label"], e["key"]][side].append(e)

    report = {"missing": [], "extra": [], "different": [], "expected": [], "not_compared": [],
              "unnamed": [], "pairs": 0, "same": 0}
    refs = ({}, {})
    paired = []
    blocked = set()
    for (label, key), (a_list, b_list) in sorted(groups.items()):
        states = (source["status"].get((label, key), NOT_LISTED), target["status"].get((label, key), NOT_LISTED))
        if any(s != "ok" for s in states):
            # A side that could not list this type: nothing here counts as missing or extra.
            if a_list or b_list:
                why = [f"{side}: {s.replace('_', ' ')}" for side, s in zip(("source", "target"), states) if s != "ok"]
                report["not_compared"].append({"region": label, "type": key, "why": ", ".join(why),
                                               "source": len(a_list), "target": len(b_list)})
            blocked.add((label, key))
            continue
        by_ident = ({}, {})
        for side, items in ((0, a_list), (1, b_list)):
            for e in sorted(items, key=lambda e: e["rid"]):
                ident = e["ident"]
                if ident is None:
                    continue
                n, base = 2, ident
                while ident in by_ident[side]:
                    ident, n = f"{base}#{n}", n + 1
                by_ident[side][ident] = e
        for ident in sorted(set(by_ident[0]) | set(by_ident[1])):
            a, b = by_ident[0].get(ident), by_ident[1].get(ident)
            row = {"region": label, "type": key, "name": ident}
            if a and b:
                for side, e in ((0, a), (1, b)):
                    for own in e["own"]:
                        refs[side][own] = f"{key}:{ident}"
                paired.append((label, key, ident, a, b))
            elif a:
                report["missing"].append({**row, "source_id": a["rid"]})
            else:
                report["extra"].append({**row, "target_id": b["rid"]})
        unnamed = sum(1 for e in a_list if e["ident"] is None), sum(1 for e in b_list if e["ident"] is None)
        if any(unnamed):
            report["unnamed"].append({"region": label, "type": key, "source": unnamed[0], "target": unnamed[1]})

    text_keys = set(cfg.get("text_keys", []))
    value = (normalizer(refs[0], source["clean"], ignore, text_keys),
             normalizer(refs[1], target["clean"], ignore, text_keys))
    for label, key, ident, a, b in paired:
        if since_keys is not None and (label, key, a["rid"]) not in since_keys:
            continue
        report["pairs"] += 1
        ra = value[0](source["read"](a) or {})
        rb = value[1](target["read"](b) or {})
        skipped = split_details(ra, rb)
        changes = diff(ra, rb)
        row = {"region": label, "type": key, "name": ident, "source_id": a["rid"], "target_id": b["rid"]}
        real = [d for d in changes if not set(re.split(r"[.\[\]]", d["path"])) & expected]
        planned = [d for d in changes if d not in real]
        if real:
            report["different"].append({**row, "changes": real})
        if planned:
            report["expected"].append({**row, "changes": planned})
        if not changes:
            report["same"] += 1
        if skipped:
            report["not_compared"].append({"region": label, "type": key, "name": ident,
                                           "why": "detail calls one side could not make: " + ", ".join(sorted(skipped))})
    if since_keys is not None:
        report["missing"] = [m for m in report["missing"] if (m["region"], m["type"], m["source_id"]) in since_keys]
        report["extra"] = []
    report["regions_only_in_source"] = sorted({e["label"] for e in source["entries"]} - {e["label"] for e in target["entries"]})
    report["regions_only_in_target"] = sorted({e["label"] for e in target["entries"]} - {e["label"] for e in source["entries"]})
    return report


# ---------------------------------------------------------------------------- accounts

def saved_inventories(data):
    """[(profile, account_id, last_sync)] for the accounts with a saved inventory."""
    out = (inv.HERE / data["settings"]["output_dir"]).resolve()
    rows = []
    for p, a in sorted(data["accounts"].items()):
        account = a.get("account_id")
        if account and (out / account).is_dir():
            rows.append((p, account, (inv.read_json(out / account / "_account.json", {}) or {}).get("last_sync", "?")))
    return rows


def resolve(value, data):
    """A profile, an account number or a merged file -> (profile, account id)."""
    value = str(value)
    name = Path(value).name
    m = re.match(r"^(.*)-(\d{12})\.json$", name)
    if m:
        return m.group(1), m.group(2)
    number = value.replace("-", "")
    for p, a in data["accounts"].items():
        if value == p or (a.get("account_id") and a["account_id"] == number):
            return p, a["account_id"]
    if re.fullmatch(r"\d{12}", number):
        return number, number
    sys.exit(f"Not a saved profile, account number or inventory file: {value}")


def changed_since_db(db, account_id, since):
    """(region, type, id) of the source's resources added or modified since a date (YYYY-MM-DD)."""
    return set(db.execute("SELECT region, type, resource_id FROM changes WHERE account_id=? AND run >= ? "
                          "AND action IN ('added', 'modified')", (account_id, since.replace("-", ""))))


def changed_since(acct_dir, since):
    """(region, type, id) of the source's resources added or modified since a date (YYYY-MM-DD)."""
    stamp = since.replace("-", "")
    keys = set()
    path = acct_dir / "_changelog.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            c = json.loads(line)
            if c.get("run", "")[:8] >= stamp and c.get("action") in ("added", "modified"):
                keys.add((c["region"], c["type"], c["id"]))
    return keys


def ask_words(label, found, default):
    shown = ", ".join(default) if default else "none"
    seen = ", ".join(f"{w} ({n})" for w, n in found.most_common(6)) or "none"
    answer = inv.ask(f"  Environment words in {label} names (found: {seen}) [{shown}]: ")
    return [w.strip() for w in re.split(r"[\s,]+", answer) if w.strip()] if answer else default


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", nargs="?", help="profile, account number or merged file built first (e.g. nonp)")
    parser.add_argument("target", nargs="?", help="the one that should match it (e.g. prod)")
    parser.add_argument("--env-source", help="environment words in the source's names, e.g. dev,tst")
    parser.add_argument("--env-target", help="environment words in the target's names, e.g. mtf,prod")
    parser.add_argument("--since", help="only what changed in the source since this date (YYYY-MM-DD)")
    parser.add_argument("--services", nargs="+", help="only these services, e.g. ec2 elbv2")
    parser.add_argument("--regions", nargs="+", help="only these regions (global is always included)")
    parser.add_argument("-y", "--yes", action="store_true", help="ask nothing: use remembered or found words")
    parser.add_argument("--show", type=int, default=15, help="rows per section on screen (all are in the report)")
    parser.add_argument("--files", action="store_true", help="read the saved files, not the database")
    args = parser.parse_args()
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


def run(args):
    data = inv.load_accounts()
    cfg = load_config()
    commands = inv.load_commands()
    catalog = {r["key"]: r for r in commands["resources"]}
    out = (inv.HERE / data["settings"]["output_dir"]).resolve()
    pairs_file = out / "_compare" / "pairs.json"
    remembered = inv.read_json(pairs_file, {}) or {}

    if not (args.source and args.target):
        rows = saved_inventories(data)
        if len(rows) < 2:
            sys.exit("Need two synced accounts; run aws_inventory.py first.")
        print("Saved inventories:")
        for i, (p, account, synced) in enumerate(rows, 1):
            print(f"  {i:3}. {account:14} {p:45} synced {synced}")

        def pick(prompt, default):
            answer = inv.ask(prompt) or str(default)
            if answer.isdigit() and 1 <= int(answer) <= len(rows):
                return rows[int(answer) - 1][0]
            return answer

        args.source = args.source or pick("Compare from (the one built first, e.g. nonp) [1]: ", 1)
        args.target = args.target or pick("Compare to (e.g. prod) [2]: ", 2)
        if args.since is None and not args.yes:
            args.since = inv.ask("Only what changed in the source since (YYYY-MM-DD, Enter = everything): ") or None
    (src_profile, src_id), (dst_profile, dst_id) = resolve(args.source, data), resolve(args.target, data)
    if src_id == dst_id:
        sys.exit("Those are the same account.")
    if args.since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.since):
        sys.exit("--since takes a date like 2026-11-01")

    def keep(label, key):
        return ((not args.services or key.split("/")[0] in args.services)
                and (not args.regions or label == "global" or label in args.regions))

    for profile, account in ((src_profile, src_id), (dst_profile, dst_id)):
        if not (out / account).is_dir():
            sys.exit(f"No inventory for {profile} ({account}); sync it with aws_inventory.py first.")
    db = None
    if not args.files and data["settings"].get("write_db", True):
        import aws_db
        db_path = out / data["settings"].get("db_file", "inventory.db")
        # Up to date for these two accounts, whatever the last sync left behind.
        aws_db.update(db_path, out, [out / src_id, out / dst_id], only=True, quiet=True)
        db = aws_db.connect(db_path)

    sides = []
    for profile, account in ((src_profile, src_id), (dst_profile, dst_id)):
        acct_dir = out / account
        if db:
            entries, status, read = load_side_db(db, account, cfg, catalog, keep)
        else:
            entries, status, read = load_side(acct_dir, cfg, catalog, keep)
        sides.append({"profile": profile, "account": account, "dir": acct_dir, "entries": entries, "status": status,
                      "read": read, "synced": (inv.read_json(acct_dir / "_account.json", {}) or {}).get("last_sync", "?")})

    # The environment words of each side: given, remembered for this pair, or what the names show.
    pair_key = f"{src_id}|{dst_id}"
    words = []
    for i, (side, given) in enumerate(zip(sides, (args.env_source, args.env_target))):
        found, examples = found_words(side["entries"], cfg)
        side["found_words"] = {w: {"count": n, "example": examples[w]} for w, n in found.most_common()}
        default = (given.split(",") if given else remembered.get(pair_key, {}).get("words", [None, None])[i]
                   or main_words(found) or [w for w in cfg["env_words"]
                                            if re.search(rf"(?<![a-z0-9]){w}(?![a-z0-9])", side["profile"].lower())])
        if not given and not args.yes:
            if i == 0:
                print("Names differ by their environment words; those are read as the same word on both sides.")
            for w, n in found.items():
                if w not in default:
                    print(f'  {side["profile"]} also has "{w}" in {n} name(s), e.g. {examples[w]}: left out, '
                          "so they show as different; type it in if it's on purpose.")
            default = ask_words(f"{side['profile']} ({side['account']})", found, default)
        words.append([w.lower() for w in default])
    remembered[pair_key] = {"words": words, "used": inv.iso(inv.utc_now())}
    inv.write_json(pairs_file, remembered)
    for side, w in zip(sides, words):
        side["clean"] = identities(side["entries"], w, catalog)

    since_keys = None
    if args.since:
        since_keys = changed_since_db(db, src_id, args.since) if db else changed_since(sides[0]["dir"], args.since)
    report = compare(sides[0], sides[1], cfg, catalog, commands.get("volatile_keys", []), since_keys)
    report["blind_spots"] = blind_spots(report, sides, db)
    report = {"source": {k: sides[0][k] for k in ("profile", "account", "synced", "found_words")} | {"env_words": words[0]},
              "target": {k: sides[1][k] for k in ("profile", "account", "synced", "found_words")} | {"env_words": words[1]},
              "since": args.since, "compared_at": inv.iso(inv.utc_now()), "read_from": "database" if db else "files",
              **report}
    stamp = inv.utc_now().strftime("%Y%m%dT%H%M%SZ")
    path = out / "_compare" / f"{inv.safe_name(src_profile)}-vs-{inv.safe_name(dst_profile)}-{stamp}.json"
    inv.write_json(path, report)
    print_report(report, args.show, path)
    return 1 if report["missing"] or report["different"] else 0


def blind_spots(report, sides, db):
    """What this compare cannot see, said before its findings."""
    lines = []
    for side, label in zip(sides, ("source", "target")):
        why = collections.Counter()
        for n in report["not_compared"]:
            for part in n["why"].split(", "):
                if part.startswith(f"{label}: "):
                    why[part.split(": ", 1)[1]] += 1
        detail = sum(1 for n in report["not_compared"] if "detail calls" in n["why"])
        if why or detail:
            lines.append(f"{side['profile']}: {sum(why.values())} type(s) not compared ("
                         + ", ".join(f"{w} {n}" for w, n in why.most_common()) + ")"
                         + (f"; {detail} resource(s) with detail calls one side could not make" if detail and label == "source" else ""))
    unnamed = [u for u in report["unnamed"] if u["source"] or u["target"]]
    if unnamed:
        lines.append(f"no name or pair_by rule to pair by: {sum(u['source'] for u in unnamed)} in source, "
                     f"{sum(u['target'] for u in unnamed)} in target ("
                     + ", ".join(sorted({u['type'] for u in unnamed})[:6]) + "); add a pair_by rule in aws_compare.json")
    for side in sides:
        if db:
            row = db.execute("SELECT report FROM coverage WHERE account_id=?", (side["account"],)).fetchone()
            coverage = json.loads(row[0]) if row else None
        else:
            coverage = inv.read_json(side["dir"] / "_coverage.json")
        if not coverage:
            lines.append(f"{side['profile']}: no coverage check yet (what AWS lists but the inventory lacks is unknown)")
        elif not coverage.get("checked"):
            lines.append(f"{side['profile']}: coverage could not be checked (no access to the Tagging API or AWS Config)")
        elif coverage.get("missed") or coverage.get("config_types_not_in_commands"):
            top = ", ".join(f"{k} {v['count']}" for k, v in list(coverage.get("missed_types", {}).items())[:4])
            lines.append(f"{side['profile']}: AWS lists {coverage.get('missed', 0)} resource(s) its inventory lacks"
                         + (f" ({top})" if top else "")
                         + (f", and {len(coverage['config_types_not_in_commands'])} type(s) aws_commands.json does not cover"
                            if coverage.get("config_types_not_in_commands") else ""))
    times = []
    for side in sides:
        try:
            times.append(dt.datetime.strptime(side["synced"], "%Y-%m-%dT%H:%M:%SZ"))
        except (TypeError, ValueError):
            times.append(None)
    if None not in times and abs(times[0] - times[1]) > dt.timedelta(hours=24):
        days = abs(times[0] - times[1]).total_seconds() / 86400
        lines.append(f"synced {days:.1f} days apart ({sides[0]['synced']} vs {sides[1]['synced']}): sync both for a fair compare")
    for side, regions in (("source", report["regions_only_in_source"]), ("target", report["regions_only_in_target"])):
        if regions:
            lines.append(f"regions synced only in the {side}: {', '.join(regions)}")
    return lines


def print_report(r, show, path):
    s, t = r["source"], r["target"]
    print(f"\nCompare {s['profile']} ({s['account']}, synced {s['synced']}) -> {t['profile']} ({t['account']}, synced {t['synced']})")
    print(f"Environment words: {', '.join(s['env_words']) or 'none'} -> {', '.join(t['env_words']) or 'none'}"
          + (f"; only what changed in the source since {r['since']}" if r["since"] else ""))
    print(f"{r['pairs']:,} paired: {r['same']:,} the same, {len(r['different']):,} different, "
          f"{len(r['expected']):,} with expected differences | {len(r['missing']):,} missing in target, "
          f"{len(r['extra']):,} extra in target | {len(r['not_compared']):,} not compared")
    if r["blind_spots"]:
        print("\nBlind spots (this compare cannot see these):")
        for line in r["blind_spots"]:
            print(f"  - {line}")

    def section(title, rows, line):
        if not rows:
            return
        print(f"\n{title} ({len(rows):,}):")
        for row in rows[:show]:
            print("  " + line(row))
        if len(rows) > show:
            print(f"  ... {len(rows) - show:,} more in the report")

    section("Missing in target", r["missing"], lambda m: f"{m['region']:12} {m['type']:38} {m['name']}")
    section("Extra in target", r["extra"], lambda m: f"{m['region']:12} {m['type']:38} {m['name']}")
    section("Different", r["different"], lambda d: f"{d['region']:12} {d['type']:38} {d['name']}\n      "
            + "\n      ".join(describe_change(c) for c in d["changes"][:3])
            + (f"\n      (+{len(d['changes']) - 3} more)" if len(d["changes"]) > 3 else ""))
    section("Not compared", r["not_compared"], lambda n: f"{n['region']:12} {n['type']:38} "
            + (f"{n['name']}: " if n.get("name") else "") + n["why"])
    if r["expected"]:
        fields = collections.Counter(c["path"].split(".")[-1].split("[")[0] for e in r["expected"] for c in e["changes"])
        print(f"\nExpected differences in {len(r['expected']):,} resource(s): "
              + ", ".join(f"{f} {n}" for f, n in fields.most_common(6)) + " (in the report)")
    print(f"\nReport: {path}")


if __name__ == "__main__":
    sys.exit(main())
