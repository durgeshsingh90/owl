"""Architecture Analyser: imported AWS snapshots, projects and their analysis.

Read-only over AWS: inventories are imported JSON, never fetched from AWS.
"""

import json

from app.architecture import download, engine, store
from app.aws import connection as aws_connection
from app.architecture.models import COLLECTIONS, Project
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

router = APIRouter(prefix="/api/architecture")


def _require_aws_cli():
    try:
        aws_connection._aws()
    except FileNotFoundError as error:
        raise HTTPException(400, str(error)) from None


def _missing(error):
    raise HTTPException(404, str(error).strip("'\"")) from None


class ImportFile(BaseModel):
    name: str = Field(max_length=1000)
    content: str


class ImportRequest(BaseModel):
    name: str = Field("", max_length=200)
    files: list[ImportFile] = Field(max_length=20000)


def _parse(files):
    parsed, errors = [], []
    for item in files:
        try:
            parsed.append((item.name, json.loads(item.content)))
        except json.JSONDecodeError as error:
            errors.append(f"{item.name}: not valid JSON ({error.msg} at line {error.lineno}).")
    return parsed, errors


@router.get("/overview")
def overview():
    projects = store.projects()
    return {
        "projects": [
            {"id": p.id, "name": p.name, "description": p.description, "environments": list(p.environments),
             "requirements": len(p.requirements)}
            for p in sorted(projects.values(), key=lambda p: p.name.lower())
        ],
        "snapshots": store.snapshots(),
        "folder": str(store.folder()),
    }


@router.get("/projects/{project_id}")
def get_project(project_id: str):
    try:
        project = store.project(project_id)
    except KeyError as error:
        _missing(error)
    return project.model_dump()


@router.put("/projects/{project_id}")
def put_project(project_id: str, value: Project):
    if value.id != project_id:
        raise HTTPException(400, "The project id in the body must match the URL.")
    return store.save_project(value).model_dump()


@router.get("/projects/{project_id}/aws")
def project_aws(project_id: str):
    """The accounts a refresh downloads, the profile for each, and what is already downloaded."""
    try:
        project = store.project(project_id)
    except KeyError as error:
        _missing(error)
    accounts = download.involved_accounts(project)
    try:
        profiles, config_error = download.profiles_by_account(), ""
    except (OSError, ValueError) as error:
        profiles, config_error = {}, str(error)
    have = download.downloaded([a["account_id"] for a in accounts])
    for account in accounts:
        choices = [p["profile"] for p in profiles.get(account["account_id"], [])]
        account["profile"] = account["profile"] or (choices[0] if choices else "")
        account["profiles"] = choices
        account["downloaded"] = have.get(account["account_id"])
    return {"accounts": accounts, "config": download.config_path(), "config_error": config_error,
            "media": str(download.media_folder()), "refresh": download.status(project_id)}


@router.post("/projects/{project_id}/refresh", status_code=202)
def refresh(project_id: str):
    """Download the project's accounts from AWS (read-only) and snapshot them."""
    try:
        project = store.project(project_id)
    except KeyError as error:
        _missing(error)
    _require_aws_cli()
    return download.start(project)


@router.get("/projects/{project_id}/refresh")
def refresh_status(project_id: str):
    return download.status(project_id)


@router.get("/aws-profiles")
def aws_profiles():
    """Accounts in the AWS config file, to pick the ones a project involves."""
    try:
        found = download.profiles_by_account()
        error = ""
    except (OSError, ValueError) as problem:
        found, error = {}, str(problem)
    accounts = [
        {"account_id": account_id, "profiles": [p["profile"] for p in items],
         "region": next((p.get("region") for p in items if p.get("region")), ""), "name": items[0]["profile"]}
        for account_id, items in sorted(found.items())
    ]
    return {"accounts": accounts, "config": download.config_path(), "error": error}


def _label(item, fallback=""):
    tags = getattr(item, "tags", {}) or {}
    return tags.get("Name") or getattr(item, "name", "") or fallback or item.id


