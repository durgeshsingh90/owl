"""Architecture Analyser: normalizer, analysis engine scenarios and API."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from main import app

from app.architecture import engine, store
from app.architecture.models import NaclRule, Route
from app.architecture.normalize import normalize

SAMPLE = Path(store.__file__).resolve().parent / "sample"


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "OWL_DB_PATH": self.temp.name + "/owl.db", "OWL_CONFIG_DIR": self.temp.name + "/config",
            "OWL_LOG_DIR": self.temp.name + "/logs",
        })
        self.env.start()
        _, inventory = store.inventory()
        self.inv = inventory.model_copy(deep=True)
        self.project = store.project("payment-platform")

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def run_env(self, environment, inventory=None, externals=None):
        return engine.analyse_project(
            inventory or self.inv, self.project, store.services(), externals or store.externals(), store.rules(), environment,
        )

    def result(self, environment, requirement, **kw):
        found = [r for r in self.run_env(environment, **kw)["results"] if r.requirement.id == requirement]
        self.assertEqual(len(found), 1, f"{requirement} not analysed in {environment}")
        return found[0]

    @staticmethod
    def failing(result):
        return [c for c in result.checks if c.status == "FAIL"]


class Normalizer(Base):
    def test_sample_export_normalizes_without_errors(self):
        files = [(str(p.relative_to(SAMPLE / "aws")), json.loads(p.read_text())) for p in (SAMPLE / "aws").rglob("*.json")]
        inventory, errors, warnings = normalize(files)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        self.assertEqual(len(inventory.accounts), 8)
        self.assertEqual(inventory.accounts["111111111111"].environments, ["DEV", "STAGE"])
        self.assertEqual(inventory.vpcs["vpc-0e55"].role, "central-egress")
        self.assertTrue(inventory.vpcs["vpc-0b22"].dns_support)
        firewall = next(iter(inventory.firewalls.values()))
        self.assertEqual(firewall.endpoints, ["vpce-0fw55a"])
        self.assertEqual([g.name for g in firewall.rule_groups], ["egress-allow", "egress-domains"])
        self.assertEqual(inventory.tgw_attachments["tgw-attach-0prod"].route_table, "tgw-rtb-0spoke")
        lb = next(lb for lb in inventory.load_balancers.values() if lb.name == "payments-prod-alb")
        self.assertEqual([l.port for l in lb.listeners], [1234])
        self.assertEqual(inventory.resolver_rules["rslvr-rr-0corp"].vpcs, ["vpc-0b22", "vpc-0c33"])

    def test_account_from_owner_id_and_bad_data(self):
        inventory, errors, warnings = normalize([
            ("vpcs.json", {"Vpcs": [{"VpcId": "vpc-1", "CidrBlock": "10.0.0.0/16", "OwnerId": "999999999999"}]}),
            ("subnets.json", {"Subnets": [{"SubnetId": "subnet-1", "VpcId": "vpc-9", "CidrBlock": "10.0.300.0/24"}]}),
            ("list.json", [1, 2]),
            ("other.json", {"Something": 1}),
        ])
        self.assertIn("999999999999", inventory.accounts)
        self.assertTrue(any("invalid CIDR" in e for e in errors))
        self.assertTrue(any("list.json" in e for e in errors))
        self.assertTrue(any("other.json" in w for w in warnings))
        self.assertTrue(any("vpc-9" in w for w in warnings))

    def test_normalized_documents_round_trip(self):
        inventory, errors, _ = normalize([("inv.json", json.loads(self.inv.model_dump_json(by_alias=True)))])
        self.assertEqual(errors, [])
        self.assertEqual(inventory.model_dump(), self.inv.model_dump())


class Scenarios(Base):
    """The test cases in the specification."""

    def test_1_successful_inbound(self):
        result = self.result("PROD", "conn-002")
        self.assertEqual(result.status, "PASS", result.summary)
        self.assertEqual([h.kind for h in result.path], ["workload", "tgw", "load-balancer", "workload"])
        names = {c.name for c in result.checks}
        self.assertIn("Listener on 1234", names)
        self.assertIn("Target group payment-api-prod", names)

    def test_2_missing_security_group_rule(self):
        self.inv.security_groups["sg-0b22lb"].inbound = []
        result = self.result("PROD", "conn-002")
        self.assertEqual(result.status, "FAIL")
        failed = self.failing(result)
        self.assertEqual([c.category for c in failed], ["Security Group"])
        self.assertIn("No rule in sg-0b22lb", failed[0].actual)
        self.assertEqual(failed[0].evidence[0].id, "sg-0b22lb")

    def test_2b_nacl_explicit_deny_differs_from_no_rule(self):
        nacl = self.inv.nacls["acl-0b22"]
        nacl.outbound.insert(0, NaclRule(number=50, protocol="tcp", action="deny", cidr="10.30.0.0/16", from_port=443, to_port=443))
        result = self.result("PROD", "conn-004")
        self.assertIn("Explicit deny: acl-0b22 rule #50", self.failing(result)[0].actual)
        nacl.outbound = [r for r in nacl.outbound if r.number == 32767]
        result = self.result("PROD", "conn-004")
        self.assertIn("default * rule denies", self.failing(result)[0].actual)

    def test_3_missing_tgw_route(self):
        spoke = self.inv.tgw_route_tables["tgw-rtb-0spoke"]
        spoke.routes = [r for r in spoke.routes if r.destination != "10.30.0.0/16"]
        # Without the specific route the default route sends it to Central Egress, which can't deliver it.
        spoke.routes = [r for r in spoke.routes if r.destination != "0.0.0.0/0"]
        result = self.result("PROD", "conn-004")
        self.assertEqual(result.status, "FAIL")
        failed = self.failing(result)[0]
        self.assertEqual(failed.category, "TGW")
        self.assertIn("No route in spoke matches 10.30.10.0/24", failed.actual)

    def test_3b_blackhole_tgw_route(self):
        route = next(r for r in self.inv.tgw_route_tables["tgw-rtb-0spoke"].routes if r.destination == "10.30.0.0/16")
        route.state, route.attachment = "blackhole", ""
        failed = self.failing(self.result("PROD", "conn-004"))[0]
        self.assertIn("blackhole", failed.actual)

    def test_4_missing_dns_association(self):
        zone = self.inv.hosted_zones["Z0SHAREDINT"]
        zone.vpcs = [v for v in zone.vpcs if v != "vpc-0b22"]
        result = self.result("PROD", "conn-004")
        self.assertEqual(result.status, "FAIL")
        dns = next(c for c in result.checks if c.category == "DNS")
        self.assertEqual(dns.status, "FAIL")
        self.assertIn("✓ Private hosted zone shared.internal (Z0SHAREDINT) exists", dns.actual)
        self.assertIn("✗ vpc-0b22 is not associated with it", dns.actual)

    def test_4b_dns_resolver_rule(self):
        self.assertEqual(next(c for c in self.result("PROD", "conn-003").checks if c.category == "DNS").status, "PASS")
        dev = next(c for c in self.result("DEV", "conn-003").checks if c.category == "DNS")
        self.assertEqual(dev.status, "FAIL")
        self.assertIn("Resolver rule rslvr-rr-0corp", dev.actual)

    def test_5_missing_external_information(self):
        result = self.result("PROD", "conn-006")
        self.assertEqual(result.status, "UNKNOWN")
        self.assertFalse(self.failing(result))
        self.assertIn("addresses supplied", result.summary)

    def test_5b_missing_firewall_rules_are_unknown_not_pass(self):
        firewall = next(iter(self.inv.firewalls.values()))
        firewall.rule_data, firewall.rule_groups = False, []
        result = self.result("DEV", "conn-001")
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(next(c for c in result.checks if c.category == "Firewall").status, "UNKNOWN")

    def test_5c_return_path_unknown_without_external_data(self):
        result = self.result("DEV", "conn-001")
        self.assertEqual(result.status, "PASS")
        external = next(c for c in result.checks if c.id == "ret-external")
        self.assertEqual(external.status, "UNKNOWN")
        self.assertEqual(external.actual, "External return-path information was not supplied.")

    def test_6_outbound_through_central_egress(self):
        result = self.result("DEV", "conn-001")
        self.assertEqual(result.status, "PASS", result.summary)
        self.assertEqual([h.kind for h in result.path],
                         ["workload", "tgw", "vpc", "firewall", "nat-gateway", "internet-gateway", "internet", "external"])
        firewall = next(c for c in result.checks if c.category == "Firewall")
        self.assertIn("egress-allow sid 1001", firewall.actual)
        standards = [c for c in result.checks if c.category == "Standards"]
        self.assertTrue(standards and all(c.status == "PASS" for c in standards))

    def test_6b_prod_firewall_rule_missing(self):
        result = self.result("PROD", "conn-001")
        self.assertEqual(result.status, "FAIL")
        failed = self.failing(result)
        self.assertEqual([c.category for c in failed], ["Firewall"])
        self.assertIn("Source 10.20.10.0/24, destination Payment Provider (203.0.113.10/32", failed[0].expected)
        self.assertIn("No matching rule found", failed[0].actual)
        self.assertEqual({e.kind for e in failed[0].evidence}, {"firewalls", "firewall_rule_groups"})

    def test_7_application_bypasses_central_egress(self):
        from app.architecture.models import InternetGateway
        self.inv.internet_gateways["igw-0b22"] = InternetGateway(id="igw-0b22", vpcs=["vpc-0b22"], account="222222222222")
        table = self.inv.route_tables["rtb-0b22app"]
        table.routes = [r for r in table.routes if r.destination != "0.0.0.0/0"] + [
            Route(destination="0.0.0.0/0", target="igw-0b22", target_type="internet-gateway")]
        result = self.result("PROD", "conn-001")
        self.assertEqual(result.status, "FAIL")
        rule = next(c for c in result.checks if c.rule == "OUTBOUND-CENTRAL-EGRESS")
        self.assertEqual(rule.status, "FAIL")
        self.assertIn("Detected: Payment API → Internet Gateway → Internet → Payment Provider", rule.actual)
        self.assertIn("missing tgw", rule.actual)
        direct = next(c for c in result.checks if c.rule == "NO-DIRECT-INTERNET")
        self.assertEqual(direct.status, "FAIL")
        self.assertFalse(any(c.category == "Firewall" for c in result.checks))

    def test_8_shared_account(self):
        dev, stage = self.run_env("DEV"), self.run_env("STAGE")
        for analysis in (dev, stage):
            by_id = {r.requirement.id: r for r in analysis["results"]}
            self.assertEqual(by_id["conn-002"].status, "PASS")
            self.assertEqual(by_id["conn-004"].status, "PASS")
            self.assertEqual(by_id["conn-002"].destination["account"], "111111111111")
        self.assertNotEqual(dev["results"][0].source["subnets"], stage["results"][0].source["subnets"])

    def test_inbound_listener_missing(self):
        result = self.result("PROD", "conn-005")
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.summary, "Listener on 8443: No listener on 8443. Listeners: HTTPS/1234.")

    def test_on_prem_return_path(self):
        result = self.result("PROD", "conn-003")
        self.assertEqual(result.status, "PASS")
        self.assertEqual(next(c for c in result.checks if c.id == "ret-external").status, "PASS")
        externals = [e.model_copy(update={"return_routes": ["10.10.0.0/16"]}) if e.id == "corporate-datacenter" else e
                     for e in store.externals()]
        result = self.result("PROD", "conn-003", externals=externals)
        self.assertEqual(result.status, "FAIL")
        self.assertIn("don't cover 10.20.10.0/24", next(c for c in result.checks if c.id == "ret-external").actual)

    def test_findings_and_determinism(self):
        first = self.run_env("PROD")
        second = self.run_env("PROD")
        dump = lambda a: json.dumps({k: [x.model_dump() for x in v] if isinstance(v, list) else v for k, v in a.items()}, sort_keys=True)
        self.assertEqual(dump(first), dump(second))
        critical = [f for f in first["findings"] if f.severity == "critical"]
        self.assertEqual([f.id for f in critical], ["F-001", "F-002"])
        self.assertEqual(critical[0].title, "PROD Payment API cannot reach Payment Provider over TCP/443.")


class Hybrid(Base):
    """Peering, VPN to another cloud and Direct Connect in the analysis."""

    def checks(self, result, category):
        return [c for c in result.checks if c.category == category]

    def test_peering_passes_both_ways(self):
        result = self.result("PROD", "conn-010")
        self.assertEqual(result.status, "PASS", result.summary)
        self.assertEqual([h.kind for h in result.path], ["workload", "peering", "workload"])
        self.assertIn("Active", self.checks(result, "Peering")[0].actual)
        self.assertEqual(next(c for c in result.checks if c.id == "ret-route").status, "PASS")

    def test_peering_not_active_fails(self):
        self.inv.vpc_peerings["pcx-0analytics"].state = "pending-acceptance"
        failed = self.failing(self.result("PROD", "conn-010"))
        self.assertEqual(failed[0].category, "Peering")
        self.assertIn("pending-acceptance", failed[0].actual)

    def test_peering_is_not_transitive(self):
        self.inv.vpcs["vpc-0g70"].cidrs = ["10.71.0.0/16"]
        failed = self.failing(self.result("PROD", "conn-010"))
        self.assertIn("isn't transitive", failed[0].actual)

    def test_denied_peering_is_unknown(self):
        del self.inv.vpc_peerings["pcx-0analytics"]
        self.inv.access_denied = {"222222222222": ["VpcPeeringConnections"]}
        result = self.result("PROD", "conn-010")
        self.assertEqual(result.status, "UNKNOWN")
        self.assertIn("No read access", self.checks(result, "Peering")[0].actual)

    def test_vpn_to_azure(self):
        result = self.result("PROD", "conn-009")
        self.assertEqual(result.status, "PASS", result.summary)
        self.assertEqual([h.kind for h in result.path], ["workload", "tgw", "vpn", "external"])
        self.assertEqual(result.destination["kind"], "other-cloud")
        self.assertEqual(next(c for c in result.checks if c.id == "ret-external").status, "PASS")
        self.assertEqual(next(c for c in result.checks if c.id == "ret-tgw").status, "PASS")

    def test_vpn_tunnels(self):
        tunnels = self.inv.vpn_connections["vpn-0azure"].tunnels
        tunnels[0].status = "DOWN"
        self.assertEqual(self.result("PROD", "conn-009").status, "WARNING")
        tunnels[1].status = "DOWN"
        result = self.result("PROD", "conn-009")
        self.assertEqual(result.status, "FAIL")
        self.assertIn("All tunnels are down", self.failing(result)[0].actual)

    def test_vpn_details_missing_is_advisory(self):
        del self.inv.vpn_connections["vpn-0azure"]
        result = self.result("PROD", "conn-009")
        self.assertEqual(result.status, "PASS")
        self.assertEqual(self.checks(result, "VPN")[0].status, "UNKNOWN")

    def test_direct_connect(self):
        result = self.result("PROD", "conn-003")
        self.assertEqual(result.status, "PASS", result.summary)
        self.assertEqual({c.status for c in self.checks(result, "Direct Connect")}, {"PASS"})
        association = self.inv.dx_gateways["dxgw-0corp"].associations[0]
        association.allowed_prefixes = ["10.10.0.0/16"]
        self.assertIn("Allowed prefixes", self.failing(self.result("PROD", "conn-003"))[0].actual)
        association.allowed_prefixes = ["10.0.0.0/8"]
        self.inv.dx_virtual_interfaces["dxvif-0corp1"].bgp = ["down"]
        self.assertIn("Virtual interfaces for dxgw-0corp", self.failing(self.result("PROD", "conn-003"))[0].name)
        self.inv.dx_virtual_interfaces["dxvif-0corp1"].bgp = ["up"]
        association.state = "disassociated"
        self.assertIn("disassociated", self.failing(self.result("PROD", "conn-003"))[0].actual)

    def test_route_to_virtual_gateway(self):
        from app.architecture.models import ExternalSystem, Route, VpnConnection, VpnGateway, VpnTunnel

        self.inv.vpn_gateways["vgw-1"] = VpnGateway(id="vgw-1", vpcs=["vpc-0b22"])
        self.inv.vpn_connections["vpn-1"] = VpnConnection(id="vpn-1", state="available", vpn_gateway="vgw-1", static_only=True, static_routes=["10.99.0.0/16"],
                                                          tunnels=[VpnTunnel(status="UP")])
        table = self.inv.route_tables["rtb-0b22app"]
        table.routes = [r for r in table.routes if r.target != "pcx-0analytics"] + [Route(destination="10.70.0.0/16", target="vgw-1", target_type="virtual-gateway")]
        system = ExternalSystem(id="analytics-api", name="DC", type="on-prem", networks=["10.70.0.0/16"], return_routes=["10.20.0.0/16"])
        analyzer = engine.Analyzer(self.inv, self.project, [], [system], [])
        trace = analyzer.trace("subnet-0b22prd-a", "10.70.0.0/16", analyzer.resolve("analytics-api", "PROD", True, self.project.requirements[0]), ("tcp", 443, "10.20.10.0/24", "10.70.0.0/16", ""))
        self.assertTrue(any(c.name == "VPN vpn-1 static route" and c.status == "FAIL" for c in trace.checks))

    def test_declared_flow_through_peering_and_wrong_vpn(self):
        from app.architecture.models import FlowHop, Requirement

        self.project.requirements.append(Requirement(id="flow-1", environment="PROD", protocol="TCP", port=443, hops=[
            FlowHop(kind="workload", id="payment-api"), FlowHop(kind="vpc-peering", id="pcx-0analytics"), FlowHop(kind="workload", id="analytics-api")]))
        self.assertEqual(self.result("PROD", "flow-1").status, "PASS")
        self.project.requirements.append(Requirement(id="flow-2", environment="PROD", protocol="TCP", port=443, hops=[
            FlowHop(kind="workload", id="payment-api"), FlowHop(kind="vpn-connection", id="vpn-0azure"), FlowHop(kind="workload", id="analytics-api")]))
        self.assertIn("not on the routed path", self.failing(self.result("PROD", "flow-2"))[0].actual)


class Flows(Base):
    """Declared hop-by-hop flows: PrivateLink, NLB to EKS, the chosen NAT gateway."""

    def test_privatelink_to_eks_then_secure_access(self):
        result = self.result("PROD", "conn-007")
        self.assertEqual(result.status, "PASS", result.summary)
        labels = [h.label for h in result.path]
        self.assertEqual(labels[:5], ["Payment API", "risk-engine-endpoint", "risk-engine", "risk-nlb", "EKS risk-engine"])
        self.assertIn("Secure Access Gateway", labels[-1])
        names = {c.name: c for c in result.checks}
        self.assertEqual(names["Connection accepted by the provider"].status, "PASS")
        self.assertIn("10.30.20.15:8443 healthy", names["risk-engine-pods targets EKS risk-engine"].actual)
        # After the target group the port is 8443; the EKS hop sets 443 for the secure access leg.
        self.assertIn("8443", names["EKS risk-engine (from risk-nlb) security group ingress"].expected)

    def test_pending_privatelink_connection_fails(self):
        self.inv.endpoint_services["vpce-svc-0risk00000000001"].connections[0].state = "pendingacceptance"
        failed = self.failing(self.result("PROD", "conn-007"))
        self.assertEqual([c.name for c in failed], ["Connection accepted by the provider"])

    def test_unhealthy_targets_fail(self):
        group = next(g for g in self.inv.target_groups.values() if g.name == "risk-engine-pods")
        for target in group.targets:
            target.health = "unhealthy"
        failed = self.failing(self.result("PROD", "conn-007"))
        self.assertEqual(failed[0].category, "Load Balancer")
        self.assertIn("unhealthy", failed[0].actual)

    def test_wrong_nat_gateway_is_reported(self):
        from app.architecture.models import NatGateway

        self.inv.nat_gateways["nat-other"] = NatGateway(id="nat-other", vpc="vpc-0e55", subnet="subnet-0e55pub-a", public_ips=["198.51.100.99"])
        hop = next(h for h in self.project.requirements if h.id == "conn-008").hops[3]
        hop.id = "nat-other"
        result = self.result("DEV", "conn-008")
        self.assertEqual(result.status, "FAIL")
        failed = self.failing(result)[0]
        self.assertEqual(failed.name, "Passes through nat-other")
        self.assertIn("✗ nat-other is not on the routed path", failed.actual)

    def test_missing_hop_resource(self):
        hop = next(h for h in self.project.requirements if h.id == "conn-007").hops[3]
        hop.id = "no-such-nlb"
        result = self.result("PROD", "conn-007")
        self.assertEqual(result.status, "FAIL")
        self.assertIn("No load balancer no-such-nlb", self.failing(result)[0].actual)


class Times(Base):
    """Created and updated times: a resource's own API field, AWS Config and CloudTrail."""

    def test_sources_merge(self):
        files = [
            ("123456789012/eu-west-2/ec2.json", {"Region": "eu-west-2",
                "Vpcs": [{"VpcId": "vpc-1", "CidrBlock": "10.0.0.0/16"}],
                "NatGateways": [{"NatGatewayId": "nat-1", "VpcId": "vpc-1", "SubnetId": "s", "CreateTime": "2025-01-02 03:04:05+00:00"}]}),
            ("123456789012/eu-west-2/config.json", {"Region": "eu-west-2", "ConfigResources": [
                {"resourceId": "vpc-1", "resourceType": "AWS::EC2::VPC", "resourceCreationTime": "2024-05-01T10:00:00.000Z",
                 "configurationItemCaptureTime": "2026-01-01T09:00:00.000Z"},
                {"resourceId": "nat-1", "resourceType": "AWS::EC2::NatGateway", "resourceCreationTime": "2025-01-02T03:10:00Z"},
                {"resourceId": "vpc-gone", "resourceType": "AWS::EC2::VPC", "resourceCreationTime": "2024-01-01T00:00:00Z"}]}),
            ("123456789012/eu-west-2/cloudtrail.json", {"Region": "eu-west-2", "ResourceHistory": {
                "vpc-1": {"changes": 3, "updated": {"time": "2026-10-20T08:00:00+00:00", "by": "alice", "event": "ModifyVpcAttribute"}}}}),
        ]
        inventory, errors, _ = normalize(files)
        self.assertEqual(errors, [])
        nat, vpc = inventory.timestamps["nat-1"], inventory.timestamps["vpc-1"]
        self.assertEqual((nat.created, nat.created_source), ("2025-01-02T03:04:05+00:00", "aws"))  # the API's own field wins
        self.assertEqual((vpc.created, vpc.created_source), ("2024-05-01T10:00:00+00:00", "config"))
        self.assertEqual((vpc.updated, vpc.updated_source, vpc.updated_by, vpc.changes), ("2026-10-20T08:00:00+00:00", "cloudtrail", "alice", 3))
        self.assertNotIn("vpc-gone", inventory.timestamps)  # only resources in the inventory

    def test_timeline_newest_first(self):
        from app.architecture.models import ResourceTimes

        self.inv.timestamps = {
            "vpc-0b22": ResourceTimes(created="2025-01-01T00:00:00+00:00", created_source="config"),
            "sg-0b22app": ResourceTimes(created="2025-02-01T00:00:00+00:00", created_source="cloudtrail", updated="2026-10-01T12:00:00+00:00",
                                        updated_by="bob", updated_source="cloudtrail", updated_event="AuthorizeSecurityGroupIngress", changes=2),
        }
        meta = store.snapshot_meta()
        with patch("app.architecture.store.inventory", return_value=(meta, self.inv)):
            body = TestClient(app).get(f"/api/architecture/snapshots/{meta['id']}/timeline").json()
        self.assertEqual([(e["id"], e["action"]) for e in body["events"]],
                         [("sg-0b22app", "Updated"), ("sg-0b22app", "Created"), ("vpc-0b22", "Created")])
        self.assertEqual((body["events"][0]["kind"], body["events"][0]["by"], body["events"][0]["account"]), ("Security group", "bob", "222222222222"))

    def test_download_settings(self):
        client = TestClient(app)
        config = Path(self.temp.name) / "aws-config"
        config.write_text("".join(f"[profile mc-{n}-{e}]\nsso_account_id = {a}\nregion = eu-west-2\n" for n, e, a in (
            ("payments", "prod", "111111111111"), ("payments", "dev", "222222222222"), ("payments", "nonp", "333333333333"),
            ("edge", "prod", "444444444444"))) + "[profile other]\nsso_account_id = 555555555555\n")
        with patch("app.architecture.download.config_path", return_value=str(config)):
            body = client.get("/api/architecture/download").json()
            self.assertEqual((body["total"], body["unique"], body["selected"]), (5, 3, 0))
            payments = next(g for g in body["groups"] if g["name"] == "payments")
            self.assertEqual([a["stage"] for a in payments["accounts"]], ["dev", "nonp", "prod"])
            self.assertEqual(client.put("/api/architecture/download/selection", json={"account_ids": ["111111111111", "111111111111"]}).json(),
                             {"selected": ["111111111111"]})
            self.assertEqual(client.get("/api/architecture/download").json()["selected"], 1)
            self.assertEqual(client.put("/api/architecture/download/selection", json={"account_ids": ["12"]}).status_code, 400)
            with patch("app.aws.connection.run_sts", return_value=({"Account": "111111111111", "Arn": "arn:x"}, None)), \
                    patch("app.architecture.download.check_access", return_value={"allowed": ["Vpcs"], "denied": ["Firewalls"], "errors": {}}), \
                    patch("app.architecture.download.account_details", return_value={"created": "2020-01-02T00:00:00+00:00", "account_name": "Payments", "created_error": ""}):
                tested = client.post("/api/architecture/download/test/111111111111").json()
                self.assertEqual((tested["ok"], tested["denied"], tested["created"]), (True, ["Firewalls"], "2020-01-02T00:00:00+00:00"))
                listing = client.get("/api/architecture/download").json()
                self.assertEqual(listing["top"][0], "111111111111")  # tested, so it counts as used
                saved = next(a for g in listing["groups"] for a in g["accounts"] if a["account_id"] == "111111111111")
                self.assertEqual((saved["info"]["connected"], saved["info"]["created"]), (True, "2020-01-02T00:00:00+00:00"))
                wrong = client.post("/api/architecture/download/test/222222222222").json()
                self.assertFalse(wrong["ok"])
                self.assertIn("signs in to 111111111111", wrong["error"])
            with patch("app.aws.connection.run_sts", return_value=(None, "ExpiredToken")):
                self.assertEqual(client.post("/api/architecture/download/test/111111111111").json()["error"], "ExpiredToken")
            missing = client.post("/api/architecture/download/test/999999999999").json()
            self.assertIn("No profile", missing["error"])

    def test_download_runs_the_aws_command(self):
        from types import SimpleNamespace
        from app.architecture import download

        config = Path(self.temp.name) / "aws-config"
        config.write_text("[profile mc-a-prod]\nsso_account_id = 111111111111\nregion = eu-west-2\n")
        commands = []

        def fake(command, **kwargs):
            if command[0] != "aws":  # nslookup
                return SimpleNamespace(returncode=0, stderr="", stdout="")
            commands.append(command)
            operation = command[2]
            if operation == "describe-vpcs":
                return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"Vpcs": [{"VpcId": "vpc-1", "CidrBlock": "10.0.0.0/16"}]}))
            if operation == "list-firewalls":
                return SimpleNamespace(returncode=254, stderr="\nAn error occurred (AccessDenied) when calling ListFirewalls\n", stdout="")
            return SimpleNamespace(returncode=0, stderr="", stdout="{}")

        with patch("app.architecture.download.config_path", return_value=str(config)), \
                patch("app.aws.connection.run_sts", return_value=({"Account": "111111111111", "Arn": "arn:x"}, None)), \
                patch("app.aws.connection._aws", return_value="aws"), patch("app.architecture.download.subprocess.run", side_effect=fake):
            manifest = download.download_account({"account_id": "111111111111"})
        self.assertTrue(all(c[0] == "aws" and c[c.index("--profile") + 1] == "mc-a-prod" and c[-2:] != [] for c in commands))
        self.assertTrue(all(c[1] != "route53" or "--region" in c for c in commands))
        self.assertEqual(next(c for c in commands if c[2] == "describe-vpcs")[-2:], ["--region", "eu-west-2"])
        self.assertEqual(manifest["Download"]["denied"], ["Firewalls"])  # denied at the check: recorded, the rest still downloads
        self.assertEqual(sum(c[2] == "list-firewalls" for c in commands), 1)  # the check's one try, never again
        self.assertIn("--max-items", next(c for c in commands if c[2] == "list-firewalls"))
        self.assertNotIn("--max-items", next(c for c in commands if c[2] == "describe-vpn-connections"))
        self.assertTrue((download.media_folder() / "111111111111-a" / "prod" / "eu-west-2" / "ec2.json").exists())
        self.assertEqual({c[1] for c in commands}, {"ec2", "elbv2", "eks", "network-firewall", "route53resolver", "route53", "account", "directconnect", "elb", "apigateway", "apigatewayv2", "autoscaling"})
        inventory, errors, _ = normalize(download.snapshot_files(["111111111111"]))
        self.assertEqual(errors, [])
        self.assertIn("vpc-1", inventory.vpcs)
        state = download.state_of("111111111111")
        self.assertEqual([(r["type"], r["id"]) for r in state["resources"]], [("aws_vpc", "vpc-1")])
        self.assertEqual(state["resources"][0]["attributes"]["CidrBlock"], "10.0.0.0/16")
        self.assertEqual(inventory.access_denied, {"111111111111": ["Firewalls"]})

    def test_account_details(self):
        from types import SimpleNamespace
        from app.architecture import download

        with patch("app.aws.connection._aws", return_value="aws"), patch("app.architecture.download.subprocess.run", return_value=SimpleNamespace(
                returncode=0, stderr="", stdout=json.dumps({"AccountCreatedDate": "2025-09-09T01:41:20+00:00", "AccountName": "Pay"}))) as run:
            self.assertEqual(download.account_details("mc-a-prod"), {"created": "2025-09-09T01:41:20+00:00", "account_name": "Pay", "created_error": ""})
            self.assertEqual(run.call_args[0][0][1:3], ["account", "get-account-information"])
        with patch("app.aws.connection._aws", return_value="aws"), patch("app.architecture.download.subprocess.run", return_value=SimpleNamespace(
                returncode=254, stdout="", stderr="An error occurred (AccessDeniedException)")):
            self.assertIn("AccessDenied", download.account_details("mc-a-prod")["created_error"])

    def test_parse_nslookup_windows_and_unix(self):
        from app.architecture import download

        windows = ("Server:  dns.corp.example\nAddress:  10.1.1.1\n\nNon-authoritative answer:\nName:    app.corp.sep\n"
                   "Addresses:  10.2.3.4\n          10.2.3.5\nAliases:  other.corp.sep\n")
        self.assertEqual(download.parse_nslookup(windows), ("10.1.1.1", ["10.2.3.4", "10.2.3.5"]))
        unix = ("Server:\t\t192.168.1.1\nAddress:\t192.168.1.1#53\n\nNon-authoritative answer:\napp.sep\tcanonical name = b.sep.\n"
                "Name:\tb.sep\nAddress: 10.9.9.9\nName:\tb.sep\nAddress: fd00::1\n")
        self.assertEqual(download.parse_nslookup(unix)[1], ["10.9.9.9", "fd00::1"])
        self.assertEqual(download.parse_nslookup("Server: x\nAddress: 10.1.1.1\n\n*** x can't find a.sep: Non-existent domain\n")[1], [])

    def test_nslookup_results_and_timeout_stop(self):
        from types import SimpleNamespace
        from app.architecture import download

        def answer(output):
            return SimpleNamespace(returncode=0, stdout=output, stderr="")

        with patch("app.architecture.download.shutil.which", return_value="nslookup"):
            with patch("app.architecture.download.subprocess.run", return_value=answer(";; connection timed out; no servers could be reached")):
                self.assertEqual(download.nslookup("a.sep")["status"], "timeout")
                many = {f"h{i}.sep": "Route 53" for i in range(40)}
                results = download.run_lookups(many)
                self.assertEqual(sum(r["status"] == "timeout" for r in results), download.LOOKUP_TRIAL)
                self.assertEqual(sum(r["status"] == "not_tried" for r in results), 8)
            with patch("app.architecture.download.subprocess.run", side_effect=download.subprocess.TimeoutExpired("nslookup", 20)):
                self.assertEqual(download.nslookup("a.sep")["status"], "timeout")
            with patch("app.architecture.download.subprocess.run", return_value=answer("Server: x\nAddress: 10.1.1.1\n\n*** can't find a.sep: Non-existent domain")):
                self.assertEqual(download.nslookup("a.sep")["status"], "not_found")
            with patch("app.architecture.download.subprocess.run", return_value=answer("Server: x\nAddress: 10.1.1.1\n\nName: a.sep\nAddress: 10.2.2.2")):
                self.assertEqual(download.nslookup("a.sep")["ips"], ["10.2.2.2"])

    def test_lookup_check_is_advisory(self):
        from app.architecture.models import DnsLookup

        analyzer = engine.Analyzer(self.inv, self.project, store.services(), store.externals(), store.rules())
        self.inv.dns_lookups["a.sep"] = DnsLookup(name="a.sep", status="timeout", detail="The DNS server didn't answer in time.")
        check = analyzer.lookup_check("A.sep")
        self.assertEqual((check.status, check.blocking), ("UNKNOWN", False))
        self.assertIn("Timed out", check.actual)
        self.assertIsNone(analyzer.lookup_check("b.sep"))

    def test_state_has_properties_and_no_rule_or_log_reads(self):
        from app.architecture import download

        folder = Path(self.temp.name) / "acct"
        download._write(folder / "eu-west-2" / "elbv2.json", {"LoadBalancers": [{"LoadBalancerArn": "arn:lb", "LoadBalancerName": "n1", "CreatedTime": "2025-01-02T03:04:05Z",
                        "Attributes": [{"Key": "deletion_protection.enabled", "Value": "true"}]}]})
        download._write(folder / "global" / "route53.json", {"HostedZones": [{"Id": "/hostedzone/Z1", "Name": "a.sep.", "ResourceRecordSets": [{"Name": "b.a.sep.", "Type": "A"}]}]})
        state = download.build_state(folder, "111111111111", "mc-a-prod", ["eu-west-2"])
        lb = state["resources"][0]
        self.assertEqual((lb["type"], lb["name"], lb["created_at"]), ("aws_lb", "n1", "2025-01-02T03:04:05+00:00"))
        self.assertEqual(lb["attributes"]["Attributes"][0]["Value"], "true")
        self.assertEqual([r["type"] for r in state["resources"][1:]], ["aws_route53_zone", "aws_route53_record"])
        self.assertFalse({"Results", "Events"} & {p[0] for p in download.PROBES})

    def test_no_access_downloads_nothing(self):
        from types import SimpleNamespace
        from app.architecture import download

        config = Path(self.temp.name) / "aws-config"
        config.write_text("[profile mc-a-prod]\nsso_account_id = 111111111111\nregion = eu-west-2\n")
        denied = SimpleNamespace(returncode=254, stdout="", stderr="An error occurred (UnauthorizedOperation)")
        with patch("app.architecture.download.config_path", return_value=str(config)), \
                patch("app.aws.connection.run_sts", return_value=({"Account": "111111111111", "Arn": "arn:x"}, None)), \
                patch("app.aws.connection._aws", return_value="aws"), patch("app.architecture.download.subprocess.run", return_value=denied):
            with self.assertRaisesRegex(PermissionError, "nothing was downloaded"):
                download.download_account({"account_id": "111111111111"})
        self.assertIsNone(download.find_folder("111111111111"))

    def test_denied_data_is_unknown_not_fail(self):
        self.inv.access_denied = {"333333333333": ["LoadBalancers", "clusters"]}
        del self.inv.load_balancers[next(k for k, v in self.inv.load_balancers.items() if v.name == "risk-nlb")]
        result = self.result("PROD", "conn-007")
        self.assertEqual(result.status, "UNKNOWN")
        self.assertFalse(self.failing(result))
        self.inv.access_denied = {}
        self.assertEqual(self.result("PROD", "conn-007").status, "FAIL")  # without the denial, missing means missing


