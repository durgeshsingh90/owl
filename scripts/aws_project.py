#!/usr/bin/env python3
"""Find everything a project uses, and work out how its traffic and DNS flow.

Reads the inventory database (inventory/inventory.db); makes no AWS calls, so sync the
project's accounts and the shared-services accounts first.

    python3 aws_project.py                                  # asks for keywords and shows the shared accounts
    python3 aws_project.py stablecoin security             # keywords
    python3 aws_project.py stablecoin --shared 333333333333 mc-centralized-endpoint-service-prod

What it does:
  1. Project: accounts whose profile or alias has a keyword are the project's application
     accounts (everything in them belongs to the project). In the shared-services accounts
     (aws_project.json) it takes what mentions a keyword (name, tags, any text) and what is
     tied to the project's accounts even without a mention: their Transit Gateway
     attachments and route tables (and the VPCs that traffic goes through), RAM shares,
     endpoint services that allow them, private hosted zones and Resolver rules on their
     VPCs, and the Resolver endpoints behind those rules.
  2. EKS: the application accounts' clusters (dev_..., mtf_...), their endpoint access,
     subnets, security groups, node groups and nodes; load balancers are tied to a cluster
     by the load balancer controller's tags or by their targets.
  3. Entry and exit points: internet-facing load balancers, API Gateway, CloudFront, Global
     Accelerator, endpoint services, the EKS API endpoint, public IPs; NAT and internet
     gateways, Transit Gateway routes into the shared egress VPC, VPC endpoints, peering.
  4. Paths, hop by hop, from the configuration only (no traffic is sent): internet -> load
     balancer -> EKS targets; networks behind the Transit Gateway -> internal load
     balancers; EKS nodes -> internet, followed through NAT or the Transit Gateway into the
     shared egress VPC. Each hop (route, network ACL both ways, security group) is PASS,
     FAIL or UNKNOWN with the evidence.
  5. DNS: per project VPC, the order names are answered in (Resolver forward rules, private
     hosted zones, then public DNS) with a check that each forward rule's endpoint can reach
     its targets on port 53; inbound endpoints; and where each project record lands: alias
     and CNAME targets, and the IPs nslookup returned matched to the resource that owns them.
  6. "What's open": security groups open to the internet, EKS endpoints open to 0.0.0.0/0,
     public subnets, public IPs.

Writes inventory/_projects/<project>-<time>.json (everything, with the shared resources'
full data and each application account's merged file), <project>-<time>.tad.json (nodes and
edges for a TAD diagram) and <project>-<time>.mmd (the same as a Mermaid flowchart).
"""

import argparse
import collections
import ipaddress
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # aws_inventory.py and aws_db.py sit next to this file
import aws_db  # noqa: E402
import aws_inventory as inv  # noqa: E402


CONFIG_FILE = inv.HERE / "aws_project.json"
PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"
INTERNET_IP = "198.51.100.10"  # stands for any address on the internet
PROTOCOLS = {"tcp": "6", "udp": "17", "icmp": "1"}

# Types read into the network model: type -> (collection, id field; None: the inventory id).
TYPES = {
    "ec2/vpcs": ("vpcs", "VpcId"), "ec2/subnets": ("subnets", "SubnetId"),
    "ec2/route_tables": ("rtbs", "RouteTableId"), "ec2/network_acls": ("nacls", "NetworkAclId"),
    "ec2/security_groups": ("sgs", "GroupId"), "ec2/network_interfaces": ("enis", "NetworkInterfaceId"),
    "ec2/internet_gateways": ("igws", "InternetGatewayId"), "ec2/nat_gateways": ("nats", "NatGatewayId"),
    "ec2/instances": ("instances", "InstanceId"), "ec2/vpc_endpoints": ("endpoints", "VpcEndpointId"),
    "ec2/vpc_endpoint_services": ("endpoint_services", "ServiceId"), "ec2/elastic_ips": ("eips", None),
    "ec2/transit_gateways": ("tgws", "TransitGatewayId"),
    "ec2/transit_gateway_attachments": ("tgw_atts", "TransitGatewayAttachmentId"),
    "ec2/transit_gateway_vpc_attachments": ("tgw_vpc_atts", "TransitGatewayAttachmentId"),
    "ec2/transit_gateway_route_tables": ("tgw_rts", "TransitGatewayRouteTableId"),
    "ec2/vpc_peering_connections": ("peerings", "VpcPeeringConnectionId"), "ec2/vpn_connections": ("vpns", "VpnConnectionId"),
    "ec2/dhcp_options": ("dhcp", "DhcpOptionsId"),
    "elbv2/load_balancers": ("lbs", "LoadBalancerArn"), "elbv2/listeners": ("listeners", "ListenerArn"),
    "elbv2/target_groups": ("tgs", "TargetGroupArn"), "elb/classic_load_balancers": ("clbs", "LoadBalancerName"),
    "eks/clusters": ("clusters", None), "eks/nodegroups": ("nodegroups", None),
    "apigateway/rest_apis": ("rest_apis", "id"), "apigateway/vpc_links": ("vpc_links", "id"),
    "apigatewayv2/apis": ("http_apis", "ApiId"), "apigatewayv2/vpc_links": ("vpc_links_v2", "VpcLinkId"),
    "cloudfront/distributions": ("distributions", "Id"), "globalaccelerator/accelerators": ("accelerators", "AcceleratorArn"),
    "network-firewall/firewalls": ("firewalls", None), "directconnect/virtual_interfaces": ("vifs", "virtualInterfaceId"),
    "ram/resource_shares_owned": ("ram_shares", "resourceShareArn"),
    "route53/hosted_zones": ("zones", "Id"), "route53resolver/endpoints": ("resolver_eps", "Id"),
    "route53resolver/rules": ("resolver_rules", "Id"), "route53resolver/rule_associations": ("rule_assocs", "Id"),
}


def load_config():
    cfg = inv.read_json(CONFIG_FILE)
    if cfg is None:
        sys.exit(f"Missing {CONFIG_FILE}")
    return cfg


# ---------------------------------------------------------------------------- small helpers

def network(cidr):
    try:
        return ipaddress.ip_network(cidr, strict=False)
    except (TypeError, ValueError):
        return None


def contains(cidr, ip):
    net = network(cidr)
    try:
        return net is not None and ipaddress.ip_address(ip) in net
    except ValueError:
        return False


def address_in(cidr, offset=10):
    """An address inside a CIDR, standing for it."""
    net = network(cidr)
    if net is None:
        return None
    return str(net.network_address + min(offset, max(net.num_addresses - 1, 0)))


def ports_cover(ports, port):
    if ports in (None, "all"):
        return True
    ports = str(ports)
    if "-" in ports:
        lo, hi = ports.split("-", 1)
        return int(lo) <= port <= int(hi)
    return ports.isdigit() and int(ports) == port


def protocol_ok(rule_protocol, protocol):
    rule_protocol = str(rule_protocol).lower()
    return rule_protocol in ("all", "-1", "none") or rule_protocol in (protocol, PROTOCOLS.get(protocol))


def worst(verdicts):
    verdicts = list(verdicts)
    return FAIL if FAIL in verdicts else UNKNOWN if UNKNOWN in verdicts or not verdicts else PASS


def hop(what, verdict, evidence):
    return {"hop": what, "verdict": verdict, "evidence": evidence}


def tag(resource, key):
    for field in ("Tags", "tags", "TagList"):
        tags = resource.get(field)
        if isinstance(tags, dict):
            return tags.get(key)
        for t in tags if isinstance(tags, list) else []:
            if isinstance(t, dict) and (t.get("Key") or t.get("key")) == key:
                return t.get("Value") or t.get("value")
    return None


def tags_of(resource):
    out = {}
    for holder in (resource, resource.get("_details") or {}):
        for field in ("Tags", "tags", "TagList"):
            tags = holder.get(field) if isinstance(holder, dict) else None
            if isinstance(tags, dict):
                out.update({str(k): str(v) for k, v in tags.items()})
            for t in tags if isinstance(tags, list) else []:
                if isinstance(t, dict) and (t.get("Key") or t.get("key")):
                    out[t.get("Key") or t.get("key")] = str(t.get("Value") or t.get("value") or "")
    return out


def label(obj):
    """A short readable name for a resource."""
    return (tag(obj, "Name") or obj.get("LoadBalancerName") or obj.get("GroupName") or obj.get("FirewallName")
            or obj.get("TargetGroupName") or obj.get("name") or obj.get("Name") or obj.get("_rid"))


# ---------------------------------------------------------------------------- the network model

