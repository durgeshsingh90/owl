"""Download a project's AWS configuration with boto3, read-only.

Only the accounts a project names are downloaded (never every account in the AWS
config). Each account's raw API output lands in a media folder shared by all projects:

    media/aws/<account id>/account.json          the account and how it was read
    media/aws/<account id>/<region>/ec2.json     VPCs, subnets, routes, SGs, NACLs, NAT, IGW, endpoints, endpoint services, TGW
    media/aws/<account id>/<region>/elbv2.json         load balancers, listeners, target groups and target health
    media/aws/<account id>/<region>/eks.json
    media/aws/<account id>/<region>/network-firewall.json
    media/aws/<account id>/<region>/route53resolver.json
    media/aws/<account id>/global/route53.json

The files use the AWS CLI's JSON shapes, so the normalizer reads them like any import.
A refresh then stores a snapshot of the project's accounts for analysis. Credentials
come from the AWS config file and profiles that the AWS Accounts app uses.
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.architecture import store
from app.core.database import database_path
from app.core.logging import event

ACCOUNT = re.compile(r"^\d{12}$")
REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
LOGIN_ARN = re.compile(r"arn:aws[\w-]*:(?:iam|sts)::(\d{12}):")
RECORD_LIMIT = 5000  # resource record sets per hosted zone
_jobs = {}
_lock = threading.Lock()


def media_folder():
    return Path(os.environ.get("OWL_MEDIA_DIR", database_path().parent / "media")) / "aws"


def config_path():
    from app.api.aws_accounts import source_state

    return source_state()["path"]


def profiles_by_account():
    """Account id → AWS config profiles that reach it."""
    from app.aws import profiles
    import configparser

    path = config_path()
    found = {}
    for item in profiles.read(path):
        if item["account_id"]:
            found.setdefault(item["account_id"], []).append(item)
    # `aws login` profiles name their account only in login_session.
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.read(path, encoding="utf-8-sig")
    for section in parser.sections():
        name = "default" if section == "default" else section[len("profile "):].strip() if section.startswith("profile ") else ""
        match = LOGIN_ARN.search(parser.get(section, "login_session", fallback=""))
        if name and match and not any(p["profile"] == name for p in found.get(match.group(1), [])):
            found.setdefault(match.group(1), []).append(
                {"profile": name, "account_id": match.group(1), "region": parser.get(section, "region", fallback="")})
    return found


def involved_accounts(project):
    """The accounts a project needs: its environments, its listed accounts and where its workloads run."""
    listed = {a.account_id: a for a in project.aws_accounts}
    ids = {b.account_id for b in project.environments.values()} | set(listed)
    services = {s.id: s for s in store.services()}
    for workload in list(project.applications) + [services[i] for i in project.shared_services + project.central_services if i in services]:
        ids |= {p.account for p in workload.placement.values() if p.account}
    out = []
    for account_id in sorted(ids):
        entry = listed.get(account_id)
        environments = sorted(e for e, b in project.environments.items() if b.account_id == account_id)
        out.append({
            "account_id": account_id,
            "name": entry.name if entry else "",
            "type": entry.type if entry else ("application" if environments else ""),
            "regions": list(entry.regions) if entry else [],
            "profile": entry.profile if entry else "",
            "environments": environments,
        })
    return out


# boto3 calls. Every call is a read: describe, list, get or search.


def _all(client, method, key, **kwargs):
    if client.can_paginate(method):
        items = []
        for page in client.get_paginator(method).paginate(**kwargs):
            items.extend(page.get(key, []))
        return items
    return getattr(client, method)(**kwargs).get(key, [])


class Recorder:
    """Runs calls, keeping going when one is denied, and remembers what failed."""

    def __init__(self):
        self.errors = []
        self.calls = 0

    def __call__(self, label, function, default=None):
        self.calls += 1
        try:
            return function()
        except Exception as error:  # noqa: BLE001 - one denied API must not stop the download
            code = getattr(error, "response", {}).get("Error", {}).get("Code", "") if hasattr(error, "response") else ""
            self.errors.append(f"{label}: {code or type(error).__name__}: {str(error)[:300]}")
            return [] if default is None else default


def _ec2(session, region, run):
    ec2 = session.client("ec2", region_name=region)
    data = {"Region": region}
    data["Vpcs"] = run("ec2 describe-vpcs", lambda: _all(ec2, "describe_vpcs", "Vpcs"))
    attributes = []
    for vpc in data["Vpcs"]:
        item = {"VpcId": vpc["VpcId"]}
        for attribute, key in (("enableDnsSupport", "EnableDnsSupport"), ("enableDnsHostnames", "EnableDnsHostnames")):
            value = run(f"ec2 describe-vpc-attribute {vpc['VpcId']}",
                        lambda a=attribute: ec2.describe_vpc_attribute(VpcId=vpc["VpcId"], Attribute=a), default={})
            if key in value:
                item[key] = value[key]
        attributes.append(item)
    data["VpcAttributes"] = attributes
    services = run("ec2 describe-vpc-endpoint-service-configurations",
                   lambda: _all(ec2, "describe_vpc_endpoint_service_configurations", "ServiceConfigurations"))
    for service in services:
        service["AllowedPrincipals"] = run(f"ec2 describe-vpc-endpoint-service-permissions {service['ServiceId']}", lambda i=service["ServiceId"]: _all(
            ec2, "describe_vpc_endpoint_service_permissions", "AllowedPrincipals", ServiceId=i), default=None)
    data["ServiceConfigurations"] = services
    data["VpcEndpointConnections"] = run("ec2 describe-vpc-endpoint-connections",
                                         lambda: _all(ec2, "describe_vpc_endpoint_connections", "VpcEndpointConnections")) if services else []
    for method, key in (
        ("describe_subnets", "Subnets"), ("describe_route_tables", "RouteTables"),
        ("describe_security_groups", "SecurityGroups"), ("describe_network_acls", "NetworkAcls"),
        ("describe_nat_gateways", "NatGateways"), ("describe_internet_gateways", "InternetGateways"),
        ("describe_vpc_endpoints", "VpcEndpoints"), ("describe_transit_gateways", "TransitGateways"),
        ("describe_transit_gateway_attachments", "TransitGatewayAttachments"),
        ("describe_transit_gateway_vpc_attachments", "TransitGatewayVpcAttachments"),
        ("describe_transit_gateway_route_tables", "TransitGatewayRouteTables"),
    ):
        data[key] = run(f"ec2 {method.replace('_', '-')}", lambda m=method, k=key: _all(ec2, m, k))
    for table in data["TransitGatewayRouteTables"]:
        table_id = table["TransitGatewayRouteTableId"]
        table["Routes"] = run(f"ec2 search-transit-gateway-routes {table_id}", lambda: ec2.search_transit_gateway_routes(
            TransitGatewayRouteTableId=table_id, Filters=[{"Name": "state", "Values": ["active", "blackhole"]}],
            MaxResults=1000).get("Routes", []))
        table["Associations"] = run(f"ec2 get-transit-gateway-route-table-associations {table_id}", lambda: _all(
            ec2, "get_transit_gateway_route_table_associations", "Associations", TransitGatewayRouteTableId=table_id))
        table["Propagations"] = run(f"ec2 get-transit-gateway-route-table-propagations {table_id}", lambda: _all(
            ec2, "get_transit_gateway_route_table_propagations", "TransitGatewayRouteTablePropagations", TransitGatewayRouteTableId=table_id))
    return data


def _elbv2(session, region, run):
    elb = session.client("elbv2", region_name=region)
    balancers = run("elbv2 describe-load-balancers", lambda: _all(elb, "describe_load_balancers", "LoadBalancers"))
    listeners = []
    for lb in balancers:
        listeners += run(f"elbv2 describe-listeners {lb['LoadBalancerName']}",
                         lambda arn=lb["LoadBalancerArn"]: _all(elb, "describe_listeners", "Listeners", LoadBalancerArn=arn))
    groups = run("elbv2 describe-target-groups", lambda: _all(elb, "describe_target_groups", "TargetGroups"))
    for group in groups:
        group["TargetHealthDescriptions"] = run(f"elbv2 describe-target-health {group['TargetGroupName']}", lambda arn=group["TargetGroupArn"]: elb.describe_target_health(
            TargetGroupArn=arn).get("TargetHealthDescriptions", []), default=None)
    return {"Region": region, "LoadBalancers": balancers, "Listeners": listeners, "TargetGroups": groups}


def _network_firewall(session, region, run):
    nfw = session.client("network-firewall", region_name=region)
    firewalls, policies, groups = [], {}, {}
    for item in run("network-firewall list-firewalls", lambda: _all(nfw, "list_firewalls", "Firewalls")):
        described = run(f"network-firewall describe-firewall {item['FirewallName']}",
                        lambda arn=item["FirewallArn"]: nfw.describe_firewall(FirewallArn=arn), default={})
        if not described:
            continue
        firewalls.append({"Firewall": described.get("Firewall", {}), "FirewallStatus": described.get("FirewallStatus", {})})
        arn = described.get("Firewall", {}).get("FirewallPolicyArn")
        if arn and arn not in policies:
            policy = run(f"network-firewall describe-firewall-policy {arn.split('/')[-1]}",
                         lambda: nfw.describe_firewall_policy(FirewallPolicyArn=arn), default={})
            if policy:
                policies[arn] = {"FirewallPolicyResponse": policy.get("FirewallPolicyResponse", {}), "FirewallPolicy": policy.get("FirewallPolicy", {})}
                for reference in policy.get("FirewallPolicy", {}).get("StatefulRuleGroupReferences", []) or []:
                    group_arn = reference["ResourceArn"]
                    if group_arn not in groups:
                        group = run(f"network-firewall describe-rule-group {group_arn.split('/')[-1]}",
                                    lambda: nfw.describe_rule_group(RuleGroupArn=group_arn), default={})
                        if group:
                            groups[group_arn] = {"RuleGroupResponse": group.get("RuleGroupResponse", {}), "RuleGroup": group.get("RuleGroup", {})}
    return {"Region": region, "Firewalls": firewalls, "FirewallPolicies": list(policies.values()), "RuleGroups": list(groups.values())}


def _resolver(session, region, run):
    resolver = session.client("route53resolver", region_name=region)
    endpoints = run("route53resolver list-resolver-endpoints", lambda: _all(resolver, "list_resolver_endpoints", "ResolverEndpoints"))
    for endpoint in endpoints:
        endpoint["IpAddresses"] = run(f"route53resolver list-resolver-endpoint-ip-addresses {endpoint['Id']}",
                                      lambda e=endpoint["Id"]: _all(resolver, "list_resolver_endpoint_ip_addresses", "IpAddresses", ResolverEndpointId=e))
    return {
        "Region": region,
        "ResolverRules": run("route53resolver list-resolver-rules", lambda: _all(resolver, "list_resolver_rules", "ResolverRules")),
        "ResolverRuleAssociations": run("route53resolver list-resolver-rule-associations",
                                        lambda: _all(resolver, "list_resolver_rule_associations", "ResolverRuleAssociations")),
        "ResolverEndpoints": endpoints,
    }


def _eks(session, region, run):
    eks = session.client("eks", region_name=region)
    clusters = []
    for name in run("eks list-clusters", lambda: _all(eks, "list_clusters", "clusters")):
        cluster = run(f"eks describe-cluster {name}", lambda n=name: eks.describe_cluster(name=n)["cluster"], default={})
        if cluster:
            clusters.append({k: cluster.get(k) for k in ("name", "arn", "version", "status", "resourcesVpcConfig", "createdAt")})
    return {"Region": region, "Clusters": clusters}


CONFIG_TYPES = [
    "AWS::EC2::VPC", "AWS::EC2::Subnet", "AWS::EC2::RouteTable", "AWS::EC2::SecurityGroup", "AWS::EC2::NetworkAcl",
    "AWS::EC2::NatGateway", "AWS::EC2::InternetGateway", "AWS::EC2::VPCEndpoint", "AWS::EC2::VPCEndpointService",
    "AWS::EC2::TransitGateway", "AWS::EC2::TransitGatewayAttachment", "AWS::EC2::TransitGatewayRouteTable",
    "AWS::ElasticLoadBalancingV2::LoadBalancer", "AWS::EKS::Cluster", "AWS::NetworkFirewall::Firewall",
    "AWS::NetworkFirewall::FirewallPolicy", "AWS::NetworkFirewall::RuleGroup", "AWS::Route53Resolver::ResolverRule",
    "AWS::Route53Resolver::ResolverEndpoint", "AWS::Route53::HostedZone",
]
# Resource ids and ARNs inside CloudTrail events.
RESOURCE_ID = re.compile(
    r"\b(?:vpc|subnet|rtb|sg|acl|nat|igw|vpce|vpce-svc|tgw|tgw-attach|tgw-rtb|eipalloc|rslvr-rr|rslvr-out|rslvr-in)-[0-9a-f]{8,17}\b"
    r"|arn:aws[\w-]*:(?:elasticloadbalancing|eks|network-firewall):[^\s\"',]+")
# Which ids a create event creates (its response also names the VPC, subnet ... it was created in).
CREATED_BY = {
    "CreateVpc": ("vpc-",), "CreateDefaultVpc": ("vpc-",), "CreateSubnet": ("subnet-",), "CreateDefaultSubnet": ("subnet-",),
    "CreateSecurityGroup": ("sg-",), "CreateRouteTable": ("rtb-",), "CreateNetworkAcl": ("acl-",), "CreateNatGateway": ("nat-",),
    "CreateInternetGateway": ("igw-",), "CreateVpcEndpoint": ("vpce-",), "CreateVpcEndpointServiceConfiguration": ("vpce-svc-",),
    "CreateTransitGateway": ("tgw-",), "CreateTransitGatewayVpcAttachment": ("tgw-attach-",),
    "CreateTransitGatewayPeeringAttachment": ("tgw-attach-",), "CreateTransitGatewayRouteTable": ("tgw-rtb-",),
    "CreateLoadBalancer": (":loadbalancer/",), "CreateTargetGroup": (":targetgroup/",), "CreateCluster": (":cluster/",),
    "CreateFirewall": (":firewall/",), "CreateFirewallPolicy": (":firewall-policy/",), "CreateRuleGroup": ("rulegroup/",),
    "CreateResolverRule": ("rslvr-rr-",), "CreateResolverEndpoint": ("rslvr-out-", "rslvr-in-"),
}
# Events that mention network resources without changing them.
NOT_A_CHANGE = {
    "RunInstances", "TerminateInstances", "StartInstances", "StopInstances", "RebootInstances", "CreateNetworkInterface",
    "DeleteNetworkInterface", "AttachNetworkInterface", "DetachNetworkInterface", "AssociateAddress", "DisassociateAddress",
    "CreateVolume", "AttachVolume", "DetachVolume", "ModifyInstanceAttribute", "CreateLaunchTemplate", "CreateLaunchTemplateVersion",
}


def _config_times(session, region, run):
    """Creation and last-change times from AWS Config's advanced query (needs a recorder)."""
    config = session.client("config", region_name=region)
    expression = ("SELECT resourceId, resourceType, resourceName, resourceCreationTime, configurationItemCaptureTime "
                  "WHERE resourceType IN (" + ", ".join(f"'{t}'" for t in CONFIG_TYPES) + ")")

    def query():
        items, token = [], None
        while True:
            page = config.select_resource_config(Expression=expression, Limit=100, **({"NextToken": token} if token else {}))
            items.extend(json.loads(row) for row in page.get("Results", []))
            token = page.get("NextToken")
            if not token:
                return items

    return run("config select-resource-config", query)


