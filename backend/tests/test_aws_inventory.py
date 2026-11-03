"""AWS Inventory: the app's read of the inventory database written by scripts/aws_inventory.py."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from main import app

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import aws_db  # noqa: E402
import aws_inventory as inv  # noqa: E402

APP, OTHER = "111111111111", "222222222222"


def write_account(out, account, profile, items, changelog=(), coverage=None):
    """One account's saved files, the way a sync leaves them."""
    acct = out / account
    manifests = {}
    for label, key, rid, resource in items:
        service, type_ = key.split("/")
        rel = f"{label}/{service}/{type_}/{inv.safe_name(rid)}.json"
        inv.write_json(acct / rel, {"_meta": {"id": rid, "last_changed": "2026-11-03T04:12:00Z"}, "resource": resource})
        m = manifests.setdefault(label, {}).setdefault(key, {"status": "ok", "count": 0, "items": {}})
        m["items"][rid] = {"file": rel, "hash": inv.digest(resource), "last_seen": "2026-11-03T04:12:00Z"}
        m["count"] += 1
    manifests.setdefault("global", {})["iam/roles"] = {"status": "no_permission", "error": "AccessDenied", "items": {}}
    for label, manifest in manifests.items():
        inv.write_json(acct / label / "_manifest.json", manifest)
    inv.write_json(acct / "_account.json", {"account_id": account, "profile": profile, "last_sync": "2026-11-03T04:12:00Z",
                                            "connectivity": {"status": "ok"}, "regions": ["us-east-1"]})
    (acct / "_changelog.jsonl").write_text("".join(json.dumps(c) + "\n" for c in changelog))
    if coverage:
        inv.write_json(acct / "_coverage.json", coverage)


