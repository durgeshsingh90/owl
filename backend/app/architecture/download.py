"""Download a project's AWS configuration with the `aws` command, read-only.

Only the accounts a project names are downloaded (never every account in the AWS
config). Before anything is read, check_access signs in and tries one item of each resource
type; what is denied is never called. Output lands in a media folder shared by all projects,
named from the mc-<name>-<env> profile:

    media/aws/<account id>-<name>/<env>/account.json          the account, how it was read, what was denied
    media/aws/<account id>-<name>/<env>/<region>/ec2.json     VPCs, subnets, routes, SGs, NACLs, NAT, IGW, endpoints, endpoint services, TGW
    media/aws/<account id>-<name>/<env>/<region>/elbv2.json   load balancers, listeners, target groups and target health
    media/aws/<account id>-<name>/<env>/<region>/eks.json
    media/aws/<account id>-<name>/<env>/<region>/network-firewall.json
    media/aws/<account id>-<name>/<env>/<region>/route53resolver.json
    media/aws/<account id>-<name>/<env>/global/route53.json   hosted zones; records for private zones only

The files use the AWS CLI's JSON shapes, so the normalizer reads them like any import.
A refresh then stores a snapshot of the project's accounts for analysis. Credentials
come from the AWS config file and profiles that the AWS Accounts app uses.
"""

import ipaddress
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from app.architecture import store
from app.architecture.normalize import iso
from app.aws import connection
from app.core.database import connection as database_connection
from app.core.database import database_path
from app.core.logging import event

ACCOUNT = re.compile(r"^\d{12}$")
REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
LOGIN_ARN = re.compile(r"arn:aws[\w-]*:(?:iam|sts)::(\d{12}):")
RECORD_LIMIT = 5000  # resource record sets per hosted zone
CALL_TIMEOUT = 600  # seconds for one `aws` command; CloudTrail reads slowly
ACCOUNTS_AT_ONCE = 4
PROFILE_NAME = re.compile(r"mc-(.+)-([^-]+)", re.IGNORECASE)
TOP_ACCOUNTS = 20
PROBES_AT_ONCE = 6
PROBE_TIMEOUT = 60  # seconds for one access check call
LOOKUPS_AT_ONCE = 16
LOOKUP_SECONDS = 5  # nslookup's own wait per try
LOOKUP_LIMIT = 20  # seconds before an nslookup process is stopped
LOOKUP_NAMES_MAX = 5000  # per account
LOOKUP_TRIAL = 32  # if this many names in a row time out, the DNS server isn't reachable: stop
DENIED = re.compile(r"AccessDenied|UnauthorizedOperation|Unauthorized|not authorized|Forbidden|AuthorizationError", re.IGNORECASE)
# The check before any download: one item of each resource type, by the key its output uses.
# Calls that need a resource id (listeners, rule groups, ...) are checked on first use instead.
PROBES = [
    ("Vpcs", "ec2", "describe-vpcs"), ("Subnets", "ec2", "describe-subnets"), ("RouteTables", "ec2", "describe-route-tables"),
    ("SecurityGroups", "ec2", "describe-security-groups"), ("NetworkAcls", "ec2", "describe-network-acls"),
    ("NatGateways", "ec2", "describe-nat-gateways"), ("InternetGateways", "ec2", "describe-internet-gateways"),
    ("VpcEndpoints", "ec2", "describe-vpc-endpoints"), ("ServiceConfigurations", "ec2", "describe-vpc-endpoint-service-configurations"),
    ("VpcEndpointConnections", "ec2", "describe-vpc-endpoint-connections"), ("TransitGateways", "ec2", "describe-transit-gateways"),
    ("TransitGatewayAttachments", "ec2", "describe-transit-gateway-attachments"),
    ("TransitGatewayVpcAttachments", "ec2", "describe-transit-gateway-vpc-attachments"),
    ("TransitGatewayRouteTables", "ec2", "describe-transit-gateway-route-tables"),
    ("LoadBalancers", "elbv2", "describe-load-balancers"), ("TargetGroups", "elbv2", "describe-target-groups"),
    ("clusters", "eks", "list-clusters"), ("Firewalls", "network-firewall", "list-firewalls"),
    ("ResolverRules", "route53resolver", "list-resolver-rules"), ("ResolverRuleAssociations", "route53resolver", "list-resolver-rule-associations"),
    ("ResolverEndpoints", "route53resolver", "list-resolver-endpoints"), ("HostedZones", "route53", "list-hosted-zones"),
]
# More of what connects an EKS application to anything: other VPCs, accounts, on-premises and
# other clouds, ingress, and the compute behind it. Saved as properties in state.json.
# (file, name, service, operation, output key, resource type, id field, created-time field)
EXTRAS = [
    ("network", "VpcPeeringConnections", "ec2", "describe-vpc-peering-connections", "VpcPeeringConnections", "aws_vpc_peering_connection", "VpcPeeringConnectionId", ""),
    ("network", "VpnConnections", "ec2", "describe-vpn-connections", "VpnConnections", "aws_vpn_connection", "VpnConnectionId", ""),
    ("network", "VpnGateways", "ec2", "describe-vpn-gateways", "VpnGateways", "aws_vpn_gateway", "VpnGatewayId", ""),
    ("network", "CustomerGateways", "ec2", "describe-customer-gateways", "CustomerGateways", "aws_customer_gateway", "CustomerGatewayId", ""),
    ("network", "TransitGatewayPeeringAttachments", "ec2", "describe-transit-gateway-peering-attachments", "TransitGatewayPeeringAttachments", "aws_ec2_transit_gateway_peering_attachment", "TransitGatewayAttachmentId", "CreationTime"),
    ("network", "TransitGatewayConnects", "ec2", "describe-transit-gateway-connects", "TransitGatewayConnects", "aws_ec2_transit_gateway_connect", "TransitGatewayAttachmentId", "CreationTime"),
    ("network", "DxConnections", "directconnect", "describe-connections", "connections", "aws_dx_connection", "connectionId", ""),
    ("network", "DxVirtualInterfaces", "directconnect", "describe-virtual-interfaces", "virtualInterfaces", "aws_dx_virtual_interface", "virtualInterfaceId", ""),
    ("network", "DxGateways", "directconnect", "describe-direct-connect-gateways", "directConnectGateways", "aws_dx_gateway", "directConnectGatewayId", ""),
    ("network", "ManagedPrefixLists", "ec2", "describe-managed-prefix-lists", "PrefixLists", "aws_ec2_managed_prefix_list", "PrefixListId", ""),
    ("network", "EgressOnlyInternetGateways", "ec2", "describe-egress-only-internet-gateways", "EgressOnlyInternetGateways", "aws_egress_only_internet_gateway", "EgressOnlyInternetGatewayId", ""),
    ("network", "ElasticIps", "ec2", "describe-addresses", "Addresses", "aws_eip", "AllocationId", ""),
    ("network", "DhcpOptions", "ec2", "describe-dhcp-options", "DhcpOptions", "aws_vpc_dhcp_options", "DhcpOptionsId", ""),
    ("network", "NetworkInterfaces", "ec2", "describe-network-interfaces", "NetworkInterfaces", "aws_network_interface", "NetworkInterfaceId", ""),
    ("ingress", "ClassicLoadBalancers", "elb", "describe-load-balancers", "LoadBalancerDescriptions", "aws_elb", "LoadBalancerName", "CreatedTime"),
    ("ingress", "RestApis", "apigateway", "get-rest-apis", "items", "aws_api_gateway_rest_api", "id", "createdDate"),
    ("ingress", "RestVpcLinks", "apigateway", "get-vpc-links", "items", "aws_api_gateway_vpc_link", "id", ""),
    ("ingress", "HttpApis", "apigatewayv2", "get-apis", "Items", "aws_apigatewayv2_api", "ApiId", "CreatedDate"),
    ("ingress", "HttpVpcLinks", "apigatewayv2", "get-vpc-links", "Items", "aws_apigatewayv2_vpc_link", "VpcLinkId", "CreatedDate"),
    ("compute", "Reservations", "ec2", "describe-instances", "Reservations", "", "", ""),
    ("compute", "AutoScalingGroups", "autoscaling", "describe-auto-scaling-groups", "AutoScalingGroups", "aws_autoscaling_group", "AutoScalingGroupName", "CreatedTime"),
    ("compute", "LaunchTemplates", "ec2", "describe-launch-templates", "LaunchTemplates", "aws_launch_template", "LaunchTemplateId", "CreateTime"),
]
# These commands don't paginate, so they don't accept --max-items.
NOT_PAGED = {"VpnConnections", "VpnGateways", "CustomerGateways", "ElasticIps", "DxConnections", "DxVirtualInterfaces", "HttpVpcLinks"}
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