def _cloudtrail(session, region, run, days, limit):
    """Who created and last changed each resource, from CloudTrail write events in the window."""
    trail = session.client("cloudtrail", region_name=region)
    start = datetime.now(timezone.utc) - timedelta(days=days)
    history, seen = {}, 0

    def scan():
        nonlocal seen
        pages = trail.get_paginator("lookup_events").paginate(
            LookupAttributes=[{"AttributeKey": "ReadOnly", "AttributeValue": "false"}], StartTime=start,
            PaginationConfig={"MaxItems": limit, "PageSize": 50})
        for page in pages:
            for event in page.get("Events", []):
                seen += 1
                name, when = event.get("EventName", ""), event.get("EventTime")
                who = event.get("Username", "")
                try:
                    detail = json.loads(event.get("CloudTrailEvent") or "{}")
                except ValueError:
                    detail = {}
                if detail.get("errorCode"):
                    continue
                kinds = CREATED_BY.get(name, ())
                created = {i for i in RESOURCE_ID.findall(json.dumps(detail.get("responseElements") or {}))
                           if any(i.startswith(k) if k.endswith("-") else k in i for k in kinds)}
                # vpce- also prefixes vpce-svc-: keep endpoint and service creations apart.
                if name == "CreateVpcEndpoint":
                    created = {i for i in created if not i.startswith("vpce-svc-")}
                touched = set()
                if name not in NOT_A_CHANGE:
                    touched = {r.get("ResourceName", "") for r in event.get("Resources", []) or []}
                    touched |= set(RESOURCE_ID.findall(json.dumps(detail.get("requestParameters") or {})))
                stamp = when.isoformat() if hasattr(when, "isoformat") else str(when)
                for resource in created:
                    item = history.setdefault(resource, {"changes": 0})
                    if not item.get("created") or stamp < item["created"]["time"]:
                        item["created"] = {"time": stamp, "by": who, "event": name}
                for resource in (touched - created) - {""}:
                    item = history.setdefault(resource, {"changes": 0})
                    item["changes"] += 1
                    if not item.get("updated") or stamp > item["updated"]["time"]:
                        item["updated"] = {"time": stamp, "by": who, "event": name}
        return True

    run(f"cloudtrail lookup-events ({days} days)", scan, default=False)
    return history, {"days": days, "events": seen, "limit": limit, "truncated": seen >= limit}


