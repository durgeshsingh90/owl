"""Architecture Analyser: imported AWS snapshots, projects and their analysis.

Read-only over AWS: inventories are imported JSON, never fetched from AWS.
"""

import json

from app.architecture import download, engine, store
from app.architecture.models import COLLECTIONS, Project
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

router = APIRouter(prefix="/api/architecture")


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
    try:
        import boto3  # noqa: F401
    except ImportError:
        raise HTTPException(400, "boto3 is not installed: run pip install -r backend/requirements.txt.") from None
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
        "eks-cluster": lambda: [(c, f"EKS {c.version} · {', '.join(cidr.get(s, s) for s in c.subnets)}", c.vpc) for c in inv.eks_clusters.values()],
    }
    if kind not in makers:
        raise HTTPException(400, f"Unknown kind {kind}.")
    items = []
    for item, detail, vpc in makers[kind]():
        item_account = owner(item) or (inv.transit_gateways[item.id].account if kind == "tgw" else "")
        if account and item_account and item_account != account:
            continue
        items.append({"id": item.id, "label": _label(item), "detail": detail, "vpc": vpc, "account": item_account})
    return {"items": sorted(items, key=lambda i: (i["label"].lower(), i["id"]))}


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
    return {
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
        "dns": {
            "hosted_zones": [z.model_dump() for z in inv.hosted_zones.values() if set(z.vpcs) & ids or z.account == account_id],
            "resolver_rules": [r.model_dump() for r in inv.resolver_rules.values() if set(r.vpcs) & ids or r.account == account_id],
        },
    }


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
                return {"kind": kind, "id": id, "object": group.model_dump()}
        raise HTTPException(404, f"Rule group {id} not found.")
    if kind not in COLLECTIONS:
        raise HTTPException(400, f"Unknown kind {kind}.")
    item = getattr(inv, kind).get(id)
    if not item:
        raise HTTPException(404, f"{id} is not in this snapshot.")
    return {"kind": kind, "id": id, "object": item.model_dump()}


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