# Every AWS call is the `aws` command in a subprocess, and only reads: describe, list, get
# or search. The CLI follows pagination itself and prints the shapes the normalizer reads.


class CliError(Exception):
    pass


class AwsCli:
    def __init__(self, profile, region="", timeout=CALL_TIMEOUT):
        self.profile = profile
        self.region = region
        self.timeout = timeout

    def call(self, service, operation, *arguments, region=None):
        command = [connection._aws(), service, operation, *arguments, "--profile", self.profile, "--output", "json"]
        if region or self.region:
            command += ["--region", region or self.region]
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=self.timeout, **connection._options())
        except subprocess.TimeoutExpired:
            raise CliError(f"Timed out: no response from AWS within {self.timeout} seconds.") from None
        if result.returncode:
            lines = [line.strip() for line in (result.stderr or result.stdout or "").splitlines() if line.strip()]
            # Usage errors end with help text, so prefer the line that names the error.
            raise CliError(next((line for line in lines if "error" in line.lower()), connection._last_line(result.stderr or result.stdout)))
        try:
            return json.loads(result.stdout) if result.stdout.strip() else {}
        except ValueError:
            raise CliError("The AWS CLI returned something that isn't JSON.") from None


class Recorder:
    """Runs calls, keeping going when one fails. A denied call is never tried again, and
    anything the access check found denied is never called at all."""

    def __init__(self, denied=()):
        self.errors = []
        self.calls = 0
        self.denied = set(denied)

    def __call__(self, label, function, default=None, key=None):
        gate = key or " ".join(label.split()[:2])
        empty = [] if default is None else default
        if gate in self.denied:
            return empty
        self.calls += 1
        try:
            return function()
        except (CliError, OSError) as error:
            if DENIED.search(str(error)):
                self.denied.add(gate)
            else:
                self.errors.append(f"{label}: {str(error)[:300]}")
            return empty


def _listing(cli, run, region, service, operation, key, *arguments):
    label = " ".join((service, operation, *[a for a in arguments[1:2] if len(a) < 200]))
    return run(label, lambda: cli.call(service, operation, *arguments, region=region).get(key, []), key=key)