class Net:
    """The resources of the project's and shared-services accounts that traffic and DNS depend on."""

    def __init__(self, db, accounts):
        for collection, _ in TYPES.values():
            setattr(self, collection, {})
        keys = list(TYPES)
        marks = ",".join("?" * len(keys))
        for account in accounts:
            for region, key, rid, data in db.execute(
                    f"SELECT region, type, resource_id, data FROM resources WHERE account_id=? AND type IN ({marks})",
                    (account, *keys)):
                collection, id_field = TYPES[key]
                obj = json.loads(data)
                if key == "eks/clusters":
                    obj = {**((obj.get("_details") or {}).get("Cluster") or {}), **obj}
                elif key == "eks/nodegroups":
                    obj = {**((obj.get("_details") or {}).get("Nodegroup") or {}), **obj}
                elif key == "network-firewall/firewalls":
                    obj = {**((obj.get("_details") or {}).get("Firewall") or {}), **obj}
                obj.update({"_acct": account, "_region": region, "_rid": rid, "_type": key})
                getattr(self, collection)[str(obj.get(id_field) or rid) if id_field else rid] = obj
        self.link()

    def link(self):
        self.subnet_rtb, self.main_rtb, self.subnet_nacl = {}, {}, {}
        for rtb in self.rtbs.values():
            for a in rtb.get("Associations") or []:
                if a.get("Main"):
                    self.main_rtb[rtb.get("VpcId")] = rtb
                elif a.get("SubnetId"):
                    self.subnet_rtb[a["SubnetId"]] = rtb
        for acl in self.nacls.values():
            for a in acl.get("Associations") or []:
                self.subnet_nacl[a.get("SubnetId")] = acl
        # Private IPs repeat across accounts (nonp and prod often use the same CIDR): ip -> [ENIs].
        self.enis_by_ip, self.instance_enis = collections.defaultdict(list), collections.defaultdict(list)
        for eni in self.enis.values():
            for ip in dict.fromkeys([eni.get("PrivateIpAddress")] + [p.get("PrivateIpAddress") for p in eni.get("PrivateIpAddresses") or []]):
                if ip:
                    self.enis_by_ip[ip].append(eni)
            if (eni.get("Attachment") or {}).get("InstanceId"):
                self.instance_enis[eni["Attachment"]["InstanceId"]].append(eni)
        self.vpc_attachment = {}
        for att in list(self.tgw_vpc_atts.values()) + list(self.tgw_atts.values()):
            vpc = att.get("VpcId") or (att.get("ResourceId") if att.get("ResourceType") == "vpc" else None)
            if vpc:
                self.vpc_attachment.setdefault(vpc, att)
        self.attachment_rt = {}  # attachment -> the TGW route table its traffic is routed by
        for rt in self.tgw_rts.values():
            for a in (rt.get("_details") or {}).get("Associations") or []:
                self.attachment_rt[a.get("TransitGatewayAttachmentId")] = rt
        self.lb_by_dns = {}
        for lb in self.lbs.values():
            if lb.get("DNSName"):
                self.lb_by_dns[lb["DNSName"].lower().rstrip(".")] = lb
        for clb in self.clbs.values():
            if clb.get("DNSName"):
                self.lb_by_dns[clb["DNSName"].lower().rstrip(".")] = clb
        self.attachments = {**self.tgw_atts, **self.tgw_vpc_atts}

    def eni_at(self, ip, vpcs=(), account=None):
        """The network interface with this private IP: in one of these VPCs, else in this account,
        else the only one there is (None when it is ambiguous or unknown)."""
        found = self.enis_by_ip.get(ip) or []
        for test in (lambda e: e.get("VpcId") in vpcs, lambda e: e["_acct"] == account):
            near = [e for e in found if test(e)]
            if near:
                return near[0]
        return found[0] if len(found) == 1 else None

    def rtb_of(self, subnet_id):
        subnet = self.subnets.get(subnet_id) or {}
        return self.subnet_rtb.get(subnet_id) or self.main_rtb.get(subnet.get("VpcId"))

    def vpc_cidrs(self, vpc_id):
        vpc = self.vpcs.get(vpc_id) or {}
        return [c["CidrBlock"] for c in vpc.get("CidrBlockAssociationSet") or [] if c.get("CidrBlock")] or (
            [vpc["CidrBlock"]] if vpc.get("CidrBlock") else [])

    def subnet_ip(self, subnet_id):
        return address_in((self.subnets.get(subnet_id) or {}).get("CidrBlock"))


# ---------------------------------------------------------------------------- hop checks

def route_for(rtb, ip):
    """The route a route table uses for an address (longest prefix)."""
    best, length = None, -1
    for r in (rtb or {}).get("Routes") or []:
        net = network(r.get("DestinationCidrBlock") or r.get("DestinationIpv6CidrBlock"))
        if net is not None and contains(str(net), ip) and net.prefixlen > length:
            best, length = r, net.prefixlen
    return best


def target_of(route):
    if route is None:
        return "none", None
    if route.get("State") == "blackhole":
        return "blackhole", route.get("TransitGatewayId") or route.get("GatewayId")
    for key, kind in (("NatGatewayId", "nat"), ("TransitGatewayId", "tgw"), ("VpcPeeringConnectionId", "pcx"),
                      ("NetworkInterfaceId", "eni"), ("VpcEndpointId", "vpce"), ("EgressOnlyInternetGatewayId", "eigw"),
                      ("LocalGatewayId", "lgw"), ("CarrierGatewayId", "cagw")):
        if route.get(key):
            return kind, route[key]
    gateway = route.get("GatewayId") or ""
    for prefix, kind in (("local", "local"), ("igw-", "igw"), ("vgw-", "vgw"), ("vpce-", "vpce")):
        if gateway.startswith(prefix):
            return kind, gateway
    return "other", gateway


def route_text(rtb, route):
    kind, target = target_of(route)
    if route is None:
        return f"{(rtb or {}).get('RouteTableId', 'route table ?')}: no route"
    dest = route.get("DestinationCidrBlock") or route.get("DestinationIpv6CidrBlock")
    return f"{rtb.get('RouteTableId')}: {dest} -> {target}"


def sg_check(net, sg_ids, direction, protocol, port, ip=None, peer_sgs=()):
    """Does any of these security groups allow this flow? (PASS/FAIL/UNKNOWN, evidence)"""
    sg_ids = [s for s in sg_ids if s]
    if not sg_ids:
        return UNKNOWN, "no security group known"
    maybe, missing = [], []
    word = "from" if direction == "inbound" else "to"
    for sg_id in sg_ids:
        sg = net.sgs.get(sg_id)
        if not sg:
            missing.append(sg_id)
            continue
        for r in (sg.get("_rules") or {}).get(direction) or []:
            if not protocol_ok(r.get("protocol"), protocol) or not ports_cover(r.get("ports"), port):
                continue
            kind, peer = r.get("peer_type"), str(r.get("peer") or "")
            if kind in ("cidr", "cidr6") and ip and contains(peer, ip):
                return PASS, f"{sg_id} ({label(sg)}) allows {protocol}/{r.get('ports')} {word} {peer}"
            if kind == "security_group" and peer.split("/")[-1] in peer_sgs:
                return PASS, f"{sg_id} ({label(sg)}) allows {protocol}/{r.get('ports')} {word} {peer.split('/')[-1]}"
            if kind == "prefix_list":
                maybe.append(f"{sg_id} allows {protocol}/{r.get('ports')} {word} prefix list {peer}")
    if maybe:
        return UNKNOWN, "; ".join(maybe[:2])
    if missing and len(missing) == len(sg_ids):
        return UNKNOWN, f"{', '.join(missing)} not in the inventory"
    who = ip or ", ".join(peer_sgs) or "?"
    return FAIL, f"no rule in {', '.join(sg_ids)} allows {protocol}/{port} {word} {who}"


def nacl_check(net, subnet_id, outbound, protocol, port, ip):
    """The network ACL of a subnet: its first matching rule decides."""
    acl = net.subnet_nacl.get(subnet_id)
    if not acl:
        return UNKNOWN, f"no network ACL known for {subnet_id}"
    rules = (acl.get("_rules") or {}).get("outbound" if outbound else "inbound") or []
    word = "to" if outbound else "from"
    for r in rules:
        if protocol_ok(r.get("protocol"), protocol) and ports_cover(r.get("ports"), port) and contains(r.get("cidr"), ip):
            verdict = PASS if r.get("action") == "allow" else FAIL
            return verdict, f"{acl.get('NetworkAclId')} rule {r.get('rule')} {r.get('action')}s {protocol}/{r.get('ports')} {word} {r.get('cidr')}"
    return FAIL, f"{acl.get('NetworkAclId')}: no rule allows {protocol}/{port} {word} {ip} (default deny)"


def nacl_both_ways(net, subnet_id, outbound, protocol, port, ip, what):
    """A stateless ACL needs the request one way and the reply (ephemeral ports) the other."""
    v1, e1 = nacl_check(net, subnet_id, outbound, protocol, port, ip)
    v2, e2 = nacl_check(net, subnet_id, not outbound, protocol, 32768, ip)
    return [hop(f"{what}: network ACL {'out' if outbound else 'in'} {protocol}/{port}", v1, e1),
            hop(f"{what}: network ACL {'in' if outbound else 'out'} for replies (ephemeral ports)", v2, e2)]


# ---------------------------------------------------------------------------- the project

def eni_owner(net, eni, cfg):
    """What a network interface belongs to: (kind, name)."""
    instance = (eni.get("Attachment") or {}).get("InstanceId")
    if instance:
        cluster = cluster_of(net.instances.get(instance) or {}, cfg)
        return ("EKS node", f"{instance} ({cluster})") if cluster else ("instance", f"{instance} ({label(net.instances.get(instance) or {})})")
    d = eni.get("Description") or ""
    m = re.match(r"ELB (app|net|gwy)/([^/]+)/", d)
    if m:
        return "load balancer", m.group(2)
    if d.startswith("ELB "):
        return "load balancer", d[4:]
    if eni.get("InterfaceType") == "vpc_endpoint" or d.startswith("VPC Endpoint"):
        return "VPC endpoint", d or eni.get("NetworkInterfaceId")
    if "Route 53 Resolver" in d:
        return "Resolver endpoint", d
    if d.startswith("Amazon EKS"):
        return "EKS control plane", d.split()[-1]
    if eni.get("InterfaceType") == "nat_gateway" or "NAT Gateway" in d:
        return "NAT gateway", d
    return eni.get("InterfaceType") or "network interface", d or eni.get("NetworkInterfaceId")


