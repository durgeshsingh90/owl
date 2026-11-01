"""Turn exported AWS JSON into the normalized Inventory.

Each file can hold AWS CLI output (describe-vpcs, describe-route-tables, elbv2
describe-load-balancers, network-firewall describe-*, route53, route53resolver ...),
several of those merged into one object, or an already normalized inventory
("schema": "owl-architecture/v1"). The account comes from an "Account" object in
the file, a 12-digit folder or file name, or the resources' OwnerId.
"""

import ipaddress
import re
from datetime import datetime, timezone

from pydantic import ValidationError

from app.architecture.models import (
    COLLECTIONS, Account, CustomerGateway, DnsLookup, DnsRecord, DxAssociation, DxGateway, DxVirtualInterface,
    VpcPeering, VpnConnection, VpnGateway, VpnTunnel, ResourceTimes, EksCluster, EndpointConnection, EndpointService, Target, Firewall, FirewallRule, FirewallRuleGroup,
    HostedZone, InternetGateway, Inventory, Listener, LoadBalancer, Nacl, NaclRule,
    NatGateway, ResolverEndpoint, ResolverRule, Route, RouteTable, SecurityGroup,
    SgRule, Subnet, TargetGroup, TgwAttachment, TgwRoute, TgwRouteTable,
    TransitGateway, Vpc, VpcEndpoint,
)

ACCOUNT_IN_PATH = re.compile(r"(?<!\d)(\d{12})(?!\d)")
PROTOCOLS = {"-1": "all", "6": "tcp", "17": "udp", "1": "icmp", "58": "icmpv6"}
KNOWN_KEYS = {
    "Account", "Accounts", "Region", "Vpcs", "VpcAttributes", "Subnets", "RouteTables",
    "SecurityGroups", "NetworkAcls", "TransitGateways", "TransitGatewayAttachments",
    "TransitGatewayVpcAttachments", "TransitGatewayRouteTables", "NatGateways",
    "InternetGateways", "VpcEndpoints", "LoadBalancers", "Listeners", "TargetGroups",
    "Firewalls", "FirewallPolicies", "RuleGroups", "HostedZones", "ResolverRules",
    "ResolverRuleAssociations", "ResolverEndpoints", "Clusters", "ServiceConfigurations",
    "VpcEndpointConnections", "ConfigResources", "ResourceHistory", "AccessDenied", "DnsLookups", "VpcPeeringConnections", "VpnConnections", "VpnGateways",
    "CustomerGateways", "DxGateways", "DxVirtualInterfaces",
}
# Resources whose own API response says when they were created (and changed).
TIME_FIELDS = [
    ("NatGateways", "NatGatewayId", "CreateTime", None),
    ("VpcEndpoints", "VpcEndpointId", "CreationTimestamp", None),
    ("TransitGateways", "TransitGatewayId", "CreationTime", None),
    ("TransitGatewayAttachments", "TransitGatewayAttachmentId", "CreationTime", None),
    ("TransitGatewayRouteTables", "TransitGatewayRouteTableId", "CreationTime", None),
    ("LoadBalancers", "LoadBalancerArn", "CreatedTime", None),
    ("Clusters", "arn", "createdAt", None),
    ("ResolverRules", "Id", "CreationTime", "ModificationTime"),
    ("ResolverEndpoints", "Id", "CreationTime", "ModificationTime"),
]


def iso(value):
    """A timestamp as ISO 8601 UTC to the second, or "" when it can't be read."""
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip().replace(" ", "T", 1).replace("Z", "+00:00")
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def tags(item):
    value = item.get("Tags") or item.get("tags") or []
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    return {t.get("Key", ""): t.get("Value", "") for t in value if isinstance(t, dict)}


def protocol(value):
    value = str(value if value is not None else "-1").lower()
    return PROTOCOLS.get(value, value)


def valid_cidr(value):
    try:
        ipaddress.ip_network(value, strict=False)
        return True
    except ValueError:
        return False


