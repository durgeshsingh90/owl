"""Declared flows: the architect names every hop, and every link between hops is checked.

    subnet → NAT nat-1 → VPC endpoint vpce-1 → endpoint service → NLB → EKS cluster → VPC

Hops are resources picked from the snapshot. Gateways (NAT, internet gateway, transit
gateway, firewall) sit between two addressable hops: the route from the first to the
second must pass through them, in order. A VPC endpoint followed by an endpoint service
and a load balancer is a PrivateLink hop; a load balancer followed by its targets is
checked through its listener, target group and target health.
"""

from app.architecture.engine import EPHEMERAL, Endpoint, contains, describe_flow, flow_of, worst
from app.architecture.models import Check, Evidence, Hop, RequirementResult

GATEWAYS = ("nat-gateway", "internet-gateway", "tgw", "firewall")
LOGICAL = ("endpoint-service",)
TITLES = {
    "workload": "Workload", "external": "External system", "subnet": "Subnet", "vpc": "VPC", "nat-gateway": "NAT gateway",
    "internet-gateway": "Internet gateway", "tgw": "Transit gateway", "firewall": "Firewall", "vpc-endpoint": "VPC endpoint",
    "endpoint-service": "Endpoint service", "load-balancer": "Load balancer", "eks-cluster": "EKS cluster",
}
GRAPH_KIND = {"workload": "workload", "external": "external", "tgw": "tgw"}


def _name(tags, fallback):
    return (tags or {}).get("Name") or fallback