def cluster_of(resource, cfg, clusters=()):
    """The EKS cluster a resource is tagged for, if any."""
    tags = tags_of(resource)
    for key in cfg["cluster_tags"]:
        if "{cluster}" in key:
            prefix = key.split("{cluster}")[0]
            for k in tags:
                if k.startswith(prefix):
                    return k[len(prefix):]
        elif tags.get(key):
            return tags[key]
    return None


def find_project(db, cfg, keywords, shared_given):
    """The project's application and shared-services accounts."""
    rows = db.execute("SELECT account_id, profile, alias, last_sync FROM accounts").fetchall()
    words = [w.lower() for w in cfg["shared_words"]]
    shared = {}
    for account, profile, alias, synced in rows:
        names = f"{profile or ''} {alias or ''}".lower()
        if account in shared_given or profile in shared_given or any(w in names for w in words):
            shared[account] = {"account": account, "profile": profile, "alias": alias, "synced": synced}
    apps = {}
    for account, profile, alias, synced in rows:
        names = f"{profile or ''} {alias or ''}".lower()
        if account not in shared and any(k.lower() in names for k in keywords):
            apps[account] = {"account": account, "profile": profile, "alias": alias, "synced": synced}
    return apps, shared


def keyword_hits(db, keywords, accounts, limit):
    """(account, region, type, id) of resources in these accounts that mention a keyword."""
    if not accounts:
        return {}
    mode = aws_db.search_mode(db)
    marks = ",".join("?" * len(accounts))
    hits = {}
    for word in keywords:
        if mode == "trigram" and len(word) >= 3 or mode == "words":
            sql = (f"SELECT r.account_id, r.region, r.type, r.resource_id FROM resource_search s JOIN resources r ON r.id = s.rowid "
                   f"WHERE resource_search MATCH ? AND r.account_id IN ({marks}) LIMIT ?")
            params = ['"' + word.replace('"', '""') + '"', *accounts, limit]
        else:
            sql = f"SELECT account_id, region, type, resource_id FROM resources WHERE data LIKE ? AND account_id IN ({marks}) LIMIT ?"
            params = [f"%{word}%", *accounts, limit]
        for row in db.execute(sql, params):
            hits.setdefault(tuple(row), f"mentions {word}")
    return hits


def linked_shared(net, apps, app_vpcs, shared):
    """Shared-services resources tied to the project's accounts without naming them: {(account, type, id): why}."""
    found = {}

    def take(obj, why):
        if obj and obj.get("_acct") in shared:
            found.setdefault((obj["_acct"], obj["_region"], obj["_type"], obj["_rid"]), why)

    project_atts = set()
    for att_id, att in net.attachments.items():
        vpc = att.get("VpcId") or att.get("ResourceId")
        if vpc in app_vpcs or att.get("ResourceOwnerId") in apps or att.get("VpcOwnerId") in apps:
            project_atts.add(att_id)
            take(att, f"Transit Gateway attachment of project VPC {vpc}")
            take(net.tgws.get(att.get("TransitGatewayId")), "Transit Gateway the project VPCs attach to")
    transit_vpcs = set()
    for rt in net.tgw_rts.values():
        d = rt.get("_details") or {}
        assoc = {a.get("TransitGatewayAttachmentId") for a in d.get("Associations") or []}
        props = {a.get("TransitGatewayAttachmentId") for a in d.get("Propagations") or []}
        routes = d.get("Routes") or []
        targets = {a.get("TransitGatewayAttachmentId") for r in routes for a in r.get("TransitGatewayAttachments") or []}
        if project_atts & (assoc | props | targets):
            take(rt, "Transit Gateway route table the project's attachments use")
            nexts = set()
            if project_atts & assoc:
                nexts |= targets - project_atts  # where project traffic is sent (egress, inspection)
            if project_atts & targets:
                nexts |= assoc - project_atts  # who is sent to the project (callers)
            for att_id in nexts:
                att = net.attachments.get(att_id) or {}
                take(att, "attachment project traffic is routed to (egress, inspection or shared services)")
                vpc = att.get("VpcId") or (att.get("ResourceId") if att.get("ResourceType") == "vpc" else None)
                if vpc and vpc not in app_vpcs:
                    transit_vpcs.add(vpc)
    for vpc in transit_vpcs:
        take(net.vpcs.get(vpc), "VPC that project traffic goes through")
        for group in (net.subnets, net.rtbs, net.nacls, net.nats, net.igws, net.firewalls, net.endpoints):
            for obj in group.values():
                if obj.get("VpcId") == vpc:
                    take(obj, f"in VPC {vpc} that project traffic goes through")
    for share in net.ram_shares.values():
        principals = {p.get("id") for p in (share.get("_details") or {}).get("Principals") or []}
        if principals & set(apps):
            take(share, "RAM share to a project account")
            arns = {r.get("arn") for r in (share.get("_details") or {}).get("Resources") or []}
            for group in (net.subnets, net.tgws, net.resolver_rules, net.endpoint_services):
                for obj in group.values():
                    if {obj.get("SubnetArn"), obj.get("TransitGatewayArn"), obj.get("Arn"), obj.get("_rid")} & arns:
                        take(obj, "shared with a project account through RAM")
    for svc in net.endpoint_services.values():
        allowed = " ".join(str(p.get("Principal")) for p in (svc.get("_details") or {}).get("AllowedPrincipals") or [])
        if any(a in allowed for a in apps) or "*" == allowed.strip():
            take(svc, "endpoint service that allows a project account")
            for lb_arn in svc.get("NetworkLoadBalancerArns") or []:
                take(net.lbs.get(lb_arn), "load balancer behind an endpoint service the project uses")
    for zone in net.zones.values():
        vpcs = {v.get("VPCId") for v in ((zone.get("_details") or {}).get("Zone") or {}).get("VPCs") or []}
        if vpcs & app_vpcs:
            take(zone, "private hosted zone associated with a project VPC")
    for assoc in net.rule_assocs.values():
        if assoc.get("VPCId") in app_vpcs:
            take(assoc, "Resolver rule association on a project VPC")
            rule = net.resolver_rules.get(assoc.get("ResolverRuleId"))
            take(rule, "Resolver rule on a project VPC")
            if rule:
                take(net.resolver_eps.get(rule.get("ResolverEndpointId")), "Resolver endpoint behind a project VPC's rule")
    return found


def eks_summary(net, cfg, apps):
    out = []
    for name, c in net.clusters.items():
        if c["_acct"] not in apps:
            continue
        cfg_vpc = c.get("resourcesVpcConfig") or {}
        m = re.match(cfg["eks_env_pattern"], c.get("name") or name)
        groups = [g for g in net.nodegroups.values() if g.get("_parent_id") == name and g["_acct"] == c["_acct"]]
        nodes = [i for i in net.instances.values() if cluster_of(i, cfg) == name and i["_acct"] == c["_acct"]]
        node_sgs = {s.get("GroupId") for i in nodes for s in i.get("SecurityGroups") or []}
        out.append({
            "name": c.get("name") or name, "env": m.group("env") if m else None, "account": c["_acct"], "region": c["_region"],
            "version": c.get("version"), "vpc": cfg_vpc.get("vpcId"),
            "endpoint_public": cfg_vpc.get("endpointPublicAccess"), "endpoint_private": cfg_vpc.get("endpointPrivateAccess"),
            "public_access_cidrs": cfg_vpc.get("publicAccessCidrs") or [],
            "subnets": sorted(set(cfg_vpc.get("subnetIds") or []) | {s for g in groups for s in g.get("subnets") or []}),
            "security_groups": sorted({cfg_vpc.get("clusterSecurityGroupId"), *(cfg_vpc.get("securityGroupIds") or []), *node_sgs} - {None}),
            "nodegroups": [{"name": g.get("nodegroupName") or g["_rid"].split("/")[-1], "subnets": g.get("subnets") or [],
                            "instance_types": g.get("instanceTypes"), "scaling": g.get("scalingConfig")} for g in groups],
            "nodes": sorted(i["InstanceId"] for i in nodes),
        })
    return out


# ---------------------------------------------------------------------------- paths

def lb_targets(net, lb):
    """[(listener, target group, [targets])] behind a load balancer."""
    out = []
    for listener in net.listeners.values():
        if listener.get("LoadBalancerArn") != lb.get("LoadBalancerArn"):
            continue
        groups = set()
        actions = list(listener.get("DefaultActions") or [])
        for rule in (listener.get("_details") or {}).get("Rules") or []:
            actions += rule.get("Actions") or []
        for a in actions:
            if a.get("TargetGroupArn"):
                groups.add(a["TargetGroupArn"])
            for t in ((a.get("ForwardConfig") or {}).get("TargetGroups")) or []:
                groups.add(t.get("TargetGroupArn"))
        for arn in sorted(g for g in groups if g):
            tg = net.tgs.get(arn) or {"TargetGroupArn": arn}
            targets = [t.get("Target") or {} for t in (tg.get("_details") or {}).get("Targets") or []]
            out.append((listener, tg, targets))
    return out


def target_eni(net, target, lb=None):
    target_id = str(target.get("Id") or "")
    if target_id.startswith("i-"):
        enis = net.instance_enis.get(target_id) or []
        return enis[0] if enis else None
    lb = lb or {}
    return net.eni_at(target_id, {lb.get("VpcId")}, lb.get("_acct"))  # an IP target sits in (or next to) the LB's VPC


