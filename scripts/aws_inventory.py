#!/usr/bin/env python3
"""Download every AWS resource of many accounts as JSON, using only the AWS CLI.

The commands it runs live in aws_commands.json; this file only runs them.
The accounts to scan are remembered in aws_accounts.json.

    python3 aws_inventory.py                                    # asks for account numbers; Enter = all saved
    python3 aws_inventory.py add prod-profile dev-profile      # remember accounts
    python3 aws_inventory.py add --all-profiles --match '^acme-' # every profile in ~/.aws/config that matches
    python3 aws_inventory.py config regions=us-east-1,eu-west-1  # or regions=all
    python3 aws_inventory.py sync                               # download / refresh everything
    python3 aws_inventory.py sync --profiles prod-profile --services ec2 elbv2 route53
    python3 aws_inventory.py changes --diff                     # what changed in the last sync

Each account starts with `sts get-caller-identity`; an account that cannot connect is
reported and skipped. Inside an account every region runs in parallel, and inside a
region every resource type runs in its own worker (list, then the per-resource detail
calls). Each resource is written to its own file, then everything is merged:

    inventory/<account_id>/inventory.json                      the account merged: global + regions -> vpcs -> resources
    inventory/<account_id>/us-east-1/ec2/instances/<InstanceId>.json
    inventory/<account_id>/global/route53/hosted_zones/<zone>.json   records with nslookup answers
    inventory/<account_id>/_changelog.jsonl                    every change ever seen
    inventory/_runs/<run>/summary.json, changes.json, run.log
    inventory/_history/<run>/<account_id>/...                  previous versions of changed files

A call that times out is tried once more; if it times out again it is saved as a
timeout (the type keeps its previous files, a detail becomes {"_error": "Timeout"}).
A sync lists every type each time, but repeats the per-resource detail calls only for
resources whose list entry changed, or once detail_refresh_hours have passed.
"""

import argparse
import concurrent.futures as cf
import configparser
import datetime as dt
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path


HERE = Path(__file__).resolve().parent
COMMANDS_FILE = HERE / "aws_commands.json"
ACCOUNTS_FILE = HERE / "aws_accounts.json"

DEFAULT_SETTINGS = {
    "output_dir": "inventory",
    "regions": ["us-east-1"],
    "global_region": "us-east-1",
    "verify_ssl": False,
    "aws_cli": "aws",
    "account_workers": 8,
    "region_workers": 4,
    "type_workers": 6,
    "detail_workers": 4,
    # Upper bound on aws processes running at once, across all accounts and regions.
    "max_parallel_calls": 32,
    "connect_timeout": 15,
    "read_timeout": 60,
    "call_timeout": 300,
    # A timed out call is tried this many more times before it is saved as a timeout.
    "timeout_retries": 1,
    "detail_refresh_hours": 24,
    "keep_history": True,
    "dns_lookup": True,
    # Only look up record names matching this regex, e.g. "\\.sep\\.?$". Empty: all names.
    "dns_match": "",
    "dns_workers": 8,
    "dns_timeout": 20,
}

# The profile may not read everything: these are skipped and saved as "no_permission".
PERMISSION_ERRORS = {
    "AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "NotAuthorized",
    "AuthorizationError", "UnauthorizedException", "Forbidden", "ForbiddenException",
}
# The service or region is not available to this account: saved as "not_available".
UNAVAILABLE_ERRORS = {
    "AuthFailure", "OptInRequired", "SubscriptionRequiredException", "UnrecognizedClientException",
    "InvalidClientTokenId", "EndpointConnectionError", "UnsupportedOperation", "InvalidAction",
    "AWSOrganizationsNotInUseException", "InvalidAccessException", "NoSuchEntity",
}
NOT_FOUND_ERRORS = {"NoSuchEntity", "ResourceNotFoundException", "NotFoundException"}


def error_status(code):
    if code == "Timeout":
        return "timeout"
    if code in PERMISSION_ERRORS:
        return "no_permission"
    if code in UNAVAILABLE_ERRORS:
        return "not_available"
    return "error"

SG_ID = re.compile(r"\bsg-[0-9a-f]{8,17}\b")
SUBNET_ID = re.compile(r"\bsubnet-[0-9a-f]{8,17}\b")
VPC_KEYS = ("VpcId", "vpcId", "VPCId")

log = logging.getLogger("aws_inventory")


# ---------------------------------------------------------------------------- files

def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True, default=str)
        f.write("\n")
    os.replace(tmp, path)


def safe_name(value):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("_.") or "_"
    if name != value or len(name) > 150:
        name = name[:120] + "__" + hashlib.sha1(str(value).encode()).hexdigest()[:10]
    return name


def fill(args, values):
    """Replace {Field} placeholders in command arguments."""
    return [a.format_map(values) if "{" in a else a for a in args]


def load_accounts():
    data = read_json(ACCOUNTS_FILE, {}) or {}
    data["settings"] = {**DEFAULT_SETTINGS, **data.get("settings", {})}
    data.setdefault("accounts", {})
    return data


def load_commands():
    data = read_json(COMMANDS_FILE)
    if data is None:
        sys.exit(f"Missing {COMMANDS_FILE}")
    for r in data["resources"]:
        r.setdefault("scope", "regional")
        r.setdefault("details", [])
        r.setdefault("id", [])
        r["key"] = f"{r['service']}/{r['type']}"
        if isinstance(r["id"], str):
            r["id"] = [r["id"]]
    keys = {r["key"] for r in data["resources"]}
    for r in data["resources"]:
        if r.get("parent") and r["parent"] not in keys:
            sys.exit(f"{r['key']}: parent {r['parent']} is not in {COMMANDS_FILE.name}")
    return data


# ---------------------------------------------------------------------------- aws cli