def _route53(session, run):
    route53 = session.client("route53")
    zones = run("route53 list-hosted-zones", lambda: _all(route53, "list_hosted_zones", "HostedZones"))
    for zone in zones:
        zone_id = zone["Id"].split("/")[-1]
        if zone.get("Config", {}).get("PrivateZone"):
            zone["VPCs"] = run(f"route53 get-hosted-zone {zone_id}", lambda: route53.get_hosted_zone(Id=zone_id).get("VPCs", []))
        records = []

        def fetch():
            for page in route53.get_paginator("list_resource_record_sets").paginate(HostedZoneId=zone_id):
                records.extend(page["ResourceRecordSets"])
                if len(records) >= RECORD_LIMIT:
                    break
            return records[:RECORD_LIMIT]

        zone["ResourceRecordSets"] = run(f"route53 list-resource-record-sets {zone_id}", fetch)
    return {"HostedZones": zones}


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=1, default=str))
    temporary.replace(path)


def download_account(account, progress=lambda text: None, history=None):
    """Download one account into the media folder. Returns its manifest."""
    import boto3

    account_id = account["account_id"]
    if not ACCOUNT.match(account_id):
        raise ValueError(f"{account_id!r} is not a 12-digit AWS account id.")
    choices = profiles_by_account().get(account_id, [])
    profile = account.get("profile") or (choices[0]["profile"] if choices else "")
    if not profile:
        raise ValueError(f"No profile for account {account_id} in {config_path()}. Add one in the AWS config file.")
    session = boto3.Session(profile_name=profile)
    run = Recorder()
    identity = run("sts get-caller-identity", lambda: session.client("sts", region_name=session.region_name or "us-east-1").get_caller_identity(), default={})
    if not identity:
        raise PermissionError(f"Profile {profile} can't sign in: {run.errors[-1].split(': ', 1)[-1]} Log in from the AWS Accounts app, then refresh.")
    if identity.get("Account") != account_id:
        raise PermissionError(f"Profile {profile} signs in to {identity.get('Account')}, not {account_id}.")
    regions = [r for r in account.get("regions") or [] if REGION.match(r)] or [session.region_name or next(
        (c["region"] for c in choices if c.get("region")), "us-east-1")]
    folder = media_folder() / account_id
    for region in regions:
        progress(f"{account_id} {region}: VPC, routing and transit gateway")
        _write(folder / region / "ec2.json", _ec2(session, region, run))
        progress(f"{account_id} {region}: load balancers")
        _write(folder / region / "elbv2.json", _elbv2(session, region, run))
        progress(f"{account_id} {region}: EKS clusters")
        _write(folder / region / "eks.json", _eks(session, region, run))
        progress(f"{account_id} {region}: network firewall")
        _write(folder / region / "network-firewall.json", _network_firewall(session, region, run))
        progress(f"{account_id} {region}: Route 53 Resolver")
        _write(folder / region / "route53resolver.json", _resolver(session, region, run))
        history = history or {}
        if history.get("config", True):
            progress(f"{account_id} {region}: creation and change times from AWS Config")
            _write(folder / region / "config.json", {"Region": region, "ConfigResources": _config_times(session, region, run)})
        if history.get("cloudtrail", True):
            progress(f"{account_id} {region}: who changed what, from CloudTrail")
            changes, window = _cloudtrail(session, region, run, int(history.get("days", 90)), int(history.get("max_events", 5000)))
            _write(folder / region / "cloudtrail.json", {"Region": region, "ResourceHistory": changes, "HistoryWindow": window})
    progress(f"{account_id}: Route 53 hosted zones")
    _write(folder / "global" / "route53.json", _route53(session, run))
    previous = {}
    if (folder / "account.json").exists():
        previous = json.loads((folder / "account.json").read_text()).get("Account", {})
    manifest = {
        "Account": {
            "Id": account_id, "Name": account.get("name") or previous.get("Name") or profile,
            "Type": account.get("type") or previous.get("Type") or "application",
            "Region": regions[0], "Environments": account.get("environments") or previous.get("Environments", []),
        },
        "Download": {
            "profile": profile, "regions": regions, "caller": identity.get("Arn", ""), "calls": run.calls,
            "errors": run.errors, "downloaded_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    _write(folder / "account.json", manifest)
    return manifest


def snapshot_files(account_ids):
    """Every downloaded file for these accounts, named <account>/<region>/<file> for the normalizer."""
    root = media_folder()
    files = []
    for account_id in account_ids:
        for path in sorted((root / account_id).rglob("*.json")):
            data = json.loads(path.read_text())
            if path.name == "account.json":
                data = {"Account": data["Account"], "Region": data["Account"].get("Region", "")}
            files.append((str(path.relative_to(root)), data))
    return files


def downloaded(account_ids):
    """What the media folder already holds for these accounts."""
    out = {}
    for account_id in account_ids:
        path = media_folder() / account_id / "account.json"
        if path.exists():
            data = json.loads(path.read_text())
            out[account_id] = {**data.get("Download", {}), "name": data.get("Account", {}).get("Name", "")}
    return out


# Background refreshes, one per project


def status(project_id):
    with _lock:
        job = _jobs.get(project_id)
        return dict(job) if job else {"state": "idle"}


def start(project):
    return start_job(project.id, project.name, involved_accounts(project))


def start_job(key, name, accounts, history=None):
    """Download the accounts in the background, then snapshot them. One job per key."""
    with _lock:
        job = _jobs.get(key)
        if job and job["state"] == "running":
            raise ValueError("A download is already running.")
        if not accounts:
            raise ValueError("No accounts to download.")
        _jobs[key] = {
            "state": "running", "started": time.time(), "finished": None, "message": "Starting…",
            "accounts": [{"account_id": a["account_id"], "state": "queued", "error": "", "warnings": 0} for a in accounts],
            "snapshot": None, "error": "",
        }
    threading.Thread(target=_run, args=(key, name, accounts, history), name=f"architecture-download-{key}", daemon=True).start()
    return status(key)


def _update(project_id, **fields):
    with _lock:
        _jobs[project_id].update(fields)


def _account_state(project_id, account_id, **fields):
    with _lock:
        for item in _jobs[project_id]["accounts"]:
            if item["account_id"] == account_id:
                item.update(fields)


def _run(key, name, accounts, history=None):
    event("architecture.refresh.started", key=key, accounts=len(accounts))
    ok = 0
    if history is None:
        history = download_config().get("history", {})
    for account in accounts:
        _account_state(key, account["account_id"], state="running")
        try:
            manifest = download_account(account, progress=lambda text: _update(key, message=text), history=history)
            ok += 1
            _account_state(key, account["account_id"], state="done", warnings=len(manifest["Download"]["errors"]),
                           error="; ".join(manifest["Download"]["errors"][:3]))
        except Exception as error:  # noqa: BLE001 - report per account and continue
            _account_state(key, account["account_id"], state="failed", error=str(error)[:500])
    files = snapshot_files([a["account_id"] for a in accounts])
    if not files:
        _update(key, state="failed", finished=time.time(), message="", error="Nothing was downloaded, so no snapshot was made.")
        event("architecture.refresh.failed", key=key)
        return
    try:
        _update(key, message="Building the snapshot")
        meta = store.import_snapshot(f"{name} · AWS download", files)
        _update(key, state="done" if ok == len(accounts) else "partial", finished=time.time(), message="", snapshot=meta["id"])
        event("architecture.refresh.completed", key=key, accounts=ok)
    except ValueError as error:
        _update(key, state="failed", finished=time.time(), message="", error=str(error)[:500])


# The central download list: every account to download, shared by all projects.

DEFAULT_CONFIG = {"accounts": [], "regions": [], "history": {"config": True, "cloudtrail": True, "days": 90, "max_events": 5000}}


def config_file():
    return store.ensure() / "download.json"


def download_config():
    path = config_file()
    value = json.loads(path.read_text()) if path.exists() else {}
    return {**DEFAULT_CONFIG, **value, "history": {**DEFAULT_CONFIG["history"], **(value.get("history") or {})}}


def save_download_config(value):
    accounts, seen = [], set()
    for item in value.get("accounts", []):
        account_id = str(item.get("account_id", "")).strip()
        if not ACCOUNT.match(account_id):
            raise ValueError(f"{account_id!r} is not a 12-digit AWS account id.")
        if account_id in seen:
            continue
        seen.add(account_id)
        regions = [r for r in item.get("regions", []) if r]
        bad = [r for r in regions if not REGION.match(r)]
        if bad:
            raise ValueError(f"Unknown region {bad[0]!r} for {account_id}.")
        accounts.append({"account_id": account_id, "name": str(item.get("name", ""))[:200], "type": str(item.get("type", "") or "application"),
                         "regions": regions, "profile": str(item.get("profile", ""))[:200]})
    regions = [r for r in value.get("regions", []) if r]
    if any(not REGION.match(r) for r in regions):
        raise ValueError("Default regions must look like eu-west-2.")
    history = value.get("history") or {}
    days = int(history.get("days", 90))
    if not 1 <= days <= 90:
        raise ValueError("CloudTrail history covers 1 to 90 days.")
    saved = {"accounts": accounts, "regions": regions, "history": {
        "config": bool(history.get("config", True)), "cloudtrail": bool(history.get("cloudtrail", True)), "days": days,
        "max_events": max(100, min(int(history.get("max_events", 5000)), 100000))}}
    _write(config_file(), saved)
    return saved


def start_all():
    config = download_config()
    accounts = [{**a, "regions": a["regions"] or config["regions"], "environments": []} for a in config["accounts"]]
    return start_job("all", "All accounts", accounts, history=config["history"])
