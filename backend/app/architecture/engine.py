"""The analysis engine: trace each required flow through the supplied configuration.

Nothing here talks to AWS. Every result says what the supplied data shows, and a
check whose evidence is missing is UNKNOWN, never PASS. The output depends only on
the inputs, in a fixed order, so the same data and rules give the same result.
"""

import ipaddress
from dataclasses import dataclass, field

from app.architecture.models import (
    Check, Evidence, Finding, Hop, Inventory, Project, Requirement,
    RequirementResult, Rule, Workload, ExternalSystem,
)

MAX_HOPS = 16
EPHEMERAL = (1024, 65535)
ROLE_TITLES = {
    "application": "Application", "central-ingress": "Central Ingress", "central-egress": "Central Egress",
    "central-network": "Central Network", "shared-services": "Shared Services", "shared-service": "Shared Services",
    "dns": "DNS",
}
SECURITY = ("Security Group", "NACL", "Firewall")
NETWORK = ("Endpoint", "Routing", "TGW", "NAT", "Internet", "Load Balancer", "Direct Connect")


def net(value):
    return ipaddress.ip_network(value, strict=False)


def contains(outer, inner):
    try:
        outer, inner = net(outer), net(inner)
    except ValueError:
        return False
    return outer.version == inner.version and inner.subnet_of(outer)


PRIVATE = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "127.0.0.0/8", "fc00::/7")]


def is_private(network):
    """Addresses that can't be reached over the internet (RFC 1918 and friends)."""
    network = net(network)
    return any(network.version == p.version and network.subnet_of(p) for p in PRIVATE)


def headline(text):
    """The line of a multi-line result that explains it: the first failure, else the first line."""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return next((line for line in lines if line.startswith(("✗", "Violation"))), lines[0] if lines else "")


def role_title(role):
    return ROLE_TITLES.get(role, role.replace("-", " ").title() if role else "")


def flow_of(requirement):
    protocol = (requirement.protocol or "TCP").lower()
    port = requirement.port
    if protocol in ("https", "tls"):
        protocol, port = "tcp", port or 443
    elif protocol == "http":
        protocol, port = "tcp", port or 80
    return protocol, port


def describe_flow(protocol, port):
    return protocol.upper() + (f"/{port}" if port is not None else "")


def port_in(rule_from, rule_to, port):
    if port is None or rule_from is None or rule_from == -1:
        return True
    return rule_from <= port <= (rule_to if rule_to is not None else rule_from)


def protocol_in(rule_protocol, protocol):
    return rule_protocol in ("all", "-1") or rule_protocol == protocol


def worst(statuses):
    statuses = list(statuses)
    for status in ("FAIL", "UNKNOWN", "WARNING"):
        if status in statuses:
            return status
    return "PASS"


@dataclass
class Endpoint:
    """A resolved source or destination in one environment."""

    id: str
    name: str
    kind: str  # aws, on-prem, third-party
    role: str = ""
    account: str = ""
    vpc: str = ""
    subnets: list = field(default_factory=list)
    security_groups: list = field(default_factory=list)
    load_balancer: str = ""
    hostname: str = ""
    networks: list = field(default_factory=list)  # CIDRs traffic is addressed to
    system: object = None
    problems: list = field(default_factory=list)  # Checks raised while resolving

    def summary(self):
        return {
            "id": self.id, "name": self.name, "kind": self.kind, "role": self.role, "account": self.account,
            "vpc": self.vpc, "subnets": self.subnets, "security_groups": self.security_groups,
            "load_balancer": self.load_balancer, "hostname": self.hostname, "networks": self.networks,
        }


@dataclass
class Trace:
    checks: list = field(default_factory=list)
    hops: list = field(default_factory=list)
    reached: bool = False
    stopped: str = ""  # UNKNOWN or FAIL when the trace could not continue
    nat_ips: list = field(default_factory=list)
    nat_subnet: str = ""
    firewalls: list = field(default_factory=list)
    final_vpc: str = ""
    final_subnet: str = ""
    exit: str = ""  # local, internet, on-prem