class AwsError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    @classmethod
    def from_stderr(cls, stderr):
        lines = [l for l in stderr.strip().splitlines() if l.strip() and "Warning" not in l and "warnings.warn" not in l]
        text = lines[-1] if lines else stderr.strip()
        m = re.search(r"An error occurred \(([^)]+)\)", text)
        if m:
            return cls(m.group(1), text)
        if "Connect timeout" in text or "Read timeout" in text or "timed out" in text:
            return cls("Timeout", text)
        if "Could not connect to the endpoint" in text:
            return cls("EndpointConnectionError", text)
        if "could not be found" in text and "profile" in text:
            return cls("ProfileNotFound", text)
        if "Token" in text and ("expired" in text or "SSO" in text):
            return cls("TokenExpired", text)
        return cls("CliError", text)


class Aws:
    """Runs `aws <args>` for one profile and returns the parsed JSON output."""

    def __init__(self, profile, settings, gate):
        self.profile = profile
        self.settings = settings
        self.gate = gate
        self.env = {**os.environ, "AWS_PAGER": "", "PYTHONWARNINGS": "ignore"}
        self.env.setdefault("AWS_RETRY_MODE", "adaptive")
        self.env.setdefault("AWS_MAX_ATTEMPTS", "10")

    def __call__(self, args, region=None, query=None):
        s = self.settings
        # aws <service> <operation> ... --profile <profile> --no-verify-ssl, then the options the script needs.
        cmd = [s["aws_cli"], *args, "--profile", self.profile]
        if not s["verify_ssl"]:
            cmd.append("--no-verify-ssl")
        cmd += ["--output", "json", "--no-cli-pager",
                "--cli-connect-timeout", str(s["connect_timeout"]), "--cli-read-timeout", str(s["read_timeout"])]
        if region:
            cmd += ["--region", region]
        if query:
            cmd += ["--query", query]
        for attempt in range(s["timeout_retries"] + 1):
            try:
                return self.run(cmd)
            except AwsError as e:
                if e.code != "Timeout" or attempt == s["timeout_retries"]:
                    raise
                log.info("%s %s: timed out, trying again: %s", self.profile, region, " ".join(args))

    def run(self, cmd):
        log.debug("run %s", " ".join(cmd))
        with self.gate:
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, env=self.env, timeout=self.settings["call_timeout"])
            except subprocess.TimeoutExpired:
                raise AwsError("Timeout", f"no answer in {self.settings['call_timeout']}s")
        if p.returncode != 0:
            raise AwsError.from_stderr(p.stderr)
        out = p.stdout.strip()
        return json.loads(out) if out else None


def run_named(aws, commands, name, region=None):
    c = commands["account"][name]
    return aws(c["command"], region, c.get("query"))


# ---------------------------------------------------------------------------- change detection

def canonical(obj, volatile):
    """The resource as compared between syncs: volatile fields dropped, list order ignored."""
    if isinstance(obj, dict):
        return {k: canonical(v, volatile) for k, v in obj.items() if k not in volatile}
    if isinstance(obj, list):
        items = [canonical(v, volatile) for v in obj]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True, default=str))
    return obj