def lb_to_target_hops(net, lb, target, port, cfg, client_ip=INTERNET_IP):
    """From a load balancer to one of its targets; client_ip is who called the load balancer."""
    hops = []
    eni = target_eni(net, target, lb)
    if not eni:
        return [hop("target found in the inventory", UNKNOWN, f"target {target.get('Id')} is not a known network interface")], None
    t_subnet, t_ip = eni.get("SubnetId"), eni.get("PrivateIpAddress")
    t_sgs = [g.get("GroupId") for g in eni.get("Groups") or []]
    lb_subnets = [z.get("SubnetId") for z in lb.get("AvailabilityZones") or [] if z.get("SubnetId")]
    lb_sgs = lb.get("SecurityGroups") or []
    lb_ip = net.subnet_ip(lb_subnets[0]) if lb_subnets else None
    owner = eni_owner(net, eni, cfg)
    if lb_sgs:
        hops.append(hop("load balancer security group out to the target", *sg_check(net, lb_sgs, "outbound", "tcp", port, t_ip, t_sgs)))
    if lb.get("Type") == "network" and not lb_sgs and str(target.get("Id", "")).startswith("i-"):
        peer_ip, peer_sgs = client_ip, ()  # an NLB keeps the client's address for instance targets
    else:
        peer_ip, peer_sgs = lb_ip, lb_sgs
    hops.append(hop(f"target security group in from the load balancer ({owner[0]})",
                    *sg_check(net, t_sgs, "inbound", "tcp", port, peer_ip, peer_sgs)))
    if lb_subnets and t_ip:
        rtb = net.rtb_of(lb_subnets[0])
        route = route_for(rtb, t_ip)
        kind, _ = target_of(route)
        hops.append(hop("route from the load balancer's subnet to the target", PASS if kind in ("local", "tgw", "pcx") else FAIL,
                        route_text(rtb, route)))
        hops += nacl_both_ways(net, lb_subnets[0], True, "tcp", port, t_ip, "load balancer subnet")
        hops += nacl_both_ways(net, t_subnet, False, "tcp", port, lb_ip, "target subnet")
    return hops, owner


def internet_to_lb_paths(net, cfg, project_lbs):
    paths = []
    for lb in project_lbs:
        if lb.get("Scheme") != "internet-facing":
            continue
        lb_subnets = [z.get("SubnetId") for z in lb.get("AvailabilityZones") or [] if z.get("SubnetId")]
        for listener, tg, targets in lb_targets(net, lb) or [(None, {}, [])]:
            port = (listener or {}).get("Port") or cfg["test_ports"][0]
            hops = [hop("load balancer is internet-facing", PASS, f"{label(lb)} scheme internet-facing")]
            no_igw = []
            for s in lb_subnets:
                rtb = net.rtb_of(s)
                kind, _ = target_of(route_for(rtb, INTERNET_IP))
                if kind != "igw":
                    no_igw.append(f"{s} ({route_text(rtb, route_for(rtb, INTERNET_IP))})")
            hops.append(hop("load balancer subnets route to an internet gateway", PASS if not no_igw else FAIL,
                            "all its subnets are public" if not no_igw else "not public: " + "; ".join(no_igw)))
            if lb_subnets:
                hops += nacl_both_ways(net, lb_subnets[0], False, "tcp", port, INTERNET_IP, "load balancer subnet")
            if lb.get("SecurityGroups"):
                hops.append(hop(f"load balancer security group in on {port} from the internet",
                                *sg_check(net, lb["SecurityGroups"], "inbound", "tcp", port, INTERNET_IP)))
            per_target, reached = [], []
            if cluster_of(lb, cfg):
                reached.append(f"EKS cluster {cluster_of(lb, cfg)} (load balancer tag)")
            for target in targets[:3]:
                t_hops, owner = lb_to_target_hops(net, lb, target, target.get("Port") or tg.get("Port") or port, cfg)
                per_target.append({"target": target.get("Id"), "owner": owner, "hops": t_hops, "verdict": worst(h["verdict"] for h in t_hops)})
                if owner:
                    reached.append(f"{owner[0]} {owner[1]}")
            if not targets:
                hops.append(hop("targets", UNKNOWN, f"no targets known for {label(tg) if tg else 'this listener'}"))
            verdict = worst([h["verdict"] for h in hops] + [t["verdict"] for t in per_target])
            paths.append({"path": "internet -> load balancer -> targets", "from": "internet",
                          "entry": label(lb), "lb": lb.get("LoadBalancerArn"), "port": port,
                          "target_group": label(tg) if tg else None, "reaches": reached,
                          "account": lb["_acct"], "region": lb["_region"],
                          "hops": hops, "targets": per_target, "verdict": verdict})
    return paths


def tgw_to_internal_lb_paths(net, cfg, project_lbs):
    """Networks behind the Transit Gateway (other VPCs, on-premises) to internal load balancers."""
    paths = []
    for lb in project_lbs:
        if lb.get("Scheme") != "internal":
            continue
        lb_subnets = [z.get("SubnetId") for z in lb.get("AvailabilityZones") or [] if z.get("SubnetId")]
        if not lb_subnets:
            continue
        rtb = net.rtb_of(lb_subnets[0])
        callers = [r for r in (rtb or {}).get("Routes") or [] if target_of(r)[0] == "tgw"]
        att = net.vpc_attachment.get(lb.get("VpcId"))
        for route in callers:
            dest = route.get("DestinationCidrBlock")
            caller_ip = address_in(dest)
            if not caller_ip:
                continue
            for listener, tg, targets in lb_targets(net, lb)[:3] or [(None, {}, [])]:
                port = (listener or {}).get("Port") or cfg["test_ports"][0]
                hops = [hop("reply route back to the callers", PASS, route_text(rtb, route))]
                if att:
                    lb_ip, att_id = net.subnet_ip(lb_subnets[0]), att.get("TransitGatewayAttachmentId")
                    routed = [rt for rt in net.tgw_rts.values()
                              if any(contains(r.get("DestinationCidrBlock"), lb_ip)
                                     and any(a.get("TransitGatewayAttachmentId") == att_id for a in r.get("TransitGatewayAttachments") or [])
                                     for r in (rt.get("_details") or {}).get("Routes") or [])]
                    hops.append(hop("Transit Gateway routes to this VPC", PASS if routed else (UNKNOWN if not net.tgw_rts else FAIL),
                                    ", ".join(rt.get("TransitGatewayRouteTableId") for rt in routed) if routed
                                    else "no Transit Gateway route table in the inventory sends this VPC's CIDR to its attachment"))
                else:
                    hops.append(hop("VPC attached to the Transit Gateway", UNKNOWN, "no attachment of this VPC in the inventory"))
                hops += nacl_both_ways(net, lb_subnets[0], False, "tcp", port, caller_ip, "load balancer subnet")
                if lb.get("SecurityGroups"):
                    hops.append(hop(f"load balancer security group in on {port} from {dest}",
                                    *sg_check(net, lb["SecurityGroups"], "inbound", "tcp", port, caller_ip)))
                per_target, reached = [], []
                for target in targets[:2]:
                    t_hops, owner = lb_to_target_hops(net, lb, target, target.get("Port") or tg.get("Port") or port, cfg, caller_ip)
                    per_target.append({"target": target.get("Id"), "owner": owner, "hops": t_hops,
                                       "verdict": worst(h["verdict"] for h in t_hops)})
                    if owner:
                        reached.append(f"{owner[0]} {owner[1]}")
                paths.append({"path": "Transit Gateway -> internal load balancer -> targets", "from": f"{dest} via {route.get('TransitGatewayId')}",
                              "entry": label(lb), "lb": lb.get("LoadBalancerArn"), "port": port,
                              "target_group": label(tg) if tg else None, "reaches": reached,
                              "account": lb["_acct"], "region": lb["_region"], "hops": hops, "targets": per_target,
                              "verdict": worst([h["verdict"] for h in hops] + [t["verdict"] for t in per_target])})
    return paths