class FlowChecker:
    def __init__(self, analyzer, requirement, environment):
        self.a = analyzer
        self.inv = analyzer.inv
        self.requirement = requirement
        self.environment = environment
        self.protocol, self.port = flow_of(requirement)
        self.checks = []
        self.path = []
        self.return_hops = []
        self.reached = True

    # Hops

    def node(self, hop, index):
        """Resolve a hop to an Endpoint, adding an existence check."""
        inv, kind, ref = self.inv, hop.kind, hop.id
        found, evidence = None, []
        node = Endpoint(id=ref, name=hop.label or ref, kind="aws", account=hop.account)
        node.hop_kind = kind
        if kind in ("workload", "external"):
            node = self.a.resolve(ref, self.environment, index > 0, self.requirement)
            node.obj = None
            node.hop_kind = kind if kind == "external" or node.kind == "aws" else "external"
            if hop.label:
                node.name = hop.label
            self.checks.extend(node.problems)
            return node
        if kind == "subnet" and ref in inv.subnets:
            found = inv.subnets[ref]
            node.vpc, node.subnets, node.networks = found.vpc, [ref], [found.cidr]
            node.name = hop.label or _name(found.tags, ref)
            evidence = [Evidence(kind="subnets", id=ref, label=found.cidr)]
        elif kind == "vpc" and ref in inv.vpcs:
            found = inv.vpcs[ref]
            node.vpc, node.networks = ref, list(found.cidrs)
            node.subnets = sorted(s.id for s in inv.subnets.values() if s.vpc == ref)
            node.role = found.role
            node.name = hop.label or _name(found.tags, ref)
            evidence = [Evidence(kind="vpcs", id=ref, label=", ".join(found.cidrs))]
        elif kind == "nat-gateway" and ref in inv.nat_gateways:
            found = inv.nat_gateways[ref]
            node.vpc, node.subnets = found.vpc, [found.subnet]
            node.name = hop.label or ref
            evidence = [Evidence(kind="nat_gateways", id=ref)]
        elif kind == "internet-gateway" and ref in inv.internet_gateways:
            found = inv.internet_gateways[ref]
            evidence = [Evidence(kind="internet_gateways", id=ref)]
        elif kind == "tgw" and ref in inv.transit_gateways:
            found = inv.transit_gateways[ref]
            node.name = hop.label or _name(found.tags, ref)
            evidence = [Evidence(kind="transit_gateways", id=ref)]
        elif kind == "firewall":
            found = inv.firewalls.get(ref) or next((f for f in inv.firewalls.values() if f.name == ref), None)
            if found:
                node.id, node.vpc, node.subnets = found.id, found.vpc, list(found.subnets)
                node.name = hop.label or f"Firewall {found.name}"
                evidence = [Evidence(kind="firewalls", id=found.id, label=found.name)]
        elif kind == "vpc-endpoint" and ref in inv.vpc_endpoints:
            found = inv.vpc_endpoints[ref]
            node.vpc, node.subnets, node.security_groups = found.vpc, list(found.subnets), list(found.security_groups)
            node.networks = [inv.subnets[s].cidr for s in found.subnets if s in inv.subnets][:1]
            node.name = hop.label or _name(found.tags, ref)
            evidence = [Evidence(kind="vpc_endpoints", id=ref, label=found.service)]
        elif kind == "endpoint-service":
            found = inv.endpoint_services.get(ref) or next((s for s in inv.endpoint_services.values() if s.name == ref), None)
            if found:
                node.id = found.id
                node.name = hop.label or _name(found.tags, found.id)
                evidence = [Evidence(kind="endpoint_services", id=found.id, label=found.name)]
        elif kind == "load-balancer":
            found = self.a.find_lb(ref)
            if found:
                node.id, node.vpc, node.subnets, node.security_groups = found.id, found.vpc, list(found.subnets), list(found.security_groups)
                node.networks = [inv.subnets[s].cidr for s in found.subnets if s in inv.subnets][:1]
                node.name = hop.label or found.name
                evidence = [Evidence(kind="load_balancers", id=found.id, label=found.name)]
        elif kind == "eks-cluster":
            found = inv.eks_clusters.get(ref) or next((c for c in inv.eks_clusters.values() if c.name == ref), None)
            if found:
                node.id, node.vpc, node.subnets, node.security_groups = found.id, found.vpc, list(found.subnets), list(found.security_groups)
                node.networks = [inv.subnets[s].cidr for s in found.subnets if s in inv.subnets][:1]
                node.name = hop.label or f"EKS {found.name}"
                evidence = [Evidence(kind="eks_clusters", id=found.id, label=found.name)]
        node.obj = found
        if found:
            node.account = node.account or getattr(found, "account", "") or (inv.vpcs[node.vpc].account if node.vpc in inv.vpcs else "")
            if node.vpc and not node.role:
                node.role = self.a.vpc_role(node.vpc)
            status, actual = "PASS", f"{TITLES.get(kind, kind)} {node.name} found" + (f" in {node.vpc}" if node.vpc else "") + "."
        elif hop.account and hop.account not in inv.accounts:
            status, actual = "UNKNOWN", f"Account {hop.account} is not in the snapshot; refresh the project's AWS details."
        else:
            status, actual = "FAIL", f"No {TITLES.get(kind, kind).lower()} {ref} in the snapshot" + (f" for account {hop.account}." if hop.account else ".")
        self.checks.append(Check(id=f"hop-{index}", category="Endpoint", name=f"{node.name} exists", status=status,
                                 expected=f"{TITLES.get(kind, kind)} {ref}" + (f" in {hop.account}" if hop.account else ""),
                                 actual=actual, evidence=evidence))
        return node

    def hop(self, node):
        kind = GRAPH_KIND.get(node.hop_kind, node.hop_kind)
        item = Hop(id=node.id, label=node.name, kind=kind, role=node.role or "")
        if not self.path or self.path[-1].id != item.id:
            self.path.append(item)

    def add(self, category, name, status, expected="", actual="", evidence=(), blocking=True, detail=""):
        self.checks.append(Check(id=f"link-{len(self.checks)}", category=category, name=name, status=status, expected=expected,
                                 actual=actual, evidence=list(evidence), blocking=blocking, detail=detail))

    # Links

    def segment(self, start, gateways, end, hostname):
        """Route from start to end through the declared gateways, then SGs and NACLs."""
        label = f"{start.name} → {end.name}"
        if not start.subnets:
            self.add("Routing", label, "UNKNOWN", "A start with subnets to route from.",
                     f"{start.name} has no subnets in the snapshot, so routing from it can't be traced.")
            self.reached = False
            return
        destinations = end.networks or (["0.0.0.0/0"] if end.hop_kind in ("external", "internet-gateway") else [])
        if not destinations:
            self.add("Routing", label, "UNKNOWN", f"An address for {end.name}.", "No address known for the next hop.")
            self.reached = False
            return
        destination = destinations[0]
        source_net = self.inv.subnets[start.subnets[0]].cidr if start.subnets[0] in self.inv.subnets else start.networks[0]
        trace = self.a.trace(start.subnets[0], destination, end, (self.protocol, self.port, source_net, destination, hostname))
        for check in trace.checks:
            check.name = f"{check.name} ({label})"
        self.checks.extend(trace.checks)
        found = [h.id for h in trace.hops]
        position = 0
        for gateway in gateways:
            ids = {gateway.id}
            hit = next((i for i in range(position, len(found)) if found[i] in ids), None)
            detected = " → ".join([start.name] + [h.label for h in trace.hops] + [end.name])
            if hit is None:
                self.add("Routing", f"Passes through {gateway.name}", "FAIL" if trace.reached or trace.hops else "UNKNOWN",
                         f"{label} goes through {TITLES[gateway.hop_kind].lower()} {gateway.id}.",
                         f"Detected: {detected}\n✗ {gateway.id} is not on the routed path.", [Evidence(kind=self._collection(gateway), id=gateway.id)])
            else:
                position = hit + 1
                self.add("Routing", f"Passes through {gateway.name}", "PASS", f"{label} goes through {gateway.id}.", f"Detected: {detected}",
                         [Evidence(kind=self._collection(gateway), id=gateway.id)])
        for hop in trace.hops:
            self.path.append(hop)
        if not trace.reached:
            self.reached = False
            return
        # Security on both ends of the link.
        if start.security_groups and start.hop_kind != "load-balancer":
            self.checks.append(self.a.sg_check(start.security_groups, "outbound", destination, self.protocol, self.port, end.security_groups,
                                               start.name, f"sg-{len(self.checks)}"))
        if end.security_groups and end.hop_kind in ("vpc-endpoint", "eks-cluster", "workload", "load-balancer"):
            self.checks.append(self.a.sg_check(end.security_groups, "inbound", source_net, self.protocol, self.port, start.security_groups,
                                               end.name, f"sg-{len(self.checks)}"))
        same_subnet = end.subnets and start.subnets[0] == end.subnets[0]
        if not same_subnet:
            self.checks.append(self.a.nacl_check(start.subnets[0], "outbound", destination, self.protocol, self.port, start.name, f"nacl-{len(self.checks)}"))
            if self.protocol != "icmp":
                self.checks.append(self.a.nacl_check(start.subnets[0], "inbound", destination, self.protocol, EPHEMERAL, start.name + " return", f"nacl-{len(self.checks)}"))
            if end.subnets and end.subnets[0] in self.inv.subnets:
                self.checks.append(self.a.nacl_check(end.subnets[0], "inbound", source_net, self.protocol, self.port, end.name, f"nacl-{len(self.checks)}"))
        if end.hop_kind == "load-balancer":
            self.listener(end)
        if end.subnets and end.subnets[0] in self.inv.subnets:
            back = self.a.trace(end.subnets[0], source_net, None, (self.protocol, self.port, destination, source_net, ""), return_leg=True)
            summary = self.a._return_summary(back, f"{end.name} → {start.name}")
            self.checks.append(summary)
            self.return_hops.extend(back.hops)
        elif end.system is not None:
            nat = trace.nat_ips
            system = end.system
            if getattr(system, "allowlist", None) is not None and nat:
                ok = all(any(contains(a, ip + "/32") for a in system.allowlist) for ip in nat)
                self.add("External", f"{end.name} allowlists our egress IPs", "PASS" if ok else "FAIL",
                         f"Allowlist contains {', '.join(nat)}.", f"Allowlist: {', '.join(system.allowlist)}.", [Evidence(kind="external", id=system.id)])
            elif system.type == "third-party":
                self.add("External", f"{end.name} allowlists our egress IPs", "UNKNOWN", f"{end.name} allows {', '.join(nat) or 'our egress IPs'}.",
                         "The third party's allowlist was not supplied.", blocking=False)

    def listener(self, lb_node):
        lb = lb_node.obj
        listener = next((l for l in lb.listeners if l.port == self.port), None)
        evidence = [Evidence(kind="load_balancers", id=lb.id, label=lb.name)]
        if listener:
            self.add("Load Balancer", f"{lb.name} listener on {self.port}", "PASS", f"A listener on {self.port}.", f"{listener.protocol}/{listener.port}.", evidence)
        else:
            self.add("Load Balancer", f"{lb.name} listener on {self.port}", "FAIL", f"A listener on {self.port}.",
                     f"Listeners: {', '.join(f'{l.protocol}/{l.port}' for l in lb.listeners) or 'none'}.", evidence)
        return listener

    def privatelink(self, endpoint_node, service_node):
        endpoint, service = endpoint_node.obj, service_node.obj
        if not endpoint or not service:
            if endpoint and not service:
                self.add("PrivateLink", f"{endpoint_node.name} → {service_node.name}", "UNKNOWN", "The provider's endpoint service.",
                         "The service's account isn't in the snapshot, so the provider side can't be checked.")
            return
        evidence = [Evidence(kind="vpc_endpoints", id=endpoint.id), Evidence(kind="endpoint_services", id=service.id, label=service.name)]
        if endpoint.type and endpoint.type != "Interface":
            self.add("PrivateLink", f"{endpoint.id} is an interface endpoint", "FAIL", "An interface endpoint.", f"Type {endpoint.type}.", evidence)
        if endpoint.service != service.name:
            self.add("PrivateLink", f"{endpoint.id} connects to {service.id}", "FAIL", f"Service name {service.name}.",
                     f"The endpoint connects to {endpoint.service or '(unknown)'}.", evidence)
            return
        self.add("PrivateLink", f"{endpoint.id} connects to {service.id}", "PASS", f"Service name {service.name}.",
                 f"{endpoint.id} (account {endpoint.account}) → {service.id} (account {service.account}).", evidence)
        if endpoint.state not in ("available", ""):
            self.add("PrivateLink", f"Endpoint {endpoint.id} state", "FAIL", "available.", f"State is {endpoint.state}.", evidence)
        connection = next((c for c in service.connections if c.endpoint == endpoint.id), None)
        if connection:
            self.add("PrivateLink", "Connection accepted by the provider", "PASS" if connection.state == "available" else "FAIL",
                     "Connection state available.", f"{endpoint.id} from {connection.owner}: {connection.state}.", evidence)
        elif service.connections or service.acceptance_required:
            self.add("PrivateLink", "Connection accepted by the provider", "FAIL", f"A connection from {endpoint.id}.",
                     f"{service.id} lists no connection from {endpoint.id}.", evidence)
        else:
            self.add("PrivateLink", "Connection accepted by the provider", "UNKNOWN", f"A connection from {endpoint.id}.",
                     "The provider's endpoint connections were not supplied.", evidence, blocking=False)
        if service.allowed_principals is not None:
            allowed = any(p == "*" or f":{endpoint.account}:" in p for p in service.allowed_principals)
            self.add("PrivateLink", f"Account {endpoint.account} allowed", "PASS" if allowed else "WARNING",
                     f"{endpoint.account} among the service's allowed principals.",
                     f"Allowed: {', '.join(service.allowed_principals) or 'none'}.", evidence)

    def service_to_lb(self, service_node, lb_node):
        service, lb = service_node.obj, lb_node.obj
        if not service or not lb:
            return
        evidence = [Evidence(kind="endpoint_services", id=service.id), Evidence(kind="load_balancers", id=lb.id, label=lb.name)]
        self.add("PrivateLink", f"{service.id} is served by {lb.name}", "PASS" if lb.id in service.load_balancers else "FAIL",
                 f"{lb.name} among the service's load balancers.",
                 f"Load balancers: {', '.join(a.split('/')[-2] if '/' in a else a for a in service.load_balancers) or 'none'}.", evidence)
        if lb.type != "network":
            self.add("PrivateLink", f"{lb.name} type", "WARNING", "A network load balancer.", f"Type {lb.type}.", evidence)
        self.listener(lb_node)

    def lb_to_targets(self, lb_node, target):
        lb = lb_node.obj
        listener = next((l for l in lb.listeners if l.port == self.port), None) if lb else None
        if not listener:
            return
        groups = [self.inv.target_groups[g] for g in listener.target_groups if g in self.inv.target_groups]
        if not groups:
            self.add("Load Balancer", f"{lb.name}:{self.port} target group", "FAIL", "The listener forwards to a target group.",
                     "No target group in the snapshot for this listener.", [Evidence(kind="load_balancers", id=lb.id)])
            return
        cidrs = [self.inv.subnets[s].cidr for s in target.subnets if s in self.inv.subnets]
        best = None
        for group in groups:
            evidence = [Evidence(kind="target_groups", id=group.id, label=group.name)]
            if group.targets is None:
                best = best or ("UNKNOWN", group, "Target health was not supplied.", evidence)
                continue
            inside = [t for t in group.targets if any(contains(c, t.id + "/32") for c in cidrs)] if group.target_type == "ip" else []
            if group.target_type != "ip":
                status = "UNKNOWN" if group.vpc == target.vpc else "FAIL"
                text = (f"{group.target_type} targets ({', '.join(t.id for t in group.targets) or 'none'}) can't be matched to {target.name} from the supplied data."
                        if status == "UNKNOWN" else f"{group.name} is in {group.vpc}, not {target.vpc}.")
                best = best or (status, group, text, evidence)
                continue
            if not inside:
                best = best or ("FAIL", group, f"No target IP is inside {target.name}'s subnets ({', '.join(cidrs)}). Targets: "
                                + (", ".join(t.id for t in group.targets) or "none") + ".", evidence)
                continue
            healthy = [t for t in inside if t.health == "healthy"]
            status = "PASS" if healthy else "FAIL"
            best = (status, group, f"{len(inside)} target(s) in {target.name}: " + ", ".join(f"{t.id}:{t.port} {t.health}" for t in inside) + ".", evidence)
            if status == "PASS":
                break
        status, group, text, evidence = best
        self.add("Load Balancer", f"{group.name} targets {target.name}", status, f"Healthy targets inside {target.name} on port {group.port}.", text, evidence)
        port = group.port or self.port
        lb_cidrs = [self.inv.subnets[s].cidr for s in lb.subnets if s in self.inv.subnets]
        if target.security_groups:
            check = self.a.sg_check(target.security_groups, "inbound", lb_cidrs[0] if lb_cidrs else "0.0.0.0/32", self.protocol, port,
                                    lb.security_groups, f"{target.name} (from {lb.name})", f"sg-{len(self.checks)}")
            if target.hop_kind == "eks-cluster":
                check.detail = "Checked on the cluster security group; pods or nodes may use other groups that aren't in the snapshot."
            self.checks.append(check)
        self.port = port

    @staticmethod
    def _collection(node):
        return {"nat-gateway": "nat_gateways", "internet-gateway": "internet_gateways", "tgw": "transit_gateways",
                "firewall": "firewalls"}.get(node.hop_kind, "vpcs")

    # The whole flow

    def run(self):
        requirement = self.requirement
        nodes = [self.node(hop, i) for i, hop in enumerate(requirement.hops)]
        if len(nodes) < 2:
            self.add("Endpoint", "Flow has hops", "UNKNOWN", "At least a start and an end hop.", f"{len(nodes)} hop(s).")
            return nodes
        if any(c.status != "PASS" and c.category == "Endpoint" and c.blocking for c in self.checks):
            for node in nodes:
                self.hop(node)
            self.reached = False
            return nodes
        start = nodes[0]
        hostname = requirement.hostname or nodes[-1].hostname
        if hostname and start.vpc:
            self.checks.append(self.a.dns_check(hostname, start.vpc, nodes[-1]))
        self.hop(start)
        current, gateways, previous = start, [], start
        for index, node in enumerate(nodes[1:], 1):
            hop = requirement.hops[index]
            if node.hop_kind in GATEWAYS:
                gateways.append(node)
            elif node.hop_kind == "endpoint-service":
                if previous.hop_kind != "vpc-endpoint":
                    self.add("PrivateLink", f"{node.name} follows a VPC endpoint", "FAIL", "A VPC endpoint before the endpoint service.",
                             f"The hop before is {TITLES.get(previous.hop_kind, previous.hop_kind).lower()} {previous.name}.")
                else:
                    self.privatelink(previous, node)
                self.hop(node)
                current = node
            elif previous.hop_kind == "endpoint-service":
                if node.hop_kind != "load-balancer":
                    self.add("PrivateLink", f"{previous.name} → {node.name}", "FAIL", "The service's network load balancer after the endpoint service.",
                             f"{TITLES.get(node.hop_kind, node.hop_kind)} {node.name}.")
                else:
                    self.service_to_lb(previous, node)
                self.hop(node)
                current = node
            elif current.hop_kind == "load-balancer" and not gateways and node.vpc == current.vpc and node.hop_kind != "load-balancer":
                self.lb_to_targets(current, node)
                self.hop(node)
                current = node
            else:
                self.segment(current, gateways, node, hostname if index == len(nodes) - 1 else "")
                self.hop(node)
                current, gateways = node, []
            if hop.port:
                self.port = hop.port
            previous = node
        if gateways:  # the flow ends at a gateway, e.g. ... → NAT → internet gateway
            internet = Endpoint(id="internet", name="Internet", kind="third-party")
            internet.hop_kind, internet.networks, internet.obj = "internet-gateway", ["0.0.0.0/0"], None
            self.segment(current, gateways, internet, hostname)
            self.hop(gateways[-1])
        return nodes


def analyse_flow(analyzer, requirement, environment):
    checker = FlowChecker(analyzer, requirement, environment)
    nodes = checker.run()
    first, last = nodes[0] if nodes else None, nodes[-1] if nodes else None
    path = [checker.path[0]] if checker.path else []
    for hop in checker.path[1:]:
        if hop.id != path[-1].id:
            path.append(hop)
    src = first.summary() if first else {"id": "", "name": "?"}
    dst = last.summary() if last else {"id": "", "name": "?"}
    checks = checker.checks + analyzer._standards(requirement, environment, first, last, path, checker.reached) if first and last else checker.checks
    status = analyzer.overall(checks)
    return RequirementResult(requirement=requirement, environment=environment, status=status, summary=analyzer.summarize(status, checks),
                             source=src, destination=dst, checks=checks, path=path, return_path=checker.return_hops)
