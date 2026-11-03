"""AWS Inventory: the app's read of the inventory database written by scripts/aws_inventory.py."""

import json
import os
import shutil
import sys
import tempfile
import time
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
RUN = "20261103T041200Z"

# Stands in for aws_inventory.py in the sync test: writes live.json like the script and stops on Ctrl+C.
FAKE_SYNC = """
import json, os, pathlib, signal, sys, time
signal.signal(signal.SIGINT, signal.default_int_handler)
runs = pathlib.Path(__file__).parent / "inventory" / "_runs"
runs.mkdir(parents=True, exist_ok=True)
def write(**fields):
    (runs / "live.json").write_text(json.dumps({"pid": os.getpid(), **fields}))
print("args", *sys.argv[1:], flush=True)
if "--full" not in sys.argv:
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # a sync that does not answer Ctrl+C
try:
    write(phase="sync", run="20261103T050000Z", list={"done": 1, "total": 4}, accounts={"done": 0, "total": 2})
    time.sleep(30)
    write(phase="done")
except KeyboardInterrupt:
    print("Stopped", flush=True)
    write(phase="stopped", message="Stopped in 00:00:01")
    sys.exit(130)
"""


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
        ], changelog=[{"run": RUN, "region": "us-east-1", "type": "ec2/security_groups", "id": "sg-0a1web", "action": "modified"}],
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
        inv.write_json(self.out / "_runs" / RUN / "changes.json", [
            {"account_id": APP, "profile": "stablecoin-nonp", "region": "us-east-1", "type": "ec2/security_groups",
             "id": "sg-0a1web", "action": "modified",
             "diff": [{"op": "changed", "path": "IpPermissions[0].FromPort", "old": 80, "new": 443}]}])

    def scripts(self):
        """The real scripts next to the test's aws_accounts.json, as in the repo."""
        for name in ("aws_inventory.py", "aws_db.py", "aws_commands.json", "aws_compare.py", "aws_compare.json",
                     "aws_project.py", "aws_project.json"):
            shutil.copy(SCRIPTS / name, self.root / name)

    def accounts_file(self):
        return json.loads((self.root / "aws_accounts.json").read_text())["accounts"]

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

    def test_account_detail_and_permission_retry(self):
        self.build()
        self.scripts()
        with TestClient(app) as client:
            a = client.get("/api/aws-inventory/accounts/stablecoin-nonp").json()
            self.assertEqual(a["account_id"], APP)
            self.assertTrue(a["synced"])
            self.assertEqual([(d["type"], d["detail"], d["regions"]) for d in a["no_permission"]], [("iam/roles", None, ["global"])])
            self.assertEqual({r["region"]: r["count"] for r in a["by_region"]}, {"global": 1, "us-east-1": 2})
            self.assertEqual(a["coverage"]["missed"], 2)
            self.assertEqual(a["changes"][0]["action"], "modified")
            self.assertEqual(client.get(f"/api/aws-inventory/accounts/{APP}").json()["profile"], "stablecoin-nonp")
            self.assertFalse(client.get("/api/aws-inventory/accounts/never-synced").json()["synced"])
            self.assertEqual(client.get("/api/aws-inventory/accounts/nobody").status_code, 404)

            # The changes go through the script's own commands.
            r = client.post("/api/aws-inventory/accounts/stablecoin-nonp/retry", json={"types": []})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(self.accounts_file()["stablecoin-nonp"]["retry_permissions"], ["*"])
            client.post("/api/aws-inventory/accounts/stablecoin-nonp/retry", json={"types": ["iam/roles"]})
            self.assertEqual(self.accounts_file()["stablecoin-nonp"]["retry_permissions"], ["iam/roles"])
            self.assertEqual(client.get("/api/aws-inventory/accounts/stablecoin-nonp").json()["retry"], ["iam/roles"])
            self.assertEqual(client.post("/api/aws-inventory/accounts/stablecoin-nonp/retry", json={"types": ["nope/none"]}).status_code, 400)
            client.delete("/api/aws-inventory/accounts/stablecoin-nonp/retry")
            self.assertNotIn("retry_permissions", self.accounts_file()["stablecoin-nonp"])
            client.put("/api/aws-inventory/accounts/payments-prod/enabled", json={"enabled": False})
            self.assertFalse(self.accounts_file()["payments-prod"]["enabled"])
            self.assertFalse(next(r for r in client.get("/api/aws-inventory/overview").json()["accounts"] if r["profile"] == "payments-prod")["enabled"])

    def test_changes_and_their_fields(self):
        self.build()
        with TestClient(app) as client:
            c = client.get("/api/aws-inventory/changes").json()
            self.assertEqual(c["run"], RUN)
            self.assertEqual(c["counts"], {"modified": 1})
            self.assertEqual([(x["resource_id"], x["profile"]) for x in c["changes"]], [("sg-0a1web", "stablecoin-nonp")])
            self.assertTrue(c["changes"][0]["pk"])
            self.assertEqual(client.get("/api/aws-inventory/changes", params={"action": "added"}).json()["changes"], [])
            self.assertEqual(client.get("/api/aws-inventory/changes", params={"action": "added"}).json()["counts"], {"modified": 1})
            self.assertEqual(client.get("/api/aws-inventory/changes", params={"account": OTHER}).json()["total"], 0)
            self.assertEqual(client.get("/api/aws-inventory/changes", params={"q": "web"}).json()["total"], 1)
            diff = client.get("/api/aws-inventory/changes/diff", params={"run": RUN, "account": APP, "region": "us-east-1",
                                                                        "type": "ec2/security_groups", "id": "sg-0a1web"}).json()
            self.assertEqual(diff["diff"][0]["new"], 443)
            self.assertEqual(client.get("/api/aws-inventory/changes/diff", params={"run": "../x", "account": APP, "region": "r",
                                                                                   "type": "t", "id": "i"}).status_code, 400)

    def test_sync_start_follow_and_stop(self):
        (self.root / "aws_inventory.py").write_text(FAKE_SYNC)

        def wait(client, test):
            for _ in range(100):
                state = client.get("/api/aws-inventory/sync").json()
                if test(state):
                    return state
                time.sleep(0.1)
            self.fail(f"never got there: {state}")

        with TestClient(app) as client:
            self.assertFalse(client.get("/api/aws-inventory/sync").json()["running"])
            self.assertEqual(client.post("/api/aws-inventory/sync", json={"profiles": ["nobody"]}).status_code, 400)
            r = client.post("/api/aws-inventory/sync", json={"profiles": ["stablecoin-nonp"], "full": True})
            self.assertEqual(r.status_code, 200, r.text)
            state = wait(client, lambda s: s["status"].get("phase") == "sync" and s["log"])
            self.assertTrue(state["running"])
            self.assertTrue(state["started_here"])
            self.assertEqual(state["log"][0], "args sync --yes --profiles stablecoin-nonp --full")
            self.assertEqual(client.post("/api/aws-inventory/sync", json={}).status_code, 409)
            self.assertEqual(client.put("/api/aws-inventory/accounts/stablecoin-nonp/enabled", json={"enabled": False}).status_code, 409)

            self.assertEqual(client.post("/api/aws-inventory/sync/stop").status_code, 200)
            state = wait(client, lambda s: not s["running"])
            self.assertEqual(state["status"]["phase"], "stopped")
            self.assertIn("Stopped", state["log"])
            self.assertEqual(client.post("/api/aws-inventory/sync/stop").status_code, 409)

            # Where no signal reaches it (Windows), Stop leaves the stop file the script reads.
            r = client.post("/api/aws-inventory/sync", json={"profiles": ["stablecoin-nonp"]})
            wait(client, lambda s: s["status"].get("phase") == "sync" and s["log"])
            with patch("app.api.aws_inventory.os.killpg", side_effect=OSError("no")), \
                    patch("app.api.aws_inventory.os.kill", side_effect=OSError("no")):
                self.assertEqual(client.post("/api/aws-inventory/sync/stop").status_code, 200)
            self.assertTrue((self.out / "_runs" / "stop").exists())
            self.assertTrue(client.get("/api/aws-inventory/sync").json()["status"]["stop_requested"])

            # Quit at once ends a sync that does not answer Ctrl+C (an older script, or a hung one).
            self.assertEqual(client.post("/api/aws-inventory/sync/stop", json={"force": True}).status_code, 200)
            state = client.get("/api/aws-inventory/sync").json()
            self.assertFalse(state["running"])
            self.assertEqual(state["status"]["phase"], "stopped")
            self.assertIn("Quit at once", state["status"]["message"])

    def test_compare_two_accounts(self):
        self.build()
        self.scripts()
        with TestClient(app) as client:
            page = client.get("/api/aws-inventory/compare").json()
            self.assertEqual({a["profile"] for a in page["accounts"]}, {"stablecoin-nonp", "payments-prod"})
            self.assertEqual(page["reports"], [])
            r = client.post("/api/aws-inventory/compare", json={"source": "stablecoin-nonp", "target": "payments-prod",
                                                               "env_source": ["nonp"], "env_target": ["prod"]})
            self.assertEqual(r.status_code, 200, r.text)
            name = r.json()["name"]
            report = client.get(f"/api/aws-inventory/compare/reports/{name}").json()["report"]
            self.assertEqual((report["source"]["account"], report["target"]["account"]), (APP, OTHER))
            self.assertEqual(report["source"]["env_words"], ["nonp"])
            self.assertIn("nonp", report["source"]["found_words"])
            self.assertIn("web-<env>", " ".join(m["name"] for m in report["missing"]))
            page = client.get("/api/aws-inventory/compare").json()
            self.assertEqual(page["reports"][0]["name"], name)
            self.assertEqual(page["reports"][0]["missing"], len(report["missing"]))
            self.assertEqual(page["pairs"][0]["words"], [["nonp"], ["prod"]])
            self.assertEqual(client.post("/api/aws-inventory/compare", json={"source": "stablecoin-nonp", "target": "nobody"}).status_code, 400)
            self.assertEqual(client.post("/api/aws-inventory/compare", json={"source": "a; rm", "target": "b"}).status_code, 400)
            self.assertEqual(client.get("/api/aws-inventory/compare/reports/pairs.json").status_code, 400)
            self.assertEqual(client.get("/api/aws-inventory/compare/reports/..%2Faws_accounts.json").status_code, 404)

    def test_project_report(self):
        self.build()
        self.scripts()
        with TestClient(app) as client:
            self.assertEqual(client.post("/api/aws-inventory/projects", json={"keywords": []}).status_code, 400)
            r = client.post("/api/aws-inventory/projects", json={"keywords": ["stablecoin"]})
            self.assertEqual(r.status_code, 200, r.text)
            data = client.get(f"/api/aws-inventory/projects/reports/{r.json()['name']}").json()
            self.assertEqual([a["account"] for a in data["report"]["accounts"]["application"]], [APP])
            self.assertTrue(data["tad"]["nodes"])
            self.assertIn("flowchart", data["mermaid"])
            self.assertTrue(all("data" not in x for x in data["report"]["shared_resources"]))
            page = client.get("/api/aws-inventory/projects").json()
            self.assertEqual(page["last"]["keywords"], ["stablecoin"])
            self.assertEqual(page["reports"][0]["project"], "stablecoin")
            self.assertEqual(client.post("/api/aws-inventory/projects", json={"keywords": ["nothing-matches"]}).status_code, 400)

    def test_settings(self):
        self.build()
        self.scripts()
        with TestClient(app) as client:
            page = client.get("/api/aws-inventory/settings").json()
            self.assertEqual(page["settings"]["full_sweep_days"], 7)  # the script's default
            self.assertEqual(page["database"]["resources"], 4)
            self.assertGreater(page["commands"]["types"], 100)
            r = client.put("/api/aws-inventory/settings", json={"values": {
                "full_sweep_days": 3, "dns_match": "\\.sep\\.?$", "regions": ["us-east-1", "eu-west-1"], "smart_skip": False,
                "max_parallel_calls": "auto"}})
            self.assertEqual(r.status_code, 200, r.text)
            saved = json.loads((self.root / "aws_accounts.json").read_text())["settings"]
            self.assertEqual((saved["full_sweep_days"], saved["dns_match"], saved["regions"], saved["smart_skip"]),
                             (3, "\\.sep\\.?$", ["us-east-1", "eu-west-1"], False))
            self.assertEqual(r.json()["settings"]["full_sweep_days"], 3)
            self.assertEqual(client.put("/api/aws-inventory/settings", json={"values": {"output_dir": "/tmp"}}).status_code, 400)
            self.assertEqual(client.put("/api/aws-inventory/settings", json={"values": {"regions": "us-east-1; rm"}}).status_code, 400)
            self.assertIn("Database", client.post("/api/aws-inventory/database/update").json()["message"])

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