class Api(Base):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app)

    def test_overview_analysis_and_evidence(self):
        overview = self.client.get("/api/architecture/overview").json()
        self.assertEqual([p["id"] for p in overview["projects"]], ["payment-platform"])
        snapshot = overview["snapshots"][0]["id"]
        response = self.client.get("/api/architecture/analysis", params={"project": "payment-platform", "environment": "PROD"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["health"]["connectivity"]["total"], 9)
        self.assertEqual([a["id"] for a in body["accounts"]], ["111111111111", "222222222222"])
        evidence = self.client.get(f"/api/architecture/snapshots/{snapshot}/object", params={"kind": "route_tables", "id": "rtb-0b22app"})
        self.assertEqual(evidence.json()["object"]["vpc"], "vpc-0b22")
        account = self.client.get(f"/api/architecture/snapshots/{snapshot}/accounts/222222222222").json()
        self.assertEqual([v["id"] for v in account["vpcs"]], ["vpc-0b22"])
        self.assertEqual(self.client.get("/api/architecture/analysis", params={"project": "payment-platform", "environment": "QA"}).status_code, 400)
        self.assertEqual(self.client.get("/api/architecture/analysis", params={"project": "nope", "environment": "PROD"}).status_code, 404)

    def test_resource_picker(self):
        snapshot = store.snapshot_meta()["id"]
        get = lambda **q: self.client.get(f"/api/architecture/snapshots/{snapshot}/resources", params=q).json()["items"]
        self.assertEqual([i["id"] for i in get(kind="nat-gateway", account="555555555555")], ["nat-0e55a"])
        self.assertEqual(get(kind="nat-gateway", account="222222222222"), [])
        self.assertEqual([i["label"] for i in get(kind="eks-cluster")], ["risk-engine"])
        self.assertEqual(self.client.get(f"/api/architecture/snapshots/{snapshot}/resources", params={"kind": "nope"}).status_code, 400)

    def test_import_validates_then_stores(self):
        files = [{"name": "123456789012/ec2.json", "content": json.dumps({"Vpcs": [{"VpcId": "vpc-new", "CidrBlock": "10.9.0.0/16"}]})},
                 {"name": "broken.json", "content": "{nope"}]
        check = self.client.post("/api/architecture/snapshots/validate", json={"name": "x", "files": files}).json()
        self.assertEqual(check["counts"]["vpcs"]["count"], 1)
        self.assertTrue(any("broken.json" in e for e in check["errors"]))
        self.assertEqual(self.client.post("/api/architecture/snapshots", json={"name": "x", "files": files}).status_code, 400)
        created = self.client.post("/api/architecture/snapshots", json={"name": "Second", "files": files[:1]})
        self.assertEqual(created.status_code, 201)
        listing = self.client.get("/api/architecture/overview").json()["snapshots"]
        self.assertEqual(listing[0]["name"], "Second")
        self.assertEqual(self.client.delete(f"/api/architecture/snapshots/{created.json()['id']}").status_code, 200)
        self.assertEqual(self.client.get("/api/architecture/snapshots/nope/accounts").status_code, 404)


class Download(Base):
    """Download from AWS: only the project's accounts, into the shared media folder. AWS is stubbed."""

    def setUp(self):
        super().setUp()
        self.config = Path(self.temp.name) / "aws-config"
        self.config.write_text("[default]\nlogin_session = arn:aws:iam::123456789012:root\nregion = ap-south-1\n"
                               "[profile prod]\nrole_arn = arn:aws:iam::222222222222:role/Reader\nregion = eu-west-2\n")
        self.paths = patch("app.architecture.download.config_path", return_value=str(self.config))
        self.paths.start()

    def tearDown(self):
        self.paths.stop()
        super().tearDown()

    def test_profiles_and_involved_accounts(self):
        from app.architecture import download

        found = download.profiles_by_account()
        self.assertEqual([p["profile"] for p in found["123456789012"]], ["default"])
        self.assertEqual([p["profile"] for p in found["222222222222"]], ["prod"])
        accounts = {a["account_id"]: a for a in download.involved_accounts(self.project)}
        self.assertEqual(sorted(accounts), ["111111111111", "222222222222", "333333333333", "444444444444", "555555555555",
                                            "666666666666", "777777777777", "888888888888"])
        self.assertEqual(accounts["555555555555"]["type"], "central-egress")
        self.assertEqual(accounts["111111111111"]["environments"], ["DEV", "STAGE"])

    def test_download_writes_media_folder_and_snapshot(self):
        from app.architecture import download

        sample = {p.parent.name.split("-")[0] + "/" + p.name: json.loads(p.read_text()) for p in (SAMPLE / "aws").rglob("*.json")}

        def fake(account, progress=lambda t: None, history=None):
            folder = download.media_folder() / f"{account['account_id']}-x" / "prod"
            for name, data in sample.items():
                if name.startswith(account["account_id"]) and not name.endswith("account.json"):
                    download._write(folder / "eu-west-2" / name.split("/")[1], data)
            manifest = {"Account": {"Id": account["account_id"], "Type": account["type"] or "application", "Region": "eu-west-2"},
                        "Download": {"errors": [], "denied": [], "downloaded_at": "now"}}
            download._write(folder / "account.json", manifest)
            return manifest

        with patch("app.architecture.download.download_account", side_effect=fake):
            client = TestClient(app)
            self.assertEqual(client.post("/api/architecture/projects/payment-platform/refresh").status_code, 202)
            for _ in range(200):
                job = client.get("/api/architecture/projects/payment-platform/refresh").json()
                if job["state"] != "running":
                    break
                __import__("time").sleep(0.05)
        self.assertEqual(job["state"], "done", job)
        self.assertTrue((download.media_folder() / "222222222222-x" / "prod" / "eu-west-2" / "ec2.json").exists())
        meta = store.snapshot_meta(job["snapshot"])
        self.assertEqual(meta["errors"], [])
        self.assertIn("222222222222", meta["accounts"])
        listed = sorted(a["account_id"] for a in download.involved_accounts(self.project))
        self.assertEqual(meta["accounts"], listed)  # exactly the project's accounts
        self.assertEqual(sorted(p.name.split("-")[0] for p in download.media_folder().iterdir()), listed)
        info = client.get("/api/architecture/projects/payment-platform/aws").json()
        self.assertEqual(next(a for a in info["accounts"] if a["account_id"] == "222222222222")["profile"], "prod")

    def test_failed_sign_in_is_reported_per_account(self):
        from app.architecture import download

        with self.assertRaisesRegex(ValueError, "No profile for account 999999999999"):
            download.download_account({"account_id": "999999999999"})


if __name__ == "__main__":
    unittest.main()
