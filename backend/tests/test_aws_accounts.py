import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from main import app

INVENTORY = {
    "generated_at": "2026-09-27T13:33:35.338514",
    "total_accounts": 3,
    "common_roles": {
        "aws_role": "77-mc-infra-design-eng-readonly",
        "kubernetes_role": "global-kubernetes-readonly",
    },
    "categories": {
        "Data": [
            {"profile": "mc-databricks-prod", "account_id": "012345678901"},
            {"profile": "mc-databricks-nonp", "account_id": 987654321098},
        ],
        "Networking": [{"profile": "mc-networking-work", "account_id": "111122223333"}],
    },
}


def delete_all(client):
    return client.request("DELETE", "/api/aws-accounts", json={"confirmation": "delete all"})


class AwsAccountsTests(unittest.TestCase):
    def test_import_roundtrip_replace_and_clear(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {"OWL_DB_PATH": folder + "/db"}),
        ):
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/aws-accounts").json()["imported"], False)
                response = client.put("/api/aws-accounts", json=INVENTORY)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["accounts"], 3)
            with TestClient(app) as client:
                saved = client.get("/api/aws-accounts").json()
                self.assertTrue(saved["imported"])
                self.assertEqual(saved["common_roles"], INVENTORY["common_roles"])
                self.assertEqual(list(saved["categories"]), ["Data", "Networking"])
                self.assertEqual(
                    saved["categories"]["Data"][1],
                    {"profile": "mc-databricks-nonp", "account_id": "987654321098"},
                )
                replacement = {"categories": {"Security": [{"profile": "mc-identity-prod"}]}}
                self.assertEqual(client.put("/api/aws-accounts", json=replacement).status_code, 200)
                saved = client.get("/api/aws-accounts").json()
                self.assertEqual(list(saved["categories"]), ["Security"])
                self.assertEqual(delete_all(client).status_code, 200)
                self.assertEqual(client.get("/api/aws-accounts").json()["imported"], False)

    def test_rejects_invalid_files(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {"OWL_DB_PATH": folder + "/db"}),
        ):
            with TestClient(app) as client:
                for payload in (
                    {},
                    {"categories": {}},
                    {"categories": {"Data": [{"account_id": "1"}]}},
                    {"categories": {"Data": [{"profile": ""}]}},
                    {"categories": ["Data"]},
                ):
                    self.assertEqual(
                        client.put("/api/aws-accounts", json=payload).status_code, 422, payload
                    )
                self.assertEqual(client.get("/api/aws-accounts").json()["imported"], False)


class AwsCopiesExportConnectionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"OWL_DB_PATH": self.folder.name + "/db"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def test_copy_counts_and_export(self):
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/aws-accounts/export").status_code, 404)
            client.put("/api/aws-accounts", json=INVENTORY)
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
            self.assertEqual(body["common_roles"], INVENTORY["common_roles"])
            self.assertEqual(body["total_accounts"], 3)
            self.assertEqual(list(body["categories"]), ["Data", "Networking"])
            self.assertEqual(body["categories"]["Data"][1]["account_id"], "987654321098")
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

    def test_delete_all_is_locked_and_removes_every_record(self):
        from app.aws import connection as aws

        identity = {"Account": "1", "Arn": "arn"}
        with TestClient(app) as client:
            client.put("/api/aws-accounts", json=INVENTORY)
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
            saved = client.get("/api/aws-accounts").json()
            self.assertFalse(saved["imported"])
            self.assertEqual(saved["copies"]["profile"], {})
            self.assertEqual((saved["stars"], saved["projects"]), ([], []))
            state = client.get("/api/aws-accounts/connection").json()
            self.assertEqual(state["profile"], "mc-stablecoinsecurity-nonp")
            self.assertEqual((state["status"], state["approved_at"], state["identity"]), ("unknown", None, None))
            self.assertEqual(delete_all(client).json()["inventory"], 0)

    def test_account_stars(self):
        with TestClient(app) as client:
            client.put("/api/aws-accounts", json=INVENTORY)
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
            client.put("/api/aws-accounts", json=INVENTORY)
            self.assertEqual(client.get("/api/aws-accounts").json()["stars"], ["mc-databricks-prod"])
            self.assertNotIn("stars", client.get("/api/aws-accounts/export").json())

    def test_projects(self):
        with TestClient(app) as client:
            client.put("/api/aws-accounts", json=INVENTORY)
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