def check_access(profile, region):
    """Which resource types this profile can read: one item of each, nothing saved."""
    cli = AwsCli(profile, region or "us-east-1", timeout=PROBE_TIMEOUT)

    def probe(item):
        key, service, operation = item
        try:
            cli.call(service, operation, *([] if key in NOT_PAGED else ["--max-items", "1"]))
            return key, "allowed", ""
        except (CliError, OSError) as error:
            return key, "denied" if DENIED.search(str(error)) else "error", str(error)[:300]

    with ThreadPoolExecutor(PROBES_AT_ONCE) as pool:
        results = list(pool.map(probe, PROBES + [(e[1], e[2], e[3]) for e in EXTRAS]))
    return {"allowed": [k for k, state, _ in results if state == "allowed"], "denied": [k for k, state, _ in results if state == "denied"],
            "errors": {k: text for k, state, text in results if state == "error"}}


def _ec2(cli, region, run):
    data = {"Region": region}
    data["Vpcs"] = _listing(cli, run, region, "ec2", "describe-vpcs", "Vpcs")
    attributes = []
    for vpc in data["Vpcs"]:
        item = {"VpcId": vpc["VpcId"]}
        for attribute, key in (("enableDnsSupport", "EnableDnsSupport"), ("enableDnsHostnames", "EnableDnsHostnames")):
            value = run(f"ec2 describe-vpc-attribute {vpc['VpcId']}", lambda: cli.call(
                "ec2", "describe-vpc-attribute", "--vpc-id", vpc["VpcId"], "--attribute", attribute, region=region), default={})
            if key in value:
                item[key] = value[key]
        attributes.append(item)
    data["VpcAttributes"] = attributes
    services = _listing(cli, run, region, "ec2", "describe-vpc-endpoint-service-configurations", "ServiceConfigurations")
    for service in services:
        service["AllowedPrincipals"] = run(f"ec2 describe-vpc-endpoint-service-permissions {service['ServiceId']}", lambda: cli.call(
            "ec2", "describe-vpc-endpoint-service-permissions", "--service-id", service["ServiceId"], region=region).get("AllowedPrincipals", []), default=None)
    data["ServiceConfigurations"] = services
    data["VpcEndpointConnections"] = _listing(cli, run, region, "ec2", "describe-vpc-endpoint-connections", "VpcEndpointConnections") if services else []
    for operation, key in (
        ("describe-subnets", "Subnets"), ("describe-route-tables", "RouteTables"),
        ("describe-security-groups", "SecurityGroups"), ("describe-network-acls", "NetworkAcls"),
        ("describe-nat-gateways", "NatGateways"), ("describe-internet-gateways", "InternetGateways"),
        ("describe-vpc-endpoints", "VpcEndpoints"), ("describe-transit-gateways", "TransitGateways"),
        ("describe-transit-gateway-attachments", "TransitGatewayAttachments"),
        ("describe-transit-gateway-vpc-attachments", "TransitGatewayVpcAttachments"),
        ("describe-transit-gateway-route-tables", "TransitGatewayRouteTables"),
    ):
        data[key] = _listing(cli, run, region, "ec2", operation, key)
    for table in data["TransitGatewayRouteTables"]:
        table_id = ("--transit-gateway-route-table-id", table["TransitGatewayRouteTableId"])
        table["Routes"] = _listing(cli, run, region, "ec2", "search-transit-gateway-routes", "Routes", *table_id,
                                   "--filters", "Name=state,Values=active,blackhole", "--max-results", "1000")
        table["Associations"] = _listing(cli, run, region, "ec2", "get-transit-gateway-route-table-associations", "Associations", *table_id)
        table["Propagations"] = _listing(cli, run, region, "ec2", "get-transit-gateway-route-table-propagations", "TransitGatewayRouteTablePropagations", *table_id)
    return data


def _elbv2(cli, region, run):
    balancers = _listing(cli, run, region, "elbv2", "describe-load-balancers", "LoadBalancers")
    listeners = []
    for lb in balancers:
        listeners += _listing(cli, run, region, "elbv2", "describe-listeners", "Listeners", "--load-balancer-arn", lb["LoadBalancerArn"])
    groups = _listing(cli, run, region, "elbv2", "describe-target-groups", "TargetGroups")
    for group in groups:
        group["TargetHealthDescriptions"] = run(f"elbv2 describe-target-health {group['TargetGroupName']}", lambda: cli.call(
            "elbv2", "describe-target-health", "--target-group-arn", group["TargetGroupArn"], region=region).get("TargetHealthDescriptions", []), default=None)
    for lb in balancers:
        lb["Attributes"] = _listing(cli, run, region, "elbv2", "describe-load-balancer-attributes", "Attributes", "--load-balancer-arn", lb["LoadBalancerArn"])
    for group in groups:
        group["Attributes"] = _listing(cli, run, region, "elbv2", "describe-target-group-attributes", "Attributes", "--target-group-arn", group["TargetGroupArn"])
    # Tags come from their own call, up to 20 resources at a time.
    items = {i[key]: i for collection, key in ((balancers, "LoadBalancerArn"), (groups, "TargetGroupArn"), (listeners, "ListenerArn")) for i in collection}
    arns = sorted(items)
    for start in range(0, len(arns), 20):
        for entry in _listing(cli, run, region, "elbv2", "describe-tags", "TagDescriptions", "--resource-arns", *arns[start:start + 20]):
            items[entry["ResourceArn"]]["Tags"] = entry.get("Tags", [])
    return {"Region": region, "LoadBalancers": balancers, "Listeners": listeners, "TargetGroups": groups}