class Analyzer:
    def __init__(self, inventory: Inventory, project: Project, services, externals, rules):
        self.inv = inventory
        self.project = project
        self.workloads = {w.id: w for w in services}
        self.workloads.update({w.id: w for w in project.applications})
        self.externals = {e.id: e for e in externals}
        self.rules = [r for r in rules if r.enabled]
        self._index()

    # Indexes over the inventory

    def _index(self):
        inv = self.inv
        self.subnet_table, self.main_table = {}, {}
        for table in sorted(inv.route_tables.values(), key=lambda t: t.id):
            for subnet in table.subnets:
                self.subnet_table[subnet] = table
            if table.main:
                self.main_table[table.vpc] = table
        self.subnet_nacl, self.default_nacl = {}, {}
        for nacl in sorted(inv.nacls.values(), key=lambda n: n.id):
            for subnet in nacl.subnets:
                self.subnet_nacl[subnet] = nacl
            if nacl.default:
                self.default_nacl[nacl.vpc] = nacl
        self.vpc_attachments = {}
        for attachment in sorted(inv.tgw_attachments.values(), key=lambda a: a.id):
            if attachment.type == "vpc":
                self.vpc_attachments.setdefault((attachment.tgw, attachment.resource), attachment)
        self.endpoint_firewall = {}
        for firewall in inv.firewalls.values():
            for endpoint in firewall.endpoints:
                self.endpoint_firewall[endpoint] = firewall
        self.lb_by_name = {lb.name: lb for lb in inv.load_balancers.values()}

    def route_table(self, subnet_id):
        subnet = self.inv.subnets.get(subnet_id)
        if subnet_id in self.subnet_table:
            return self.subnet_table[subnet_id]
        return self.main_table.get(subnet.vpc) if subnet else None

    def nacl(self, subnet_id):
        subnet = self.inv.subnets.get(subnet_id)
        if subnet_id in self.subnet_nacl:
            return self.subnet_nacl[subnet_id]
        return self.default_nacl.get(subnet.vpc) if subnet else None

    def denied(self, account, *keys):
        """Did this account's download lack permission for any of these resource types?"""
        return bool(set(keys) & set(self.inv.access_denied.get(account, [])))

    def missing_status(self, account, *keys):
        """Not in the snapshot: FAIL, unless we simply weren't allowed to read it."""
        return "UNKNOWN" if self.denied(account, *keys) else "FAIL"

    def vpc_role(self, vpc_id):
        vpc = self.inv.vpcs.get(vpc_id)
        return vpc.role if vpc else ""

    def account_name(self, account_id):
        account = self.inv.accounts.get(account_id)
        return account.name if account and account.name else account_id

    # Endpoints

    def resolve(self, endpoint_id, environment, as_destination, requirement):
        if endpoint_id in self.workloads:
            return self._resolve_workload(self.workloads[endpoint_id], environment, as_destination, requirement)
        if endpoint_id in self.externals:
            return self._resolve_external(self.externals[endpoint_id], requirement)
        endpoint = Endpoint(id=endpoint_id, name=endpoint_id, kind="unknown")
        endpoint.problems.append(Check(
            id="endpoint", category="Endpoint", name=f"{endpoint_id} is defined", status="UNKNOWN",
            expected=f"An application, service or external system with id {endpoint_id}.",
            actual="No definition found in the project, services or external systems.",
        ))
        return endpoint

    def _resolve_workload(self, workload: Workload, environment, as_destination, requirement):
        placement = workload.placement.get(environment) or workload.placement.get("*")
        endpoint = Endpoint(id=workload.id, name=workload.name or workload.id, kind="aws", role=workload.kind)
        label = endpoint.name
        if not placement:
            endpoint.problems.append(Check(
                id=f"placement-{workload.id}", category="Endpoint", name=f"{label} placement in {environment}",
                status="UNKNOWN", expected=f"Account, VPC and subnets for {environment}.",
                actual=f"{label} has no placement for {environment}.",
            ))
            return endpoint
        endpoint.account, endpoint.vpc = placement.account, placement.vpc
        endpoint.subnets, endpoint.security_groups = list(placement.subnets), list(placement.security_groups)
        endpoint.load_balancer, endpoint.hostname = placement.load_balancer, placement.hostname
        evidence, missing = [], []
        for subnet_id in endpoint.subnets:
            subnet = self.inv.subnets.get(subnet_id)
            if subnet:
                evidence.append(Evidence(kind="subnets", id=subnet_id, label=subnet.cidr))
                endpoint.vpc = endpoint.vpc or subnet.vpc
                endpoint.account = endpoint.account or subnet.account
            else:
                missing.append(subnet_id)
        if endpoint.vpc in self.inv.vpcs:
            evidence.insert(0, Evidence(kind="vpcs", id=endpoint.vpc, label=", ".join(self.inv.vpcs[endpoint.vpc].cidrs)))
        account_known = endpoint.account in self.inv.accounts
        if not endpoint.subnets:
            status, actual = "UNKNOWN", "No subnets are defined for this placement."
        elif missing and not account_known:
            status, actual = "UNKNOWN", f"Account {endpoint.account or '?'} is not in the imported snapshot."
        elif missing and self.denied(endpoint.account, "Subnets"):
            status, actual = "UNKNOWN", f"No read access to subnets in {endpoint.account}, so {', '.join(missing)} can't be confirmed."
        elif missing:
            status, actual = "FAIL", f"Not found in the snapshot: {', '.join(missing)}."
        else:
            status = "PASS"
            actual = f"{len(endpoint.subnets)} subnet(s) in {endpoint.vpc} ({self.account_name(endpoint.account)})."
        endpoint.problems.append(Check(
            id=f"exists-{workload.id}", category="Endpoint", name=f"{label} exists", status=status,
            expected=f"Subnets {', '.join(endpoint.subnets) or '(none)'} in account {endpoint.account or '?'}.",
            actual=actual, evidence=evidence,
        ))
        bound = self.project.environments.get(environment)
        if workload.kind == "application" and bound and endpoint.account and bound.account_id != endpoint.account:
            endpoint.problems.append(Check(
                id=f"account-{workload.id}", category="Endpoint", name=f"{label} account matches {environment}",
                status="WARNING", expected=f"Account {bound.account_id} ({environment} binding).",
                actual=f"Placed in {endpoint.account}.", evidence=[Evidence(kind="accounts", id=endpoint.account)],
            ))
        subnets = [self.inv.subnets[s] for s in endpoint.subnets if s in self.inv.subnets]
        if as_destination and endpoint.load_balancer and requirement.direction in ("inbound", "bidirectional"):
            lb = self.find_lb(endpoint.load_balancer)
            if lb:
                subnets = [self.inv.subnets[s] for s in lb.subnets if s in self.inv.subnets] or subnets
        endpoint.networks = [subnets[0].cidr] if subnets else []
        return endpoint

    def _resolve_external(self, system: ExternalSystem, requirement):
        endpoint = Endpoint(id=system.id, name=system.name or system.id, kind=system.type, role=system.type, system=system)
        if system.type == "on-prem":
            endpoint.networks = list(system.networks)
            endpoint.hostname = requirement.hostname
            if not endpoint.networks:
                endpoint.problems.append(Check(
                    id=f"networks-{system.id}", category="External", name=f"{endpoint.name} networks supplied",
                    status="UNKNOWN", expected="On-premises CIDRs for the destination.",
                    actual="No networks are defined for this on-premises system.",
                    evidence=[Evidence(kind="external", id=system.id)],
                ))
            return endpoint
        protocol, port = flow_of(requirement)
        matching = [e for e in system.endpoints if e.port in (None, port)] or system.endpoints
        endpoint.hostname = requirement.hostname or next((e.hostname for e in matching if e.hostname), "") or (system.domains[0] if system.domains else "")
        ips = [e.ip for e in matching if e.ip]
        endpoint.networks = [f"{ip}/32" if "/" not in ip else ip for ip in dict.fromkeys(ips)]
        if not endpoint.networks:
            endpoint.networks = ["0.0.0.0/0"]  # somewhere on the internet: only default routes match
            endpoint.problems.append(Check(
                id=f"addresses-{system.id}", category="External", name=f"{endpoint.name} addresses supplied",
                status="UNKNOWN", expected="IP addresses for the third-party endpoint.",
                actual="No IP addresses were supplied, so IP-based firewall rules and routing to a specific address cannot be confirmed.",
                evidence=[Evidence(kind="external", id=system.id)],
            ))
        return endpoint

    def find_lb(self, reference):
        return self.inv.load_balancers.get(reference) or self.lb_by_name.get(reference)

    # Routing

    def best_route(self, routes, destination):
        """Longest prefix that contains the whole destination; prefix lists can't be expanded."""
        best, unknown = None, []
        for route in routes:
            if route.destination.startswith("pl-"):
                unknown.append(route)
                continue
            if contains(route.destination, destination):
                if best is None or net(route.destination).prefixlen > net(best.destination).prefixlen:
                    best = route
        return best, unknown

    def trace(self, start_subnet, destination, dst: Endpoint, flow, label="", return_leg=False):
        """Follow routes from a subnet towards a destination network."""
        t = Trace()
        inv = self.inv
        category = (lambda c: "Return Path") if return_leg else (lambda c: c)
        prefix = "Return: " if return_leg else ""

        def add(cat, name, status, expected="", actual="", evidence=(), check_id=None, blocking=True, detail=""):
            t.checks.append(Check(
                id=check_id or f"{'ret-' if return_leg else ''}{cat.lower().replace(' ', '-')}-{len(t.checks)}",
                category=category(cat), name=prefix + name + (f" ({label})" if label else ""), status=status,
                expected=expected, actual=actual, evidence=list(evidence), blocking=blocking, detail=detail,
            ))

        subnet = inv.subnets.get(start_subnet)
        if not subnet:
            add("Routing", "Start subnet known", "UNKNOWN", start_subnet, "Subnet not in the snapshot.")
            t.stopped = "UNKNOWN"
            return t
        vpc_id, came_from_tgw, visited = subnet.vpc, False, set()
        for _ in range(MAX_HOPS):
            vpc = inv.vpcs.get(vpc_id)
            # Arrived in the VPC that holds the destination: local routing delivers it.
            if vpc and any(contains(c, destination) for c in vpc.cidrs) and came_from_tgw:
                add("Routing", f"Delivered inside {vpc_id}", "PASS", f"{destination} within {vpc_id}",
                    f"{vpc_id} ({', '.join(vpc.cidrs)}) contains {destination}; VPC local route delivers it.",
                    [Evidence(kind="vpcs", id=vpc_id)])
                t.reached, t.exit, t.final_vpc, t.final_subnet = True, "local", vpc_id, subnet.id
                return t
            key = (subnet.id, vpc_id)
            if key in visited:
                add("Routing", "No routing loop", "FAIL", "A path that ends at the destination.",
                    f"Routing returns to {subnet.id} in {vpc_id}: loop.", [Evidence(kind="subnets", id=subnet.id)])
                t.stopped = "FAIL"
                return t
            visited.add(key)
            table = self.route_table(subnet.id)
            if not table:
                add("Routing", f"Route table for {subnet.id}", "UNKNOWN", "An associated or main route table.",
                    "No route table for this subnet in the snapshot.", [Evidence(kind="subnets", id=subnet.id)])
                t.stopped = "UNKNOWN"
                return t
            route, unresolved = self.best_route(table.routes, destination)
            if not route:
                if unresolved:
                    add("Routing", f"Route to {destination}", "UNKNOWN", f"A route covering {destination} in {table.id}.",
                        f"Only prefix-list routes might match ({', '.join(r.destination for r in unresolved)}); prefix list contents were not supplied.",
                        [Evidence(kind="route_tables", id=table.id)])
                    t.stopped = "UNKNOWN"
                else:
                    add("Routing", f"Route to {destination}", "FAIL", f"A route covering {destination} in {table.id}.",
                        f"No route in {table.id} matches {destination}. Routes: "
                        + ", ".join(f"{r.destination} → {r.target}" for r in table.routes),
                        [Evidence(kind="route_tables", id=table.id), Evidence(kind="subnets", id=subnet.id)])
                    t.stopped = "FAIL"
                return t
            if route.state != "active":
                add("Routing", f"Route to {destination}", "FAIL", f"An active route to {destination}.",
                    f"{route.destination} → {route.target or '(none)'} in {table.id} is {route.state}.",
                    [Evidence(kind="route_tables", id=table.id)])
                t.stopped = "FAIL"
                return t
            add("Routing", f"Route {route.destination} → {route.target}", "PASS",
                f"A route covering {destination} from {subnet.id}.",
                f"{table.id}: {route.destination} → {route.target} ({route.target_type}).",
                [Evidence(kind="route_tables", id=table.id)])
            kind = route.target_type
            if kind == "local":
                if vpc and any(contains(c, destination) for c in vpc.cidrs):
                    t.reached, t.exit, t.final_vpc, t.final_subnet = True, "local", vpc_id, subnet.id
                    return t
                add("Routing", "Destination in local VPC", "FAIL", f"{destination} inside {vpc_id}.",
                    "The local route matched but the VPC CIDRs don't contain the destination.", [Evidence(kind="vpcs", id=vpc_id)])
                t.stopped = "FAIL"
                return t
            if kind == "transit-gateway":
                result = self._tgw_hop(t, add, route.target, vpc_id, destination, dst, return_leg)
                if result is None:
                    return t
                if result == "exit":
                    return t
                vpc_id, subnet = result
                came_from_tgw = True
                continue
            came_from_tgw = False
            if kind == "vpc-endpoint":
                firewall = self.endpoint_firewall.get(route.target)
                if not firewall:
                    endpoint = inv.vpc_endpoints.get(route.target)
                    add("Firewall", f"Inspection at {route.target}", "UNKNOWN", "Firewall or appliance rules for this endpoint.",
                        "The route targets a VPC endpoint whose firewall/appliance rules were not supplied."
                        + (f" Endpoint type {endpoint.type}, service {endpoint.service}." if endpoint else ""),
                        [Evidence(kind="vpc_endpoints", id=route.target)] if endpoint else [])
                    t.stopped = "UNKNOWN"
                    return t
                t.hops.append(Hop(id=firewall.id, label=f"Firewall {firewall.name}", kind="firewall", role=self.vpc_role(firewall.vpc)))
                t.firewalls.append(firewall.id)
                if not return_leg:
                    t.checks.append(self.firewall_check(firewall, flow, dst))
                next_subnet = next((s for s in firewall.subnets if s in inv.subnets), None)
                if not next_subnet:
                    add("Firewall", f"Firewall {firewall.name} subnet", "UNKNOWN", "The firewall's subnet and its route table.",
                        "Firewall subnets are not in the snapshot.", [Evidence(kind="firewalls", id=firewall.id)])
                    t.stopped = "UNKNOWN"
                    return t
                subnet = inv.subnets[next_subnet]
                vpc_id = subnet.vpc
                continue
            if kind == "nat-gateway":
                nat = inv.nat_gateways.get(route.target)
                if not nat or nat.state != "available":
                    status = "FAIL" if nat else self.missing_status(vpc.account if vpc else "", "NatGateways")
                    add("NAT", f"NAT gateway {route.target}", status, "An available NAT gateway.",
                        ("No read access to NAT gateways." if status == "UNKNOWN" else "Not in the snapshot.") if not nat else f"State is {nat.state}.",
                        [Evidence(kind="nat_gateways", id=route.target)] if nat else [])
                    t.stopped = status
                    return t
                add("NAT", f"NAT gateway {nat.id}", "PASS", "An available NAT gateway.",
                    f"{nat.id} in {nat.subnet}, public IP {', '.join(nat.public_ips) or '(not supplied)'}.",
                    [Evidence(kind="nat_gateways", id=nat.id)])
                t.hops.append(Hop(id=nat.id, label="NAT Gateway", kind="nat-gateway", role=self.vpc_role(nat.vpc)))
                t.nat_ips, t.nat_subnet = list(nat.public_ips), nat.subnet
                if nat.subnet not in inv.subnets:
                    add("NAT", "NAT subnet route table", "UNKNOWN", "The NAT gateway's subnet.", "Not in the snapshot.")
                    t.stopped = "UNKNOWN"
                    return t
                subnet = inv.subnets[nat.subnet]
                vpc_id = subnet.vpc
                continue
            if kind == "internet-gateway":
                igw = inv.internet_gateways.get(route.target)
                if not igw or vpc_id not in igw.vpcs:
                    status = "FAIL" if igw else self.missing_status(vpc.account if vpc else "", "InternetGateways")
                    add("Internet", f"Internet gateway {route.target}", status, f"An internet gateway attached to {vpc_id}.",
                        ("No read access to internet gateways." if status == "UNKNOWN" else "Not in the snapshot.") if not igw else f"Attached to {', '.join(igw.vpcs) or 'nothing'}.",
                        [Evidence(kind="internet_gateways", id=route.target)] if igw else [])
                    t.stopped = status
                    return t
                t.hops.append(Hop(id=igw.id, label="Internet Gateway", kind="internet-gateway", role=self.vpc_role(vpc_id)))
                if is_private(destination) and destination != "0.0.0.0/0":
                    add("Internet", "Destination is reachable on the internet", "FAIL", "A public destination.",
                        f"{destination} is a private address but is routed to the internet gateway.",
                        [Evidence(kind="internet_gateways", id=igw.id)])
                    t.stopped = "FAIL"
                    return t
                if not t.nat_ips and not subnet.public and not return_leg:
                    add("Internet", "Address translation before the internet", "WARNING", "A NAT gateway, or a public subnet with public IPs.",
                        f"{subnet.id} reaches {igw.id} without NAT and does not assign public IPs.",
                        [Evidence(kind="subnets", id=subnet.id)])
                add("Internet", f"Internet gateway {igw.id}", "PASS", f"An internet gateway attached to {vpc_id}.",
                    f"{igw.id} attached to {vpc_id}.", [Evidence(kind="internet_gateways", id=igw.id)])
                t.hops.append(Hop(id="internet", label="Internet", kind="internet"))
                t.reached, t.exit, t.final_vpc = True, "internet", vpc_id
                return t
            if kind == "virtual-gateway":
                t.hops.append(Hop(id=route.target, label="VPN / Direct Connect (VGW)", kind="direct-connect"))
                return self._exit_on_prem(t, add, destination, dst, route.target)
            if kind == "peering":
                peer = next((v for v in sorted(inv.vpcs.values(), key=lambda v: v.id) if v.id != vpc_id and any(contains(c, destination) for c in v.cidrs)), None)
                if not peer:
                    add("Routing", f"Peering {route.target}", "UNKNOWN", "The peer VPC holding the destination.",
                        "No VPC in the snapshot contains the destination.")
                    t.stopped = "UNKNOWN"
                    return t
                t.hops.append(Hop(id=route.target, label="VPC Peering", kind="peering"))
                add("Routing", f"Peering {route.target} to {peer.id}", "PASS", "A peer VPC containing the destination.",
                    f"{peer.id} ({', '.join(peer.cidrs)}).", [Evidence(kind="vpcs", id=peer.id)])
                t.reached, t.exit, t.final_vpc = True, "local", peer.id
                return t
            add("Routing", f"Route target {route.target}", "UNKNOWN", "A target the analyser understands.",
                f"Target type {kind} is not analysed in this version.", [Evidence(kind="route_tables", id=table.id)])
            t.stopped = "UNKNOWN"
            return t
        add("Routing", "Path length", "FAIL", f"At most {MAX_HOPS} hops.", "The path is too long; check for loops.")
        t.stopped = "FAIL"
        return t

    def _tgw_hop(self, t, add, tgw_id, vpc_id, destination, dst, return_leg):
        inv = self.inv
        attachment = self.vpc_attachments.get((tgw_id, vpc_id))
        if tgw_id not in inv.transit_gateways and not attachment:
            add("TGW", f"Transit gateway {tgw_id}", "UNKNOWN", "The transit gateway's attachments and route tables.",
                f"{tgw_id} is not in the snapshot (central network account not imported?).")
            t.stopped = "UNKNOWN"
            return None
        if not attachment:
            add("TGW", f"{vpc_id} attached to {tgw_id}", "FAIL", f"A VPC attachment for {vpc_id} on {tgw_id}.",
                "No attachment found; the route points at a transit gateway this VPC is not attached to.",
                [Evidence(kind="transit_gateways", id=tgw_id)])
            t.stopped = "FAIL"
            return None
        if attachment.state != "available":
            add("TGW", f"Attachment {attachment.id}", "FAIL", "State available.", f"State is {attachment.state}.",
                [Evidence(kind="tgw_attachments", id=attachment.id)])
            t.stopped = "FAIL"
            return None
        add("TGW", f"Attachment {attachment.id}", "PASS", f"{vpc_id} attached to {tgw_id}.",
            f"{attachment.id} is available.", [Evidence(kind="tgw_attachments", id=attachment.id)])
        t.hops.append(Hop(id=tgw_id, label="Transit Gateway", kind="tgw", role="central-network"))
        table = inv.tgw_route_tables.get(attachment.route_table)
        if not table:
            status = "FAIL" if not attachment.route_table else "UNKNOWN"
            add("TGW", f"TGW route table for {attachment.id}", status, "An associated TGW route table.",
                "The attachment is not associated with a route table." if status == "FAIL"
                else f"{attachment.route_table} is not in the snapshot.",
                [Evidence(kind="tgw_attachments", id=attachment.id)])
            t.stopped = status
            return None
        route, _ = self.best_route(table.routes, destination)
        if not route:
            add("TGW", f"TGW route to {destination}", "FAIL", f"A route covering {destination} in {table.id}.",
                f"No route in {table.name or table.id} matches {destination}. Routes: "
                + (", ".join(f"{r.destination} → {r.attachment or 'blackhole'}" for r in table.routes) or "(none)"),
                [Evidence(kind="tgw_route_tables", id=table.id)])
            t.stopped = "FAIL"
            return None
        if route.state == "blackhole" or not route.attachment:
            add("TGW", f"TGW route to {destination}", "FAIL", f"An active route to {destination}.",
                f"{route.destination} in {table.name or table.id} is a blackhole.", [Evidence(kind="tgw_route_tables", id=table.id)])
            t.stopped = "FAIL"
            return None
        target = inv.tgw_attachments.get(route.attachment)
        if not target:
            add("TGW", f"TGW route to {destination}", "UNKNOWN", "The target attachment.",
                f"{route.attachment} is not in the snapshot.", [Evidence(kind="tgw_route_tables", id=table.id)])
            t.stopped = "UNKNOWN"
            return None
        add("TGW", f"TGW route {route.destination} → {target.id}", "PASS", f"A route covering {destination} in {table.name or table.id}.",
            f"{route.destination} → {target.id} ({target.type} {target.resource}, {route.type}).",
            [Evidence(kind="tgw_route_tables", id=table.id), Evidence(kind="tgw_attachments", id=target.id)])
        if target.type in ("direct-connect-gateway", "vpn"):
            t.hops.append(Hop(id=target.id, label="Direct Connect" if target.type != "vpn" else "Site-to-site VPN", kind="direct-connect", role="central-network"))
            return "exit" if self._exit_on_prem(t, add, destination, dst, target.id) else None
        if target.type != "vpc":
            add("TGW", f"Attachment type {target.type}", "UNKNOWN", "A VPC, Direct Connect or VPN attachment.",
                f"{target.type} attachments ({target.id}) are not followed in this version.", [Evidence(kind="tgw_attachments", id=target.id)])
            t.stopped = "UNKNOWN"
            return None
        if target.resource == vpc_id:
            add("TGW", "TGW sends traffic onwards", "FAIL", "A different VPC.", f"{target.id} sends traffic back to {vpc_id}.",
                [Evidence(kind="tgw_route_tables", id=table.id)])
            t.stopped = "FAIL"
            return None
        next_vpc = inv.vpcs.get(target.resource)
        subnet = next((inv.subnets[s] for s in target.subnets if s in inv.subnets), None)
        if not next_vpc or not subnet:
            add("TGW", f"VPC {target.resource}", "UNKNOWN", "The attached VPC and its attachment subnets.",
                f"{target.resource} or its subnets are not in the snapshot.", [Evidence(kind="tgw_attachments", id=target.id)])
            t.stopped = "UNKNOWN"
            return None
        role = next_vpc.role
        if not any(contains(c, destination) for c in next_vpc.cidrs):
            title = role_title(role) or next_vpc.id
            t.hops.append(Hop(id=next_vpc.id, label=f"{title} VPC", kind="vpc", role=role))
            add("TGW", f"{title} reached", "PASS", f"Traffic for {destination} handed to the next VPC.",
                f"{next_vpc.id} in {self.account_name(next_vpc.account)} ({role or 'no role'}).", [Evidence(kind="vpcs", id=next_vpc.id)])
        return next_vpc.id, subnet

    def _exit_on_prem(self, t, add, destination, dst, via):
        system = dst.system if dst and dst.kind == "on-prem" else None
        if not system:
            add("Direct Connect", "Destination is on-premises", "FAIL", "An on-premises destination for this route.",
                f"{destination} is routed to {via} (on-premises), but the destination is not an on-premises system.")
            t.stopped = "FAIL"
            return False
        allowed = (system.connectivity or {}).get("attachments")
        if allowed and via not in allowed:
            add("Direct Connect", f"{system.name} connectivity", "FAIL", f"One of {', '.join(allowed)}.",
                f"Traffic leaves via {via}.", [Evidence(kind="external", id=system.id)])
            t.stopped = "FAIL"
            return False
        add("Direct Connect", f"Leaves AWS for {system.name}", "PASS",
            f"{destination} reachable via {(system.connectivity or {}).get('type', 'hybrid connectivity')}.",
            f"Routed to {via}.", [Evidence(kind="tgw_attachments", id=via), Evidence(kind="external", id=system.id)])
        t.reached, t.exit = True, "on-prem"
        return True

    # Security

    def firewall_check(self, firewall, flow, dst):
        protocol, port, source, destination, hostname = flow
        expected = (f"Source {source}, destination {dst.name} ({destination}"
                    + (f", {hostname}" if hostname else "") + f"), {describe_flow(protocol, port)}, action PASS.")
        evidence = [Evidence(kind="firewalls", id=firewall.id, label=firewall.name)] + [
            Evidence(kind="firewall_rule_groups", id=g.id, label=g.name) for g in firewall.rule_groups
        ]
        check = Check(id=f"fw-{firewall.name}", category="Firewall", name=f"Firewall {firewall.name} allows the flow",
                      status="UNKNOWN", expected=expected, evidence=evidence)
        if not firewall.rule_data:
            check.actual = "The firewall policy or some rule groups were not supplied, so the rules can't be evaluated."
            return check
        rules = [(g, r) for g in firewall.rule_groups for r in g.rules]
        if firewall.rule_order != "STRICT_ORDER":
            order = {"pass": 0, "drop": 1, "reject": 2, "alert": 3}
            rules.sort(key=lambda gr: order.get(gr[1].action, 4))
        undecided = []
        for group, rule in rules:
            if rule.action == "alert":
                continue
            match = self._fw_match(rule, protocol, port, source, destination, hostname)
            if match is None:
                undecided.append(f"{group.name}{(' sid ' + rule.sid) if rule.sid else ''}")
                continue
            if match:
                where = f"{group.name}" + (f" sid {rule.sid}" if rule.sid else "") + (f" (domains {', '.join(rule.domains)})" if rule.domains else "")
                if rule.action == "pass":
                    check.status, check.actual = "PASS", f"Allowed by {where}."
                else:
                    check.status, check.actual = "FAIL", f"Explicitly denied ({rule.action.upper()}) by {where}."
                return check
        groups = ", ".join(g.name for g in firewall.rule_groups) or "(none)"
        if undecided:
            check.actual = (f"No rule definitely matches; {', '.join(undecided)} may match but "
                            "use domain rules or variables the supplied data can't resolve (give the requirement a hostname).")
            return check
        drops = [a for a in firewall.default_actions if "drop" in a or "reject" in a]
        if drops:
            check.status = "FAIL"
            check.actual = f"No matching rule found. Default action {', '.join(drops)} drops it. Rule groups checked: {groups}."
        else:
            check.status = "WARNING"
            check.actual = f"No matching rule; passed only because the policy has no default drop. Rule groups checked: {groups}."
        return check

    def _fw_match(self, rule, protocol, port, source, destination, hostname):
        """True, False, or None when the supplied data can't decide."""
        rule_protocol = rule.protocol
        if rule_protocol in ("tls", "http", "https"):
            if protocol != "tcp" or (rule.destination_port not in ("any", str(port))):
                return False
            if rule.domains:
                if not hostname:
                    return None
                return any(hostname == d.lstrip(".") or (d.startswith(".") and hostname.endswith(d)) for d in rule.domains)
        elif rule_protocol not in ("ip", "any", protocol):
            return False
        if not self._fw_port(rule.destination_port, port):
            return False
        for values, address in ((rule.source, source), (rule.destination, destination)):
            if "any" in values:
                continue
            if any(v.startswith("$") for v in values):
                return None
            if not any(contains(v, address) for v in values):
                return False
        return True

    def _fw_port(self, spec, port):
        spec = str(spec).strip("[] ").lower()
        if spec in ("any", "") or port is None:
            return True
        for part in spec.split(","):
            part = part.strip()
            if ":" in part:
                low, high = part.split(":")
                if int(low or 0) <= port <= int(high or 65535):
                    return True
            elif part.isdigit() and int(part) == port:
                return True
        return False

    def sg_check(self, groups, direction, other, protocol, port, other_groups, label, check_id):
        """Does any of the groups allow traffic to (outbound) or from (inbound) `other`?"""
        name = f"{label} security group {'egress' if direction == 'outbound' else 'ingress'}"
        what = f"{'to' if direction == 'outbound' else 'from'} {other}, {describe_flow(protocol, port)}"
        check = Check(id=check_id, category="Security Group", name=name, status="UNKNOWN",
                      expected=f"A rule allowing {what}.")
        if not groups:
            check.actual = "No security groups are defined for this workload."
            return check
        missing = [g for g in groups if g not in self.inv.security_groups]
        check.evidence = [Evidence(kind="security_groups", id=g, label=self.inv.security_groups[g].name)
                          for g in groups if g in self.inv.security_groups]
        maybe = []
        for group_id in groups:
            group = self.inv.security_groups.get(group_id)
            if not group:
                continue
            for rule in group.inbound if direction == "inbound" else group.outbound:
                if not protocol_in(rule.protocol, protocol) or not port_in(rule.from_port, rule.to_port, port):
                    continue
                hit = next((c for c in rule.cidrs if contains(c, other)), None)
                if hit:
                    check.status, check.actual = "PASS", f"{group.id} ({group.name}) allows {rule.protocol} {self._ports(rule)} {'to' if direction == 'outbound' else 'from'} {hit}."
                    return check
                ref = next((g for g in rule.groups if g in other_groups), None)
                if ref:
                    check.status, check.actual = "PASS", f"{group.id} ({group.name}) allows {rule.protocol} {self._ports(rule)} via security group reference {ref}."
                    return check
                if rule.prefix_lists:
                    maybe.append(f"{group.id} → {', '.join(rule.prefix_lists)}")
        if maybe:
            check.actual = f"Only prefix-list rules could match ({'; '.join(maybe)}); prefix list contents were not supplied."
        elif missing and len(missing) == len(groups):
            check.actual = f"Security groups {', '.join(missing)} are not in the snapshot."
        else:
            check.status = "FAIL"
            check.actual = f"No rule in {', '.join(groups)} allows {what}. Security groups deny by default."
            if missing:
                check.status = "UNKNOWN"
                check.actual += f" Not supplied: {', '.join(missing)}."
        return check

    @staticmethod
    def _ports(rule):
        if rule.from_port in (None, -1):
            return "all ports"
        return str(rule.from_port) if rule.from_port == rule.to_port else f"{rule.from_port}-{rule.to_port}"

    def nacl_check(self, subnet_id, direction, other, protocol, ports, label, check_id, blocking=True):
        """NACLs are stateless: evaluated in rule order, first match wins, then the * deny."""
        low, high = ports if isinstance(ports, tuple) else (ports, ports)
        port_text = "all" if low is None else (str(low) if low == high else f"{low}-{high}")
        name = f"{label} NACL {direction}"
        check = Check(id=check_id, category="NACL", name=name, status="UNKNOWN", blocking=blocking,
                      expected=f"A rule allowing {protocol.upper()} {port_text} {'to' if direction == 'outbound' else 'from'} {other}.")
        nacl = self.nacl(subnet_id)
        if not nacl:
            check.actual = f"No network ACL for {subnet_id} in the snapshot."
            return check
        check.evidence = [Evidence(kind="nacls", id=nacl.id)]
        for rule in sorted(nacl.inbound if direction == "inbound" else nacl.outbound, key=lambda r: r.number):
            if rule.number >= 32767:
                continue
            if not protocol_in(rule.protocol, protocol) or not contains(rule.cidr, other):
                continue
            if low is not None and rule.from_port is not None and not (rule.from_port <= low and high <= rule.to_port):
                continue
            if rule.action == "allow":
                check.status, check.actual = "PASS", f"{nacl.id} rule #{rule.number} allows {rule.protocol} {rule.cidr}."
            else:
                check.status, check.actual = "FAIL", f"Explicit deny: {nacl.id} rule #{rule.number} denies {rule.protocol} {rule.cidr}."
            return check
        check.status, check.actual = "FAIL", f"No rule in {nacl.id} matches; the default * rule denies it."
        return check

    # DNS

    def dns_check(self, hostname, vpc_id, dst):
        hostname = hostname.lower().rstrip(".")
        check = Check(id="dns", category="DNS", name=f"Resolve {hostname}", status="UNKNOWN",
                      expected=f"{hostname} resolvable from {vpc_id}.")
        vpc = self.inv.vpcs.get(vpc_id)
        if not vpc:
            check.actual = "Source VPC not in the snapshot."
            return check
        if vpc.dns_support is False:
            check.status, check.actual = "FAIL", f"DNS resolution (enableDnsSupport) is disabled on {vpc_id}."
            check.evidence = [Evidence(kind="vpcs", id=vpc_id)]
            return check

        def suffix(domain):
            domain = domain.lower().rstrip(".")
            return hostname == domain or hostname.endswith("." + domain)

        rules = [r for r in self.inv.resolver_rules.values() if suffix(r.domain) and r.type in ("FORWARD", "SYSTEM")]
        zones = [z for z in self.inv.hosted_zones.values() if z.private and suffix(z.name)]
        options = [("rule", r.domain, r) for r in rules if vpc_id in r.vpcs] + [("zone", z.name, z) for z in zones if vpc_id in z.vpcs]
        lines = [f"Source VPC {vpc_id}"]
        if options:
            options.sort(key=lambda o: (-len(o[1]), o[0] != "rule", o[2].id))
            kind, domain, item = options[0]
            if kind == "rule":
                check.evidence = [Evidence(kind="resolver_rules", id=item.id, label=item.domain)]
                lines.append(f"✓ Resolver rule {item.id} for {item.domain} is associated with {vpc_id}")
                if item.type == "SYSTEM":
                    check.status = "PASS"
                    lines.append("✓ SYSTEM rule: Route 53 Resolver answers it")
                elif item.endpoint and item.endpoint not in self.inv.resolver_endpoints:
                    lines.append(f"? Outbound endpoint {item.endpoint} not in the snapshot")
                else:
                    if item.endpoint:
                        check.evidence.append(Evidence(kind="resolver_endpoints", id=item.endpoint))
                        lines.append(f"✓ Outbound endpoint {item.endpoint}")
                    lines.append(f"✓ Forwards to {', '.join(item.target_ips) or '(no target IPs)'}")
                    check.status = "PASS" if item.target_ips else "FAIL"
                check.actual = "\n".join(lines)
                return check
            check.evidence = [Evidence(kind="hosted_zones", id=item.id, label=item.name)]
            lines.append(f"✓ Private hosted zone {item.name} ({item.id}) is associated with {vpc_id}")
            record = next((r for r in item.records if r.name == hostname or (r.name.startswith("*.") and hostname.endswith(r.name[1:]))), None)
            if record:
                lines.append(f"✓ Record {record.name} {record.type} → {', '.join(record.values) or record.alias}")
                check.status = "PASS"
            else:
                lines.append(f"✗ No record for {hostname}: the private zone answers NXDOMAIN")
                check.status = "FAIL"
            check.actual = "\n".join(lines)
            return check
        if not (rules or zones) and any(k in v for v in self.inv.access_denied.values() for k in ("HostedZones", "ResolverRules", "ResolverRuleAssociations")):
            lines.append("? No read access to some hosted zones or resolver rules, so this name can't be traced")
            check.actual = "\n".join(lines)
            return check
        unassociated = [r for r in rules if vpc_id not in r.vpcs] + [z for z in zones if vpc_id not in z.vpcs]
        internal = dst and dst.kind == "on-prem" or any(suffix(d) for e in self.externals.values() if e.type == "on-prem" for d in e.dns_domains)
        if unassociated or internal:
            for item in unassociated:
                label = f"Resolver rule {item.id} for {item.domain}" if hasattr(item, "domain") else f"Private hosted zone {item.name} ({item.id})"
                lines.append(f"✓ {label} exists")
                lines.append(f"✗ {vpc_id} is not associated with it")
                check.evidence.append(Evidence(kind="resolver_rules" if hasattr(item, "domain") else "hosted_zones", id=item.id))
            if not unassociated:
                lines.append("✗ No resolver rule or private zone covers this internal name")
            check.status = "FAIL"
            check.actual = "\n".join(lines)
            return check
        declared = dst and dst.system and dst.kind == "third-party" and (
            any(suffix(d) for d in dst.system.domains) or any(e.hostname == hostname for e in dst.system.endpoints))
        if declared:
            check.status = "PASS"
            check.evidence = [Evidence(kind="external", id=dst.system.id), Evidence(kind="vpcs", id=vpc_id)]
            lines.append("✓ No private zone or forwarding rule overrides it")
            lines.append(f"✓ Public name declared by {dst.system.name}; Amazon-provided DNS resolves public names")
            check.actual = "\n".join(lines)
            return check
        check.blocking = False
        lines.append("? Public DNS is outside the supplied data")
        check.actual = "\n".join(lines)
        return check

    def lookup_check(self, hostname):
        """What nslookup said for this name when the data was downloaded. Advisory: it ran on
        the downloading computer, whose DNS view can differ from the VPC's."""
        lookup = self.inv.dns_lookups.get(hostname.lower().rstrip("."))
        if not lookup:
            return None
        status = {"resolved": "PASS", "not_found": "WARNING"}.get(lookup.status, "UNKNOWN")
        text = {"resolved": ", ".join(lookup.ips), "timeout": f"Timed out. {lookup.detail}", "not_tried": lookup.detail,
                "not_found": lookup.detail}.get(lookup.status, lookup.detail)
        return Check(id="nslookup", category="DNS", name=f"nslookup {hostname}", status=status, blocking=False,
                     expected="The name resolves from the computer that downloaded the data.",
                     actual=f"{text} (server {lookup.server or 'unknown'}, {lookup.looked_up_at})")

    # Requirements

    def analyse(self, requirement: Requirement, environment):
        if requirement.hops:
            from app.architecture.flows import analyse_flow

            return analyse_flow(self, requirement, environment)
        protocol, port = flow_of(requirement)
        flows = [(requirement.source, requirement.destination)]
        if requirement.direction == "bidirectional":
            flows.append((requirement.destination, requirement.source))
        results = [self._analyse_flow(requirement, environment, s, d, protocol, port, len(flows) > 1) for s, d in flows]
        checks = [c for r in results for c in r["checks"]]
        status = self.overall(checks)
        first = results[0]
        return RequirementResult(
            requirement=requirement, environment=environment, status=status, summary=self.summarize(status, checks),
            source=first["source"], destination=first["destination"], checks=checks, path=first["path"],
            return_path=first["return_path"],
        )

    @staticmethod
    def overall(checks):
        blocking = [c.status for c in checks if c.blocking]
        if "FAIL" in blocking:
            return "FAIL"
        if "UNKNOWN" in blocking:
            return "UNKNOWN"
        if any(c.status == "WARNING" for c in checks):
            return "WARNING"
        return "PASS"

    @staticmethod
    def summarize(status, checks):
        if status == "PASS":
            unknown = [c for c in checks if c.status == "UNKNOWN"]
            return "Based on the supplied configuration, this connectivity should work." + (
                f" {len(unknown)} advisory check(s) could not be confirmed." if unknown else "")
        wanted = {"FAIL": "FAIL", "UNKNOWN": "UNKNOWN", "WARNING": "WARNING"}[status]
        check = next(c for c in checks if c.status == wanted and (c.blocking or status == "WARNING"))
        return f"{check.name}: {headline(check.actual) or check.status}"

    def _analyse_flow(self, requirement, environment, source_id, destination_id, protocol, port, both_ways):
        src = self.resolve(source_id, environment, False, requirement)
        dst = self.resolve(destination_id, environment, True, requirement)
        checks = list(src.problems) + list(dst.problems)
        tag = f"{src.name} → {dst.name}" if both_ways else ""
        path = [Hop(id=src.id, label=src.name, kind="workload" if src.kind == "aws" else "external", role=src.role)]
        return_hops, reached = [], False
        if src.kind != "aws":
            checks.append(Check(
                id="source-aws", category="Endpoint", name=f"{src.name} is in AWS", status="UNKNOWN",
                expected="Flows that start outside AWS are traced from where they enter AWS.",
                actual=f"{src.name} is {src.kind}; model the entry point (e.g. Central Ingress) as the source.",
            ))
        elif not any(c.status in ("FAIL", "UNKNOWN") and c.category == "Endpoint" for c in src.problems) and dst.networks:
            destination = dst.networks[0]
            hostname = requirement.hostname or dst.hostname
            src_subnets = [self.inv.subnets[s] for s in src.subnets if s in self.inv.subnets]
            source_net = src_subnets[0].cidr
            flow = (protocol, port, source_net, destination, hostname)
            if hostname:
                checks.append(self.dns_check(hostname, src.vpc, dst))
                checks.extend(filter(None, [self.lookup_check(hostname)]))
            primary = None
            for index, subnet in enumerate(src_subnets):
                for network in dst.networks:
                    leg = self.trace(subnet.id, network, dst, (protocol, port, subnet.cidr, network, hostname),
                                     label=(network if len(dst.networks) > 1 else ""))
                    if primary is None:
                        primary = leg
                        checks.extend(leg.checks)
                        path.extend(leg.hops)
                    elif worst(c.status for c in leg.checks) != worst(c.status for c in primary.checks):
                        bad = next((c for c in leg.checks if c.status != "PASS"), None)
                        checks.append(Check(
                            id=f"subnet-{subnet.id}-{network}", category="Routing", name=f"Same path from {subnet.id} ({subnet.az})",
                            status=bad.status if bad else "WARNING", expected="Every source subnet routes the same way.",
                            actual=f"{bad.name}: {bad.actual}" if bad else "Differs from the first subnet.",
                            evidence=[Evidence(kind="subnets", id=subnet.id)],
                        ))
            reached = bool(primary and primary.reached)
            if reached:
                checks.extend(self._security(requirement, src, dst, source_net, destination, protocol, port, primary))
                ret_checks, return_hops = self._return_path(src, dst, source_net, destination, primary, flow)
                checks.extend(ret_checks)
            if dst.kind == "aws" and dst.load_balancer and requirement.direction in ("inbound", "bidirectional"):
                lb = self.find_lb(dst.load_balancer)
                path.append(Hop(id=lb.id if lb else dst.load_balancer, label=f"Load Balancer {lb.name if lb else dst.load_balancer}", kind="load-balancer", role="application"))
        path.append(Hop(id=dst.id, label=dst.name, kind="workload" if dst.kind == "aws" else "external", role=dst.role))
        checks.extend(self._standards(requirement, environment, src, dst, path, reached))
        if tag:
            for check in checks:
                check.name = f"{tag}: {check.name}"
                check.id = f"{source_id}>{check.id}"
        return {"source": src.summary(), "destination": dst.summary(), "checks": checks, "path": path, "return_path": return_hops}

    def _security(self, requirement, src, dst, source_net, destination, protocol, port, trace):
        checks = []
        src_subnet = src.subnets[0]
        dst_groups = list(dst.security_groups)
        if dst.kind == "aws" and dst.load_balancer and requirement.direction in ("inbound", "bidirectional"):
            checks.extend(self._load_balancer(requirement, src, dst, source_net, protocol, port))
            lb = self.find_lb(dst.load_balancer)
            dst_groups = (lb.security_groups if lb else []) or dst_groups
        checks.append(self.sg_check(src.security_groups, "outbound", destination, protocol, port, dst_groups, src.name, "sg-src"))
        same_subnet = dst.kind == "aws" and trace.final_subnet == src_subnet and dst.subnets and dst.subnets[0] == src_subnet
        if not same_subnet:
            checks.append(self.nacl_check(src_subnet, "outbound", destination, protocol, port, src.name, "nacl-src-out"))
            if protocol != "icmp":
                checks.append(self.nacl_check(src_subnet, "inbound", destination, protocol, EPHEMERAL, src.name + " return", "nacl-src-in"))
        if dst.kind == "aws":
            lb = self.find_lb(dst.load_balancer) if dst.load_balancer and requirement.direction in ("inbound", "bidirectional") else None
            if not lb:
                checks.append(self.sg_check(dst.security_groups, "inbound", source_net, protocol, port, src.security_groups, dst.name, "sg-dst"))
            dst_subnet = (lb.subnets[0] if lb and lb.subnets else (dst.subnets[0] if dst.subnets else ""))
            if dst_subnet and not same_subnet:
                checks.append(self.nacl_check(dst_subnet, "inbound", source_net, protocol, port, dst.name, "nacl-dst-in"))
                if protocol != "icmp":
                    checks.append(self.nacl_check(dst_subnet, "outbound", source_net, protocol, EPHEMERAL, dst.name + " return", "nacl-dst-out"))
        return checks

    def _load_balancer(self, requirement, src, dst, source_net, protocol, port):
        checks = []
        lb = self.find_lb(dst.load_balancer)
        if not lb:
            status = self.missing_status(dst.account, "LoadBalancers")
            checks.append(Check(id="lb", category="Load Balancer", name=f"Load balancer {dst.load_balancer}", status=status,
                                expected=f"Load balancer {dst.load_balancer} in front of {dst.name}.",
                                actual="No read access to load balancers." if status == "UNKNOWN" else "Not in the snapshot."))
            return checks
        evidence = [Evidence(kind="load_balancers", id=lb.id, label=lb.name)]
        checks.append(Check(id="lb", category="Load Balancer", name=f"Load balancer {lb.name}", status="PASS",
                            expected=f"A load balancer in front of {dst.name}.", actual=f"{lb.type} {lb.scheme} in {lb.vpc}.", evidence=evidence))
        listener = next((l for l in lb.listeners if l.port == port), None)
        if not listener:
            status = self.missing_status(lb.account, "Listeners")
            checks.append(Check(id="lb-listener", category="Load Balancer", name=f"Listener on {port}", status=status,
                                expected=f"A listener on port {port}.",
                                actual="No read access to listeners." if status == "UNKNOWN" else f"No listener on {port}. Listeners: {', '.join(f'{l.protocol}/{l.port}' for l in lb.listeners) or '(none)'}.",
                                evidence=evidence))
            return checks
        checks.append(Check(id="lb-listener", category="Load Balancer", name=f"Listener on {port}", status="PASS",
                            expected=f"A listener on port {port}.", actual=f"{listener.protocol}/{listener.port}.", evidence=evidence))
        groups = [self.inv.target_groups[g] for g in listener.target_groups if g in self.inv.target_groups]
        if not groups:
            checks.append(Check(id="lb-target-group", category="Load Balancer", name="Target group", status="FAIL" if not listener.target_groups else "UNKNOWN",
                                expected="The listener forwards to a target group.",
                                actual="The listener has no forward action." if not listener.target_groups else "Target groups not in the snapshot.",
                                evidence=evidence))
            return checks
        group = groups[0]
        target_port = requirement.target_port
        status = "PASS" if target_port in (None, group.port) else "FAIL"
        checks.append(Check(id="lb-target-group", category="Load Balancer", name=f"Target group {group.name}", status=status,
                            expected=f"Forwards to {dst.name}" + (f" on port {target_port}." if target_port else "."),
                            actual=f"{group.protocol}/{group.port}, target type {group.target_type or '?'}.",
                            evidence=[Evidence(kind="target_groups", id=group.id, label=group.name)]))
        lb_cidrs = [self.inv.subnets[s].cidr for s in lb.subnets if s in self.inv.subnets]
        if lb.security_groups:
            checks.append(self.sg_check(lb.security_groups, "inbound", source_net, protocol, port, src.security_groups, f"Load balancer {lb.name}", "sg-lb"))
        target = self.sg_check(dst.security_groups, "inbound", lb_cidrs[0] if lb_cidrs else "0.0.0.0/32", protocol, group.port or port,
                               lb.security_groups, dst.name + " (from load balancer)", "sg-target")
        checks.append(target)
        return checks

    def _return_path(self, src, dst, source_net, destination, trace, flow):
        checks, hops = [], []
        if dst.kind == "aws":
            dst_subnet = trace.final_subnet if trace.exit == "local" and trace.final_subnet in (dst.subnets or []) else (dst.subnets[0] if dst.subnets else "")
            if dst.load_balancer:
                lb = self.find_lb(dst.load_balancer)
                if lb and lb.subnets:
                    dst_subnet = lb.subnets[0]
            back = self.trace(dst_subnet, source_net, None, flow, return_leg=True)
            hops = back.hops
            checks.append(self._return_summary(back, f"{dst.name} → {src.name}"))
            return checks, hops
        if dst.kind == "on-prem":
            system = dst.system
            dx = next((h.id for h in reversed(trace.hops) if h.kind == "direct-connect"), "")
            attachment = self.inv.tgw_attachments.get(dx)
            if attachment:
                table = self.inv.tgw_route_tables.get(attachment.route_table)
                route, _ = self.best_route(table.routes, source_net) if table else (None, [])
                if table and route and route.state == "active" and route.attachment:
                    target = self.inv.tgw_attachments.get(route.attachment)
                    ok = target and target.resource == src.vpc
                    checks.append(Check(
                        id="ret-tgw", category="Return Path", name="Return: TGW route from Direct Connect",
                        status="PASS" if ok else "WARNING", expected=f"{table.name or table.id} routes {source_net} back to {src.vpc}.",
                        actual=f"{route.destination} → {route.attachment}" + ("" if ok else " (not the source VPC's attachment: asymmetric?)"),
                        evidence=[Evidence(kind="tgw_route_tables", id=table.id)],
                    ))
                else:
                    checks.append(Check(
                        id="ret-tgw", category="Return Path", name="Return: TGW route from Direct Connect",
                        status="FAIL" if table else "UNKNOWN", expected=f"A route for {source_net} in the Direct Connect attachment's TGW route table.",
                        actual=(f"No active route for {source_net} in {table.name or table.id}." if table else "The Direct Connect attachment's route table is not in the snapshot."),
                        evidence=[Evidence(kind="tgw_attachments", id=attachment.id)],
                    ))
            if system.return_routes is None:
                checks.append(Check(
                    id="ret-external", category="Return Path", name=f"Return: {system.name} routes back to AWS", status="UNKNOWN",
                    blocking=False, expected=f"{system.name} routes {source_net} towards AWS.",
                    actual="External return-path information was not supplied.", evidence=[Evidence(kind="external", id=system.id)],
                ))
            else:
                covered = next((r for r in system.return_routes if contains(r, source_net)), None)
                checks.append(Check(
                    id="ret-external", category="Return Path", name=f"Return: {system.name} routes back to AWS",
                    status="PASS" if covered else "FAIL", expected=f"{system.name} routes {source_net} towards AWS.",
                    actual=f"Covered by advertised route {covered}." if covered else f"Return routes {', '.join(system.return_routes) or '(none)'} don't cover {source_net}.",
                    evidence=[Evidence(kind="external", id=system.id)],
                ))
            return checks, hops
        # Third party over the internet: replies come back to the NAT's public IP.
        system = dst.system
        if trace.nat_subnet:
            back = self.trace(trace.nat_subnet, source_net, None, flow, return_leg=True)
            hops = back.hops
            summary = self._return_summary(back, f"NAT → {src.name}")
            if summary.status == "PASS" and trace.firewalls and not back.firewalls:
                summary.status = "WARNING"
                summary.actual += "\nReturn traffic skips the firewall that inspected the outbound flow (asymmetric inspection)."
            checks.append(summary)
        if system and system.allowlist is not None and trace.nat_ips:
            allowed = [ip for ip in trace.nat_ips if any(contains(a, ip + "/32") for a in system.allowlist)]
            checks.append(Check(
                id="ext-allowlist", category="External", name=f"{system.name} allowlists our egress IPs",
                status="PASS" if len(allowed) == len(trace.nat_ips) else "FAIL",
                expected=f"Allowlist contains {', '.join(trace.nat_ips)}.", actual=f"Allowlist: {', '.join(system.allowlist) or '(empty)'}.",
                evidence=[Evidence(kind="external", id=system.id)],
            ))
        else:
            checks.append(Check(
                id="ext-allowlist", category="External", name=f"{dst.name} allowlists our egress IPs", status="UNKNOWN", blocking=False,
                expected=f"The third party allows {', '.join(trace.nat_ips) or 'our egress IPs'}.",
                actual="The third party's allowlist was not supplied." if trace.nat_ips else "No NAT on the path, so the egress IP is unknown.",
                evidence=[Evidence(kind="external", id=system.id)] if system else [],
            ))
        checks.append(Check(
            id="ret-external", category="Return Path", name=f"Return: {dst.name} back to our egress IP", status="UNKNOWN", blocking=False,
            expected="Replies routed from the third party to our public egress address.",
            actual="External return-path information was not supplied.",
        ))
        return checks, hops

    def _return_summary(self, back, label):
        bad = next((c for c in back.checks if c.status in ("FAIL", "UNKNOWN")), None)
        status = "PASS" if back.reached else (bad.status if bad else "UNKNOWN")
        steps = "\n".join(f"{'✓' if c.status == 'PASS' else '✗' if c.status == 'FAIL' else '?'} {c.name.replace('Return: ', '')}: {c.actual}" for c in back.checks)
        evidence = [e for c in back.checks for e in c.evidence]
        return Check(
            id="ret-route", category="Return Path", name=f"Return route {label}", status=status, blocking=status == "FAIL",
            expected="Replies route back to the source.", actual=steps or "No return routing data.", evidence=evidence[:8],
        )

    # Standards

    def _standards(self, requirement, environment, src, dst, path, reached):
        checks = []
        tokens = [self._tokens(h, i, len(path)) for i, h in enumerate(path)]
        detected = " → ".join(self._hop_title(h) for h in path)
        facts = {
            "environment": [environment], "direction": [requirement.direction],
            "source_kind": [src.role or src.kind], "destination_kind": [dst.role or dst.kind],
            "destination_type": ["aws" if dst.kind == "aws" else dst.kind], "source_type": ["aws" if src.kind == "aws" else src.kind],
        }
        for rule in sorted(self.rules, key=lambda r: r.id):
            if rule.type != "connectivity":
                continue
            if rule.direction and rule.direction != requirement.direction:
                continue
            if any(not set(values) & set(facts.get(key, [])) for key, values in rule.conditions.items()):
                continue
            problems = []
            position = 0
            for wanted in rule.required_path:
                while position < len(tokens) and not self._token_match(wanted, tokens[position]):
                    position += 1
                if position == len(tokens):
                    problems.append(f"missing {wanted}")
                    break
                position += 1
            for banned in rule.forbidden_path:
                hit = next((path[i] for i, t in enumerate(tokens) if self._token_match(banned, t)), None)
                if hit:
                    problems.append(f"uses {self._hop_title(hit)} ({banned})")
            # A path that stopped early can't prove a component is missing.
            status = "PASS" if not problems else ("FAIL" if reached or any("uses" in p for p in problems) else "UNKNOWN")
            checks.append(Check(
                id=f"rule-{rule.id}", category="Standards", name=rule.name, status=status, rule=rule.id,
                expected=" → ".join(rule.required_path) if rule.required_path else "No forbidden components.",
                actual=f"Detected: {detected}" + (f"\nViolation: {'; '.join(problems)}." if problems else ""),
                detail=rule.description, blocking=rule.severity in ("critical", "high") or status != "FAIL",
            ))
        return checks

    def _tokens(self, hop, index, count):
        values = {hop.kind}
        if hop.role:
            values.add(hop.role)
            values.add(f"{hop.kind}@{hop.role}")
        if index == 0:
            values.add("source")
        if index == count - 1:
            values.add("destination")
        if hop.kind == "workload" and hop.role == "application":
            values.add("application")
        if hop.kind == "external":
            values.add("external")
        return values

    @staticmethod
    def _token_match(wanted, tokens):
        return wanted in tokens

    def _hop_title(self, hop):
        if hop.kind == "vpc":
            return role_title(hop.role) or hop.label
        return hop.label