def follow_out(net, subnet_id, ip, depth=0, seen=None):
    """Follow traffic from a subnet towards an address until it leaves AWS or stops: [hops], exit."""
    seen = seen if seen is not None else set()
    if depth > 6 or subnet_id in seen:
        return [hop("following the route", UNKNOWN, "route loops or goes too deep")], None
    seen.add(subnet_id)
    rtb = net.rtb_of(subnet_id)
    if not rtb:
        return [hop(f"route table of {subnet_id}", UNKNOWN, "no route table in the inventory")], None
    route = route_for(rtb, ip)
    kind, target = target_of(route)
    where = f"{subnet_id} ({label(net.subnets.get(subnet_id) or {}) or ''})"
    if kind == "igw":
        return [hop(f"{where} routes to the internet gateway", PASS, route_text(rtb, route))], ("internet gateway", target)
    if kind == "nat":
        nat = net.nats.get(target)
        hops = [hop(f"{where} routes to the NAT gateway", PASS if nat else UNKNOWN, route_text(rtb, route))]
        if not nat:
            return hops + [hop("NAT gateway in the inventory", UNKNOWN, f"{target} not found")], ("NAT gateway", target)
        if nat.get("ConnectivityType") == "private":
            more, exit_ = follow_out(net, nat.get("SubnetId"), ip, depth + 1, seen)
            return hops + [hop("NAT gateway", PASS, f"{target} is private (no internet by itself)")] + more, exit_
        more, exit_ = follow_out(net, nat.get("SubnetId"), ip, depth + 1, seen)
        return hops + more, ("NAT gateway", target) if exit_ and exit_[0] == "internet gateway" else exit_
    if kind == "tgw":
        hops = [hop(f"{where} routes to the Transit Gateway", PASS, route_text(rtb, route))]
        subnet = net.subnets.get(subnet_id) or {}
        att = net.vpc_attachment.get(subnet.get("VpcId"))
        if not att:
            return hops + [hop("Transit Gateway attachment of this VPC", UNKNOWN, "not in the inventory (sync the shared account)")], ("Transit Gateway", target)
        rt = net.attachment_rt.get(att.get("TransitGatewayAttachmentId"))
        if not rt:
            return hops + [hop("Transit Gateway route table of the attachment", UNKNOWN,
                               f"{att.get('TransitGatewayAttachmentId')}: no associated route table in the inventory")], ("Transit Gateway", target)
        best, length = None, -1
        for r in (rt.get("_details") or {}).get("Routes") or []:
            net_ = network(r.get("DestinationCidrBlock"))
            if net_ is not None and contains(str(net_), ip) and net_.prefixlen > length:
                best, length = r, net_.prefixlen
        if not best or best.get("State") == "blackhole":
            return hops + [hop("Transit Gateway route", FAIL, f"{rt.get('TransitGatewayRouteTableId')}: "
                                                              + ("blackhole" if best else f"no route to {ip}"))], None
        next_atts = [a.get("TransitGatewayAttachmentId") for a in best.get("TransitGatewayAttachments") or []]
        hops.append(hop("Transit Gateway route", PASS, f"{rt.get('TransitGatewayRouteTableId')}: {best.get('DestinationCidrBlock')} -> {', '.join(next_atts)}"))
        nxt = net.attachments.get(next_atts[0]) if next_atts else None
        if not nxt:
            return hops + [hop("next attachment", UNKNOWN, f"{next_atts} not in the inventory")], ("Transit Gateway", target)
        if nxt.get("ResourceType") in ("vpn", "direct-connect-gateway", "connect", "peering"):
            return hops + [hop("leaves through", PASS, f"{nxt.get('ResourceType')} {nxt.get('ResourceId')}")], (nxt.get("ResourceType"), nxt.get("ResourceId"))
        vpc = nxt.get("VpcId") or nxt.get("ResourceId")
        subnets = nxt.get("SubnetIds") or [s for s, obj in net.subnets.items() if obj.get("VpcId") == vpc][:1]
        if not subnets:
            return hops + [hop(f"into VPC {vpc}", UNKNOWN, "its subnets are not in the inventory")], ("Transit Gateway", target)
        hops.append(hop(f"into VPC {vpc} ({label(net.vpcs.get(vpc) or {}) or 'shared'}, account {nxt.get('_acct')})", PASS,
                        f"through attachment {nxt.get('TransitGatewayAttachmentId')}"))
        more, exit_ = follow_out(net, subnets[0], ip, depth + 1, seen)
        return hops + more, exit_
    if kind == "vpce":
        fw = next((f for f in net.firewalls.values()
                   if target in {(s.get("Attachment") or {}).get("EndpointId")
                                 for s in (((f.get("_details") or {}).get("Status") or {}).get("SyncStates") or {}).values()}),
                  None)
        hops = [hop(f"{where} routes to {'Network Firewall ' + label(fw) if fw else 'endpoint ' + target}", PASS, route_text(rtb, route))]
        if fw:
            hops.append(hop("inspected by Network Firewall", UNKNOWN, f"{label(fw)}: its rules are not evaluated"))
            fw_subnets = [m.get("SubnetId") for m in fw.get("SubnetMappings") or []]
            if fw_subnets:
                more, exit_ = follow_out(net, fw_subnets[0], ip, depth + 1, seen)
                return hops + more, exit_
        return hops, ("endpoint", target)
    if kind in ("pcx", "vgw", "eni", "eigw"):
        return [hop(f"{where} routes to {kind} {target}", PASS, route_text(rtb, route))], (kind, target)
    if kind == "blackhole":
        return [hop(f"{where} route", FAIL, f"{route_text(rtb, route)} (blackhole)")], None
    return [hop(f"{where} route to {ip}", FAIL, route_text(rtb, route))], None


def egress_paths(net, cfg, clusters):
    paths = []
    for c in clusters:
        sgs = c["security_groups"]
        for subnet_id in c["subnets"]:
            for port in cfg["egress_ports"]:
                hops = [hop(f"node security groups out on {port} to the internet", *sg_check(net, sgs, "outbound", "tcp", port, INTERNET_IP))]
                hops += nacl_both_ways(net, subnet_id, True, "tcp", port, INTERNET_IP, "node subnet")
                more, exit_ = follow_out(net, subnet_id, INTERNET_IP)
                hops += more
                paths.append({"path": "EKS nodes -> internet", "from": f"EKS {c['name']} subnet {subnet_id}", "port": port,
                              "exit": f"{exit_[0]} {exit_[1]}" if exit_ else None, "account": c["account"], "region": c["region"],
                              "hops": hops, "verdict": worst(h["verdict"] for h in hops) if exit_ else FAIL})
            rtb = net.rtb_of(subnet_id)
            for route in (rtb or {}).get("Routes") or []:
                kind, _ = target_of(route)
                dest = route.get("DestinationCidrBlock")
                if kind not in ("tgw", "vgw", "pcx") or dest in (None, "0.0.0.0/0"):
                    continue
                ip, port = address_in(dest), cfg["egress_ports"][0]
                hops = [hop(f"node security groups out on {port} to {dest}", *sg_check(net, sgs, "outbound", "tcp", port, ip))]
                hops += nacl_both_ways(net, subnet_id, True, "tcp", port, ip, "node subnet")
                more, exit_ = follow_out(net, subnet_id, ip)
                hops += more
                paths.append({"path": "EKS nodes -> private networks", "from": f"EKS {c['name']} subnet {subnet_id}", "to": dest,
                              "port": port, "exit": f"{exit_[0]} {exit_[1]}" if exit_ else None, "account": c["account"],
                              "region": c["region"], "hops": hops, "verdict": worst(h["verdict"] for h in hops) if exit_ else FAIL})
    return paths


def endpoint_checks(net, clusters):
    """AWS services the clusters reach through VPC endpoints, and whether they may."""
    out = []
    for c in clusters:
        rtbs = {(net.rtb_of(s) or {}).get("RouteTableId") for s in c["subnets"]}
        for ep in net.endpoints.values():
            if ep.get("VpcId") != c["vpc"]:
                continue
            service = re.sub(r"^com\.amazonaws\.(vpce\.)?[a-z0-9-]+\.", "", ep.get("ServiceName") or "")
            if ep.get("VpcEndpointType") == "Gateway":
                used = rtbs & set(ep.get("RouteTableIds") or [])
                out.append({"cluster": c["name"], "endpoint": ep.get("VpcEndpointId"), "service": service, "type": "Gateway",
                            "verdict": PASS if used else FAIL,
                            "evidence": f"in route tables {', '.join(sorted(used))}" if used else "not in the node subnets' route tables"})
            else:
                subnet_ip = net.subnet_ip(c["subnets"][0]) if c["subnets"] else None
                verdict, evidence = sg_check(net, [g.get("GroupId") for g in ep.get("Groups") or []], "inbound", "tcp", 443,
                                             subnet_ip, c["security_groups"])
                out.append({"cluster": c["name"], "endpoint": ep.get("VpcEndpointId"), "service": service,
                            "type": ep.get("VpcEndpointType"), "verdict": verdict, "evidence": evidence})
    return out


# ---------------------------------------------------------------------------- dns

def dns_analysis(net, cfg, app_vpcs, project_zone_ids):
    assoc_by_vpc, zones_by_vpc = collections.defaultdict(list), collections.defaultdict(list)
    for a in net.rule_assocs.values():
        assoc_by_vpc[a.get("VPCId")].append(a)
    for z in net.zones.values():
        for v in ((z.get("_details") or {}).get("Zone") or {}).get("VPCs") or []:
            zones_by_vpc[v.get("VPCId")].append(z)
    vpcs = []
    for vpc_id in sorted(app_vpcs):
        vpc = net.vpcs.get(vpc_id) or {}
        details = vpc.get("_details") or {}
        dhcp = net.dhcp.get(vpc.get("DhcpOptionsId")) or {}
        servers = [v.get("Value") for c in dhcp.get("DhcpConfigurations") or [] if c.get("Key") == "domain-name-servers"
                   for v in c.get("Values") or []]
        order = []
        for a in assoc_by_vpc[vpc_id]:
            rule = net.resolver_rules.get(a.get("ResolverRuleId"))
            if not rule:
                order.append({"domain": a.get("Name") or "?", "answered_by": f"Resolver rule {a.get('ResolverRuleId')} (not in the inventory)",
                              "verdict": UNKNOWN})
                continue
            domain = (rule.get("DomainName") or "").rstrip(".") or "."
            if rule.get("RuleType") == "FORWARD":
                targets = [f"{t.get('Ip')}:{t.get('Port', 53)}" for t in rule.get("TargetIps") or []]
                checks = forward_checks(net, net.resolver_eps.get(rule.get("ResolverEndpointId")), rule.get("TargetIps") or [])
                order.append({"domain": domain, "answered_by": f"forwarded to {', '.join(targets)} through outbound endpoint "
                                                               f"{rule.get('ResolverEndpointId')} (account {rule['_acct']})",
                              "rule": rule.get("Id"), "checks": checks, "verdict": worst(c["verdict"] for c in checks)})
            else:
                order.append({"domain": domain, "answered_by": f"Route 53 ({rule.get('RuleType', '').lower()} rule {rule.get('Id')})",
                              "verdict": PASS})
        for z in zones_by_vpc[vpc_id]:
            records = len((z.get("_details") or {}).get("Records") or [])
            order.append({"domain": (z.get("Name") or "").rstrip("."), "answered_by": f"private hosted zone {z.get('Id')} "
                                                                                    f"({records} records, account {z['_acct']})",
                          "zone": z.get("Id"), "verdict": PASS})
        # Most specific domain first; a forward rule wins over a private zone for the same domain.
        order.sort(key=lambda o: (-len(o["domain"].split(".")) if o["domain"] != "." else 1, "rule" not in o))
        order.append({"domain": "(anything else)", "answered_by": "public DNS through the Route 53 Resolver (VPC +2)", "verdict": PASS})
        vpcs.append({"vpc": vpc_id, "name": label(vpc), "account": vpc.get("_acct"),
                     "dns_support": details.get("EnableDnsSupport"), "dns_hostnames": details.get("EnableDnsHostnames"),
                     "dhcp_dns_servers": servers or ["AmazonProvidedDNS"], "resolution_order": order})
    inbound = []
    for ep in net.resolver_eps.values():
        if ep.get("Direction") != "INBOUND":
            continue
        sources = [r.get("peer") for sg in ep.get("SecurityGroupIds") or [] for r in ((net.sgs.get(sg) or {}).get("_rules") or {}).get("inbound") or []
                   if ports_cover(r.get("ports"), 53)]
        inbound.append({"endpoint": ep.get("Id"), "name": ep.get("Name"), "account": ep["_acct"],
                        "ips": [i.get("Ip") for i in (ep.get("_details") or {}).get("IpAddresses") or []],
                        "allowed_from": sources or ["(no port 53 rule found)"]})
    names = []
    for zone_id in sorted(project_zone_ids):
        zone = net.zones.get(zone_id)
        if not zone:
            continue
        private = bool((zone.get("Config") or {}).get("PrivateZone"))
        for rec in (zone.get("_details") or {}).get("Records") or []:
            if not isinstance(rec, dict) or rec.get("Type") not in ("A", "AAAA", "CNAME"):
                continue
            names.append(record_target(net, cfg, zone, private, rec))
    return {"vpcs": vpcs, "inbound_endpoints": inbound, "names": names}