class SyncOutputTests(unittest.TestCase):
    def test_bars_are_left_out_of_the_output(self):
        from app.api.aws_inventory import tail
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "app-sync.log"
            log.write_text("Syncing 2 account(s): 10 list commands.\n"
                           "List  [###-----]  3/10   30%  accounts 0/2  workers 4/8  ETA 00:00:20\n"
                           "Save  [--------]  1/~40    2%  elapsed 00:00:03  total ETA estimating\n"
                           "\x1b[2KDone in 00:00:09: 2/2 accounts.\n"
                           "DB    [########]  45/45  100%  accounts 2/2  elapsed 00:00:00  ETA 00:00:00\n")
            self.assertEqual(tail(log), ["Syncing 2 account(s): 10 list commands.", "Done in 00:00:09: 2/2 accounts."])


class SuffixAliasTests(unittest.TestCase):
    """aws_compare.py reads an EKS cluster's differing random suffix as the same name."""

    @staticmethod
    def side(*idents):
        return {"entries": [{"label": "us-east-1", "key": key, "ident": ident} for key, ident in idents]}

    def test_cluster_suffix_is_learnt_from_its_names(self):
        import aws_compare
        source = self.side(("eks/clusters", "<env>_ghdjegd"), ("ec2/instances", "vpc/<env>_ghdjegd-node"),
                           ("ec2/security_groups", "vpc/web-<env>"), ("ec2/security_groups", "vpc/app-<env>"))
        target = self.side(("eks/clusters", "<env>_gdjhsw"), ("ec2/instances", "vpc/<env>_gdjhsw-node"),
                           ("ec2/security_groups", "vpc/web-<env>"))
        self.assertEqual(aws_compare.suffix_aliases([source, target]), [("ghdjegd", "gdjhsw", 2)])

    def test_different_names_are_not_paired(self):
        import aws_compare
        # app also exists in the target, so app -> apiy is a different resource, not a suffix.
        source = self.side(("ec2/security_groups", "vpc/app-<env>"), ("ec2/instances", "vpc/apix-<env>"))
        target = self.side(("ec2/security_groups", "vpc/apiy-<env>"), ("ec2/instances", "vpc/app-<env>"))
        self.assertEqual(aws_compare.suffix_aliases([source, target]), [])
        # a single name differing is more likely a rename than a suffix
        self.assertEqual(aws_compare.suffix_aliases([self.side(("eks/clusters", "<env>_aaaa")),
                                                     self.side(("eks/clusters", "<env>_bbbb"))]), [])


if __name__ == "__main__":
    unittest.main()