class Normalizer:
    def __init__(self):
        self.inv = Inventory()
        self.errors = []
        self.warnings = []
        # Pieces that arrive in separate files and are joined at the end.
        self._vpc_attributes = {}
        self._vpc_attachment_subnets = {}
        self._listeners = []
        self._policies = {}
        self._rule_groups = {}
        self._resolver_associations = []
        self._raw_firewalls = []
        self._endpoint_connections = []
        self._config_items = []
        self._history = {}
        self._firewall_changes = {}  # policy or rule group ARN -> last modified

    # Entry points

    def add_file(self, name, document):
        try:
            if isinstance(document, dict) and str(document.get("schema", "")).startswith("owl-architecture/"):
                self._add_normalized(document)
                return
            if isinstance(document, list):
                self.errors.append(f"{name}: expected a JSON object with AWS CLI keys, found a list.")
                return
            if not isinstance(document, dict):
                self.errors.append(f"{name}: expected a JSON object.")
                return
            keys = set(document) & KNOWN_KEYS
            if not keys:
                self.warnings.append(f"{name}: no recognised AWS keys; skipped.")
                return
            account = self._account(name, document)
            region = document.get("Region", "") or (account.region if account else "")
            self._add_raw(document, account.id if account else "", region, name)
        except (ValidationError, KeyError, TypeError, ValueError, AttributeError) as error:
            self.errors.append(f"{name}: {type(error).__name__}: {error}"[:500])

    def finish(self):
        self._join()
        self._join_times()
        self._validate()
        return self.inv

    # Normalized documents merge collection by collection

    def _add_normalized(self, document):
        partial = Inventory.model_validate(document)
        for collection in COLLECTIONS:
            getattr(self.inv, collection).update(getattr(partial, collection))
        self.inv.timestamps.update(partial.timestamps)
        self.inv.access_denied.update(partial.access_denied)
        self.inv.dns_lookups.update(partial.dns_lookups)

    # Raw AWS CLI output

    def _account(self, name, document):
        info = document.get("Account")
        if isinstance(info, dict) and (info.get("Id") or info.get("id")):
            account = Account(
                id=str(info.get("Id") or info.get("id")),
                name=info.get("Name") or info.get("name") or "",
                type=info.get("Type") or info.get("type") or tags(info).get("owl:account-type", "application"),
                region=info.get("Region") or document.get("Region", ""),
                environments=info.get("Environments") or info.get("environments") or [],
                tags=tags(info),
            )
            self.inv.accounts[account.id] = account
            return account
        for item in document.get("Accounts", []) or []:  # organizations list-accounts
            self.inv.accounts.setdefault(
                str(item["Id"]), Account(id=str(item["Id"]), name=item.get("Name", ""), tags=tags(item))
            )
        found = ACCOUNT_IN_PATH.findall(name)
        if found:
            account_id = found[-1]
        else:
            owners = {
                str(item.get("OwnerId"))
                for key in ("Vpcs", "Subnets", "RouteTables", "SecurityGroups", "NetworkAcls", "InternetGateways")
                for item in document.get(key, []) or []
                if item.get("OwnerId")
            }
            if len(owners) != 1:
                if set(document) - {"Accounts"}:
                    self.warnings.append(f"{name}: account not identified; resources kept without an account.")
                return None
            account_id = owners.pop()
        return self.inv.accounts.setdefault(account_id, Account(id=account_id, region=document.get("Region", "")))

    def _add_raw(self, d, account, region, name):
        inv = self.inv
        for key, id_field, created, updated in TIME_FIELDS:
            for item in d.get(key, []) or []:
                if item.get(id_field):
                    self.times(item[id_field], created=item.get(created), updated=item.get(updated) if updated else None, source="aws")
        for item in d.get("FirewallPolicies", []) or []:
            response = item.get("FirewallPolicyResponse") or {}
            if response.get("FirewallPolicyArn") and response.get("LastModifiedTime"):
                self._firewall_changes[response["FirewallPolicyArn"]] = iso(response["LastModifiedTime"])
        for item in d.get("RuleGroups", []) or []:
            response = item.get("RuleGroupResponse") or {}
            if response.get("RuleGroupArn") and response.get("LastModifiedTime"):
                self._firewall_changes[response["RuleGroupArn"]] = iso(response["LastModifiedTime"])
                self.times(response["RuleGroupArn"], updated=response["LastModifiedTime"], source="aws")
        if d.get("AccessDenied") and account:
            inv.access_denied[account] = list(d["AccessDenied"])
        for item in d.get("DnsLookups", []) or []:
            lookup = DnsLookup.model_validate({**item, "account": item.get("account") or account})
            inv.dns_lookups[lookup.name] = lookup
        self._add_hybrid(d, account)
        self._config_items.extend(d.get("ConfigResources", []) or [])
        for resource_id, summary in (d.get("ResourceHistory") or {}).items():
            self._history[resource_id] = summary
        for item in d.get("Vpcs", []) or []:
            cidrs = [a["CidrBlock"] for a in item.get("CidrBlockAssociationSet", []) if a.get("CidrBlock")]
            if not cidrs and item.get("CidrBlock"):
                cidrs = [item["CidrBlock"]]
            t = tags(item)
            inv.vpcs[item["VpcId"]] = Vpc(
                id=item["VpcId"], account=str(item.get("OwnerId") or account), region=region,
                cidrs=cidrs, tags=t, role=t.get("owl:role", ""),
                dns_support=item.get("EnableDnsSupport"), dns_hostnames=item.get("EnableDnsHostnames"),
            )
        for item in d.get("VpcAttributes", []) or []:
            self._vpc_attributes[item["VpcId"]] = item
        for item in d.get("Subnets", []) or []:
            inv.subnets[item["SubnetId"]] = Subnet(
                id=item["SubnetId"], vpc=item["VpcId"], account=str(item.get("OwnerId") or account),
                cidr=item["CidrBlock"], az=item.get("AvailabilityZone", ""),
                public=bool(item.get("MapPublicIpOnLaunch")), tags=tags(item),
            )
        for item in d.get("RouteTables", []) or []:
            associations = item.get("Associations", []) or []
            inv.route_tables[item["RouteTableId"]] = RouteTable(
                id=item["RouteTableId"], vpc=item["VpcId"], account=str(item.get("OwnerId") or account),
                subnets=[a["SubnetId"] for a in associations if a.get("SubnetId")],
                main=any(a.get("Main") for a in associations),
                routes=[r for r in (self._route(route) for route in item.get("Routes", []) or []) if r],
            )
        for item in d.get("SecurityGroups", []) or []:
            inv.security_groups[item["GroupId"]] = SecurityGroup(
                id=item["GroupId"], name=item.get("GroupName", ""), vpc=item.get("VpcId", ""),
                account=str(item.get("OwnerId") or account),
                inbound=[self._sg_rule(p) for p in item.get("IpPermissions", []) or []],
                outbound=[self._sg_rule(p) for p in item.get("IpPermissionsEgress", []) or []],
            )
        for item in d.get("NetworkAcls", []) or []:
            entries = item.get("Entries", []) or []
            inv.nacls[item["NetworkAclId"]] = Nacl(
                id=item["NetworkAclId"], vpc=item["VpcId"], account=str(item.get("OwnerId") or account),
                subnets=[a["SubnetId"] for a in item.get("Associations", []) or [] if a.get("SubnetId")],
                default=bool(item.get("IsDefault")),
                inbound=[self._nacl_rule(e) for e in entries if not e.get("Egress")],
                outbound=[self._nacl_rule(e) for e in entries if e.get("Egress")],
            )
        for item in d.get("TransitGateways", []) or []:
            inv.transit_gateways[item["TransitGatewayId"]] = TransitGateway(
                id=item["TransitGatewayId"], account=str(item.get("OwnerId") or account), region=region, tags=tags(item),
            )
        for item in d.get("TransitGatewayAttachments", []) or []:
            association = item.get("Association") or {}
            inv.tgw_attachments[item["TransitGatewayAttachmentId"]] = TgwAttachment(
                id=item["TransitGatewayAttachmentId"], tgw=item["TransitGatewayId"],
                type=item.get("ResourceType", "vpc"), resource=item.get("ResourceId", ""),
                resource_account=str(item.get("ResourceOwnerId", "")), state=item.get("State", "available"),
                route_table=association.get("TransitGatewayRouteTableId", "")
                if association.get("State", "associated") == "associated" else "",
                subnets=item.get("SubnetIds", []) or [], tags=tags(item),
            )
        for item in d.get("TransitGatewayVpcAttachments", []) or []:
            self._vpc_attachment_subnets[item["TransitGatewayAttachmentId"]] = item
        for item in d.get("TransitGatewayRouteTables", []) or []:
            inv.tgw_route_tables[item["TransitGatewayRouteTableId"]] = TgwRouteTable(
                id=item["TransitGatewayRouteTableId"], tgw=item["TransitGatewayId"],
                name=tags(item).get("Name", ""),
                routes=[self._tgw_route(r) for r in item.get("Routes", []) or []],
                propagations=[p["TransitGatewayAttachmentId"] for p in item.get("Propagations", []) or []],
            )
            for association in item.get("Associations", []) or []:
                attachment = inv.tgw_attachments.get(association.get("TransitGatewayAttachmentId", ""))
                if attachment:
                    attachment.route_table = item["TransitGatewayRouteTableId"]
        for item in d.get("NatGateways", []) or []:
            inv.nat_gateways[item["NatGatewayId"]] = NatGateway(
                id=item["NatGatewayId"], vpc=item.get("VpcId", ""), subnet=item.get("SubnetId", ""),
                account=account, state=item.get("State", "available"),
                public_ips=[a["PublicIp"] for a in item.get("NatGatewayAddresses", []) or [] if a.get("PublicIp")],
            )
        for item in d.get("InternetGateways", []) or []:
            inv.internet_gateways[item["InternetGatewayId"]] = InternetGateway(
                id=item["InternetGatewayId"], account=str(item.get("OwnerId") or account),
                vpcs=[a["VpcId"] for a in item.get("Attachments", []) or [] if a.get("State", "available") in ("available", "attached")],
            )
        for item in d.get("VpcEndpoints", []) or []:
            inv.vpc_endpoints[item["VpcEndpointId"]] = VpcEndpoint(
                id=item["VpcEndpointId"], type=item.get("VpcEndpointType", ""), service=item.get("ServiceName", ""),
                vpc=item.get("VpcId", ""), account=str(item.get("OwnerId") or account),
                subnets=item.get("SubnetIds", []) or [], route_tables=item.get("RouteTableIds", []) or [],
                security_groups=[g["GroupId"] for g in item.get("Groups", []) or []],
                state=str(item.get("State", "available")).lower(), tags=tags(item),
                dns_names=[e["DnsName"] for e in item.get("DnsEntries", []) or [] if e.get("DnsName")],
            )
        for item in d.get("ServiceConfigurations", []) or []:
            inv.endpoint_services[item["ServiceId"]] = EndpointService(
                id=item["ServiceId"], name=item.get("ServiceName", ""), account=account, region=region,
                load_balancers=(item.get("NetworkLoadBalancerArns") or []) + (item.get("GatewayLoadBalancerArns") or []),
                acceptance_required=bool(item.get("AcceptanceRequired")), tags=tags(item),
                allowed_principals=[p["Principal"] for p in item["AllowedPrincipals"]] if isinstance(item.get("AllowedPrincipals"), list) else None,
            )
        self._endpoint_connections.extend(d.get("VpcEndpointConnections", []) or [])
        for item in d.get("Clusters", []) or []:
            config = item.get("resourcesVpcConfig") or {}
            groups = [config["clusterSecurityGroupId"]] if config.get("clusterSecurityGroupId") else []
            inv.eks_clusters[item["arn"]] = EksCluster(
                id=item["arn"], name=item["name"], account=account, region=region, vpc=config.get("vpcId", ""),
                subnets=config.get("subnetIds", []) or [], version=item.get("version", ""),
                security_groups=groups + [g for g in config.get("securityGroupIds", []) or [] if g not in groups],
                endpoint_public=config.get("endpointPublicAccess"), endpoint_private=config.get("endpointPrivateAccess"),
            )
        for item in d.get("LoadBalancers", []) or []:
            arn = item["LoadBalancerArn"]
            inv.load_balancers[arn] = LoadBalancer(
                id=arn, name=item.get("LoadBalancerName", ""), type=item.get("Type", "application"),
                scheme=item.get("Scheme", "internal"), vpc=item.get("VpcId", ""), account=account,
                subnets=[z["SubnetId"] for z in item.get("AvailabilityZones", []) or [] if z.get("SubnetId")],
                security_groups=item.get("SecurityGroups", []) or [], dns_name=item.get("DNSName", ""),
            )
        self._listeners.extend(d.get("Listeners", []) or [])
        for item in d.get("TargetGroups", []) or []:
            inv.target_groups[item["TargetGroupArn"]] = TargetGroup(
                id=item["TargetGroupArn"], name=item.get("TargetGroupName", ""), protocol=item.get("Protocol", ""),
                port=item.get("Port"), vpc=item.get("VpcId", ""), load_balancers=item.get("LoadBalancerArns", []) or [],
                target_type=item.get("TargetType", ""),
                targets=[Target(id=str(t["Target"]["Id"]), port=t["Target"].get("Port"), health=(t.get("TargetHealth") or {}).get("State", ""))
                         for t in item["TargetHealthDescriptions"]] if isinstance(item.get("TargetHealthDescriptions"), list) else None,
            )
        for item in d.get("Firewalls", []) or []:
            firewall = item.get("Firewall", item)
            status = (item.get("FirewallStatus") or {}).get("SyncStates") or {}
            endpoints = [s["Attachment"]["EndpointId"] for s in status.values() if (s.get("Attachment") or {}).get("EndpointId")]
            self._raw_firewalls.append(firewall["FirewallArn"])
            inv.firewalls[firewall["FirewallArn"]] = Firewall(
                id=firewall["FirewallArn"], name=firewall.get("FirewallName", ""), vpc=firewall.get("VpcId", ""),
                account=account, policy=firewall.get("FirewallPolicyArn", ""), endpoints=endpoints,
                subnets=[m["SubnetId"] for m in firewall.get("SubnetMappings", []) or []],
            )
        for item in d.get("FirewallPolicies", []) or []:
            arn = (item.get("FirewallPolicyResponse") or {}).get("FirewallPolicyArn") or item.get("FirewallPolicyArn")
            self._policies[arn] = item.get("FirewallPolicy", {})
        for item in d.get("RuleGroups", []) or []:
            response = item.get("RuleGroupResponse") or {}
            self._rule_groups[response.get("RuleGroupArn") or item.get("RuleGroupArn")] = (
                response.get("RuleGroupName", ""), item.get("RuleGroup", {}),
            )
        for item in d.get("HostedZones", []) or []:
            zone_id = item["Id"].split("/")[-1]
            inv.hosted_zones[zone_id] = HostedZone(
                id=zone_id, name=item["Name"].rstrip(".").lower(), account=account,
                private=bool((item.get("Config") or {}).get("PrivateZone")),
                vpcs=[v.get("VPCId", "") for v in item.get("VPCs", []) or []],
                records=[
                    DnsRecord(
                        name=r["Name"].rstrip(".").lower(), type=r.get("Type", ""),
                        values=[v["Value"] for v in r.get("ResourceRecords", []) or []],
                        alias=((r.get("AliasTarget") or {}).get("DNSName") or "").rstrip("."),
                    )
                    for r in item.get("ResourceRecordSets", []) or []
                ],
            )
        for item in d.get("ResolverRules", []) or []:
            inv.resolver_rules[item["Id"]] = ResolverRule(
                id=item["Id"], domain=item["DomainName"].rstrip(".").lower(), type=item.get("RuleType", "FORWARD"),
                target_ips=[t["Ip"] for t in item.get("TargetIps", []) or [] if t.get("Ip")],
                endpoint=item.get("ResolverEndpointId", "") or "", account=str(item.get("OwnerId") or account),
            )
        self._resolver_associations.extend(d.get("ResolverRuleAssociations", []) or [])
        for item in d.get("ResolverEndpoints", []) or []:
            inv.resolver_endpoints[item["Id"]] = ResolverEndpoint(
                id=item["Id"], direction=item.get("Direction", "OUTBOUND"), vpc=item.get("HostVPCId", ""),
                subnets=[a["SubnetId"] for a in item.get("IpAddresses", []) or [] if a.get("SubnetId")],
                ips=[a["Ip"] for a in item.get("IpAddresses", []) or [] if a.get("Ip")],
                security_groups=item.get("SecurityGroupIds", []) or [],
            )

    def _add_hybrid(self, d, account):
        """Peering, VPN and Direct Connect, however many accounts each is seen from."""
        inv = self.inv

        def cidrs(info):
            found = [c["CidrBlock"] for c in info.get("CidrBlockSet", []) or [] if c.get("CidrBlock")]
            return found or ([info["CidrBlock"]] if info.get("CidrBlock") else [])

        for item in d.get("VpcPeeringConnections", []) or []:
            requester, accepter = item.get("RequesterVpcInfo") or {}, item.get("AccepterVpcInfo") or {}
            inv.vpc_peerings[item["VpcPeeringConnectionId"]] = VpcPeering(
                id=item["VpcPeeringConnectionId"], state=str((item.get("Status") or {}).get("Code", "")).lower(), tags=tags(item),
                requester_vpc=requester.get("VpcId", ""), requester_account=str(requester.get("OwnerId", "")), requester_cidrs=cidrs(requester),
                accepter_vpc=accepter.get("VpcId", ""), accepter_account=str(accepter.get("OwnerId", "")), accepter_cidrs=cidrs(accepter),
            )
        for item in d.get("VpnConnections", []) or []:
            inv.vpn_connections[item["VpnConnectionId"]] = VpnConnection(
                id=item["VpnConnectionId"], account=account, state=str(item.get("State", "")).lower(), tags=tags(item),
                customer_gateway=item.get("CustomerGatewayId", ""), vpn_gateway=item.get("VpnGatewayId", "") or "", tgw=item.get("TransitGatewayId", "") or "",
                static_only=bool((item.get("Options") or {}).get("StaticRoutesOnly")),
                static_routes=[r["DestinationCidrBlock"] for r in item.get("Routes", []) or [] if r.get("DestinationCidrBlock") and r.get("State", "available") == "available"],
                tunnels=[VpnTunnel(outside_ip=t.get("OutsideIpAddress", ""), status=str(t.get("Status", "")).upper(), message=t.get("StatusMessage", "") or "")
                         for t in item.get("VgwTelemetry", []) or []],
            )
        for item in d.get("VpnGateways", []) or []:
            inv.vpn_gateways[item["VpnGatewayId"]] = VpnGateway(
                id=item["VpnGatewayId"], account=account, state=str(item.get("State", "")).lower(), tags=tags(item),
                vpcs=[a["VpcId"] for a in item.get("VpcAttachments", []) or [] if a.get("State") == "attached"],
            )
        for item in d.get("CustomerGateways", []) or []:
            inv.customer_gateways[item["CustomerGatewayId"]] = CustomerGateway(
                id=item["CustomerGatewayId"], account=account, state=str(item.get("State", "")).lower(), ip=item.get("IpAddress", "") or "", tags=tags(item))
        for item in d.get("DxGateways", []) or []:
            inv.dx_gateways[item["directConnectGatewayId"]] = DxGateway(
                id=item["directConnectGatewayId"], name=item.get("directConnectGatewayName", ""), account=str(item.get("ownerAccount") or account),
                state=str(item.get("directConnectGatewayState", "")).lower(),
                associations=[DxAssociation(gateway=(a.get("associatedGateway") or {}).get("id") or a.get("virtualGatewayId", ""), state=str(a.get("associationState", "")).lower(),
                                            allowed_prefixes=[p["cidr"] for p in a.get("allowedPrefixesToDirectConnectGateway", []) or [] if p.get("cidr")])
                              for a in item.get("Associations", []) or []],
            )
        for item in d.get("DxVirtualInterfaces", []) or []:
            inv.dx_virtual_interfaces[item["virtualInterfaceId"]] = DxVirtualInterface(
                id=item["virtualInterfaceId"], name=item.get("virtualInterfaceName", ""), account=str(item.get("ownerAccount") or account),
                type=item.get("virtualInterfaceType", ""), state=str(item.get("virtualInterfaceState", "")).lower(), connection=item.get("connectionId", ""),
                dx_gateway=item.get("directConnectGatewayId", "") or "", vpn_gateway=item.get("virtualGatewayId", "") or "",
                bgp=[str(p.get("bgpStatus", "")).lower() for p in item.get("bgpPeers", []) or []],
            )

    def _route(self, route):
        destination = route.get("DestinationCidrBlock") or route.get("DestinationIpv6CidrBlock") or route.get("DestinationPrefixListId")
        if not destination:
            return None
        for key, kind in (
            ("TransitGatewayId", "transit-gateway"), ("NatGatewayId", "nat-gateway"),
            ("VpcPeeringConnectionId", "peering"), ("NetworkInterfaceId", "network-interface"),
            ("VpcEndpointId", "vpc-endpoint"), ("GatewayId", None), ("LocalGatewayId", "local-gateway"),
        ):
            target = route.get(key)
            if not target:
                continue
            if kind is None:
                kind = (
                    "local" if target == "local" else "internet-gateway" if target.startswith("igw-")
                    else "virtual-gateway" if target.startswith("vgw-") else "vpc-endpoint" if target.startswith("vpce-")
                    else "unknown"
                )
            return Route(destination=destination, target=target, target_type=kind, state=route.get("State", "active"))
        return Route(destination=destination, target="", target_type="unknown", state=route.get("State", "blackhole"))

    def _sg_rule(self, permission):
        return SgRule(
            protocol=protocol(permission.get("IpProtocol")),
            from_port=permission.get("FromPort"), to_port=permission.get("ToPort"),
            cidrs=[r["CidrIp"] for r in permission.get("IpRanges", []) or [] if r.get("CidrIp")]
            + [r["CidrIpv6"] for r in permission.get("Ipv6Ranges", []) or [] if r.get("CidrIpv6")],
            groups=[g["GroupId"] for g in permission.get("UserIdGroupPairs", []) or [] if g.get("GroupId")],
            prefix_lists=[p["PrefixListId"] for p in permission.get("PrefixListIds", []) or []],
            description="; ".join(r.get("Description", "") for r in permission.get("IpRanges", []) or [] if r.get("Description")),
        )

    def _nacl_rule(self, entry):
        ports = entry.get("PortRange") or {}
        return NaclRule(
            number=int(entry["RuleNumber"]), protocol=protocol(entry.get("Protocol")),
            action="allow" if entry.get("RuleAction") == "allow" else "deny",
            cidr=entry.get("CidrBlock") or entry.get("Ipv6CidrBlock") or "0.0.0.0/0",
            from_port=ports.get("From"), to_port=ports.get("To"),
        )

    def _tgw_route(self, route):
        attachments = route.get("TransitGatewayAttachments") or []
        return TgwRoute(
            destination=route.get("DestinationCidrBlock") or route.get("PrefixListId", ""),
            attachment=attachments[0]["TransitGatewayAttachmentId"] if attachments else route.get("TransitGatewayAttachmentId", ""),
            type=route.get("Type", "static"), state=route.get("State", "active"),
        )

    # Pieces joined after every file has been read

    def times(self, resource_id, created=None, updated=None, source="", created_by="", updated_by="", event="", changes=0):
        """Merge what one source says about a resource's times. A resource's own API
        field wins for creation; the latest change from any source wins for updates."""
        entry = self.inv.timestamps.setdefault(resource_id, ResourceTimes())
        rank = {"aws": 0, "config": 1, "cloudtrail": 2, "": 3}
        created = iso(created)
        if created and (not entry.created or rank[source] < rank[entry.created_source]
                        or (source == entry.created_source and created < entry.created)):
            entry.created, entry.created_source, entry.created_by = created, source, created_by or entry.created_by
        elif created and created_by and not entry.created_by and created == entry.created:
            entry.created_by = created_by
        updated = iso(updated)
        if updated and updated > entry.updated:
            entry.updated, entry.updated_source, entry.updated_by, entry.updated_event = updated, source, updated_by, event
        entry.changes += changes

    def _join_times(self):
        inv = self.inv
        # Config and CloudTrail name some resources by name; the inventory keys them by ARN.
        alias = {}
        for collection in ("load_balancers", "eks_clusters", "firewalls"):
            for item in getattr(inv, collection).values():
                alias[item.name] = item.id
                alias[item.id.split("/")[-1]] = alias.get(item.id.split("/")[-1], item.id)
        resolve = lambda key: key if key in alias.values() else alias.get(key, key)
        for item in self._config_items:
            key = resolve(item.get("resourceId", "")) if item.get("resourceId") else ""
            if not key:
                continue
            self.times(key, created=item.get("resourceCreationTime"), updated=item.get("configurationItemCaptureTime"), source="config",
                       event="AWS Config: configuration recorded")
        for key, summary in self._history.items():
            self.times(resolve(key), created=(summary.get("created") or {}).get("time"), source="cloudtrail",
                       created_by=(summary.get("created") or {}).get("by", ""))
            last = summary.get("updated") or {}
            self.times(resolve(key), updated=last.get("time"), source="cloudtrail", updated_by=last.get("by", ""),
                       event=last.get("event", ""), changes=int(summary.get("changes") or 0))
        for firewall in inv.firewalls.values():
            changes = [self._firewall_changes[a] for a in [firewall.policy] + [g.id for g in firewall.rule_groups] if a in self._firewall_changes]
            if changes:
                self.times(firewall.id, updated=max(changes), source="aws", event="Firewall policy or rule group changed")
        # Keep only resources the inventory has (and the rule groups inside firewalls).
        known = {i for c in COLLECTIONS for i in getattr(inv, c)} | {g.id for f in inv.firewalls.values() for g in f.rule_groups}
        inv.timestamps = {k: v for k, v in sorted(inv.timestamps.items()) if k in known and (v.created or v.updated)}

    def _join(self):
        inv = self.inv
        for vpc_id, item in self._vpc_attributes.items():
            vpc = inv.vpcs.get(vpc_id)
            if vpc:
                for key, field in (("EnableDnsSupport", "dns_support"), ("EnableDnsHostnames", "dns_hostnames")):
                    value = item.get(key)
                    if isinstance(value, dict):
                        value = value.get("Value")
                    if value is not None:
                        setattr(vpc, field, bool(value))
        for attachment_id, item in self._vpc_attachment_subnets.items():
            attachment = inv.tgw_attachments.get(attachment_id)
            if attachment:
                attachment.subnets = item.get("SubnetIds", []) or attachment.subnets
                attachment.resource = attachment.resource or item.get("VpcId", "")
            else:
                inv.tgw_attachments[attachment_id] = TgwAttachment(
                    id=attachment_id, tgw=item["TransitGatewayId"], type="vpc", resource=item.get("VpcId", ""),
                    resource_account=str(item.get("VpcOwnerId", "")), subnets=item.get("SubnetIds", []) or [],
                    state=item.get("State", "available"),
                )
        for item in self._listeners:
            lb = inv.load_balancers.get(item.get("LoadBalancerArn", ""))
            if not lb:
                self.warnings.append(f"Listener {item.get('ListenerArn', '?')} refers to an unknown load balancer.")
                continue
            groups = []
            for action in item.get("DefaultActions", []) or []:
                if action.get("TargetGroupArn"):
                    groups.append(action["TargetGroupArn"])
                for group in ((action.get("ForwardConfig") or {}).get("TargetGroups") or []):
                    groups.append(group["TargetGroupArn"])
            lb.listeners.append(Listener(
                id=item["ListenerArn"], port=int(item["Port"]), protocol=item.get("Protocol", ""),
                target_groups=list(dict.fromkeys(groups)),
            ))
        for firewall in (inv.firewalls[arn] for arn in self._raw_firewalls):
            policy = self._policies.get(firewall.policy)
            if policy is None:
                firewall.rule_data = False
                continue
            firewall.default_actions = policy.get("StatefulDefaultActions", []) or []
            firewall.rule_order = (policy.get("StatefulEngineOptions") or {}).get("RuleOrder", "DEFAULT_ACTION_ORDER")
            for reference in policy.get("StatefulRuleGroupReferences", []) or []:
                arn = reference["ResourceArn"]
                if arn not in self._rule_groups:
                    firewall.rule_data = False
                    firewall.rule_groups.append(FirewallRuleGroup(id=arn, name=arn.split("/")[-1], priority=reference.get("Priority")))
                    continue
                name, group = self._rule_groups[arn]
                firewall.rule_groups.append(FirewallRuleGroup(
                    id=arn, name=name or arn.split("/")[-1], priority=reference.get("Priority"),
                    rules=self._firewall_rules(group),
                ))
            firewall.rule_groups.sort(key=lambda g: (g.priority is None, g.priority or 0))
        for item in self._endpoint_connections:
            service = inv.endpoint_services.get(item.get("ServiceId", ""))
            if service:
                service.connections.append(EndpointConnection(
                    endpoint=item.get("VpcEndpointId", ""), owner=str(item.get("VpcEndpointOwner", "")),
                    state=str(item.get("VpcEndpointState", "")).lower()))
        for item in self._resolver_associations:
            rule = inv.resolver_rules.get(item.get("ResolverRuleId", ""))
            if rule and item.get("VPCId") and item["VPCId"] not in rule.vpcs:
                rule.vpcs.append(item["VPCId"])
        for vpc in inv.vpcs.values():
            if not vpc.role and vpc.account in inv.accounts:
                vpc.role = inv.accounts[vpc.account].type

    def _firewall_rules(self, group):
        source = group.get("RulesSource") or {}
        variables = {
            name: value.get("Definition", [])
            for name, value in ((group.get("RuleVariables") or {}).get("IPSets") or {}).items()
        }

        def expand(value):
            value = str(value)
            if value.startswith("$"):
                return variables.get(value[1:], [value])
            return [v.strip() for v in value.strip("[]").split(",") if v.strip()]

        rules = []
        for rule in source.get("StatefulRules", []) or []:
            header = rule.get("Header", {})
            sid = next((o.get("Settings", [""])[0] for o in rule.get("RuleOptions", []) or [] if o.get("Keyword") == "sid"), "")
            rules.append(FirewallRule(
                action=rule.get("Action", "PASS").lower(), protocol=header.get("Protocol", "IP").lower(),
                source=[s.lower() for s in expand(header.get("Source", "ANY"))],
                source_port=str(header.get("SourcePort", "ANY")).lower(),
                destination=[s.lower() for s in expand(header.get("Destination", "ANY"))],
                destination_port=str(header.get("DestinationPort", "ANY")).lower(), sid=str(sid),
            ))
        listing = source.get("RulesSourceList")
        if listing:
            action = "pass" if listing.get("GeneratedRulesType", "ALLOWLIST") == "ALLOWLIST" else "drop"
            rules.append(FirewallRule(
                action=action, protocol="tls" if "TLS_SNI" in listing.get("TargetTypes", []) else "http",
                destination_port="443" if "TLS_SNI" in listing.get("TargetTypes", []) else "80",
                domains=[t.lower() for t in listing.get("Targets", [])],
            ))
            if "TLS_SNI" in listing.get("TargetTypes", []) and "HTTP_HOST" in listing.get("TargetTypes", []):
                rules.append(FirewallRule(action=action, protocol="http", destination_port="80", domains=rules[-1].domains))
        return rules

    def _validate(self):
        inv = self.inv
        for subnet in inv.subnets.values():
            if not valid_cidr(subnet.cidr):
                self.errors.append(f"Subnet {subnet.id}: invalid CIDR {subnet.cidr!r}.")
            if subnet.vpc not in inv.vpcs:
                self.warnings.append(f"Subnet {subnet.id} belongs to VPC {subnet.vpc}, which is not in the import.")
        for table in inv.route_tables.values():
            for route in table.routes:
                if not route.destination.startswith("pl-") and not valid_cidr(route.destination):
                    self.errors.append(f"Route table {table.id}: invalid destination {route.destination!r}.")
        for attachment in inv.tgw_attachments.values():
            if attachment.tgw not in inv.transit_gateways:
                self.warnings.append(f"TGW attachment {attachment.id} refers to {attachment.tgw}, which is not in the import.")