def _network_firewall(cli, region, run):
    """The firewalls' own properties. Their policies and rule groups aren't read."""
    firewalls = []
    for item in _listing(cli, run, region, "network-firewall", "list-firewalls", "Firewalls"):
        described = run(f"network-firewall describe-firewall {item['FirewallName']}", lambda: cli.call(
            "network-firewall", "describe-firewall", "--firewall-arn", item["FirewallArn"], region=region), default={})
        if described:
            firewalls.append({"Firewall": described.get("Firewall", {}), "FirewallStatus": described.get("FirewallStatus", {})})
    return {"Region": region, "Firewalls": firewalls}


def _resolver(cli, region, run):
    endpoints = _listing(cli, run, region, "route53resolver", "list-resolver-endpoints", "ResolverEndpoints")
    for endpoint in endpoints:
        endpoint["IpAddresses"] = _listing(cli, run, region, "route53resolver", "list-resolver-endpoint-ip-addresses", "IpAddresses", "--resolver-endpoint-id", endpoint["Id"])
    return {
        "Region": region,
        "ResolverRules": _listing(cli, run, region, "route53resolver", "list-resolver-rules", "ResolverRules"),
        "ResolverRuleAssociations": _listing(cli, run, region, "route53resolver", "list-resolver-rule-associations", "ResolverRuleAssociations"),
        "ResolverEndpoints": endpoints,
    }


def _extras(cli, region, run, file):
    data = {"Region": region}
    for _, name, service, operation, key, *_rest in (e for e in EXTRAS if e[0] == file):
        data[name] = run(f"{service} {operation}", lambda: cli.call(service, operation, region=region).get(key, []), key=name)
    if file == "network":
        for gateway in data["DxGateways"]:
            gateway["Associations"] = _listing(cli, run, region, "directconnect", "describe-direct-connect-gateway-associations", "directConnectGatewayAssociations",
                                               "--direct-connect-gateway-id", gateway["directConnectGatewayId"])
        for prefix_list in data["ManagedPrefixLists"]:
            prefix_list["Entries"] = _listing(cli, run, region, "ec2", "get-managed-prefix-list-entries", "Entries", "--prefix-list-id", prefix_list["PrefixListId"])
    if file == "compute":
        data["Instances"] = [i for r in data.pop("Reservations") for i in r.get("Instances", [])]
    return data


def _eks(cli, region, run):
    clusters = []
    for name in _listing(cli, run, region, "eks", "list-clusters", "clusters"):
        cluster = run(f"eks describe-cluster {name}", lambda: cli.call("eks", "describe-cluster", "--name", name, region=region).get("cluster", {}), default={})
        if cluster:
            clusters.append(cluster)
    nodegroups, fargate, addons = [], [], []
    for cluster in clusters:
        name = ("--cluster-name", cluster["name"])
        for group in _listing(cli, run, region, "eks", "list-nodegroups", "nodegroups", *name):
            found = run(f"eks describe-nodegroup {group}", lambda: cli.call("eks", "describe-nodegroup", *name, "--nodegroup-name", group, region=region).get("nodegroup", {}), default={})
            if found:
                nodegroups.append(found)
        for profile in _listing(cli, run, region, "eks", "list-fargate-profiles", "fargateProfileNames", *name):
            found = run(f"eks describe-fargate-profile {profile}", lambda: cli.call("eks", "describe-fargate-profile", *name, "--fargate-profile-name", profile, region=region).get("fargateProfile", {}), default={})
            if found:
                fargate.append(found)
        for addon in _listing(cli, run, region, "eks", "list-addons", "addons", *name):
            found = run(f"eks describe-addon {addon}", lambda: cli.call("eks", "describe-addon", *name, "--addon-name", addon, region=region).get("addon", {}), default={})
            if found:
                addons.append(found)
    return {"Region": region, "Clusters": clusters, "Nodegroups": nodegroups, "FargateProfiles": fargate, "Addons": addons}


def _route53(cli, region, run):
    zones = _listing(cli, run, region, "route53", "list-hosted-zones", "HostedZones")
    for zone in zones:
        zone_id = zone["Id"].split("/")[-1]
        if not zone.get("Config", {}).get("PrivateZone"):
            continue  # traffic stays private: public zones are listed, their records aren't read
        zone["VPCs"] = _listing(cli, run, region, "route53", "get-hosted-zone", "VPCs", "--id", zone_id)
        zone["ResourceRecordSets"] = _listing(cli, run, region, "route53", "list-resource-record-sets", "ResourceRecordSets",
                                              "--hosted-zone-id", zone_id, "--max-items", str(RECORD_LIMIT))
    return {"HostedZones": zones}


def parse_nslookup(output):
    """(server, ips) from nslookup output, on Windows, macOS or Linux."""
    server = ""
    match = re.search(r"^Address(?:es)?:\s*([^\s#]+)", output, re.MULTILINE)
    if match:
        server = match.group(1)
    # The answer starts at the first Name: line; lines before it describe the DNS server.
    answer = re.split(r"^Name:", output, maxsplit=1, flags=re.MULTILINE)
    ips = []
    if len(answer) > 1:
        for line in answer[1].splitlines()[1:]:
            if line.strip().lower().startswith("aliases"):
                break
            for token in re.findall(r"[0-9A-Fa-f:.]+", line.split(":", 1)[1] if re.match(r"\s*Address(es)?:", line) else line):
                try:
                    ips.append(str(ipaddress.ip_address(token)))
                except ValueError:
                    pass
    return server, list(dict.fromkeys(ips))