def digest(obj, volatile=()):
    text = json.dumps(canonical(obj, volatile), sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def json_diff(old, new, path="", out=None, limit=200):
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(old, dict) and isinstance(new, dict):
        for k in sorted(set(old) | set(new), key=str):
            p = f"{path}.{k}" if path else str(k)
            if k not in old:
                out.append({"path": p, "op": "added", "new": new[k]})
            elif k not in new:
                out.append({"path": p, "op": "removed", "old": old[k]})
            else:
                json_diff(old[k], new[k], p, out, limit)
    elif isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        for i, (a, b) in enumerate(zip(old, new)):
            json_diff(a, b, f"{path}[{i}]", out, limit)
    elif old != new:
        out.append({"path": path, "op": "changed", "old": old, "new": new})
    return out


def parse_embedded_json(obj):
    """Policies come back as JSON strings; store them as JSON."""
    if isinstance(obj, str) and obj[:1] in "{[" and obj[-1:] in "}]":
        try:
            return parse_embedded_json(json.loads(obj))
        except ValueError:
            return obj
    if isinstance(obj, dict):
        return {k: parse_embedded_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [parse_embedded_json(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------- inbound / outbound

def sg_rules(permissions):
    rules = []
    for p in permissions or []:
        proto = "all" if p.get("IpProtocol") == "-1" else p.get("IpProtocol")
        lo, hi = p.get("FromPort"), p.get("ToPort")
        ports = "all" if proto == "all" or lo is None or lo == -1 else (str(lo) if lo == hi else f"{lo}-{hi}")
        peers = (
            [("cidr", r.get("CidrIp"), r) for r in p.get("IpRanges", [])]
            + [("cidr6", r.get("CidrIpv6"), r) for r in p.get("Ipv6Ranges", [])]
            + [("prefix_list", r.get("PrefixListId"), r) for r in p.get("PrefixListIds", [])]
            + [("security_group", "/".join(filter(None, [r.get("UserId"), r.get("GroupId")])), r) for r in p.get("UserIdGroupPairs", [])]
        )
        for kind, peer, r in peers:
            rules.append({"protocol": proto, "ports": ports, "peer_type": kind, "peer": peer, "description": r.get("Description")})
    return rules


def nacl_rules(entries, egress):
    rules = []
    for e in sorted(entries or [], key=lambda e: e.get("RuleNumber", 0)):
        if e.get("Egress") != egress:
            continue
        pr = e.get("PortRange") or {}
        ports = "all" if not pr else (str(pr.get("From")) if pr.get("From") == pr.get("To") else f"{pr.get('From')}-{pr.get('To')}")
        rules.append({
            "rule": e.get("RuleNumber"), "action": e.get("RuleAction"),
            "protocol": "all" if e.get("Protocol") == "-1" else e.get("Protocol"),
            "ports": ports, "cidr": e.get("CidrBlock") or e.get("Ipv6CidrBlock"),
        })
    return rules


def find_ids(obj, pattern, found):
    if isinstance(obj, str):
        found.update(pattern.findall(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            find_ids(v, pattern, found)
    elif isinstance(obj, list):
        for v in obj:
            find_ids(v, pattern, found)
    return found


def add_network(resources, by_key, reference):
    """Attach security group, network ACL and route rules to the resources that use them.

    reference(key) returns that type's resources, from this sync or else from disk.
    """
    sgs = reference("ec2/security_groups")
    for sg in sgs.values():
        sg["_rules"] = {"inbound": sg_rules(sg.get("IpPermissions")), "outbound": sg_rules(sg.get("IpPermissionsEgress"))}
    nacls = reference("ec2/network_acls")
    subnet_nacl = {}
    for acl in nacls.values():
        acl["_rules"] = {"inbound": nacl_rules(acl.get("Entries"), False), "outbound": nacl_rules(acl.get("Entries"), True)}
        for a in acl.get("Associations", []):
            subnet_nacl[a.get("SubnetId")] = acl
    subnets = reference("ec2/subnets")
    subnet_rtb, main_rtb = {}, {}
    for rtb in reference("ec2/route_tables").values():
        for a in rtb.get("Associations", []):
            if a.get("Main"):
                main_rtb[rtb.get("VpcId")] = rtb
            elif a.get("SubnetId"):
                subnet_rtb[a["SubnetId"]] = rtb

    for key, items in resources.items():
        if by_key[key].get("network") != "attach":
            continue
        for item in items.values():
            item.pop("_network", None)
            network = {}
            sg_ids = sorted(find_ids(item, SG_ID, set()) & set(sgs))
            if sg_ids:
                network["security_groups"] = [
                    {"id": i, "name": sgs[i].get("GroupName"), **sgs[i]["_rules"]} for i in sg_ids
                ]
                network["inbound"] = [{"security_group": i, **r} for i in sg_ids for r in sgs[i]["_rules"]["inbound"]]
                network["outbound"] = [{"security_group": i, **r} for i in sg_ids for r in sgs[i]["_rules"]["outbound"]]
            subnet_ids = sorted(find_ids(item, SUBNET_ID, set()) & set(subnets))
            if subnet_ids:
                network["subnets"] = []
                for s in subnet_ids:
                    acl = subnet_nacl.get(s)
                    rtb = subnet_rtb.get(s) or main_rtb.get(subnets[s].get("VpcId"))
                    network["subnets"].append({
                        "id": s,
                        "cidr": subnets[s].get("CidrBlock"),
                        "network_acl": acl and {"id": acl.get("NetworkAclId"), **acl["_rules"]},
                        "route_table": rtb and {"id": rtb.get("RouteTableId"), "routes": rtb.get("Routes", [])},
                    })
            if network:
                item["_network"] = network


# ---------------------------------------------------------------------------- dns

def parse_nslookup(text):
    low = text.lower()
    if "timed out" in low or "no servers could be reached" in low:
        return {"status": "timeout"}
    if "nxdomain" in low or "non-existent domain" in low:
        return {"status": "nxdomain"}
    if "servfail" in low:
        return {"status": "servfail"}
    addresses, cnames, answer = [], [], False
    for line in text.splitlines():
        s = line.strip()
        if "canonical name =" in s:
            cnames.append(s.split("=", 1)[1].strip().rstrip("."))
        elif "has AAAA address" in s:
            addresses.append(s.rsplit(" ", 1)[1])
        elif s.startswith("Name:"):
            answer = True
        elif answer and s.startswith(("Address:", "Addresses:")):
            value = s.split(":", 1)[1].strip()
            if value:
                addresses.append(value)
        elif answer and line[:1].isspace() and re.fullmatch(r"[0-9A-Fa-f:.]+", s):
            addresses.append(s)  # Windows lists further addresses on their own lines
    if addresses or cnames:
        return {"status": "ok", "addresses": addresses, "cnames": cnames}
    return {"status": "no_answer"}


def lookup(run, name):
    cmd = fill(run.commands["dns_lookup"]["command"], {"name": name})
    result = {"status": "timeout"}
    for _ in range(run.settings["timeout_retries"] + 1):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=run.settings["dns_timeout"])
        except subprocess.TimeoutExpired:
            continue
        except FileNotFoundError:
            return {"status": "error", "error": f"{cmd[0]} not found"}
        result = parse_nslookup(p.stdout + p.stderr)
        if result["status"] != "timeout":
            return result
    return result


def add_dns_lookups(run, zones):
    """nslookup every A/AAAA/CNAME record name and save the answer next to the record."""
    cfg = run.commands["dns_lookup"]
    match = re.compile(run.settings["dns_match"]) if run.settings["dns_match"] else None
    wanted = []
    for zone in zones.values():
        for rec in (zone.get("_details") or {}).get(cfg["records_detail"]) or []:
            if not isinstance(rec, dict):
                continue
            rec.pop("_lookup", None)
            name = rec.get("Name", "").replace("\\052", "*").rstrip(".")
            if rec.get("Type") in cfg["record_types"] and name and "*" not in name and (not match or match.search(name + ".")):
                wanted.append((rec, name))
    names = sorted({n for _, n in wanted})
    with cf.ThreadPoolExecutor(run.settings["dns_workers"]) as pool:
        answers = dict(zip(names, pool.map(lambda n: lookup(run, n), names)))
    for rec, name in wanted:
        rec["_lookup"] = answers[name]
    if names:
        timeouts = sum(1 for a in answers.values() if a["status"] == "timeout")
        log.info("dns: looked up %d name(s), %d timed out", len(names), timeouts)


# ---------------------------------------------------------------------------- sync

class Run:
    def __init__(self, data, commands, args):
        self.settings = data["settings"]
        self.commands = commands
        self.full = args.full
        self.started = utc_now()
        self.id = self.started.strftime("%Y%m%dT%H%M%SZ")
        self.out = (HERE / self.settings["output_dir"]).resolve()
        self.run_dir = self.out / "_runs" / self.id
        self.gate = threading.BoundedSemaphore(self.settings["max_parallel_calls"])
        self.volatile = set(commands.get("volatile_keys", []))
        self.lock = threading.Lock()
        self.changes = []
        self.seen_accounts = {}
        self.by_key = {r["key"]: r for r in commands["resources"]}

        keys = [r["key"] for r in commands["resources"]
                if (not args.services or r["service"] in args.services) and (not args.types or r["key"] in args.types)]
        # A child type needs its parent's resources, so the parent runs too.
        for k in list(keys):
            while self.by_key[k].get("parent") and self.by_key[k]["parent"] not in keys:
                k = self.by_key[k]["parent"]
                keys.append(k)
        self.resources = [r for r in commands["resources"] if r["key"] in keys]


def resource_id(item, rt):
    if rt.get("singleton"):
        return rt["type"]
    for f in rt["id"]:
        if item.get(f) not in (None, ""):
            return str(item[f])
    return hashlib.sha1(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()[:16]


def resource_arn(item):
    for k, v in item.items():
        if k.lower().endswith("arn") and isinstance(v, str) and v.startswith("arn:"):
            return v
    return None


def find_vpc_id(obj):
    """The first VpcId in the resource, nearest to the top."""
    queue = [obj]
    while queue:
        node = queue.pop(0)
        if isinstance(node, dict):
            for k in VPC_KEYS:
                if isinstance(node.get(k), str) and node[k].startswith("vpc-"):
                    return node[k]
            queue += [v for v in node.values() if isinstance(v, (dict, list))]
        elif isinstance(node, list):
            queue += [v for v in node if isinstance(v, (dict, list))]
    return None


def fetch_details(aws, rt, item, region, context):
    details = {}
    for d in rt["details"]:
        try:
            args = fill(d["command"], {**context, **item})
        except KeyError as e:
            details[d["key"]] = {"_error": f"missing field {e}"}
            continue
        try:
            details[d["key"]] = parse_embedded_json(aws(args, region, d.get("query")))
        except AwsError as e:
            if e.code in d.get("ignore_errors", []) or e.code in NOT_FOUND_ERRORS:
                details[d["key"]] = None
            else:
                (log.info if e.code == "Timeout" else log.debug)("%s %s %s detail %s: %s", aws.profile, region, rt["key"], d["key"], e)
                details[d["key"]] = {"_status": error_status(e.code), "_error": e.code}
    return details


def fetch_type(run, aws, account, region, rt, prev, parents):
    """One worker: list one resource type (per parent for child types), then its details.

    Returns {id: resource} and {id: when its details were fetched}. Raises AwsError when
    the list fails, so the previous files are kept.
    """
    acct_dir = run.out / account["account_id"]
    context = {"account_id": account["account_id"], "region": region}
    raw = []
    if rt.get("parent"):
        for pid, parent in parents.items():
            try:
                found = aws(fill(rt["command"], {**context, **parent}), region, rt.get("query")) or []
            except AwsError as e:
                if e.code in NOT_FOUND_ERRORS:
                    continue
                raise
            raw += [(pid, r) for r in (found if isinstance(found, list) else [found])]
    else:
        found = aws(fill(rt["command"], context), region, rt.get("query")) or []
        raw = [(None, r) for r in (found if isinstance(found, list) else [found])]

    items = {}
    for pid, r in raw:
        item = parse_embedded_json(r) if isinstance(r, dict) else {"Id": r}
        if pid is not None:
            item["_parent_id"] = pid
        rid = resource_id(item, rt)
        if pid is not None:
            rid = f"{pid}/{rid}"
        while rid in items:
            rid += "#"
        items[rid] = item

    now = iso(utc_now())
    refresh = dt.timedelta(hours=run.settings["detail_refresh_hours"])
    times = {}
    if not rt["details"]:
        return items, times

    def details_for(pair):
        rid, item = pair
        old = prev.get(rid)
        if old and not run.full and old.get("list_hash") == digest(item, run.volatile) and old.get("detail_ts"):
            fetched = dt.datetime.strptime(old["detail_ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
            doc = read_json(acct_dir / old["file"]) if utc_now() - fetched < refresh else None
            if doc and "_details" in doc.get("resource", {}):
                return rid, doc["resource"]["_details"], old["detail_ts"]
        return rid, fetch_details(aws, rt, item, region, context), now

    with cf.ThreadPoolExecutor(run.settings["detail_workers"]) as pool:
        for rid, details, ts in pool.map(details_for, list(items.items())):
            items[rid]["_details"] = details
            times[rid] = ts
    return items, times


def scan_scope(run, aws, account, label, region, rtypes):
    """Scan one region (or 'global') of one account; write changed files and the manifest."""
    acct_dir = run.out / account["account_id"]
    manifest_path = acct_dir / label / "_manifest.json"
    manifest = read_json(manifest_path, {})
    now = iso(utc_now())
    resources, detail_time, errors = {}, {}, []

    # Every type runs in its own worker; a child type starts once its parent is done.
    waiting = list(rtypes)
    running = {}
    with cf.ThreadPoolExecutor(run.settings["type_workers"]) as pool:
        while waiting or running:
            for rt in list(waiting):
                parent = rt.get("parent")
                if parent and parent not in resources and parent not in {e["type"] for e in errors}:
                    continue
                waiting.remove(rt)
                if parent and parent not in resources:
                    status = next(e["status"] for e in errors if e["type"] == parent)
                    errors.append({"scope": label, "type": rt["key"], "status": status, "code": "ParentFailed",
                                   "message": f"{parent} could not be listed"})
                    continue
                prev = manifest.get(rt["key"], {}).get("items", {})
                running[pool.submit(fetch_type, run, aws, account, rt.get("region", region), rt, prev,
                                    resources.get(parent, {}))] = rt
            if not running:
                continue
            done, _ = cf.wait(running, return_when=cf.FIRST_COMPLETED)
            for f in done:
                rt = running.pop(f)
                try:
                    resources[rt["key"]], detail_time[rt["key"]] = f.result()
                except AwsError as e:
                    status = error_status(e.code)
                    errors.append({"scope": label, "type": rt["key"], "status": status, "code": e.code, "message": e.message[:500]})
                    # No permission is normal here: counted per account on screen, listed in run.log.
                    if status in ("no_permission", "not_available"):
                        log.debug("%s %s %s: %s, skipped (%s)", account["profile"], label, rt["key"], status, e.code)
                    else:
                        log.warning("%s %s %s: %s", account["profile"], label, rt["key"], e)

    def reference(key):
        if key in resources:
            return resources[key]
        loaded = {}
        for rid, entry in manifest.get(key, {}).get("items", {}).items():
            doc = read_json(acct_dir / entry["file"])
            if doc:
                loaded[rid] = doc["resource"]
        return loaded

    if label != "global":
        add_network(resources, run.by_key, reference)
    dns_key = run.commands.get("dns_lookup", {}).get("resource")
    if run.settings["dns_lookup"] and dns_key in resources:
        add_dns_lookups(run, resources[dns_key])

    changes = []
    for key, items in resources.items():
        rt = run.by_key[key]
        prev = manifest.get(key, {}).get("items", {})
        entries = {}
        for rid, item in items.items():
            base = {k: v for k, v in item.items() if k not in ("_details", "_network", "_rules")}
            h = digest(item, run.volatile)
            raw_hash = digest(item)
            rel = f"{label}/{rt['service']}/{rt['type']}/{safe_name(rid)}.json"
            path = acct_dir / rel
            old = prev.get(rid)
            entry = {"file": rel, "hash": h, "raw_hash": raw_hash, "list_hash": digest(base, run.volatile),
                     "detail_ts": detail_time.get(key, {}).get(rid), "last_seen": now,
                     "last_changed": old.get("last_changed") if old else now}
            if old and old["hash"] == h and old.get("raw_hash") == raw_hash and path.exists():
                entries[rid] = entry
                continue
            change = None
            if not old:
                change = {"action": "added"}
            elif old["hash"] != h:
                old_doc = read_json(acct_dir / old["file"])
                change = {"action": "modified"}
                if old_doc:
                    change["diff"] = json_diff(canonical(old_doc["resource"], run.volatile), canonical(item, run.volatile))
                    archive(run, account, old["file"])
            # Same hash: only volatile fields (or a file deleted by hand) changed; save without reporting.
            if old and old["file"] != rel:
                (acct_dir / old["file"]).unlink(missing_ok=True)
            if change:
                entry["last_changed"] = now
                changes.append({"account_id": account["account_id"], "profile": account["profile"], "region": label,
                                "type": key, "id": rid, "file": rel, **change})
            write_json(path, {
                "_meta": {"account_id": account["account_id"], "account_alias": account.get("alias"),
                          "profile": account["profile"], "region": label, "service": rt["service"],
                          "type": rt["type"], "id": rid, "arn": resource_arn(item), "hash": h,
                          "last_changed": entry["last_changed"]},
                "resource": item,
            })
            entries[rid] = entry
        for rid in set(prev) - set(entries):
            archive(run, account, prev[rid]["file"])
            (acct_dir / prev[rid]["file"]).unlink(missing_ok=True)
            changes.append({"account_id": account["account_id"], "profile": account["profile"], "region": label,
                            "type": key, "id": rid, "file": prev[rid]["file"], "action": "removed"})
        manifest[key] = {"status": "ok", "last_run": now, "count": len(entries), "items": entries}

    for e in errors:
        # A failed list keeps the previous files: nothing is reported as removed.
        manifest.setdefault(e["type"], {"items": {}})
        manifest[e["type"]].update({"status": e["status"], "error": e["code"], "last_error": now})

    write_json(manifest_path, manifest)
    counts = {k: v.get("count", 0) for k, v in manifest.items()}
    return changes, errors, counts


def archive(run, account, rel):
    if not run.settings["keep_history"]:
        return
    src = run.out / account["account_id"] / rel
    if src.exists():
        dst = run.out / "_history" / run.id / account["account_id"] / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def connectivity(aws, commands, region):
    """sts get-caller-identity: proves the profile can reach AWS before anything else runs."""
    try:
        ident = run_named(aws, commands, "identity", region)
        return {"status": "ok", "arn": ident.get("Arn"), "user_id": ident.get("UserId"), "account_id": ident["Account"]}
    except AwsError as e:
        return {"status": "timeout" if e.code == "Timeout" else "error", "error": e.code, "message": e.message[:500]}


def scan_account(run, profile, account_cfg):
    aws = Aws(profile, run.settings, run.gate)
    result = {"profile": profile, "added": 0, "modified": 0, "removed": 0, "errors": [],
              "no_permission": [], "timeout": [], "not_available": 0}
    g = run.settings["global_region"]
    conn = connectivity(aws, run.commands, g)
    conn["checked_at"] = iso(utc_now())
    result["connectivity"] = conn
    if conn["status"] != "ok":
        result["error"] = f"cannot connect: {conn['error']}"
        log.error("%s: cannot connect: %s", profile, conn["message"])
        # Keep the old data of a known account, marked as not reachable this time.
        account_id = account_cfg.get("account_id")
        if account_id and (run.out / account_id / "_account.json").exists():
            acct_dir = run.out / account_id
            write_json(acct_dir / "_account.json", {**read_json(acct_dir / "_account.json", {}), "connectivity": conn})
            build_account_inventory(acct_dir, run.by_key)
        return result
    account_id = conn["account_id"]
    result["account_id"] = account_id
    with run.lock:
        if account_id in run.seen_accounts:
            result["duplicate_of"] = run.seen_accounts[account_id]
            return result
        run.seen_accounts[account_id] = profile
    try:
        alias = run_named(aws, run.commands, "alias", g)
    except AwsError:
        alias = None

    regions = account_cfg.get("regions") or run.settings["regions"]
    if regions == "all" or regions == ["all"]:
        regions = sorted(run_named(aws, run.commands, "regions", g) or [])
    account = {"profile": profile, "account_id": account_id, "alias": alias}

    scopes = []
    global_types = [r for r in run.resources if r["scope"] == "global"]
    regional_types = [r for r in run.resources if r["scope"] == "regional"]
    if global_types:
        scopes.append(("global", g, global_types))
    if regional_types:
        scopes += [(r, r, regional_types) for r in regions]

    counts, changes = {}, []
    with cf.ThreadPoolExecutor(run.settings["region_workers"]) as pool:
        futures = {pool.submit(scan_scope, run, aws, account, *s): s[0] for s in scopes}
        for f in cf.as_completed(futures):
            label = futures[f]
            try:
                c, errs, counts[label] = f.result()
            except Exception as e:
                log.exception("%s %s failed", profile, label)
                result["errors"].append({"scope": label, "code": type(e).__name__, "message": str(e)})
                continue
            changes += c
            for e in errs:
                if e["status"] == "not_available":
                    result["not_available"] += 1
                elif e["status"] in ("no_permission", "timeout"):
                    result[e["status"]].append(f"{label} {e['type']}")
                else:
                    result["errors"].append(e)

    acct_dir = run.out / account_id
    old = read_json(acct_dir / "_account.json", {})
    write_json(acct_dir / "_account.json", {
        **old, "account_id": account_id, "alias": alias, "profile": profile, "connectivity": conn,
        "regions": regions, "last_sync": iso(run.started), "resource_counts": counts,
    })
    if changes:
        with open(acct_dir / "_changelog.jsonl", "a", encoding="utf-8") as f:
            for c in changes:
                f.write(json.dumps({"run": run.id, **{k: v for k, v in c.items() if k != "diff"}}, default=str) + "\n")
    for c in changes:
        result[c["action"]] += 1
    result["resources"] = sum(sum(c.values()) for c in counts.values())
    build_account_inventory(acct_dir, run.by_key)
    with run.lock:
        run.changes += changes
    return result


# ---------------------------------------------------------------------------- merged json

def build_account_inventory(acct_dir, by_key):
    """Merge every resource file of one account into <account_id>/inventory.json.

    {"account": {...}, "global": {service: {type: {id: resource}}},
     "regions": {region: {"vpcs": {vpc_id: {"vpc": {...}, service: {type: {id: resource}}}},
                          "outside_vpc": {service: {type: {id: resource}}},
                          "skipped": [{"type", "status": no_permission|timeout|error, "error", "at"}]}}}
    """
    doc = {"account": read_json(acct_dir / "_account.json", {}), "global": {}, "regions": {}}
    for manifest_path in sorted(acct_dir.glob("*/_manifest.json")):
        label = manifest_path.parent.name
        manifest = read_json(manifest_path, {})
        if label == "global":
            section = doc["global"]
        else:
            section = doc["regions"].setdefault(label, {"vpcs": {}, "outside_vpc": {}})
        skipped = []
        loaded = {}
        for key, entry in sorted(manifest.items()):
            if entry.get("status") in ("no_permission", "timeout", "error"):
                skipped.append({"type": key, "status": entry["status"], "error": entry.get("error"), "at": entry.get("last_error")})
            for rid, item in sorted(entry.get("items", {}).items()):
                resource = (read_json(acct_dir / item["file"]) or {}).get("resource")
                if resource is not None:
                    loaded.setdefault(key, {})[rid] = resource
        if label == "global":
            for key, items in loaded.items():
                service, type_ = key.split("/", 1)
                section.setdefault(service, {})[type_] = items
        else:
            place_in_vpcs(section, loaded, by_key)
        if skipped:
            section["skipped"] = skipped
    write_json(acct_dir / "inventory.json", doc)


def place_in_vpcs(section, loaded, by_key):
    """Put each regional resource under the VPC it belongs to.

    The VPC is the resource's own VpcId, else the VPC of a subnet or security group it
    uses, else its parent's VPC (an EKS node group follows its cluster).
    """
    subnet_vpc = {rid: s.get("VpcId") for rid, s in loaded.get("ec2/subnets", {}).items()}
    sg_vpc = {rid: s.get("VpcId") for rid, s in loaded.get("ec2/security_groups", {}).items()}
    vpc_of = {}

    def find(key, rid, resource):
        if (key, rid) in vpc_of:
            return vpc_of[key, rid]
        vpc = find_vpc_id({k: v for k, v in resource.items() if k != "_network"})
        if not vpc:
            vpcs = {subnet_vpc[s] for s in find_ids(resource, SUBNET_ID, set()) if subnet_vpc.get(s)}
            vpcs |= {sg_vpc[s] for s in find_ids(resource, SG_ID, set()) if sg_vpc.get(s)}
            vpc = vpcs.pop() if len(vpcs) == 1 else None
        parent = by_key.get(key, {}).get("parent")
        if not vpc and parent and resource.get("_parent_id") in loaded.get(parent, {}):
            vpc = find(parent, resource["_parent_id"], loaded[parent][resource["_parent_id"]])
        vpc_of[key, rid] = vpc
        return vpc

    for key, items in loaded.items():
        service, type_ = key.split("/", 1)
        for rid, resource in items.items():
            if key == "ec2/vpcs":
                section["vpcs"].setdefault(rid, {})["vpc"] = resource
                continue
            vpc = find(key, rid, resource)
            target = section["vpcs"].setdefault(vpc, {}) if vpc else section["outside_vpc"]
            target.setdefault(service, {}).setdefault(type_, {})[rid] = resource


# ---------------------------------------------------------------------------- commands

def setup_logging(verbose, log_file=None):
    log.setLevel(logging.DEBUG)
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    log.addHandler(console)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
        log.addHandler(fh)


def cmd_add(args):
    data = load_accounts()
    settings, commands = data["settings"], load_commands()
    gate = threading.BoundedSemaphore(settings["max_parallel_calls"])
    profiles = list(args.profiles)
    if args.file:
        profiles += [l.strip() for l in Path(args.file).read_text().splitlines() if l.strip() and not l.startswith("#")]
    if args.all_profiles:
        c = commands["account"]["profiles"]["command"]
        p = subprocess.run([settings["aws_cli"], *c], capture_output=True, text=True)
        if p.returncode != 0:
            sys.exit(f"{' '.join(c)} failed: {p.stderr.strip()}")
        profiles += p.stdout.split()
    if args.match:
        profiles = [p for p in profiles if re.search(args.match, p)]
    profiles = list(dict.fromkeys(profiles))
    if not profiles:
        sys.exit("No profiles given.")

    def check(profile):
        if args.no_check:
            return profile, {"status": "not checked"}
        return profile, connectivity(Aws(profile, settings, gate), commands, settings["global_region"])

    with cf.ThreadPoolExecutor(settings["account_workers"]) as pool:
        for profile, conn in pool.map(check, profiles):
            entry = data["accounts"].setdefault(profile, {"added": iso(utc_now())})
            entry["enabled"] = True
            if conn.get("account_id"):
                entry["account_id"] = conn["account_id"]
            if args.regions:
                entry["regions"] = "all" if args.regions == ["all"] else args.regions
            note = conn["status"] if conn["status"] in ("ok", "not checked") else f"WARNING {conn['status']}: {conn['message']}"
            print(f"{profile:40} {conn.get('account_id', ''):14} {note}")
    write_json(ACCOUNTS_FILE, data)
    print(f"{len(profiles)} profile(s) saved in {ACCOUNTS_FILE.name}")


def cmd_remove(args):
    data = load_accounts()
    for p in args.profiles:
        if data["accounts"].pop(p, None) is None:
            print(f"{p}: not in the list")
    write_json(ACCOUNTS_FILE, data)


def cmd_list(args):
    data = load_accounts()
    for p, a in sorted(data["accounts"].items()):
        regions = a.get("regions") or "(default)"
        print(f"{p:40} {a.get('account_id', '?'):14} {'enabled ' if a.get('enabled', True) else 'disabled'} {regions}")
    print(f"{len(data['accounts'])} account(s). Default regions: {data['settings']['regions']}")


def cmd_enable(args, enabled):
    data = load_accounts()
    for p in args.profiles:
        if p in data["accounts"]:
            data["accounts"][p]["enabled"] = enabled
        else:
            print(f"{p}: not in the list")
    write_json(ACCOUNTS_FILE, data)


def cmd_config(args):
    data = load_accounts()
    for pair in args.pairs:
        key, _, value = pair.partition("=")
        if key not in DEFAULT_SETTINGS:
            sys.exit(f"Unknown setting {key}. Known: {', '.join(DEFAULT_SETTINGS)}")
        if key == "regions":
            data["settings"][key] = "all" if value == "all" else [r.strip() for r in value.split(",") if r.strip()]
        else:
            try:
                data["settings"][key] = json.loads(value)
            except ValueError:
                data["settings"][key] = value
    if args.pairs:
        write_json(ACCOUNTS_FILE, data)
    print(json.dumps(data["settings"], indent=2))


def cmd_catalog(args):
    for r in load_commands()["resources"]:
        where = r["scope"] + (f" ({r['region']})" if r.get("region") else "")
        parent = f"  per {r['parent']}" if r.get("parent") else ""
        print(f"{r['key']:48} {where:22} {' '.join(r['command'])}{parent}")


def cmd_sync(args):
    data = load_accounts()
    commands = load_commands()
    if args.regions:
        data["settings"]["regions"] = "all" if args.regions == ["all"] else args.regions
    run = Run(data, commands, args)
    setup_logging(args.verbose, run.run_dir / "run.log")
    profiles = args.profiles or [p for p, a in data["accounts"].items() if a.get("enabled", True)]
    missing = [p for p in profiles if p not in data["accounts"]]
    if missing:
        sys.exit(f"Not added yet (use `add`): {', '.join(missing)}")
    if not profiles:
        sys.exit("No accounts. Add some with: aws_inventory.py add <profile> ...")
    if not run.resources:
        sys.exit("No resource types match --services/--types.")

    log.info("Sync %s: %d account(s), %d resource type(s), output %s", run.id, len(profiles), len(run.resources), run.out)
    results = {}
    with cf.ThreadPoolExecutor(run.settings["account_workers"]) as pool:
        futures = {pool.submit(scan_account, run, p, data["accounts"][p]): p for p in profiles}
        for i, f in enumerate(cf.as_completed(futures), 1):
            p = futures[f]
            try:
                r = f.result()
            except Exception as e:
                log.exception("%s failed", p)
                r = {"profile": p, "error": str(e)}
            results[p] = r
            if r.get("error"):
                log.info("[%d/%d] %s: %s", i, len(profiles), p, r["error"])
            elif r.get("duplicate_of"):
                log.info("[%d/%d] %s: same account as profile %s, skipped", i, len(profiles), p, r["duplicate_of"])
            else:
                log.info("[%d/%d] %s (%s): %d resources, +%d ~%d -%d, %d no permission, %d timeout, %d error(s)",
                         i, len(profiles), p, r["account_id"], r["resources"], r["added"], r["modified"], r["removed"],
                         len(r["no_permission"]), len(r["timeout"]), len(r["errors"]))

    data = load_accounts()
    for p, r in results.items():
        if r.get("account_id") and p in data["accounts"]:
            expected = data["accounts"][p].get("account_id")
            if expected and expected != r["account_id"]:
                log.warning("%s: saved as account %s but signs in to %s; saved %s now", p, expected, r["account_id"], r["account_id"])
            data["accounts"][p]["account_id"] = r["account_id"]
            data["accounts"][p]["last_sync"] = iso(run.started)
    write_json(ACCOUNTS_FILE, data)

    totals = {k: sum(r.get(k, 0) for r in results.values()) for k in ("resources", "added", "modified", "removed")}
    summary = {"run": run.id, "started": iso(run.started), "finished": iso(utc_now()), "full": run.full,
               "types": [r["key"] for r in run.resources], "totals": totals, "accounts": results}
    write_json(run.run_dir / "summary.json", summary)
    write_json(run.run_dir / "changes.json", sorted(run.changes, key=lambda c: (c["account_id"], c["region"], c["type"], c["id"])))
    write_json(run.out / "_runs" / "latest.json", {"run": run.id, "path": str(run.run_dir)})
    failed = [p for p, r in results.items() if r.get("error")]
    log.info("Done: %d resources, %d added, %d modified, %d removed, %d account(s) could not connect. Details: %s",
             totals["resources"], totals["added"], totals["modified"], totals["removed"], len(failed), run.run_dir)
    return 1 if failed else 0


def cmd_changes(args):
    data = load_accounts()
    out = (HERE / data["settings"]["output_dir"]).resolve()
    run_id = args.run or (read_json(out / "_runs" / "latest.json") or {}).get("run")
    if not run_id:
        sys.exit("No sync has run yet.")
    changes = read_json(out / "_runs" / run_id / "changes.json", [])
    if args.account:
        changes = [c for c in changes if args.account in (c["account_id"], c["profile"])]
    print(f"Run {run_id}: {len(changes)} change(s)")
    for c in changes:
        print(f"  {c['action']:8} {c['account_id']} {c['region']:15} {c['type']:35} {c['id']}")
        if args.diff:
            for d in c.get("diff", [])[:args.diff]:
                old = json.dumps(d.get("old"), default=str)[:80]
                new = json.dumps(d.get("new"), default=str)[:80]
                print(f"             {d['op']:7} {d['path']}: {old} -> {new}")


def config_profiles():
    """{account number: [profile, ...]} from ~/.aws/config (sso_account_id, role_arn or a number in the name)."""
    parser = configparser.RawConfigParser()
    parser.read(Path(os.environ.get("AWS_CONFIG_FILE", "~/.aws/config")).expanduser())
    found = {}
    for section in parser.sections():
        if section != "default" and not section.startswith("profile "):
            continue  # sso-session and services sections
        name = section[len("profile "):] if section.startswith("profile ") else section
        values = parser[section]
        role = re.search(r":iam::(\d{12}):", values.get("role_arn", ""))
        number = re.search(r"(?<!\d)\d{12}(?!\d)", name)
        account = values.get("sso_account_id") or (role and role.group(1)) or (number and number.group(0))
        if account:
            found.setdefault(account.strip(), []).append(name)
    return found


def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def profile_for(account, data, from_config):
    """The profile to use for an account number: a saved one, else from ~/.aws/config, else ask."""
    saved = sorted(p for p, a in data["accounts"].items() if a.get("account_id") == account)
    if saved:
        return saved[0]
    candidates = from_config.get(account, [])
    if len(candidates) == 1:
        print(f"  {account}: profile {candidates[0]}")
        return candidates[0]
    if candidates:
        for i, p in enumerate(candidates, 1):
            print(f"    {i}. {p}")
        choice = ask(f"  {account} has {len(candidates)} profiles, which one? [1] ")
        if choice.isdigit() and 1 <= int(choice) <= len(candidates):
            return candidates[int(choice) - 1]
        return choice if choice in candidates else candidates[0]
    return ask(f"  No profile in ~/.aws/config for {account}. Profile name (Enter to skip): ") or None


def cmd_interactive(args):
    """Ask which accounts to sync; Enter syncs every account used before."""
    data = load_accounts()
    saved = [p for p, a in data["accounts"].items() if a.get("enabled", True)]
    print(f"{len(saved)} saved account(s) in {ACCOUNTS_FILE.name}.")
    answer = ask("AWS account numbers (or profiles), separated by spaces or commas. Enter = all saved: ")
    if not answer:
        if not saved:
            sys.exit("No saved accounts yet: type at least one account number.")
        profiles = saved
    else:
        from_config = config_profiles()
        profiles = []
        for token in re.split(r"[\s,]+", answer):
            number = token.replace("-", "")
            if re.fullmatch(r"\d{12}", number):
                profile = profile_for(number, data, from_config)
                if not profile:
                    print(f"  {number}: skipped")
                    continue
                entry = data["accounts"].setdefault(profile, {"added": iso(utc_now())})
                entry.setdefault("account_id", number)
            elif token:
                profile = token
                entry = data["accounts"].setdefault(profile, {"added": iso(utc_now())})
            else:
                continue
            entry["enabled"] = True
            entry["last_used"] = iso(utc_now())
            profiles.append(profile)
        if not profiles:
            sys.exit("Nothing to sync.")
        write_json(ACCOUNTS_FILE, data)
        print(f"Saved. Next time press Enter to sync all {len({*saved, *profiles})} account(s).")
    profiles = list(dict.fromkeys(profiles))
    return cmd_sync(argparse.Namespace(profiles=profiles, services=None, types=None, regions=None,
                                       full=False, verbose=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.set_defaults(func=cmd_interactive)
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("add", help="remember profiles (accounts) to scan")
    p.add_argument("profiles", nargs="*")
    p.add_argument("--file", help="text file with one profile per line")
    p.add_argument("--all-profiles", action="store_true", help="every profile from `aws configure list-profiles`")
    p.add_argument("--match", help="keep only profiles matching this regex")
    p.add_argument("--regions", nargs="+", help="regions for these accounts (default: settings.regions); 'all' for every enabled region")
    p.add_argument("--no-check", action="store_true", help="do not call sts get-caller-identity")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("remove", help="forget profiles")
    p.add_argument("profiles", nargs="+")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("enable", help="scan these profiles again")
    p.add_argument("profiles", nargs="+")
    p.set_defaults(func=lambda a: cmd_enable(a, True))

    p = sub.add_parser("disable", help="keep but do not scan these profiles")
    p.add_argument("profiles", nargs="+")
    p.set_defaults(func=lambda a: cmd_enable(a, False))

    p = sub.add_parser("list", help="show remembered accounts")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("config", help="show or change settings: config key=value ...")
    p.add_argument("pairs", nargs="*")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("catalog", help="list the resource types from aws_commands.json")
    p.set_defaults(func=cmd_catalog)

    p = sub.add_parser("sync", help="download new and changed resources")
    p.add_argument("--profiles", nargs="+", help="only these remembered profiles")
    p.add_argument("--services", nargs="+", help="only these services, e.g. ec2 elbv2 route53")
    p.add_argument("--types", nargs="+", help="only these types, e.g. ec2/instances")
    p.add_argument("--regions", nargs="+", help="regions for this run only")
    p.add_argument("--full", action="store_true", help="repeat every detail call, even for unchanged resources")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("changes", help="show what the last (or a given) sync changed")
    p.add_argument("--run", help="run id from inventory/_runs")
    p.add_argument("--account", help="account id or profile")
    p.add_argument("--diff", type=int, nargs="?", const=20, default=0, help="also show up to N changed fields")
    p.set_defaults(func=cmd_changes)

    args = parser.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