def forward_checks(net, ep, targets):
    if not ep:
        return [hop("outbound endpoint in the inventory", UNKNOWN, "not found (sync the account that owns it)")]
    ips = (ep.get("_details") or {}).get("IpAddresses") or []
    if not ips:
        return [hop("outbound endpoint addresses", UNKNOWN, f"{ep.get('Id')}: no IP addresses known")]
    subnet = ips[0].get("SubnetId")
    checks = []
    for t in targets:
        ip, port = t.get("Ip"), int(t.get("Port") or 53)
        rtb = net.rtb_of(subnet)
        route = route_for(rtb, ip)
        kind, _ = target_of(route)
        checks.append(hop(f"endpoint subnet routes to {ip}", PASS if kind not in ("none", "blackhole") else FAIL, route_text(rtb, route)))
        checks.append(hop(f"endpoint security group out udp/{port} to {ip}",
                          *sg_check(net, ep.get("SecurityGroupIds") or [], "outbound", "udp", port, ip)))
        checks += nacl_both_ways(net, subnet, True, "udp", port, ip, "endpoint subnet")
    return checks


def record_target(net, cfg, zone, private, rec):
    # An answer's IP is looked up in the VPCs the private zone serves, then in the zone's account.
    vpcs = {v.get("VPCId") for v in ((zone.get("_details") or {}).get("Zone") or {}).get("VPCs") or []}
    name = rec.get("Name", "").replace("\\052", "*").rstrip(".")
    alias = ((rec.get("AliasTarget") or {}).get("DNSName") or "").lower().rstrip(".")
    values = [v.get("Value") for v in rec.get("ResourceRecords") or []]
    target, kind = None, None
    if alias:
        host = alias[len("dualstack."):] if alias.startswith("dualstack.") else alias
        lb = net.lb_by_dns.get(host)
        if lb:
            target, kind = f"load balancer {label(lb)} ({lb['_acct']}, {lb['_region']})", "load balancer"
        elif "cloudfront.net" in host:
            target, kind = f"CloudFront {host}", "cloudfront"
        elif "execute-api" in host:
            target, kind = f"API Gateway {host}", "api gateway"
        elif "vpce" in host:
            target, kind = f"VPC endpoint {host}", "endpoint"
        else:
            target, kind = f"{host} (not in the inventory)", "outside"
    elif rec.get("Type") == "CNAME" and values:
        host = values[0].lower().rstrip(".")
        lb = net.lb_by_dns.get(host)
        target, kind = (f"load balancer {label(lb)}", "load balancer") if lb else (f"CNAME {host}", "cname")
    elif values:
        owners = [eni_owner(net, eni, cfg) for eni in (net.eni_at(v, vpcs, zone.get("_acct")) for v in values) if eni]
        target, kind = (", ".join(f"{o[0]} {o[1]}" for o in owners), "address") if owners else (", ".join(values), "address")
    lookup = rec.get("_lookup") or {}
    seen = []
    for address in lookup.get("addresses") or []:
        eni = net.eni_at(address, vpcs, zone.get("_acct"))
        seen.append({"ip": address, "owner": " ".join(eni_owner(net, eni, cfg)) if eni else "not in the inventory"})
    note = None
    if lookup.get("status") in ("nxdomain", "timeout", "servfail", "no_answer"):
        note = f"nslookup: {lookup['status']}"
    elif kind == "load balancer" and seen and not any("load balancer" in s["owner"] for s in seen) and \
            any(s["owner"] != "not in the inventory" for s in seen):
        note = "nslookup answers point somewhere else than the record's load balancer"
    return {"name": name, "type": rec.get("Type"), "zone": zone.get("Name"), "private": private,
            "target": target, "nslookup": lookup.get("status"), "answers": seen, "note": note}


# ---------------------------------------------------------------------------- open, entry and exit points

def open_findings(net, cfg, apps, clusters, internet_lbs):
    out = []
    lb_sgs = {sg for lb in internet_lbs for sg in lb.get("SecurityGroups") or []}
    for sg in net.sgs.values():
        if sg["_acct"] not in apps:
            continue
        for r in (sg.get("_rules") or {}).get("inbound") or []:
            if r.get("peer") in ("0.0.0.0/0", "::/0"):
                expected = sg.get("GroupId") in lb_sgs and r.get("ports") in ("80", "443")
                out.append({"kind": "security group open to the internet" + (" (internet-facing load balancer)" if expected else ""),
                            "severity": "info" if expected else "high" if r.get("ports") in ("all", "22", "3389") else "medium",
                            "where": f"{sg.get('GroupId')} ({label(sg)})", "detail": f"{r.get('protocol')}/{r.get('ports')} from {r.get('peer')}",
                            "account": sg["_acct"], "region": sg["_region"]})
    for c in clusters:
        if c["endpoint_public"] and ("0.0.0.0/0" in c["public_access_cidrs"] or not c["public_access_cidrs"]):
            out.append({"kind": "EKS API endpoint open to the internet", "severity": "high", "where": c["name"],
                        "detail": f"public access from {', '.join(c['public_access_cidrs']) or '0.0.0.0/0'}",
                        "account": c["account"], "region": c["region"]})
    for i in net.instances.values():
        if i["_acct"] in apps and i.get("PublicIpAddress"):
            out.append({"kind": "instance with a public IP", "severity": "medium", "where": f"{i.get('InstanceId')} ({label(i)})",
                        "detail": i.get("PublicIpAddress"), "account": i["_acct"], "region": i["_region"]})
    for acl in net.nacls.values():
        if acl["_acct"] not in apps:
            continue
        rules = (acl.get("_rules") or {}).get("inbound") or []
        if rules and rules[0].get("action") == "allow" and rules[0].get("cidr") == "0.0.0.0/0" and rules[0].get("ports") == "all":
            out.append({"kind": "network ACL allows everything in", "severity": "low", "where": acl.get("NetworkAclId"),
                        "detail": f"rule {rules[0].get('rule')}", "account": acl["_acct"], "region": acl["_region"]})
    return sorted(out, key=lambda o: ("high", "medium", "low", "info").index(o["severity"]))


def entry_points(net, apps, clusters, paths):
    out = []
    for p in paths:
        if p["path"].startswith("internet"):
            out.append({"kind": "internet-facing load balancer", "name": p["entry"], "port": p["port"],
                        "reaches": p["reaches"], "verdict": p["verdict"], "account": p["account"], "region": p["region"]})
        elif p["path"].startswith("Transit"):
            out.append({"kind": "internal load balancer over the Transit Gateway", "name": p["entry"], "port": p["port"],
                        "from": p["from"], "reaches": p["reaches"], "verdict": p["verdict"], "account": p["account"], "region": p["region"]})
    for api in net.rest_apis.values():
        if api["_acct"] in apps:
            types = (api.get("endpointConfiguration") or {}).get("types") or []
            out.append({"kind": f"API Gateway ({', '.join(types) or '?'})", "name": api.get("name"), "account": api["_acct"], "region": api["_region"]})
    for api in net.http_apis.values():
        if api["_acct"] in apps:
            out.append({"kind": f"API Gateway {api.get('ProtocolType', '')}", "name": api.get("Name"), "account": api["_acct"], "region": api["_region"]})
    for d in net.distributions.values():
        if d["_acct"] in apps:
            origins = [o.get("DomainName") for o in (d.get("Origins") or {}).get("Items") or []]
            to = [label(net.lb_by_dns[o.lower()]) for o in origins if o and o.lower() in net.lb_by_dns]
            out.append({"kind": "CloudFront", "name": d.get("DomainName"), "origins": origins, "reaches": to,
                        "account": d["_acct"], "region": d["_region"]})
    for a in net.accelerators.values():
        if a["_acct"] in apps:
            out.append({"kind": "Global Accelerator", "name": a.get("Name"), "account": a["_acct"], "region": a["_region"]})
    for svc in net.endpoint_services.values():
        if svc["_acct"] in apps:
            out.append({"kind": "endpoint service (PrivateLink, others connect in)", "name": svc.get("ServiceName"),
                        "allowed": [p.get("Principal") for p in (svc.get("_details") or {}).get("AllowedPrincipals") or []],
                        "account": svc["_acct"], "region": svc["_region"]})
    for c in clusters:
        if c["endpoint_public"]:
            out.append({"kind": "EKS API endpoint (public)", "name": c["name"], "from": c["public_access_cidrs"] or ["0.0.0.0/0"],
                        "account": c["account"], "region": c["region"]})
    for vpn in net.vpns.values():
        out.append({"kind": "site-to-site VPN", "name": vpn.get("VpnConnectionId"), "account": vpn["_acct"], "region": vpn["_region"]})
    for vif in net.vifs.values():
        out.append({"kind": "Direct Connect", "name": vif.get("virtualInterfaceName") or vif.get("virtualInterfaceId"),
                    "account": vif["_acct"], "region": vif["_region"]})
    return out