def nslookup(name):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    base = {"name": name, "ips": [], "server": "", "looked_up_at": now, "detail": ""}
    tool = shutil.which("nslookup")
    if not tool:
        return {**base, "status": "error", "detail": "nslookup isn't installed on this computer."}
    try:
        result = subprocess.run([tool, f"-timeout={LOOKUP_SECONDS}", name], capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=LOOKUP_LIMIT, **connection._options())
    except subprocess.TimeoutExpired:
        return {**base, "status": "timeout", "detail": f"No answer within {LOOKUP_LIMIT} seconds."}
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    server, ips = parse_nslookup(output)
    base["server"] = server
    if ips:
        return {**base, "status": "resolved", "ips": ips}
    low = output.lower()
    if "timed out" in low or "timeout" in low or "no response" in low or "no servers could be reached" in low:
        return {**base, "status": "timeout", "detail": "The DNS server didn't answer in time."}
    if "non-existent domain" in low or "nxdomain" in low or "can't find" in low:
        return {**base, "status": "not_found", "detail": "The DNS server says this name doesn't exist."}
    return {**base, "status": "error", "detail": connection._last_line(output)}


def project_hostnames(account_id):
    """Names the projects involving this account rely on: requirement and external system hostnames."""
    externals = {e.id: e for e in store.externals()}
    names = set()
    for project in store.projects().values():
        accounts = {b.account_id for b in project.environments.values()} | {a.account_id for a in project.aws_accounts}
        if account_id not in accounts:
            continue
        for requirement in project.requirements:
            names.add(requirement.hostname)
            ids = [requirement.destination, requirement.source] + [h.id for h in requirement.hops if h.kind == "external"]
            for system in (externals[i] for i in ids if i in externals):
                names |= {e.hostname for e in system.endpoints}
    return {n for n in names if n}


def lookup_names(folder, regions, account_id):
    """Every name worth resolving: private zone records, endpoints, load balancers, project hostnames."""
    found = {}

    def add(name, source):
        name = str(name or "").rstrip(".").lower()
        if name and "*" not in name and "\\" not in name and "." in name:
            found.setdefault(name, source)

    for zone in json.loads((folder / "global" / "route53.json").read_text()).get("HostedZones", []):
        for record in zone.get("ResourceRecordSets") or []:
            if record.get("Type") in ("A", "AAAA", "CNAME"):
                add(record.get("Name"), f"Route 53 {zone.get('Name', '').rstrip('.')}")
    for region in regions:
        ec2 = json.loads((folder / region / "ec2.json").read_text())
        for endpoint in ec2.get("VpcEndpoints", []):
            for entry in endpoint.get("DnsEntries") or []:
                add(entry.get("DnsName"), f"VPC endpoint {endpoint.get('VpcEndpointId')}")
        for balancer in json.loads((folder / region / "elbv2.json").read_text()).get("LoadBalancers", []):
            add(balancer.get("DNSName"), f"Load balancer {balancer.get('LoadBalancerName')}")
    for name in sorted(project_hostnames(account_id)):
        add(name, "Project requirement")
    return found


def run_lookups(names):
    """nslookup every name. If the first batch all time out, stop: the DNS server isn't reachable."""
    items = sorted(names)[:LOOKUP_NAMES_MAX]
    results = []
    with ThreadPoolExecutor(LOOKUPS_AT_ONCE) as pool:
        trial = list(pool.map(nslookup, items[:LOOKUP_TRIAL]))
        results.extend(trial)
        rest = items[LOOKUP_TRIAL:]
        if rest and all(r["status"] == "timeout" for r in trial):
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            results.extend({"name": n, "status": "not_tried", "ips": [], "server": "", "looked_up_at": now,
                            "detail": "Skipped: the DNS server wasn't answering."} for n in rest)
        else:
            results.extend(pool.map(nslookup, rest))
    for result in results:
        result["source"] = names[result["name"]]
    return results


# Resource records for state.json: file, list key, Terraform-style type, id field, created-time field.
STATE_TYPES = [
    ("ec2", "Vpcs", "aws_vpc", "VpcId", ""), ("ec2", "Subnets", "aws_subnet", "SubnetId", ""),
    ("ec2", "RouteTables", "aws_route_table", "RouteTableId", ""), ("ec2", "SecurityGroups", "aws_security_group", "GroupId", ""),
    ("ec2", "NetworkAcls", "aws_network_acl", "NetworkAclId", ""), ("ec2", "NatGateways", "aws_nat_gateway", "NatGatewayId", "CreateTime"),
    ("ec2", "InternetGateways", "aws_internet_gateway", "InternetGatewayId", ""),
    ("ec2", "VpcEndpoints", "aws_vpc_endpoint", "VpcEndpointId", "CreationTimestamp"),
    ("ec2", "ServiceConfigurations", "aws_vpc_endpoint_service", "ServiceId", ""),
    ("ec2", "VpcEndpointConnections", "aws_vpc_endpoint_connection", "VpcEndpointId", "CreationTimestamp"),
    ("ec2", "TransitGateways", "aws_ec2_transit_gateway", "TransitGatewayId", "CreationTime"),
    ("ec2", "TransitGatewayVpcAttachments", "aws_ec2_transit_gateway_vpc_attachment", "TransitGatewayAttachmentId", "CreationTime"),
    ("ec2", "TransitGatewayAttachments", "aws_ec2_transit_gateway_attachment", "TransitGatewayAttachmentId", "CreationTime"),
    ("ec2", "TransitGatewayRouteTables", "aws_ec2_transit_gateway_route_table", "TransitGatewayRouteTableId", "CreationTime"),
    ("elbv2", "LoadBalancers", "aws_lb", "LoadBalancerArn", "CreatedTime"), ("elbv2", "Listeners", "aws_lb_listener", "ListenerArn", ""),
    ("elbv2", "TargetGroups", "aws_lb_target_group", "TargetGroupArn", ""), ("eks", "Clusters", "aws_eks_cluster", "arn", "createdAt"),
    ("route53resolver", "ResolverRules", "aws_route53_resolver_rule", "Id", "CreationTime"),
    ("route53resolver", "ResolverRuleAssociations", "aws_route53_resolver_rule_association", "Id", ""),
    ("route53resolver", "ResolverEndpoints", "aws_route53_resolver_endpoint", "Id", "CreationTime"),
    ("eks", "Nodegroups", "aws_eks_node_group", "nodegroupArn", "createdAt"), ("eks", "FargateProfiles", "aws_eks_fargate_profile", "fargateProfileArn", "createdAt"),
    ("eks", "Addons", "aws_eks_addon", "addonArn", "createdAt"), ("compute", "Instances", "aws_instance", "InstanceId", "LaunchTime"),
] + [(e[0], e[1], e[5], e[6], e[7]) for e in EXTRAS if e[5]]