def sg(gid, name, port, peer):
    res = {"GroupId": gid, "GroupName": name, "VpcId": "vpc-0a1",
           "IpPermissions": [{"IpProtocol": "tcp", "FromPort": port, "ToPort": port, "IpRanges": [{"CidrIp": peer}]}],
           "IpPermissionsEgress": [{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]}
    res["_rules"] = inv.sg_rule_set(res)
    return res


class AwsInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.out = self.root / "inventory"
        env = {"OWL_DB_PATH": str(self.root / "owl.db"), "OWL_CONFIG_DIR": str(self.root / "config"),
               "OWL_AWS_SCRIPTS_DIR": str(self.root)}
        self.env = patch.dict(os.environ, env)
        self.env.start()
        inv.write_json(self.root / "aws_accounts.json", {"settings": {}, "accounts": {
            "stablecoin-nonp": {"account_id": APP, "regions": ["us-east-1"], "avg_seconds": 118},
            "payments-prod": {"account_id": OTHER},
            "never-synced": {"account_id": "333333333333"}}})

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def build(self):
        eni = {"NetworkInterfaceId": "eni-0a1", "VpcId": "vpc-0a1", "SubnetId": "subnet-0a1", "PrivateIpAddress": "10.1.0.15",
               "Groups": [{"GroupId": "sg-0a1web"}], "Description": "ELB app/api-nonp/50dc6c495c0c9188",
               "_network": {"security_groups": [{"id": "sg-0a1web", "name": "web"}],
                            "inbound": [{"security_group": "sg-0a1web", "protocol": "tcp", "ports": "443", "peer_type": "cidr", "peer": "0.0.0.0/0"}],
                            "subnets": [{"id": "subnet-0a1", "cidr": "10.1.0.0/24", "network_acl": {"id": "acl-0a1"},
                                         "route_table": {"id": "rtb-0a1", "routes": [{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-0a1"}]}}]}}
        zone = {"Id": "/hostedzone/Z1", "Name": "stablecoin-nonp.sep.", "Config": {"PrivateZone": True}, "_details": {"Records": [
            {"Name": "api.stablecoin-nonp.sep.", "Type": "A", "AliasTarget": {"DNSName": "api-nonp.elb.amazonaws.com."},
             "_lookup": {"status": "ok", "addresses": ["10.1.0.15"]}}]}}
        write_account(self.out, APP, "stablecoin-nonp", [
            ("us-east-1", "ec2/security_groups", "sg-0a1web", sg("sg-0a1web", "web-nonp", 443, "0.0.0.0/0")),
            ("us-east-1", "ec2/network_interfaces", "eni-0a1", eni),
            ("global", "route53/hosted_zones", "/hostedzone/Z1", zone),
        ], changelog=[{"run": "20261103T041200Z", "region": "us-east-1", "type": "ec2/security_groups", "id": "sg-0a1web", "action": "modified"}],
           coverage={"checked": True, "missed": 2, "missed_types": {"ec2:instance": {"count": 2}},
                     "config_types_not_in_commands": {}, "suggested_commands": ["AWS::Glue::Job"]})
        write_account(self.out, OTHER, "payments-prod", [
            ("us-east-1", "ec2/security_groups", "sg-0b1", sg("sg-0b1", "payments", 8443, "10.0.0.0/8"))])
        summary = {"run": "20261103T041200Z", "started": "2026-11-03T04:12:00Z", "finished": "2026-11-03T04:19:02Z",
                   "seconds": 422, "stopped": False, "totals": {"resources": 4, "added": 3, "modified": 1, "removed": 0},
                   "accounts": {"stablecoin-nonp": {"connectivity": {"status": "ok"}, "skipped_types": 40, "denied_skipped": 1, "errors": [], "timeout": []},
                                "payments-prod": {"connectivity": {"status": "ok"}, "errors": [], "timeout": []}},
                   "not_ready": {"never-synced": {"status": "timeout", "message": "Connect timeout"}}}
        aws_db.update(self.out / "inventory.db", self.out, run=summary, quiet=True)

    def test_not_ready_without_a_database(self):
        with TestClient(app) as client:
            for path in ("/api/aws-inventory/overview", "/api/aws-inventory/filters", "/api/aws-inventory/search?q=x"):
                body = client.get(path).json()
                self.assertFalse(body["ready"], path)
                self.assertIn("aws_inventory.py", body["message"])

    def test_overview_search_and_resource(self):
        self.build()
        with TestClient(app) as client:
            o = client.get("/api/aws-inventory/overview").json()
            self.assertTrue(o["ready"])
            self.assertEqual(o["totals"]["resources"], 4)
            self.assertEqual(o["totals"]["accounts"], 3)
            self.assertEqual(o["totals"]["ready"], 2)
            self.assertEqual(o["totals"]["coverage_gaps"], 1)
            self.assertEqual(o["last_run"]["seconds"], 422)
            self.assertEqual(o["last_run"]["skipped_empty"], 40)
            rows = {r["profile"]: r for r in o["accounts"]}
            self.assertEqual(rows["stablecoin-nonp"]["resources"], 3)
            self.assertEqual(rows["stablecoin-nonp"]["no_permission"], 1)
            self.assertEqual(rows["stablecoin-nonp"]["changes"], {"modified": 1})
            self.assertEqual(rows["stablecoin-nonp"]["average_seconds"], 118)
            self.assertEqual(rows["never-synced"]["status"], "timeout")
            kinds = [i["kind"] for i in o["issues"]]
            self.assertIn("sign-in", kinds)
            self.assertIn("coverage", kinds)
            self.assertIn("permission", kinds)
            self.assertEqual(o["recent_changes"][0]["resource_id"], "sg-0a1web")

            f = client.get("/api/aws-inventory/filters").json()
            self.assertEqual([a["account_id"] for a in f["accounts"]], [OTHER, APP])
            self.assertIn("ec2/security_groups", [t["type"] for t in f["types"]])

            hits = client.get("/api/aws-inventory/search", params={"q": "10.1.0.15"}).json()["hits"]
            self.assertEqual({h["type"] for h in hits}, {"ec2/network_interfaces", "route53/hosted_zones"})
            self.assertTrue(all(h["where"] for h in hits))
            narrowed = client.get("/api/aws-inventory/search", params={"q": "sg-", "account": OTHER}).json()["hits"]
            self.assertEqual([h["resource_id"] for h in narrowed], ["sg-0b1"])
            self.assertEqual(client.get("/api/aws-inventory/search", params={"q": "  "}).json()["hits"], [])

            sg_hit = next(h for h in client.get("/api/aws-inventory/search", params={"q": "web-nonp"}).json()["hits"])
            detail = client.get(f"/api/aws-inventory/resources/{sg_hit['id']}").json()
            self.assertEqual(detail["resource"]["name"], "web-nonp")
            self.assertEqual([(r["direction"], r["ports"], r["peer"]) for r in detail["rules"]],
                             [("inbound", "443", "0.0.0.0/0"), ("outbound", "all", "0.0.0.0/0")])
            self.assertEqual([u["resource_id"] for u in detail["used_by"]], ["eni-0a1"])
            self.assertEqual([h["action"] for h in detail["history"]], ["modified"])

            zone_hit = next(h for h in hits if h["type"] == "route53/hosted_zones")
            zone = client.get(f"/api/aws-inventory/resources/{zone_hit['id']}").json()
            self.assertEqual(zone["dns"][0]["lookup_addresses"], ["10.1.0.15"])
            self.assertEqual(client.get("/api/aws-inventory/resources/999999").status_code, 404)

    def test_app_is_on_the_home_page(self):
        self.build()
        with TestClient(app) as client:
            self.assertEqual(client.post("/api/home/opened", json={"app": "inventory"}).json()["count"], 1)
            storage = client.get("/api/home/storage?fresh=true").json()
            inventory = next(a for a in storage["apps"] if a["app"] == "inventory")
            self.assertGreater(inventory["bytes"], 0)
            page = client.get("/aws-inventory/")
            self.assertEqual(page.status_code, 200)
            self.assertIn("AWS Inventory", page.text)


if __name__ == "__main__":
    unittest.main()