def findings(results):
    """Critical findings for failures, warnings for warnings and unknowns; numbered in a fixed order."""
    out, seen = [], set()
    failures = sorted((r for r in results if r.status == "FAIL"), key=lambda r: (r.environment, r.requirement.id))
    for index, result in enumerate(failures, 1):
        requirement = result.requirement
        check = next(c for c in result.checks if c.status == "FAIL" and c.blocking)
        protocol, port = flow_of(requirement)
        out.append(Finding(
            id=f"F-{index:03d}", severity="critical" if requirement.required else "high",
            title=f"{result.environment} {result.source['name']} cannot reach {result.destination['name']} over {describe_flow(protocol, port)}.",
            cause=f"{check.name}: {headline(check.actual)}",
            environment=result.environment, requirement=requirement.id, check=check.id, rule=check.rule,
        ))
    number = 0
    for result in sorted(results, key=lambda r: (r.environment, r.requirement.id)):
        for check in result.checks:
            if check.status not in ("WARNING", "UNKNOWN"):
                continue
            key = (result.environment, check.category, check.actual)
            if key in seen:
                continue
            seen.add(key)
            number += 1
            out.append(Finding(
                id=f"W-{number:03d}", severity="warning" if check.status == "WARNING" or check.blocking else "info",
                title=f"{check.name} ({result.requirement.id})" if check.status == "WARNING" else f"{check.name} cannot be validated ({result.requirement.id})",
                cause=headline(check.actual) or check.status,
                environment=result.environment, requirement=result.requirement.id, check=check.id, rule=check.rule,
            ))
    return out