def build_state(folder, account_id, profile, regions):
    """Every downloaded resource with all its properties, one record each, like a Terraform
    state file's resources. It describes AWS; Terraform itself can't load it."""
    resources = []

    def add(kind, resource_id, region, attributes, created=""):
        tags = attributes.get("Tags") or attributes.get("tags") or []
        name = next((t.get("Value") for t in tags if t.get("Key") == "Name"), "") if isinstance(tags, list) else tags.get("Name", "")
        name = name or attributes.get("name") or attributes.get("LoadBalancerName") or attributes.get("TargetGroupName") or attributes.get("GroupName") or attributes.get("Name") or ""
        resources.append({"type": kind, "id": resource_id, "name": name, "account": account_id, "region": region,
                          "created_at": iso(attributes.get(created)) if created else "", "attributes": attributes})

    def read(path):
        return json.loads(path.read_text()) if path.exists() else {}

    for region in regions:
        for file, key, kind, id_field, created in STATE_TYPES:
            for item in read(folder / region / f"{file}.json").get(key, []) or []:
                add(kind, item.get(id_field) or item.get("PublicIp", ""), region, item, created)
        for item in read(folder / region / "network-firewall.json").get("Firewalls", []) or []:
            firewall = item.get("Firewall", {})
            add("aws_networkfirewall_firewall", firewall.get("FirewallArn", ""), region, {**firewall, "FirewallStatus": item.get("FirewallStatus", {})})
    for zone in read(folder / "global" / "route53.json").get("HostedZones", []) or []:
        zone_id = zone.get("Id", "").split("/")[-1]
        add("aws_route53_zone", zone_id, "global", {k: v for k, v in zone.items() if k != "ResourceRecordSets"})
        for record in zone.get("ResourceRecordSets") or []:
            add("aws_route53_record", f"{zone_id}_{record.get('Name', '')}_{record.get('Type', '')}", "global", record)
    return {"format": "owl-resources/v1", "account": account_id, "profile": profile, "regions": regions,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "resources": resources}


def state_of(account_id):
    folder = find_folder(account_id)
    path = folder / "state.json" if folder else None
    return json.loads(path.read_text()) if path and path.exists() else None


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=1, default=str))
    temporary.replace(path)


def profile_for(account_id, wanted=""):
    choices = profiles_by_account().get(account_id, [])
    profile = wanted or (choices[0]["profile"] if choices else "")
    if not profile:
        raise ValueError(f"No profile for account {account_id} in {config_path()}. Add one in the AWS config file.")
    region = next((c["region"] for c in choices if c["profile"] == profile and c.get("region")), "") or next(
        (c["region"] for c in choices if c.get("region")), "")
    return profile, region


def info_file():
    return store.ensure() / "account-info.json"


def account_info():
    """What the last test or download learned about each account: created date, status."""
    path = info_file()
    return json.loads(path.read_text()) if path.exists() else {}


def remember(account_id, **fields):
    """Save what a test or download learned; each one also counts as a use of the account."""
    with _lock:
        info = account_info()
        before = info.get(account_id, {})
        info[account_id] = {**before, **fields, "uses": before.get("uses", 0) + 1}
        _write(info_file(), info)


def account_details(profile):
    """The account's created date and name. AWS may not let a read-only role see them."""
    try:
        answer = AwsCli(profile, timeout=PROBE_TIMEOUT).call("account", "get-account-information")
    except (CliError, OSError) as error:
        return {"created_error": str(error)[:300]}
    return {"created": iso(answer.get("AccountCreatedDate")), "account_name": answer.get("AccountName", ""), "created_error": ""}


def test_account(account_id, wanted=""):
    """`aws sts get-caller-identity` for one account: can its profile sign in, to that account?"""
    if not ACCOUNT.match(account_id):
        raise ValueError(f"{account_id!r} is not a 12-digit AWS account id.")
    try:
        profile, _ = profile_for(account_id, wanted)
    except ValueError as error:
        return {"account_id": account_id, "profile": "", "ok": False, "arn": "", "error": str(error)}
    identity, error = connection.run_sts(profile)
    if identity and identity.get("Account") != account_id:
        identity, error = None, f"Profile {profile} signs in to {identity.get('Account')}, not {account_id}."
    result = {"account_id": account_id, "profile": profile, "ok": bool(identity), "arn": (identity or {}).get("Arn", ""), "error": error or "",
              "allowed": [], "denied": [], "errors": {}}
    if identity:
        result.update(check_access(profile, profile_for(account_id, wanted)[1]))
        result.update(account_details(profile))
    remember(account_id, connected=result["ok"], checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), arn=result["arn"], error=result["error"],
             denied=result["denied"], errors=result["errors"], allowed=len(result["allowed"]),
             **{k: result[k] for k in ("created", "account_name", "created_error") if k in result})
    return result