def normalize(files):
    """files: [(name, parsed JSON)]. Returns (inventory, errors, warnings)."""
    normalizer = Normalizer()
    for name, document in sorted(files, key=lambda f: f[0]):
        normalizer.add_file(name, document)
    inventory = normalizer.finish()
    return inventory, normalizer.errors, normalizer.warnings


def counts(inventory):
    labels = {
        "accounts": "Accounts", "vpcs": "VPCs", "subnets": "Subnets", "route_tables": "Route tables",
        "transit_gateways": "Transit gateways", "tgw_attachments": "TGW attachments",
        "tgw_route_tables": "TGW route tables", "security_groups": "Security groups", "nacls": "NACLs",
        "load_balancers": "Load balancers", "target_groups": "Target groups", "firewalls": "Firewalls",
        "nat_gateways": "NAT gateways", "internet_gateways": "Internet gateways",
        "vpc_endpoints": "VPC endpoints", "endpoint_services": "Endpoint services", "vpc_peerings": "VPC peerings",
        "vpn_connections": "VPN connections", "dx_gateways": "Direct Connect gateways", "eks_clusters": "EKS clusters", "hosted_zones": "Route 53 zones",
        "resolver_rules": "Resolver rules", "resolver_endpoints": "Resolver endpoints",
    }
    return {key: {"label": label, "count": len(getattr(inventory, key))} for key, label in labels.items()}