def health(results):
    def count(categories):
        checks = [c for r in results for c in r.checks if c.category in categories]
        return {"pass": sum(c.status == "PASS" for c in checks), "total": len(checks),
                "fail": sum(c.status == "FAIL" for c in checks), "warning": sum(c.status == "WARNING" for c in checks),
                "unknown": sum(c.status == "UNKNOWN" for c in checks)}

    all_checks = [c for r in results for c in r.checks]
    statuses = [r.status for r in results]
    return {
        "connectivity": {"pass": statuses.count("PASS"), "total": len(statuses), "fail": statuses.count("FAIL"),
                         "warning": statuses.count("WARNING"), "unknown": statuses.count("UNKNOWN")},
        "dns": count(("DNS",)), "security": count(SECURITY), "network": count(NETWORK + ("TGW",)),
        "return_path": count(("Return Path", "External")), "standards": count(("Standards",)),
        "checks": {"pass": sum(c.status == "PASS" for c in all_checks), "total": len(all_checks),
                   "fail": sum(c.status == "FAIL" for c in all_checks), "warning": sum(c.status == "WARNING" for c in all_checks),
                   "unknown": sum(c.status == "UNKNOWN" for c in all_checks)},
        "percent": round(100 * sum(c.status == "PASS" for c in all_checks) / len(all_checks)) if all_checks else 0,
    }


def analyse_project(inventory, project, services, externals, rules, environment):
    analyzer = Analyzer(inventory, project, services, externals, rules)
    results = [analyzer.analyse(r, environment) for r in sorted(project.requirements, key=lambda r: r.id) if r.applies_to(environment)]
    return {
        "project": project.id, "environment": environment, "results": results,
        "findings": findings(results), "health": health(results),
    }