def download_account(account, progress=lambda text: None):
    """Download one account into the media folder. Returns its manifest."""
    account_id = account["account_id"]
    if not ACCOUNT.match(account_id):
        raise ValueError(f"{account_id!r} is not a 12-digit AWS account id.")
    profile, profile_region = profile_for(account_id, account.get("profile", ""))
    identity, problem = connection.run_sts(profile)
    if not identity:
        raise PermissionError(f"Profile {profile} can't sign in: {problem} Log in, then download again.")
    if identity.get("Account") != account_id:
        raise PermissionError(f"Profile {profile} signs in to {identity.get('Account')}, not {account_id}.")
    details = account_details(profile)
    remember(account_id, connected=True, checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), arn=identity.get("Arn", ""), error="", **details)
    progress(f"{account_id}: checking access")
    access = check_access(profile, profile_region)
    if not access["allowed"]:
        raise PermissionError(f"No read access with profile {profile}; nothing was downloaded. Denied: {', '.join(access['denied']) or 'none'}."
                              + (f" {next(iter(access['errors'].values()))}" if access["errors"] else ""))
    # Types that failed the check for another reason aren't called either.
    run = Recorder(denied=access["denied"] + list(access["errors"]))
    run.errors.extend(f"{key}: {text}" for key, text in access["errors"].items())
    cli = AwsCli(profile, profile_region)
    regions = [r for r in account.get("regions") or [] if REGION.match(r)] or [profile_region or "us-east-1"]
    folder = account_folder(account_id, profile)
    for region in regions:
        progress(f"{account_id} {region}: VPC, routing and transit gateway")
        _write(folder / region / "ec2.json", _ec2(cli, region, run))
        progress(f"{account_id} {region}: load balancers")
        _write(folder / region / "elbv2.json", _elbv2(cli, region, run))
        progress(f"{account_id} {region}: EKS clusters")
        _write(folder / region / "eks.json", _eks(cli, region, run))
        for file, label in (("network", "peering, VPN, Direct Connect and interfaces"), ("ingress", "API gateways and classic load balancers"), ("compute", "instances and Auto Scaling")):
            progress(f"{account_id} {region}: {label}")
            _write(folder / region / f"{file}.json", _extras(cli, region, run, file))
        progress(f"{account_id} {region}: network firewall")
        _write(folder / region / "network-firewall.json", _network_firewall(cli, region, run))
        progress(f"{account_id} {region}: Route 53 Resolver")
        _write(folder / region / "route53resolver.json", _resolver(cli, region, run))
        for stale in ("config.json", "cloudtrail.json"):  # written by earlier versions
            (folder / region / stale).unlink(missing_ok=True)
    progress(f"{account_id}: Route 53 hosted zones")
    _write(folder / "global" / "route53.json", _route53(cli, None, run))
    progress(f"{account_id}: writing state.json")
    _write(folder / "state.json", build_state(folder, account_id, profile, regions))
    progress(f"{account_id}: nslookup for hosted zone, endpoint and load balancer names")
    lookups = run_lookups(lookup_names(folder, regions, account_id))
    _write(folder / "global" / "dns-lookups.json", {"DnsLookups": lookups})
    timed_out = sum(item["status"] == "timeout" for item in lookups)
    not_tried = sum(item["status"] == "not_tried" for item in lookups)
    if timed_out or not_tried:
        run.errors.append(f"nslookup timed out for {timed_out} name(s)" + (f"; {not_tried} more not tried because the DNS server wasn't answering" if not_tried else "")
                          + ". Are you on the company network or VPN?")
    previous = {}
    if (folder / "account.json").exists():
        previous = json.loads((folder / "account.json").read_text()).get("Account", {})
    manifest = {
        "Account": {
            "Id": account_id, "Name": account.get("name") or previous.get("Name") or profile,
            "Type": account.get("type") or previous.get("Type") or "application",
            "Created": details.get("created", ""), "Region": regions[0], "Environments": account.get("environments") or previous.get("Environments", []),
        },
        "Download": {
            "profile": profile, "regions": regions, "caller": identity.get("Arn", ""), "calls": run.calls,
            "lookups": {state: sum(item["status"] == state for item in lookups) for state in ("resolved", "timeout", "not_found", "error", "not_tried")},
            "errors": run.errors, "denied": sorted(run.denied - set(access["errors"])), "downloaded_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    _write(folder / "account.json", manifest)
    return manifest


def safe_name(text):
    return re.sub(r"[^\w.-]+", "_", text).strip("._") or "account"


def account_folder(account_id, profile):
    name, stage = split_profile(profile)
    return media_folder() / f"{account_id}-{safe_name(name)}" / safe_name(stage or "default")


def find_folder(account_id):
    """The account's download folder (the newest, if its profile was renamed), or None."""
    found = sorted(media_folder().glob(f"{account_id}-*/*/account.json"), key=lambda p: p.stat().st_mtime)
    return found[-1].parent if found else None


def snapshot_files(account_ids):
    """Every downloaded file for these accounts, named <account>/<region>/<file> for the normalizer."""
    files = []
    for account_id in account_ids:
        folder = find_folder(account_id)
        if not folder:
            continue
        for path in sorted(folder.rglob("*.json")):
            if path.name == "state.json":
                continue  # the same resources again, in another layout
            data = json.loads(path.read_text())
            if path.name == "account.json":
                data = {"Account": data["Account"], "Region": data["Account"].get("Region", ""),
                        "AccessDenied": data.get("Download", {}).get("denied", [])}
            files.append((f"{account_id}/{path.relative_to(folder).as_posix()}", data))
    return files


def downloaded(account_ids):
    """What the media folder already holds for these accounts."""
    out = {}
    for account_id in account_ids:
        folder = find_folder(account_id)
        if folder:
            data = json.loads((folder / "account.json").read_text())
            out[account_id] = {**data.get("Download", {}), "name": data.get("Account", {}).get("Name", "")}
    return out


# Background refreshes, one per project


def status(project_id):
    with _lock:
        job = _jobs.get(project_id)
        return dict(job) if job else {"state": "idle"}


def start(project):
    return start_job(project.id, project.name, involved_accounts(project))


def start_job(key, name, accounts):
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
    threading.Thread(target=_run, args=(key, name, accounts), name=f"architecture-download-{key}", daemon=True).start()
    return status(key)


def _update(project_id, **fields):
    with _lock:
        _jobs[project_id].update(fields)


def _account_state(project_id, account_id, **fields):
    with _lock:
        for item in _jobs[project_id]["accounts"]:
            if item["account_id"] == account_id:
                item.update(fields)


def _run(key, name, accounts):
    event("architecture.refresh.started", key=key, accounts=len(accounts))

    def one(account):
        _account_state(key, account["account_id"], state="running")
        try:
            manifest = download_account(account, progress=lambda text: _update(key, message=text))
            _account_state(key, account["account_id"], state="done", warnings=len(manifest["Download"]["errors"]) + len(manifest["Download"]["denied"]),
                           denied=manifest["Download"]["denied"], error="; ".join(manifest["Download"]["errors"][:3]))
            return True
        except Exception as error:  # noqa: BLE001 - report per account and continue
            _account_state(key, account["account_id"], state="failed", error=str(error)[:500])
            return False

    with ThreadPoolExecutor(ACCOUNTS_AT_ONCE) as pool:
        ok = sum(pool.map(one, accounts))
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

DEFAULT_CONFIG = {"accounts": [], "regions": []}


def config_file():
    return store.ensure() / "download.json"


def download_config():
    path = config_file()
    value = json.loads(path.read_text()) if path.exists() else {}
    return {"accounts": value.get("accounts", []), "regions": value.get("regions", [])}


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
        accounts.append({"account_id": account_id, "name": str(item.get("name", ""))[:200], "type": str(item.get("type", "")),
                         "regions": regions, "profile": str(item.get("profile", ""))[:200]})
    regions = [r for r in value.get("regions", []) if r]
    if any(not REGION.match(r) for r in regions):
        raise ValueError("Default regions must look like eu-west-2.")
    saved = {"accounts": accounts, "regions": regions}
    _write(config_file(), saved)
    return saved


def split_profile(profile):
    """(name, stage) from mc-<name>-<stage>; other profile names are their own name, with no stage."""
    match = PROFILE_NAME.fullmatch(profile)
    return (match.group(1).lower(), match.group(2).lower()) if match else (profile, "")


def copy_counts():
    """How often the AWS Accounts app copied each profile and account id."""
    try:
        with database_connection() as db:
            rows = db.execute("SELECT value,count FROM aws_account_copies WHERE kind IN ('profile','account_id')").fetchall()
    except sqlite3.Error:
        return {}
    return {row[0]: row[1] for row in rows}


def account_groups():
    """Every account in the AWS config, one group per name with an entry per stage, and the counts."""
    config = download_config()
    selected = {a["account_id"] for a in config["accounts"]}
    found, error = {}, ""
    try:
        found = profiles_by_account()
    except (OSError, ValueError) as problem:
        error = str(problem)
    have = downloaded(sorted(set(found) | selected))
    info = account_info()
    copies = copy_counts()
    groups = {}
    for account_id in sorted(set(found) | selected):
        profile_names = [p["profile"] for p in found.get(account_id, [])]
        first = profile_names[0] if profile_names else ""
        name, stage = split_profile(first) if first else (account_id, "")
        groups.setdefault(name, []).append({
            "account_id": account_id, "stage": stage or first or "account", "profile": first, "profiles": profile_names,
            "selected": account_id in selected, "downloaded_at": (have.get(account_id) or {}).get("downloaded_at", ""),
            "info": info.get(account_id, {}),
            "uses": info.get(account_id, {}).get("uses", 0) + copies.get(account_id, 0) + sum(copies.get(p, 0) for p in profile_names),
        })
    order = {"dev": 0, "test": 1, "nonp": 2, "nonprod": 2, "stage": 3, "mtf": 4, "prod": 5}
    listing = [{"name": name, "accounts": sorted(items, key=lambda a: (order.get(a["stage"], 9), a["stage"]))}
               for name, items in sorted(groups.items())]
    used = sorted(((a["uses"], g["name"], a["stage"], a["account_id"]) for g in listing for a in g["accounts"] if a["uses"]), key=lambda i: (-i[0], i[1], i[2]))
    return {"groups": listing, "top": [i[3] for i in used[:TOP_ACCOUNTS]], "total": sum(len(g["accounts"]) for g in listing), "unique": len(listing),
            "selected": len(selected), "error": error}


def start_all():
    config = download_config()
    accounts = [{**a, "regions": a["regions"] or config["regions"], "environments": []} for a in config["accounts"]]
    return start_job("all", "All accounts", accounts)