@router.get("/snapshots/{snapshot_id}/resources")
def resources(snapshot_id: str, kind: str, account: str = ""):
    """Resources of one kind (optionally one account) for the flow designer's pickers."""
    try:
        _, inv = store.inventory(snapshot_id)
    except KeyError as error:
        _missing(error)
    vpc_account = {v.id: v.account for v in inv.vpcs.values()}

    def owner(item):
        return getattr(item, "account", "") or vpc_account.get(getattr(item, "vpc", ""), "")

    cidr = {s.id: s.cidr for s in inv.subnets.values()}
    makers = {
        "subnet": lambda: [(s, f"{s.cidr} · {s.az}" + (" · public" if s.public else ""), s.vpc) for s in inv.subnets.values()],
        "vpc": lambda: [(v, ", ".join(v.cidrs) + (f" · {v.role}" if v.role else ""), v.id) for v in inv.vpcs.values()],
        "security-group": lambda: [(g, g.id, g.vpc) for g in inv.security_groups.values()],
        "nat-gateway": lambda: [(n, f"{n.subnet} · {', '.join(n.public_ips)}", n.vpc) for n in inv.nat_gateways.values()],
        "internet-gateway": lambda: [(i, ", ".join(i.vpcs), (i.vpcs or [""])[0]) for i in inv.internet_gateways.values()],
        "tgw": lambda: [(t, t.id, "") for t in inv.transit_gateways.values()],
        "firewall": lambda: [(f, f"{len(f.rule_groups)} rule groups", f.vpc) for f in inv.firewalls.values()],
        "vpc-endpoint": lambda: [(e, f"{e.type} · {e.service}", e.vpc) for e in inv.vpc_endpoints.values()],
        "endpoint-service": lambda: [(e, f"{e.name} · {len(e.connections)} connections", "") for e in inv.endpoint_services.values()],
        "load-balancer": lambda: [(l, f"{l.type} · " + (", ".join(f"{x.protocol}/{x.port}" for x in l.listeners) or "no listeners"), l.vpc) for l in inv.load_balancers.values()],
        "vpc-peering": lambda: [(i, f"{i.requester_vpc} ↔ {i.accepter_vpc} · {i.state}", "") for i in inv.vpc_peerings.values()],
        "vpn-connection": lambda: [(i, f"{i.state} · {sum(t.status == 'UP' for t in i.tunnels)}/{len(i.tunnels)} tunnels up", "") for i in inv.vpn_connections.values()],
        "dx-gateway": lambda: [(i, f"{i.state} · {len(i.associations)} associations", "") for i in inv.dx_gateways.values()],
        "eks-cluster": lambda: [(c, f"EKS {c.version} · {', '.join(cidr.get(s, s) for s in c.subnets)}", c.vpc) for c in inv.eks_clusters.values()],
    }
    if kind not in makers:
        raise HTTPException(400, f"Unknown kind {kind}.")
    items = []
    for item, detail, vpc in makers[kind]():
        item_account = owner(item) or (inv.transit_gateways[item.id].account if kind == "tgw" else "")
        if account and kind == "vpc-peering":  # seen from both of its accounts
            if account not in (item.requester_account, item.accepter_account):
                continue
        elif account and item_account and item_account != account:
            continue
        items.append({"id": item.id, "label": _label(item), "detail": detail, "vpc": vpc, "account": item_account})
    return {"items": sorted(items, key=lambda i: (i["label"].lower(), i["id"]))}


@router.get("/download")
def download_settings():
    """Accounts in the AWS config grouped by name, which are selected, and the running download."""
    return {**download.account_groups(), "aws_config": download.config_path(), "media": str(download.media_folder()),
            "job": download.status("all")}


class Selection(BaseModel):
    account_ids: list[str] = Field(max_length=5000)


@router.put("/download/selection")
def save_selection(value: Selection):
    """Choose the accounts to download; settings saved for accounts that stay are kept."""
    config = download.download_config()
    kept = {a["account_id"]: a for a in config["accounts"]}
    config["accounts"] = [kept.get(i) or {"account_id": i} for i in value.account_ids]
    saved = download.save_download_config(config)
    return {"selected": [a["account_id"] for a in saved["accounts"]]}


@router.get("/download/state/{account_id}")
def resource_state(account_id: str):
    """The account's latest resources with all their properties (state.json)."""
    state = download.state_of(account_id)
    if not state:
        raise HTTPException(404, f"No resource properties downloaded for {account_id} yet.")
    return state


@router.post("/download/denied/{account_id}/reset")
def retry_denied(account_id: str):
    """Forget the account's denied types, so the next test or download tries them again."""
    download.forget_denied(account_id)
    return {"account_id": account_id, "denied_types": []}


@router.post("/download/test/{account_id}")
def test_connectivity(account_id: str):
    """Sign in with `aws sts get-caller-identity`, then check what the profile can read."""
    return download.test_account(account_id)


class StartDownload(BaseModel):
    everything: bool = False


@router.post("/download/start", status_code=202)
def start_download(value: StartDownload | None = None):
    """Download the selected accounts, or all of them (read-only), then snapshot them."""
    _require_aws_cli()
    return download.start_all(bool(value and value.everything))


@router.get("/download/status")
def download_status():
    return download.status("all")


