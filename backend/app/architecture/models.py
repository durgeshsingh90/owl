"""The normalized architecture model, project requirements, rules and results.

Layers stay separate: raw AWS JSON is turned into an Inventory by the normalizer;
projects, services and external systems are maintained by architects; rules say
what should exist; the engine produces Analysis results from all of them.
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field

Status = Literal["PASS", "FAIL", "WARNING", "UNKNOWN"]
Environment = str


class Model(BaseModel):
    model_config = {"extra": "ignore"}


# Normalized AWS inventory


class Account(Model):
    id: str
    name: str = ""
    type: str = "application"  # application, central-ingress, central-egress, central-network, shared-services, dns
    region: str = ""
    environments: list[str] = []
    tags: dict[str, str] = {}


class Vpc(Model):
    id: str
    account: str = ""
    region: str = ""
    cidrs: list[str] = []
    dns_support: Optional[bool] = None
    dns_hostnames: Optional[bool] = None
    role: str = ""  # from an "owl:role" tag, else the account type
    tags: dict[str, str] = {}


class Subnet(Model):
    id: str
    vpc: str
    account: str = ""
    cidr: str
    az: str = ""
    public: bool = False
    tags: dict[str, str] = {}


class Route(Model):
    destination: str  # CIDR or prefix list id
    target: str
    target_type: str  # local, transit-gateway, internet-gateway, nat-gateway, vpc-endpoint, peering, virtual-gateway, network-interface, unknown
    state: str = "active"


class RouteTable(Model):
    id: str
    vpc: str
    account: str = ""
    subnets: list[str] = []
    main: bool = False
    routes: list[Route] = []


class TransitGateway(Model):
    id: str
    account: str = ""
    region: str = ""
    tags: dict[str, str] = {}


class TgwAttachment(Model):
    id: str
    tgw: str
    type: str  # vpc, direct-connect-gateway, vpn, peering
    resource: str = ""  # VPC id, DX gateway id, VPN id
    resource_account: str = ""
    subnets: list[str] = []
    state: str = "available"
    route_table: str = ""  # association
    tags: dict[str, str] = {}


class TgwRoute(Model):
    destination: str
    attachment: str = ""
    type: str = "static"  # static, propagated
    state: str = "active"  # active, blackhole


class TgwRouteTable(Model):
    id: str
    tgw: str
    name: str = ""
    routes: list[TgwRoute] = []
    propagations: list[str] = []


class SgRule(Model):
    protocol: str  # tcp, udp, icmp, all
    from_port: Optional[int] = None
    to_port: Optional[int] = None
    cidrs: list[str] = []
    groups: list[str] = []
    prefix_lists: list[str] = []
    description: str = ""


class SecurityGroup(Model):
    id: str
    name: str = ""
    vpc: str = ""
    account: str = ""
    inbound: list[SgRule] = []
    outbound: list[SgRule] = []


class NaclRule(Model):
    number: int
    protocol: str
    action: Literal["allow", "deny"]
    cidr: str
    from_port: Optional[int] = None
    to_port: Optional[int] = None


class Nacl(Model):
    id: str
    vpc: str
    account: str = ""
    subnets: list[str] = []
    default: bool = False
    inbound: list[NaclRule] = []
    outbound: list[NaclRule] = []


class Listener(Model):
    id: str
    port: int
    protocol: str
    target_groups: list[str] = []


class Target(Model):
    id: str  # IP address or instance id
    port: Optional[int] = None
    health: str = ""  # healthy, unhealthy, unused, draining, ...


class TargetGroup(Model):
    id: str
    name: str = ""
    protocol: str = ""
    port: Optional[int] = None
    vpc: str = ""
    load_balancers: list[str] = []
    target_type: str = ""
    targets: Optional[list[Target]] = None  # None = target health not supplied


class LoadBalancer(Model):
    id: str
    name: str = ""
    type: str = "application"  # application, network
    scheme: str = "internal"
    vpc: str = ""
    account: str = ""
    subnets: list[str] = []
    security_groups: list[str] = []
    dns_name: str = ""
    listeners: list[Listener] = []


class FirewallRule(Model):
    action: str  # pass, drop, reject, alert
    protocol: str = "ip"
    source: list[str] = ["any"]
    source_port: str = "any"
    destination: list[str] = ["any"]
    destination_port: str = "any"
    domains: list[str] = []  # domain list rules (TLS SNI / HTTP host)
    sid: str = ""


class FirewallRuleGroup(Model):
    id: str
    name: str = ""
    priority: Optional[int] = None
    rules: list[FirewallRule] = []


class Firewall(Model):
    id: str
    name: str = ""
    vpc: str = ""
    account: str = ""
    subnets: list[str] = []
    endpoints: list[str] = []  # VPC endpoint ids that routes point at
    policy: str = ""
    rule_order: str = "DEFAULT_ACTION_ORDER"  # or STRICT_ORDER
    default_actions: list[str] = []
    rule_groups: list[FirewallRuleGroup] = []
    rule_data: bool = True  # False when the policy or its rule groups were not supplied


class NatGateway(Model):
    id: str
    vpc: str = ""
    subnet: str = ""
    account: str = ""
    public_ips: list[str] = []
    state: str = "available"


class InternetGateway(Model):
    id: str
    vpcs: list[str] = []
    account: str = ""


class VpcEndpoint(Model):
    id: str
    type: str = ""  # Interface, Gateway, GatewayLoadBalancer
    service: str = ""
    vpc: str = ""
    account: str = ""
    state: str = "available"
    subnets: list[str] = []
    security_groups: list[str] = []
    route_tables: list[str] = []
    dns_names: list[str] = []
    tags: dict[str, str] = {}


class EndpointConnection(Model):
    endpoint: str
    owner: str = ""
    state: str = ""  # available, pendingAcceptance, rejected, ...


class EndpointService(Model):
    """A PrivateLink service this account provides."""

    id: str
    name: str = ""  # com.amazonaws.vpce.<region>.vpce-svc-...
    account: str = ""
    region: str = ""
    load_balancers: list[str] = []
    acceptance_required: bool = False
    allowed_principals: Optional[list[str]] = None
    connections: list[EndpointConnection] = []
    tags: dict[str, str] = {}


class EksCluster(Model):
    id: str  # ARN
    name: str
    account: str = ""
    region: str = ""
    vpc: str = ""
    subnets: list[str] = []
    security_groups: list[str] = []  # cluster security group first, then additional groups
    endpoint_public: Optional[bool] = None
    endpoint_private: Optional[bool] = None
    version: str = ""


class DnsRecord(Model):
    name: str
    type: str
    values: list[str] = []
    alias: str = ""


class HostedZone(Model):
    id: str
    name: str
    private: bool = False
    account: str = ""
    vpcs: list[str] = []
    records: list[DnsRecord] = []


class ResolverRule(Model):
    id: str
    domain: str
    type: str = "FORWARD"  # FORWARD, SYSTEM, RECURSIVE
    target_ips: list[str] = []
    endpoint: str = ""
    account: str = ""
    vpcs: list[str] = []


class ResolverEndpoint(Model):
    id: str
    direction: str = "OUTBOUND"
    vpc: str = ""
    subnets: list[str] = []
    ips: list[str] = []
    security_groups: list[str] = []


class ResourceTimes(Model):
    """When a resource was created and last changed, and where that came from.

    Sources: "aws" (the resource's own API field), "config" (AWS Config) or
    "cloudtrail" (write events in the CloudTrail lookup window). Empty = not known.
    """

    created: str = ""
    created_by: str = ""
    created_source: str = ""
    updated: str = ""
    updated_by: str = ""
    updated_source: str = ""
    updated_event: str = ""
    changes: int = 0  # CloudTrail write events seen in the window


class Inventory(Model):
    """Everything known about AWS, keyed by resource id."""

    schema_: str = Field("owl-architecture/v1", alias="schema")
    accounts: dict[str, Account] = {}
    vpcs: dict[str, Vpc] = {}
    subnets: dict[str, Subnet] = {}
    route_tables: dict[str, RouteTable] = {}
    transit_gateways: dict[str, TransitGateway] = {}
    tgw_attachments: dict[str, TgwAttachment] = {}
    tgw_route_tables: dict[str, TgwRouteTable] = {}
    security_groups: dict[str, SecurityGroup] = {}
    nacls: dict[str, Nacl] = {}
    load_balancers: dict[str, LoadBalancer] = {}
    target_groups: dict[str, TargetGroup] = {}
    firewalls: dict[str, Firewall] = {}
    nat_gateways: dict[str, NatGateway] = {}
    internet_gateways: dict[str, InternetGateway] = {}
    vpc_endpoints: dict[str, VpcEndpoint] = {}
    endpoint_services: dict[str, EndpointService] = {}
    eks_clusters: dict[str, EksCluster] = {}
    hosted_zones: dict[str, HostedZone] = {}
    resolver_rules: dict[str, ResolverRule] = {}
    resolver_endpoints: dict[str, ResolverEndpoint] = {}
    timestamps: dict[str, ResourceTimes] = {}  # by resource id

    model_config = {"extra": "ignore", "populate_by_name": True}


COLLECTIONS = [
    name for name in Inventory.model_fields if name not in ("schema_", "timestamps")
]


# Architect-maintained definitions


class Placement(Model):
    """Where a workload lives in one environment."""

    account: str = ""
    vpc: str = ""
    subnets: list[str] = []
    security_groups: list[str] = []
    load_balancer: str = ""  # inbound traffic arrives at this load balancer
    hostname: str = ""


class Workload(Model):
    """A project application, or a shared/central service hosted in AWS."""

    id: str
    name: str = ""
    kind: str = "application"  # application, central-ingress, central-egress, central-network, shared-service
    description: str = ""
    placement: dict[str, Placement] = {}  # by environment; "*" applies to every environment


class Endpoint(Model):
    hostname: str = ""
    ip: str = ""
    protocol: str = "TCP"
    port: Optional[int] = None


class ExternalSystem(Model):
    id: str
    name: str = ""
    type: Literal["on-prem", "third-party"] = "third-party"
    description: str = ""
    networks: list[str] = []
    dns_domains: list[str] = []
    domains: list[str] = []
    endpoints: list[Endpoint] = []
    connectivity: dict = {}  # e.g. {"type": "direct-connect", "attachments": ["tgw-attach-..."]}
    return_routes: Optional[list[str]] = None  # AWS CIDRs the system routes back; None = not supplied
    allowlist: Optional[list[str]] = None  # source IPs the third party allows; None = not supplied


class EnvironmentBinding(Model):
    account_id: str


class ProjectAccount(Model):
    """An AWS account the project involves: downloaded on refresh, nothing else is."""

    account_id: str
    name: str = ""
    type: str = "application"  # application, central-ingress, central-egress, central-network, shared-services, dns
    regions: list[str] = []  # empty: the profile's region
    profile: str = ""  # empty: the first AWS config profile for the account


class FlowHop(Model):
    """One resource the architect says traffic passes through, in order."""

    kind: str  # workload, external, subnet, vpc, nat-gateway, internet-gateway, tgw, firewall, vpc-endpoint, endpoint-service, load-balancer, eks-cluster
    id: str
    account: str = ""
    label: str = ""
    port: Optional[int] = None  # the port from here on, when this hop changes it (NLB 443 -> pods 8443)


class Requirement(Model):
    id: str
    name: str = ""
    environment: str = ""
    environments: list[str] = []
    source: str = ""
    destination: str = ""
    hops: list[FlowHop] = []  # a declared flow: every hop and every link between hops is checked
    direction: Literal["inbound", "outbound", "bidirectional"] = "outbound"
    protocol: str = "TCP"
    port: Optional[int] = None
    target_port: Optional[int] = None  # inbound through a load balancer: the port targets listen on
    hostname: str = ""
    required: bool = True
    notes: str = ""

    def applies_to(self, environment):
        wanted = self.environments or ([self.environment] if self.environment else [])
        return not wanted or environment in wanted or "*" in wanted


class Project(Model):
    id: str
    name: str = ""
    description: str = ""
    environments: dict[str, EnvironmentBinding] = {}
    shared_services: list[str] = []
    central_services: list[str] = []
    aws_accounts: list[ProjectAccount] = []
    applications: list[Workload] = []
    requirements: list[Requirement] = []


class Rule(Model):
    """An architecture standard; conditions select requirements it applies to."""

    id: str
    name: str
    description: str = ""
    type: str = "connectivity"
    severity: Literal["critical", "high", "medium", "low"] = "high"
    direction: str = ""
    conditions: dict[str, list[str]] = {}
    required_path: list[str] = []
    forbidden_path: list[str] = []
    enabled: bool = True


# Results


class Evidence(Model):
    kind: str
    id: str
    label: str = ""


class Check(Model):
    id: str
    category: str  # Endpoint, Routing, TGW, Firewall, NAT, Internet, Security Group, NACL, DNS, Load Balancer, Return Path, Standards, External
    name: str
    status: Status
    expected: str = ""
    actual: str = ""
    detail: str = ""
    evidence: list[Evidence] = []
    blocking: bool = True  # advisory checks (return path, external allowlists) never decide the overall result
    rule: str = ""


class Hop(Model):
    """One node on the traced path, for the architecture graph."""

    id: str
    label: str
    kind: str
    role: str = ""


class RequirementResult(Model):
    requirement: Requirement
    environment: str
    status: Status
    summary: str
    source: dict
    destination: dict
    checks: list[Check]
    path: list[Hop]
    return_path: list[Hop] = []


class Finding(Model):
    id: str
    severity: Literal["critical", "high", "warning", "info"]
    title: str
    cause: str
    environment: str
    requirement: str
    check: str = ""
    rule: str = ""