def exit_points(paths, endpoints):
    out, seen = [], set()
    for p in paths:
        if p["path"].startswith("EKS nodes -> "):
            where = "the internet" if p["path"].endswith("internet") else p["to"]
            key = (where, p["exit"], p["account"])
            if key in seen:
                continue
            seen.add(key)
            out.append({"kind": f"to {where}", "through": p["exit"] or "no way out", "from": p["from"], "verdict": p["verdict"],
                        "account": p["account"], "region": p["region"]})
    for e in endpoints:
        out.append({"kind": f"to AWS {e['service']} (VPC endpoint, {e['type']})", "through": e["endpoint"],
                    "from": f"EKS {e['cluster']}", "verdict": e["verdict"], "evidence": e["evidence"]})
    return out


# ---------------------------------------------------------------------------- diagram

def tad_model(net, apps, shared, clusters, entries, exits, paths, dns):
    """Nodes and edges for a TAD: accounts > VPCs > subnets, with gateways, load balancers, clusters and DNS."""
    nodes, edges = {}, []

    def node(node_id, kind, text, parent=None, **extra):
        nodes.setdefault(node_id, {"id": node_id, "kind": kind, "label": text, "parent": parent, **extra})

    node("internet", "internet", "Internet")
    for acct, info in {**apps, **shared}.items():
        node(acct, "account", f"{info.get('profile') or acct} ({acct})", role="application" if acct in apps else "shared")
    vpcs_used = {c["vpc"] for c in clusters} | {lb.get("VpcId") for lb in net.lbs.values() if lb["_acct"] in apps}
    for p in paths:
        for h in p["hops"]:
            for vpc in re.findall(r"vpc-[0-9a-f]+", h["evidence"] + h["hop"]):
                vpcs_used.add(vpc)
    for vpc_id in sorted(v for v in vpcs_used if v in net.vpcs):
        vpc = net.vpcs[vpc_id]
        node(vpc_id, "vpc", f"{label(vpc)} {', '.join(net.vpc_cidrs(vpc_id))}", parent=vpc["_acct"])
        for s_id, s in net.subnets.items():
            if s.get("VpcId") == vpc_id:
                public = target_of(route_for(net.rtb_of(s_id), INTERNET_IP))[0] == "igw"
                node(s_id, "subnet", f"{label(s)} {s.get('CidrBlock')}", parent=vpc_id, public=public)
        for igw in net.igws.values():
            if any(a.get("VpcId") == vpc_id for a in igw.get("Attachments") or []):
                node(igw["InternetGatewayId"], "internet_gateway", "Internet gateway", parent=vpc_id)
                edges.append({"from": igw["InternetGatewayId"], "to": "internet", "kind": "route"})
        for nat in net.nats.values():
            if nat.get("VpcId") == vpc_id:
                node(nat["NatGatewayId"], "nat_gateway", f"NAT {nat.get('ConnectivityType', '')}", parent=nat.get("SubnetId"))
        att = net.vpc_attachment.get(vpc_id)
        if att:
            tgw = att.get("TransitGatewayId")
            node(tgw, "transit_gateway", f"Transit Gateway {tgw}", parent=(net.tgws.get(tgw) or {}).get("_acct"))
            edges.append({"from": vpc_id, "to": tgw, "kind": "attachment", "label": att.get("TransitGatewayAttachmentId")})
    for lb in net.lbs.values():
        if lb["_acct"] in apps or lb.get("VpcId") in nodes:
            node(lb["LoadBalancerArn"], "load_balancer", f"{lb.get('Type', '')} {label(lb)} ({lb.get('Scheme')})", parent=lb.get("VpcId"))
    for c in clusters:
        node(f"eks:{c['account']}:{c['name']}", "eks_cluster", f"EKS {c['name']}", parent=c["vpc"],
             endpoint="public" if c["endpoint_public"] else "private")
    for p in paths:
        if p["path"].startswith(("internet", "Transit")):
            source = "internet" if p["path"].startswith("internet") else p["from"]
            if source != "internet":
                node(source, "network", source)
            edges.append({"from": source, "to": p["lb"], "kind": "path", "label": f"tcp/{p['port']}", "verdict": p["verdict"]})
            for c in clusters:
                if any(c["name"] in r for r in p["reaches"]):
                    edges.append({"from": p["lb"], "to": f"eks:{c['account']}:{c['name']}", "kind": "path",
                                  "label": p.get("target_group") or "", "verdict": p["verdict"]})
        elif p["path"].startswith("EKS nodes -> "):
            cluster = next((c for c in clusters if f"EKS {c['name']} " in p["from"]), None)
            if cluster and p["exit"]:
                exit_id = p["exit"].split()[-1]
                node(exit_id, "exit", p["exit"])
                edges.append({"from": f"eks:{cluster['account']}:{cluster['name']}", "to": exit_id, "kind": "egress",
                              "label": f"tcp/{p['port']}", "verdict": p["verdict"]})
    for v in dns["vpcs"]:
        for o in v["resolution_order"]:
            if o.get("rule"):
                node(o["rule"], "resolver_rule", f"forward {o['domain']}")
                edges.append({"from": v["vpc"], "to": o["rule"], "kind": "dns", "label": o["domain"], "verdict": o["verdict"]})
            elif o.get("zone"):
                node(o["zone"], "hosted_zone", f"private zone {o['domain']}")
                edges.append({"from": v["vpc"], "to": o["zone"], "kind": "dns", "label": o["domain"]})
    for n in dns["names"]:
        if n["target"] and n["target"].startswith("load balancer "):
            lb = next((lb for lb in net.lbs.values() if n["target"].startswith(f"load balancer {label(lb)}")), None)
            if lb:
                node(f"dns:{n['name']}", "dns_name", n["name"])
                edges.append({"from": f"dns:{n['name']}", "to": lb["LoadBalancerArn"], "kind": "alias", "label": n["type"]})
    return {"nodes": list(nodes.values()), "edges": edges}