@router.get("/catalog")
def catalog():
    """Services, external systems and rules every project can use."""
    return {
        "services": [s.model_dump() for s in store.services()],
        "external": [e.model_dump() for e in store.externals()],
        "rules": [r.model_dump() for r in store.rules()],
    }


@router.get("/analysis")
def analysis(project: str, environment: str, snapshot: str = ""):
    try:
        value = store.project(project)
        meta, inventory = store.inventory(snapshot)
    except KeyError as error:
        _missing(error)
    if environment not in value.environments:
        raise HTTPException(400, f"{value.name} has no {environment} environment.")
    result = engine.analyse_project(inventory, value, store.services(), store.externals(), store.rules(), environment)
    accounts = sorted({b.account_id for b in value.environments.values()})
    return {
        **result,
        "results": [r.model_dump() for r in result["results"]],
        "findings": [f.model_dump() for f in result["findings"]],
        "snapshot": {"id": meta["id"], "name": meta["name"], "created": meta["created"]},
        "project_detail": value.model_dump(),
        "accounts": [inventory.accounts[a].model_dump() if a in inventory.accounts else {"id": a, "name": "", "missing": True} for a in accounts],
    }


@router.get("/snapshots/{snapshot_id}/accounts")
def accounts(snapshot_id: str):
    try:
        _, inventory = store.inventory(snapshot_id)
    except KeyError as error:
        _missing(error)
    return {"accounts": [a.model_dump() for a in sorted(inventory.accounts.values(), key=lambda a: a.id)]}


@router.get("/snapshots/{snapshot_id}/accounts/{account_id}")
def account(snapshot_id: str, account_id: str):
    """One account's architecture inventory, grouped by VPC."""
    try:
        _, inv = store.inventory(snapshot_id)
    except KeyError as error:
        _missing(error)
    if account_id not in inv.accounts:
        raise HTTPException(404, f"Account {account_id} is not in this snapshot.")
    vpcs = sorted((v for v in inv.vpcs.values() if v.account == account_id), key=lambda v: v.id)
    ids = {v.id for v in vpcs}

    def within(collection, key="vpc"):
        return [i.model_dump() for i in sorted(getattr(inv, collection).values(), key=lambda i: i.id) if getattr(i, key, "") in ids]

    attachments = [a.model_dump() for a in sorted(inv.tgw_attachments.values(), key=lambda a: a.id) if a.resource in ids or a.resource_account == account_id]
    result = {
        "account": inv.accounts[account_id].model_dump(),
        "vpcs": [v.model_dump() for v in vpcs],
        "subnets": within("subnets"), "route_tables": within("route_tables"),
        "tgw_attachments": attachments, "security_groups": within("security_groups"), "nacls": within("nacls"),
        "load_balancers": within("load_balancers"), "nat_gateways": within("nat_gateways"),
        "vpc_endpoints": within("vpc_endpoints"), "firewalls": within("firewalls"),
        "internet_gateways": [i.model_dump() for i in inv.internet_gateways.values() if set(i.vpcs) & ids],
        "transit_gateways": [t.model_dump() for t in inv.transit_gateways.values() if t.account == account_id],
        "tgw_route_tables": [t.model_dump() for t in inv.tgw_route_tables.values()
                             if inv.transit_gateways.get(t.tgw) and inv.transit_gateways[t.tgw].account == account_id],
        "vpc_peerings": [i.model_dump() for i in inv.vpc_peerings.values() if account_id in (i.requester_account, i.accepter_account)],
        "vpn_connections": [i.model_dump() for i in inv.vpn_connections.values() if i.account == account_id],
        "dx_gateways": [i.model_dump() for i in inv.dx_gateways.values() if i.account == account_id],
        "dx_virtual_interfaces": [i.model_dump() for i in inv.dx_virtual_interfaces.values() if i.account == account_id],
        "dns": {
            "hosted_zones": [z.model_dump() for z in inv.hosted_zones.values() if set(z.vpcs) & ids or z.account == account_id],
            "resolver_rules": [r.model_dump() for r in inv.resolver_rules.values() if set(r.vpcs) & ids or r.account == account_id],
        },
    }
    shown = {item["id"] for value in result.values() if isinstance(value, list) for item in value if isinstance(item, dict) and "id" in item}
    shown |= {item["id"] for value in result["dns"].values() for item in value} | set(ids)
    result["dns"]["lookups"] = [l.model_dump() for _, l in sorted(inv.dns_lookups.items()) if l.account == account_id]
    result["timestamps"] = {k: v.model_dump() for k, v in inv.timestamps.items() if k in shown}
    return result


