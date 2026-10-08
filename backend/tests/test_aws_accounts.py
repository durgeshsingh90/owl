import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from main import app

CONFIG = """[default]
region = us-east-1

[sso-session mc]
sso_start_url = https://mc.awsapps.com/start
sso_region = us-east-1

[profile mc-databricks-prod]
sso_session = mc
sso_account_id = 012345678901
sso_role_name = 77-mc-infra-design-eng-readonly

[profile mc-databricks-nonp]
sso_session = mc
sso_account_id = 987654321098
sso_role_name = 77-mc-infra-design-eng-readonly

[profile mc-networking-work]
role_arn = arn:aws:iam::111122223333:role/global-kubernetes-readonly
source_profile = default
"""


def delete_all(client):
    return client.request("DELETE", "/api/aws-accounts", json={"confirmation": "delete all"})


def use_config(client, folder, text=CONFIG):
    path = os.path.join(folder, "config")
    with open(path, "w") as file:
        file.write(text)
    return client.put("/api/aws-accounts/config", json={"path": path})


class AwsAccountsTests(unittest.TestCase):
    def test_accounts_come_from_the_aws_config_file(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {"OWL_DB_PATH": folder + "/db", "AWS_CONFIG_FILE": folder + "/missing"}),
        ):
            with TestClient(app) as client:
                empty = client.get("/api/aws-accounts").json()
                self.assertFalse(empty["imported"])
                self.assertIn("No AWS config file", empty["config"]["error"])
                self.assertEqual(client.put("/api/aws-accounts/config", json={"path": folder + "/nope"}).status_code, 400)
                response = use_config(client, folder)
                self.assertEqual(response.status_code, 200, response.text)
                saved = client.get("/api/aws-accounts").json()
                self.assertTrue(saved["imported"])
                self.assertEqual(list(saved["categories"]), ["Data & Analytics", "Networking", "Other accounts"])
                data = saved["categories"]["Data & Analytics"]
                self.assertEqual([a["profile"] for a in data], ["mc-databricks-prod", "mc-databricks-nonp"][::-1])
                self.assertEqual({a["environment"] for a in data}, {"Prod", "Non-prod"})
                network = saved["categories"]["Networking"][0]
                self.assertEqual(
                    (network["account_id"], network["role"], network["environment"]),
                    ("111122223333", "global-kubernetes-readonly", "Dev"),
                )
                self.assertEqual(data[0]["sso_start_url"], "https://mc.awsapps.com/start")
                self.assertIn("77-mc-infra-design-eng-readonly", saved["common_roles"])
            # The file is read again automatically when it changes.
            with TestClient(app) as client:
                path = os.path.join(folder, "config")
                with open(path, "a") as file:
                    file.write("\n[profile mc-centralizednetworking-prod]\nsso_account_id = 222233334444\n")
                os.utime(path, (1, 2_000_000_000))
                saved = client.get("/api/aws-accounts").json()
                self.assertEqual(saved["categories"]["Shared Services"][0]["profile"], "mc-centralizednetworking-prod")
                self.assertEqual(client.put("/api/aws-accounts", json={"categories": {}}).status_code, 405)

    def test_grouping_rules(self):
        from app.aws import profiles

        cases = {
            "mc-egressnetworkingpalo-prod": ("Networking", "Prod"),
            "mc-egressnetworkingpalo-stage": ("Networking", "Stage"),
            "mc-centralizednetworking-nonp": ("Shared Services", "Non-prod"),
            "mc-stablecoinsecurity-nonp": ("Security", "Non-prod"),
            "mc-paymentgateway-prod-123456789012": ("Payments & Cards", "Prod"),
            "mc-log-archive-prod": ("Logging & Monitoring", "Prod"),
            "mc-somethingelse-uat": ("Other accounts", "Stage"),
        }
        for name, expected in cases.items():
            self.assertEqual((profiles.category(name), profiles.environment(name)[0]), expected, name)


class AwsCopiesExportConnectionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"OWL_DB_PATH": self.folder.name + "/db", "AWS_CONFIG_FILE": self.folder.name + "/missing"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def test_copy_counts_and_export(self):
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/aws-accounts/export").status_code, 404)
            use_config(client, self.folder.name)
            for expected in (1, 2):
                response = client.post(
                    "/api/aws-accounts/copies",
                    json={"kind": "profile", "value": "mc-databricks-prod"},
                )
                self.assertEqual(response.json(), {"count": expected})
            client.post("/api/aws-accounts/copies", json={"kind": "role", "value": "r"})
            self.assertEqual(
                client.post("/api/aws-accounts/copies", json={"kind": "x", "value": "r"}).status_code,
                422,
            )
            copies = client.get("/api/aws-accounts").json()["copies"]
            self.assertEqual(copies["profile"], {"mc-databricks-prod": 2})
            self.assertEqual(copies["role"], {"r": 1})
            exported = client.get("/api/aws-accounts/export")
            self.assertIn("attachment", exported.headers["content-disposition"])
            body = exported.json()
            self.assertIn("77-mc-infra-design-eng-readonly", body["common_roles"])
            self.assertEqual(body["total_accounts"], 4)
            self.assertEqual(list(body["categories"]), ["Data & Analytics", "Networking", "Other accounts"])
            self.assertEqual(body["categories"]["Data & Analytics"][0]["account_id"], "987654321098")
            self.assertNotIn("copies", body)
            self.assertNotIn("imported_at", body)

    def test_connection_check_and_profile(self):
        from app.aws import connection as aws

        identity = {"Account": "111122223333", "Arn": "arn:aws:sts::111122223333:assumed-role/x/me"}
        with TestClient(app) as client:
            state = client.get("/api/aws-accounts/connection").json()
            self.assertEqual(state["profile"], "mc-stablecoinsecurity-nonp")
            self.assertEqual(state["status"], "unknown")
            with patch.object(aws, "run_sts", return_value=(None, "Token has expired")) as sts:
                state = client.get("/api/aws-accounts/connection?refresh=1").json()
                sts.assert_called_once_with("mc-stablecoinsecurity-nonp")
            self.assertEqual((state["status"], state["error"]), ("disconnected", "Token has expired"))
            with patch.object(aws, "run_sts", return_value=(identity, None)):
                state = client.get("/api/aws-accounts/connection?refresh=1").json()
            self.assertEqual(state["status"], "connected")
            self.assertEqual(state["identity"], identity)
            self.assertEqual(state["source"], "detected")
            self.assertAlmostEqual(state["expires_at"] - state["approved_at"], 12 * 3600)
            approved = state["approved_at"]
            with patch.object(aws, "run_sts", return_value=(identity, None)):
                state = client.get("/api/aws-accounts/connection?refresh=1").json()
            self.assertEqual(state["approved_at"], approved)
            self.assertEqual(
                client.put("/api/aws-accounts/connection", json={"profile": "--debug"}).status_code,
                400,
            )
            with patch.object(aws, "run_sts", return_value=(None, "no profile")):
                state = client.put("/api/aws-accounts/connection", json={"profile": "other"}).json()
            self.assertEqual((state["profile"], state["approved_at"]), ("other", None))

    def test_sso_login_marks_approval(self):
        import subprocess
        import sys
        import time

        from app.aws import connection as aws

        script = "print('https://device.sso.example.com/?user_code=ABCD-EFGH'); print('ABCD-EFGH')"
        identity = {"Account": "1", "Arn": "arn"}
        popen = subprocess.Popen
        with (
            TestClient(app) as client,
            patch.object(aws, "_aws", return_value=sys.executable),
            patch.object(aws.subprocess, "Popen", lambda args, **kw: popen([sys.executable, "-c", script], **kw)),
            patch.object(aws, "run_sts", return_value=(identity, None)),
        ):
            state = client.post("/api/aws-accounts/connection/login").json()
            self.assertIn(state["login_status"], ("pending", "approved"))
            for _ in range(100):
                state = client.get("/api/aws-accounts/connection").json()
                if state["login_status"] != "pending":
                    break
                time.sleep(0.05)
            self.assertEqual(state["login_status"], "approved")
            self.assertEqual(state["status"], "connected")
            self.assertEqual(state["source"], "login")
            self.assertEqual(state["login_url"], "https://device.sso.example.com/?user_code=ABCD-EFGH")
            self.assertEqual(state["login_code"], "ABCD-EFGH")

    def test_approved_login_is_not_reported_failed_while_the_profile_is_checked(self):
        import subprocess
        import sys
        import threading
        import time

        from app.aws import connection as aws

        # The CLI exits as soon as the browser approves (printing a non-ASCII character);
        # checking the profile afterwards takes a moment. Polls in between stay pending.
        script = "import sys; sys.stdout.buffer.write('Successfully logged into Start URL \u2713\\n'.encode())"
        identity = {"Account": "1", "Arn": "arn"}
        checking = threading.Event()
        release = threading.Event()

        def slow_sts(profile):
            checking.set()
            release.wait(5)
            return identity, None

        popen = subprocess.Popen
        with (
            TestClient(app) as client,
            patch.object(aws, "_aws", return_value=sys.executable),
            patch.object(aws.subprocess, "Popen", lambda args, **kw: popen([sys.executable, "-c", script], **kw)),
            patch.object(aws, "run_sts", side_effect=slow_sts),
        ):
            client.post("/api/aws-accounts/connection/login")
            self.assertTrue(checking.wait(5))
            for _ in range(3):
                state = client.get("/api/aws-accounts/connection").json()
                self.assertEqual((state["login_status"], state["error"]), ("pending", ""))
                time.sleep(0.05)
            release.set()
            for _ in range(100):
                state = client.get("/api/aws-accounts/connection").json()
                if state["login_status"] != "pending":
                    break
                time.sleep(0.05)
            self.assertEqual((state["login_status"], state["status"]), ("approved", "connected"))

    def test_delete_all_is_locked_and_removes_every_record(self):
        from app.aws import connection as aws

        identity = {"Account": "1", "Arn": "arn"}
        with TestClient(app) as client:
            use_config(client, self.folder.name)
            client.post("/api/aws-accounts/copies", json={"kind": "profile", "value": "mc-databricks-prod"})
            with patch.object(aws, "run_sts", return_value=(identity, None)):
                client.put("/api/aws-accounts/connection", json={"profile": "other"})
            for body in (None, {}, {"confirmation": "DELETE ALL"}, {"confirmation": "delete"}):
                response = client.request("DELETE", "/api/aws-accounts", json=body)
                self.assertIn(response.status_code, (400, 422), body)
            self.assertTrue(client.get("/api/aws-accounts").json()["imported"])

            client.put("/api/aws-accounts/stars", json={"profile": "mc-databricks-prod", "starred": True})
            pid = client.post("/api/aws-accounts/projects", json={"name": "Stablecoin Project"}).json()["id"]
            client.put(f"/api/aws-accounts/projects/{pid}/accounts", json={"profile": "mc-databricks-prod"})
            self.assertEqual(
                delete_all(client).json(),
                {"ok": True, "inventory": 1, "copies": 1, "stars": 1, "projects": 1},
            )
            # OWL's own records are gone; the accounts are read again from the config file.
            saved = client.get("/api/aws-accounts").json()
            self.assertTrue(saved["imported"])
            self.assertEqual(saved["copies"]["profile"], {})
            self.assertEqual((saved["stars"], saved["projects"]), ([], []))
            state = client.get("/api/aws-accounts/connection").json()
            self.assertEqual(state["profile"], "mc-stablecoinsecurity-nonp")
            self.assertEqual((state["status"], state["approved_at"], state["identity"]), ("unknown", None, None))
            self.assertEqual(delete_all(client).json()["inventory"], 1)

    def test_account_stars(self):
        with TestClient(app) as client:
            use_config(client, self.folder.name)
            self.assertEqual(client.get("/api/aws-accounts").json()["stars"], [])
            for profile in ("mc-networking-work", "mc-databricks-prod", "mc-networking-work"):
                response = client.put("/api/aws-accounts/stars", json={"profile": profile, "starred": True})
                self.assertEqual(response.status_code, 200)
            self.assertEqual(
                client.get("/api/aws-accounts").json()["stars"],
                ["mc-networking-work", "mc-databricks-prod"],
            )
            client.put("/api/aws-accounts/stars", json={"profile": "mc-networking-work", "starred": False})
            self.assertEqual(client.get("/api/aws-accounts").json()["stars"], ["mc-databricks-prod"])
            self.assertEqual(
                client.put("/api/aws-accounts/stars", json={"profile": "", "starred": True}).status_code, 422
            )
            # Re-importing keeps stars; export stays in the original format.
            use_config(client, self.folder.name)
            self.assertEqual(client.get("/api/aws-accounts").json()["stars"], ["mc-databricks-prod"])
            self.assertNotIn("stars", client.get("/api/aws-accounts/export").json())

    def test_projects(self):
        with TestClient(app) as client:
            use_config(client, self.folder.name)
            project = client.post("/api/aws-accounts/projects", json={"name": "  Stablecoin   Project "}).json()
            self.assertEqual(project["name"], "Stablecoin Project")
            pid = project["id"]
            for payload, status in (({"name": "stablecoin project"}, 409), ({"name": "   "}, 422)):
                self.assertEqual(client.post("/api/aws-accounts/projects", json=payload).status_code, status)
            other = client.post("/api/aws-accounts/projects", json={"name": "Payments"}).json()["id"]
            url = f"/api/aws-accounts/projects/{pid}/accounts"
            for profile in ("mc-networking-work", "mc-databricks-prod", "mc-networking-work"):
                self.assertEqual(client.put(url, json={"profile": profile}).status_code, 200)
            client.put(f"/api/aws-accounts/projects/{other}/accounts", json={"profile": "mc-databricks-prod"})
            projects = client.get("/api/aws-accounts").json()["projects"]
            self.assertEqual(
                projects,
                [
                    {"id": pid, "name": "Stablecoin Project", "accounts": ["mc-networking-work", "mc-databricks-prod"]},
                    {"id": other, "name": "Payments", "accounts": ["mc-databricks-prod"]},
                ],
            )
            client.delete(url, params={"profile": "mc-networking-work"})
            self.assertEqual(client.patch(f"/api/aws-accounts/projects/{pid}", json={"name": "Payments"}).status_code, 409)
            self.assertEqual(client.patch(f"/api/aws-accounts/projects/{pid}", json={"name": "Stablecoin"}).status_code, 200)
            projects = client.get("/api/aws-accounts").json()["projects"]
            self.assertEqual(projects[0], {"id": pid, "name": "Stablecoin", "accounts": ["mc-databricks-prod"]})
            self.assertEqual(client.delete(f"/api/aws-accounts/projects/{other}").status_code, 200)
            self.assertEqual(client.delete(f"/api/aws-accounts/projects/{other}").status_code, 404)
            self.assertEqual(client.put(f"/api/aws-accounts/projects/{other}/accounts", json={"profile": "x"}).status_code, 404)
            self.assertEqual([p["id"] for p in client.get("/api/aws-accounts").json()["projects"]], [pid])
            self.assertNotIn("projects", client.get("/api/aws-accounts/export").json())