def mermaid(model):
    """The TAD model as a Mermaid flowchart (paste into any Mermaid viewer)."""
    safe = {}

    def ident(node_id):
        return safe.setdefault(node_id, f"n{len(safe)}")

    def text(s):
        return str(s).replace('"', "'")

    children = collections.defaultdict(list)
    for n in model["nodes"]:
        children[n.get("parent")].append(n)
    lines = ["flowchart LR"]
    containers = {"account", "vpc", "subnet"}

    def emit(parent, depth):
        for n in children.get(parent, []):
            pad = "  " * depth
            if n["kind"] in containers and children.get(n["id"]):
                lines.append(f'{pad}subgraph {ident(n["id"])}["{text(n["label"])}"]')
                emit(n["id"], depth + 1)
                lines.append(f"{pad}end")
            else:
                lines.append(f'{pad}{ident(n["id"])}["{text(n["label"])}"]')
                emit(n["id"], depth + 1)

    emit(None, 1)
    known = {n["id"] for n in model["nodes"]}
    for e in model["edges"]:
        if e["from"] in known and e["to"] in known:
            verdict = f" {e['verdict']}" if e.get("verdict") else ""
            label_ = text(f"{e.get('label', '')}{verdict}").strip()
            arrow = "-.->" if e["kind"] in ("dns", "alias") else "-->"
            lines.append(f'  {ident(e["from"])} {arrow}{"|" + label_ + "|" if label_ else ""} {ident(e["to"])}')
    styles = {"FAIL": "stroke:#d33,stroke-width:2px", "UNKNOWN": "stroke:#c90,stroke-dasharray:4", "PASS": "stroke:#2a2,stroke-width:2px"}
    for i, e in enumerate([e for e in model["edges"] if e["from"] in known and e["to"] in known]):
        if e.get("verdict") in styles:
            lines.append(f"  linkStyle {i} {styles[e['verdict']]}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------- run

def run(args):
    cfg = load_config()
    data = inv.load_accounts()
    out = (inv.HERE / data["settings"]["output_dir"]).resolve()
    db_path = out / data["settings"].get("db_file", "inventory.db")
    if not db_path.exists():
        sys.exit(f"No database yet ({db_path}); run aws_inventory.py first.")
    aws_db.update(db_path, out, quiet=True)  # catch up whatever the last syncs left behind
    db = aws_db.connect(db_path)
    remembered = inv.read_json(out / "_projects" / "last.json", {}) or {}

    keywords = args.keywords
    if not keywords:
        default = " ".join(remembered.get("keywords", []))
        answer = inv.ask(f"Project keywords (account names, tags, resource names){f' [{default}]' if default else ''}: ")
        keywords = (answer or default).split()
    keywords = [k.strip(",").lower() for k in keywords if k.strip(",")]
    if not keywords:
        sys.exit("Give at least one keyword, e.g. stablecoin.")
    shared_given = set(args.shared or []) | set(cfg["shared_accounts"]) | set(remembered.get("shared", []) if not args.shared else [])
    apps, shared = find_project(db, cfg, keywords, shared_given)
    if not args.yes:
        print(f"Shared-services accounts ({len(shared)}): " + (", ".join(f"{s['profile'] or a} ({a})" for a, s in shared.items()) or "none"))
        answer = inv.ask("  Enter to use these, or type the shared-services account numbers or profiles: ")
        if answer:
            tokens = [t for t in re.split(r"[\s,]+", answer) if t]
            shared_given = {t.replace("-", "") if re.fullmatch(r"[\d-]{12,14}", t) else t for t in tokens}
            apps, shared = find_project(db, {**cfg, "shared_words": []}, keywords, shared_given)
    if not apps:
        sys.exit(f"No account name matches {', '.join(keywords)}; `aws_inventory.py list` shows the saved accounts.")
    inv.write_json(out / "_projects" / "last.json", {"keywords": keywords, "shared": sorted(shared)})

    net = Net(db, list(apps) + list(shared))
    app_vpcs = {v for v, obj in net.vpcs.items() if obj["_acct"] in apps}
    linked = linked_shared(net, apps, app_vpcs, shared)
    hits = keyword_hits(db, keywords, list(shared), cfg["keyword_limit"])
    others = [a for (a,) in db.execute("SELECT account_id FROM accounts").fetchall() if a not in apps and a not in shared]
    elsewhere = keyword_hits(db, keywords, others, 50) if others else {}
    shared_resources = {}
    for (acct, region, type_, rid), why in {**hits, **linked}.items():
        shared_resources[(acct, region, type_, rid)] = why

    clusters = eks_summary(net, cfg, apps)
    project_lbs = [lb for lb in net.lbs.values() if lb["_acct"] in apps]
    paths = internet_to_lb_paths(net, cfg, project_lbs) + tgw_to_internal_lb_paths(net, cfg, project_lbs)
    out_paths = egress_paths(net, cfg, clusters)
    endpoints = endpoint_checks(net, clusters)
    zone_ids = {z_id for z_id, z in net.zones.items() if z["_acct"] in apps
                or (z["_acct"], z["_region"], "route53/hosted_zones", z["_rid"]) in shared_resources}
    dns = dns_analysis(net, cfg, app_vpcs, zone_ids)
    internet_lbs = [lb for lb in project_lbs if lb.get("Scheme") == "internet-facing"]
    findings = open_findings(net, cfg, apps, clusters, internet_lbs)
    entries = entry_points(net, apps, clusters, paths)
    exits = exit_points(out_paths, endpoints)

    blind = []
    for acct, info in {**apps, **shared}.items():
        status = collections.Counter(s for (s,) in db.execute(
            "SELECT status FROM type_status WHERE account_id=? AND status NOT IN ('ok', 'not_available')", (acct,)))
        if status:
            blind.append(f"{info['profile'] or acct}: " + ", ".join(f"{n} type(s) {s.replace('_', ' ')}" for s, n in status.items()))
    if not shared:
        blind.append("no shared-services account: Transit Gateway, egress VPC and Resolver paths stop at the project accounts")
    if elsewhere:
        accounts_ = sorted({a for a, *_ in elsewhere})
        blind.append(f"keywords also appear in {len(accounts_)} other account(s): " + ", ".join(accounts_[:5])
                     + " (not part of this report; add them with --shared if they serve the project)")
    blind.append("Kubernetes Services, Ingresses and NetworkPolicies live inside the clusters and are not in the inventory; "
                 "load balancers show where they reach AWS")
    blind.append("Network Firewall rules, IAM and resource policies, and on-premises networks are not evaluated")

    name = args.name or keywords[0]
    stamp = inv.utc_now().strftime("%Y%m%dT%H%M%SZ")
    report = {
        "project": name, "keywords": keywords, "generated": inv.iso(inv.utc_now()),
        "accounts": {
            "application": [{**info, "merged_file": str(next(iter(out.glob(f"*-{acct}.json")), ""))} for acct, info in apps.items()],
            "shared": [{**info, "resources_for_project": sum(1 for k in shared_resources if k[0] == acct)} for acct, info in shared.items()],
        },
        "eks": clusters, "entry_points": entries, "exit_points": exits, "open": findings, "dns": dns,
        "paths": paths + out_paths, "vpc_endpoints": endpoints, "blind_spots": blind,
        "shared_resources": [
            {"account": a, "region": r, "type": t, "id": i, "why": why,
             "data": json.loads((db.execute("SELECT data FROM resources WHERE account_id=? AND region=? AND type=? AND resource_id=?",
                                            (a, r, t, i)).fetchone() or ["null"])[0])}
            for (a, r, t, i), why in sorted(shared_resources.items())],
    }
    model = tad_model(net, apps, shared, clusters, entries, exits, paths + out_paths, dns)
    folder = out / "_projects"
    base = folder / f"{inv.safe_name(name)}-{stamp}"
    inv.write_json(base.with_suffix(".json"), report)
    inv.write_json(Path(f"{base}.tad.json"), model)
    Path(f"{base}.mmd").write_text(mermaid(model), encoding="utf-8")
    print_report(report, base, args.show)
    return 1 if any(p["verdict"] == FAIL for p in paths + out_paths) else 0


def print_report(r, base, show):
    apps, shared = r["accounts"]["application"], r["accounts"]["shared"]
    print(f"\nProject {r['project']} (keywords: {', '.join(r['keywords'])})")
    print("Application accounts: " + ", ".join(f"{a['profile']} ({a['account']})" for a in apps))
    print("Shared-services accounts: " + (", ".join(f"{s['profile']} ({s['account']}): {s['resources_for_project']} resource(s) for this project"
                                                   for s in shared) or "none"))
    for c in r["eks"]:
        access = ("public " + ", ".join(c["public_access_cidrs"] or ["0.0.0.0/0"])) if c["endpoint_public"] else "private only"
        print(f"EKS {c['name']} ({c['env'] or '?'}, {c['account']}, {c['region']}): API {access}, {len(c['nodegroups'])} node group(s), "
              f"{len(c['nodes'])} node(s), {len(c['subnets'])} subnet(s)")
    if r["blind_spots"]:
        print("\nBlind spots:")
        for line in r["blind_spots"]:
            print(f"  - {line}")

    def section(title, rows, line):
        if rows:
            print(f"\n{title} ({len(rows)}):")
            for row in rows[:show]:
                print("  " + line(row))
            if len(rows) > show:
                print(f"  ... {len(rows) - show} more in the report")

    section("Entry points", r["entry_points"], lambda e: f"{e.get('verdict', ''):8} {e['kind']}: {e['name']}"
            + (f" :{e['port']}" if e.get("port") else "") + (f" -> {', '.join(e['reaches'])}" if e.get("reaches") else ""))
    section("Exit points", r["exit_points"], lambda e: f"{e.get('verdict', ''):8} {e['kind']} through {e['through']} (from {e['from']})")
    failing = [p for p in r["paths"] if p["verdict"] != PASS]
    print(f"\nPaths: {len(r['paths'])} checked, {sum(p['verdict'] == PASS for p in r['paths'])} pass, "
          f"{sum(p['verdict'] == FAIL for p in r['paths'])} fail, {sum(p['verdict'] == UNKNOWN for p in r['paths'])} unknown")
    for p in failing[:show]:
        stop = next((h for h in p["hops"] + [h for t in p.get("targets", []) for h in t["hops"]] if h["verdict"] != PASS),
                    {"hop": "no way out", "evidence": "the route ends without an exit"})
        print(f"  {p['verdict']:8} {p['path']} ({p.get('entry') or p['from']}, port {p['port']}): {stop['hop']}: {stop['evidence']}")
    print("\nDNS:")
    for v in r["dns"]["vpcs"]:
        print(f"  {v['vpc']} ({v['name']}): DNS {'on' if v['dns_support'] is not False else 'OFF'}, servers {', '.join(v['dhcp_dns_servers'])}")
        for o in v["resolution_order"]:
            print(f"    {o['verdict']:8} {o['domain']:30} {o['answered_by']}")
            stop = next((c for c in o.get("checks") or [] if c["verdict"] != PASS), None)
            if stop:
                print(f"    {'':8} {'':30} {stop['hop']}: {stop['evidence']}")
    for e in r["dns"]["inbound_endpoints"]:
        print(f"  inbound endpoint {e['endpoint']} ({', '.join(e['ips'])}) allows from {', '.join(map(str, e['allowed_from']))}")
    names = r["dns"]["names"]
    if names:
        print(f"  {len(names)} record(s); " + f"{sum(1 for n in names if n['note'])} with a note:")
        for n in [n for n in names if n["note"]][:show] or names[:3]:
            answers = ", ".join(f"{a['ip']} = {a['owner']}" for a in n["answers"][:2])
            print(f"    {n['name']} -> {n['target']}" + (f" | nslookup {answers}" if answers else "") + (f" | {n['note']}" if n["note"] else ""))
    section("What's open", r["open"], lambda o: f"{o['severity']:6} {o['kind']}: {o['where']} {o['detail']}")
    print(f"\nReport: {base}.json\nTAD model: {base}.tad.json   Mermaid: {base}.mmd")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("keywords", nargs="*", help="words in the project's account names, tags or resource names")
    parser.add_argument("--shared", nargs="+", help="shared-services account numbers or profiles (also in aws_project.json)")
    parser.add_argument("--name", help="project name for the files (default: the first keyword)")
    parser.add_argument("-y", "--yes", action="store_true", help="ask nothing")
    parser.add_argument("--show", type=int, default=15, help="rows per section on screen (all are in the report)")
    args = parser.parse_args()
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