TIMELINE_KINDS = {
    "vpcs": "VPC", "subnets": "Subnet", "route_tables": "Route table", "security_groups": "Security group", "nacls": "NACL",
    "nat_gateways": "NAT gateway", "internet_gateways": "Internet gateway", "vpc_endpoints": "VPC endpoint",
    "endpoint_services": "Endpoint service", "transit_gateways": "Transit gateway", "tgw_attachments": "TGW attachment",
    "tgw_route_tables": "TGW route table", "load_balancers": "Load balancer", "target_groups": "Target group",
    "eks_clusters": "EKS cluster", "vpc_peerings": "VPC peering", "vpn_connections": "VPN connection", "dx_gateways": "Direct Connect gateway", "firewalls": "Network Firewall", "hosted_zones": "Hosted zone",
    "resolver_rules": "Resolver rule", "resolver_endpoints": "Resolver endpoint",
}


@router.get("/snapshots/{snapshot_id}/timeline")
def timeline(snapshot_id: str):
    """Every known creation and change, newest first: one row per event."""
    try:
        meta, inv = store.inventory(snapshot_id)
    except KeyError as error:
        _missing(error)
    where = {}
    for collection, kind in TIMELINE_KINDS.items():
        for item in getattr(inv, collection).values():
            vpc = getattr(item, "vpc", "") or ""
            account = getattr(item, "account", "") or (inv.vpcs[vpc].account if vpc in inv.vpcs else "")
            where[item.id] = (collection, kind, _label(item), account, vpc)
    for firewall in inv.firewalls.values():
        for group in firewall.rule_groups:
            where[group.id] = ("firewall_rule_groups", "Firewall rule group", group.name or group.id, firewall.account, firewall.vpc)
    events = []
    for resource_id, times in inv.timestamps.items():
        if resource_id not in where:
            continue
        collection, kind, label, account, vpc = where[resource_id]
        base = {"id": resource_id, "collection": collection, "kind": kind, "label": label, "account": account,
                "account_name": inv.accounts[account].name if account in inv.accounts else "", "vpc": vpc}
        if times.created:
            events.append({**base, "action": "Created", "time": times.created, "by": times.created_by, "source": times.created_source, "event": ""})
        if times.updated and times.updated != times.created:
            events.append({**base, "action": "Updated", "time": times.updated, "by": times.updated_by, "source": times.updated_source,
                           "event": times.updated_event, "changes": times.changes})
    events.sort(key=lambda e: (e["time"], e["action"] == "Updated", e["id"]), reverse=True)
    unknown = sum(1 for i in where if i not in inv.timestamps)
    return {"snapshot": {"id": meta["id"], "name": meta["name"], "created": meta["created"]}, "events": events,
            "resources": len(where), "without_times": unknown}


@router.get("/snapshots/{snapshot_id}/object")
def evidence_object(snapshot_id: str, kind: str, id: str = Query(max_length=2000)):
    """The normalized object behind a piece of evidence."""
    if kind == "external":
        found = next((e for e in store.externals() if e.id == id), None)
        if not found:
            raise HTTPException(404, f"External system {id} not found.")
        return {"kind": kind, "id": id, "object": found.model_dump()}
    try:
        _, inv = store.inventory(snapshot_id)
    except KeyError as error:
        _missing(error)
    if kind == "firewall_rule_groups":
        for firewall in inv.firewalls.values():
            group = next((g for g in firewall.rule_groups if g.id == id), None)
            if group:
                times = inv.timestamps.get(id)
                return {"kind": kind, "id": id, "object": group.model_dump(), "times": times.model_dump() if times else None}
        raise HTTPException(404, f"Rule group {id} not found.")
    if kind not in COLLECTIONS:
        raise HTTPException(400, f"Unknown kind {kind}.")
    item = getattr(inv, kind).get(id)
    if not item:
        raise HTTPException(404, f"{id} is not in this snapshot.")
    times = inv.timestamps.get(id)
    return {"kind": kind, "id": id, "object": item.model_dump(), "times": times.model_dump() if times else None}


@router.post("/snapshots/validate")
async def validate_snapshot(value: ImportRequest):
    files, errors = _parse(value.files)
    result = await run_in_threadpool(store.validate, files)
    result["errors"] = errors + result["errors"]
    result["files"] = len(value.files)
    return result


@router.post("/snapshots", status_code=201)
async def import_snapshot(value: ImportRequest):
    files, errors = _parse(value.files)
    if errors:
        raise HTTPException(400, "Some files are not valid JSON: " + "; ".join(errors[:5]))
    return await run_in_threadpool(store.import_snapshot, value.name, files)


@router.delete("/snapshots/{snapshot_id}")
def delete_snapshot(snapshot_id: str):
    try:
        store.delete_snapshot(snapshot_id)
    except KeyError as error:
        _missing(error)
    return {"deleted": snapshot_id}
