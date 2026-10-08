"""JSON Visualizer backend: snapshots, the command runner, URL open and server mode."""

import gzip
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from main import app

from app.jsonviz import filters, runner, serverdoc


def wait(check, seconds=20):
    deadline = time.time() + seconds
    while time.time() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("Timed out waiting.")


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = self.temp.name
        self.env = patch.dict(os.environ, {
            "OWL_DB_PATH": self.folder + "/owl.db", "OWL_CONFIG_DIR": self.folder + "/config",
            "OWL_LOG_DIR": self.folder + "/logs", "AWS_CONFIG_FILE": self.folder + "/aws-config",
        })
        self.env.start()
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        serverdoc.drop_all()
        self.client.__exit__(None, None, None)
        self.env.stop()
        self.temp.cleanup()

    def upload(self, body, name="data.json", **params):
        return self.client.post("/api/json-visualizer/snapshots", params={"name": name, **params}, content=body)


class SnapshotTests(Base):
    def test_create_list_content_patch_delete(self):
        body = json.dumps({"Vpcs": [{"VpcId": "vpc-1"}]}).encode()
        response = self.upload(body, labels=json.dumps({"account": "123", "region": "us-east-1"}), source="upload")
        self.assertEqual(response.status_code, 201, response.text)
        record = response.json()
        self.assertEqual((record["size"], record["format"], record["labels"]["account"]), (len(body), "json", "123"))
        self.assertTrue(os.path.exists(f"{self.folder}/json-visualizer/snapshots/{record['id']}.json.gz"))
        content = self.client.get(f"/api/json-visualizer/snapshots/{record['id']}/content")
        self.assertEqual(content.content, body)
        self.assertIn("data.json", content.headers["content-disposition"])
        changed = self.client.patch(f"/api/json-visualizer/snapshots/{record['id']}", json={"name": "vpcs.json", "labels": {"environment": "prod"}}).json()
        self.assertEqual((changed["name"], changed["labels"]), ("vpcs.json", {"environment": "prod"}))
        self.assertEqual(self.client.patch(f"/api/json-visualizer/snapshots/{record['id']}", json={"labels": {"a": 1}}).status_code, 400)
        listing = self.client.get("/api/json-visualizer/snapshots").json()
        self.assertEqual([item["id"] for item in listing["snapshots"]], [record["id"]])
        self.assertEqual(listing["settings"], {"retention_days": 0, "max_snapshots": 200})
        self.assertGreater(listing["total_bytes"], 0)
        storage = self.client.get("/api/home/storage?fresh=true").json()
        self.assertGreater({item["app"]: item["bytes"] for item in storage["apps"]}["json"], 0)
        self.assertEqual(self.client.delete(f"/api/json-visualizer/snapshots/{record['id']}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/json-visualizer/snapshots/{record['id']}").status_code, 404)

    def test_gzip_upload_is_kept_and_validation(self):
        body = b'{"a": [1, 2, 3]}\n'
        packed = gzip.compress(body)
        record = self.upload(packed, name="a.json.gz").json()
        self.assertEqual(record["size"], len(body))
        stored = f"{self.folder}/json-visualizer/snapshots/{record['id']}.json.gz"
        with open(stored, "rb") as file:
            self.assertEqual(file.read(), packed)
        self.assertEqual(self.client.get(f"/api/json-visualizer/snapshots/{record['id']}/content").content, body)
        self.assertEqual(self.upload(b"\x1f\x8bbroken").status_code, 400)
        self.assertEqual(self.upload(b"").status_code, 400)
        self.assertEqual(self.upload(b"[]", labels="[1]").status_code, 400)
        self.assertEqual(self.upload(b"[]", labels=json.dumps({str(i): "x" for i in range(21)})).status_code, 400)
        self.assertEqual(self.upload(b"[]", labels=json.dumps({"a": "x" * 201})).status_code, 400)
        self.assertEqual(self.upload(b"[]", name="lines.jsonl").json()["format"], "jsonl")
        # No temporary files are left behind.
        names = os.listdir(f"{self.folder}/json-visualizer/snapshots")
        self.assertTrue(all(name.endswith(".json.gz") for name in names), names)

    def test_delete_all_needs_confirmation(self):
        self.upload(b"[1]")
        document = self.client.post("/api/json-visualizer/documents", params={"name": "x.json"}, content=b"[1]").json()
        self.assertEqual(self.client.request("DELETE", "/api/json-visualizer/snapshots", json={"confirm": "yes"}).status_code, 400)
        self.assertEqual(self.client.request("DELETE", "/api/json-visualizer/snapshots", json={"confirm": "delete all"}).json(), {"deleted": 1})
        self.assertEqual(self.client.get("/api/json-visualizer/snapshots").json()["snapshots"], [])
        self.assertEqual(self.client.get(f"/api/json-visualizer/documents/{document['id']}/status").status_code, 404)

    def test_retention_by_count_and_age(self):
        from app.core.database import connection

        self.assertEqual(self.client.put("/api/json-visualizer/settings", json={"max_snapshots": 0}).status_code, 400)
        self.assertEqual(self.client.put("/api/json-visualizer/settings", json={"retention_days": 3651}).status_code, 400)
        self.assertEqual(self.client.put("/api/json-visualizer/settings", json={"max_snapshots": 2}).json(), {"retention_days": 0, "max_snapshots": 2})
        ids = [self.upload(f"[{i}]".encode(), name=f"{i}.json").json()["id"] for i in range(3)]
        listed = [item["id"] for item in self.client.get("/api/json-visualizer/snapshots").json()["snapshots"]]
        self.assertEqual(listed, [ids[2], ids[1]])
        self.assertFalse(os.path.exists(f"{self.folder}/json-visualizer/snapshots/{ids[0]}.json.gz"))
        with connection() as db:
            db.execute("UPDATE json_viz_snapshots SET created_at=? WHERE id=?", (time.time() - 3 * 86400, ids[1]))
        self.client.put("/api/json-visualizer/settings", json={"retention_days": 2, "max_snapshots": 10})
        listed = [item["id"] for item in self.client.get("/api/json-visualizer/snapshots").json()["snapshots"]]
        self.assertEqual(listed, [ids[2]])


class ValidateTests(unittest.TestCase):
    def ok(self, tool, args):
        self.assertEqual(runner.validate(tool, args), args)

    def bad(self, tool, args):
        with self.assertRaises(ValueError, msg=f"{tool} {args}"):
            runner.validate(tool, args)

    def test_common(self):
        self.bad("aws", [])
        self.bad("bash", ["ls"])
        self.bad("aws", ["ec2", "describe-instances", "--filters", "a\nb"])
        self.bad("aws", ["ec2", "describe-instances", "a\x00"])
        self.bad("aws", ["ec2", "describe-instances", "--cli-input-json", "file://x.json"])
        self.bad("az", ["vm", "list", "--query", "fileb://x"])

    def test_aws(self):
        self.ok("aws", ["ec2", "describe-instances"])
        self.ok("aws", ["ec2", "describe-instances", "--query", "Reservations[].Instances[]", "--filters", "Name=x,Values=y"])
        self.ok("aws", ["s3api", "list-buckets"])
        self.ok("aws", ["iam", "get-role", "--role-name", "x"])
        self.ok("aws", ["ssm", "get-parameter", "--name", "x"])
        self.ok("aws", ["cloudtrail", "lookup-events"])
        self.ok("aws", ["logs", "filter-log-events", "--log-group-name", "g"])
        for operation in ("create-vpc", "delete-bucket", "scan", "terminate-instances", "run-instances"):
            self.bad("aws", ["ec2", operation])
        for service, operation in (("secretsmanager", "get-secret-value"), ("ec2", "get-password-data"),
                                   ("ecr", "get-authorization-token"), ("ecr", "get-login-password"),
                                   ("sts", "get-session-token"), ("sts", "get-federation-token"),
                                   ("cognito-identity", "get-credentials-for-identity"), ("redshift", "get-cluster-credentials"),
                                   ("s3api", "get-object"), ("eks", "get-token"), ("sso", "get-role-credentials")):
            self.bad("aws", [service, operation])
        self.bad("aws", ["ssm", "get-parameter", "--name", "x", "--with-decryption"])
        self.bad("aws", ["ssm", "get-parameters-by-path", "--path", "/", "--with-decryption"])
        self.bad("aws", ["ec2", "describe-instances", "--output", "text"])
        self.bad("aws", ["ec2", "describe-instances", "--output=text"])
        self.bad("aws", ["ec2", "describe-instances", "--profile", "x"])
        self.bad("aws", ["ec2", "describe-instances", "--region=us-east-1"])
        self.bad("aws", ["ec2", "describe-instances", "--endpoint-url", "http://x"])
        self.bad("aws", ["ec2"])
        self.bad("aws", ["EC2;", "describe-instances"])
        self.assertEqual(
            runner.arguments("aws", ["ec2", "describe-vpcs"], {"profile": "p1", "region": "eu-west-1"}),
            ["ec2", "describe-vpcs", "--output", "json", "--profile", "p1", "--region", "eu-west-1"],
        )

    def test_az(self):
        self.ok("az", ["vm", "list"])
        self.ok("az", ["network", "nsg", "show", "--name", "x", "-g", "rg"])
        self.ok("az", ["vm", "list", "--query", "[].name"])
        self.bad("az", ["vm", "delete", "--name", "x"])
        self.bad("az", ["vm", "list", "-o", "table"])
        self.bad("az", ["vm", "list", "--output=table"])
        self.bad("az", ["list"])
        self.bad("az", ["keyvault", "secret", "show", "--name", "x"])
        self.bad("az", ["storage", "account", "keys", "list"])
        self.bad("az", ["acr", "credential", "show"])
        self.bad("az", ["vm", "list", "--subscription", "s"])
        self.assertEqual(runner.arguments("az", ["vm", "list"], {"subscription": "Sub 1"}), ["vm", "list", "-o", "json", "--subscription", "Sub 1"])

    def test_kubectl(self):
        self.ok("kubectl", ["get", "pods", "-A"])
        self.ok("kubectl", ["get", "deployments", "-n", "kube-system", "-l", "app=x"])
        self.bad("kubectl", ["describe", "pods"])
        self.bad("kubectl", ["delete", "pod", "x"])
        for flag in ("-o", "-oyaml", "--output=yaml", "--kubeconfig=x", "--token", "--as", "-w", "--watch", "--context=x"):
            self.bad("kubectl", ["get", "pods", flag])
        self.bad("kubectl", ["get", "secrets"])
        self.bad("kubectl", ["get", "pods,secret"])
        self.assertEqual(runner.arguments("kubectl", ["get", "pods"], {"context": "arn:aws:eks:x/y"}), ["get", "pods", "-o", "json", "--context", "arn:aws:eks:x/y"])

    def test_gcloud(self):
        self.ok("gcloud", ["compute", "instances", "list"])
        self.ok("gcloud", ["compute", "instances", "describe", "vm-1", "--zone", "z"])
        self.bad("gcloud", ["compute", "instances", "delete", "vm-1"])
        self.bad("gcloud", ["auth", "print-access-token"])
        self.bad("gcloud", ["compute", "instances", "list", "--format=table"])
        self.bad("gcloud", ["compute", "instances", "list", "--project", "p"])
        self.assertEqual(runner.arguments("gcloud", ["projects", "list"], {"project": "p-1"}), ["projects", "list", "--format=json", "--project", "p-1"])

    def test_fields(self):
        self.assertEqual(runner.options("aws", {"profile": " p ", "region": ""}), {"profile": "p"})
        for tool, fields in (("aws", {"profile": "-x"}), ("aws", {"region": "nowhere"}), ("az", {"profile": "p"}),
                             ("kubectl", {"context": "a\nb"})):
            with self.assertRaises(ValueError):
                runner.options(tool, fields)

    def test_windows_cmd_shims(self):
        self.assertEqual(runner.launcher("/usr/bin/az", ["a & b"], windows=False), ["/usr/bin/az"])
        with tempfile.TemporaryDirectory() as folder:
            shim = os.path.join(folder, "wbin", "gcloud.cmd")
            self.assertEqual(runner.launcher(shim, ["list"], windows=True), [shim])
            with self.assertRaises(ValueError):
                runner.launcher(shim, ["x & calc"], windows=True)
            os.makedirs(os.path.join(folder, "Lib", "site-packages", "azure", "cli"))
            open(os.path.join(folder, "python.exe"), "w").close()
            az = os.path.join(folder, "wbin", "az.cmd")
            self.assertEqual(runner.launcher(az, ['[?a=="b"] | x'], windows=True), [os.path.join(folder, "python.exe"), "-IBm", "azure.cli"])


class RunnerTests(Base):
    def fake(self, code):
        return patch.object(runner, "command", lambda tool, args, fields: ([sys.executable, "-c", code], "aws ec2 describe-vpcs"))

    def finished(self, job_id):
        return wait(lambda: (lambda job: job if job["state"] != "running" else None)(
            self.client.get(f"/api/json-visualizer/run/{job_id}").json()))

    def test_run_saves_a_snapshot_and_a_command(self):
        with self.fake("print('[1, 2]')"):
            response = self.client.post("/api/json-visualizer/run", json={
                "tool": "aws", "args": ["ec2", "describe-vpcs"], "profile": "p1", "region": "us-east-1", "save_as": "VPCs",
            })
        self.assertEqual(response.status_code, 202, response.text)
        job = self.finished(response.json()["job_id"])
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(job["snapshot"]["labels"], {"command": "aws ec2 describe-vpcs", "tool": "aws", "profile": "p1", "region": "us-east-1"})
        self.assertEqual(job["snapshot"]["source"], "runner")
        content = self.client.get(f"/api/json-visualizer/snapshots/{job['snapshot_id']}/content").json()
        self.assertEqual(content, [1, 2])
        saved = self.client.get("/api/json-visualizer/commands").json()["commands"]
        self.assertEqual([(item["name"], item["args"], item["profile"]) for item in saved], [("VPCs", ["ec2", "describe-vpcs"], "p1")])
        self.assertIsNotNone(wait(lambda: self.client.get("/api/json-visualizer/commands").json()["commands"][0]["last_run_at"]))
        self.assertEqual(self.client.get("/api/json-visualizer/runner").json()["saved"][0]["name"], "VPCs")
        self.assertEqual(self.client.delete(f"/api/json-visualizer/commands/{saved[0]['id']}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/json-visualizer/commands/{saved[0]['id']}").status_code, 404)

    def test_failures(self):
        with self.fake("import sys; sys.stderr.write('warning\\nAccessDenied: nope\\n'); sys.exit(3)"):
            job = self.finished(self.client.post("/api/json-visualizer/run", json={"tool": "aws", "args": ["ec2", "describe-vpcs"]}).json()["job_id"])
        self.assertEqual((job["state"], job["returncode"], job["error"]), ("failed", 3, "AccessDenied: nope"))
        self.assertIn("warning", job["stderr"])
        with self.fake("pass"):
            job = self.finished(self.client.post("/api/json-visualizer/run", json={"tool": "aws", "args": ["ec2", "describe-vpcs"]}).json()["job_id"])
        self.assertEqual(job["error"], "The command returned no output.")
        with self.fake("print('hello there')"):
            job = self.finished(self.client.post("/api/json-visualizer/run", json={"tool": "aws", "args": ["ec2", "describe-vpcs"]}).json()["job_id"])
        self.assertEqual(job["error"], "The command did not return JSON: hello there")
        with self.fake("print('{\"a\": 1}'); print('{\"a\": 2}')"):
            job = self.finished(self.client.post("/api/json-visualizer/run", json={"tool": "aws", "args": ["ec2", "describe-vpcs"]}).json()["job_id"])
        self.assertEqual(job["snapshot"]["format"], "jsonl")
        # Checks that reach the real allowlist.
        self.assertEqual(self.client.post("/api/json-visualizer/run", json={"tool": "aws", "args": ["ec2", "terminate-instances"]}).status_code, 400)
        self.assertEqual(self.client.post("/api/json-visualizer/run", json={"tool": "aws", "args": ["ec2", "describe-vpcs"], "timeout": 1}).status_code, 422)
        with patch.object(runner.shutil, "which", return_value=None):
            response = self.client.post("/api/json-visualizer/run", json={"tool": "aws", "args": ["ec2", "describe-vpcs"]})
        self.assertIn("was not found", response.json()["detail"])
        self.assertEqual(self.client.get("/api/json-visualizer/run/nope").status_code, 404)

    def test_timeout_and_cancel(self):
        with self.fake("import time; time.sleep(30)"):
            job = runner.start("aws", ["ec2", "describe-vpcs"], {}, timeout=1)
            done = self.finished(job.id)
            self.assertEqual(done["state"], "failed")
            self.assertIn("did not finish within 1 seconds", done["error"])
            job_id = self.client.post("/api/json-visualizer/run", json={"tool": "aws", "args": ["ec2", "describe-vpcs"]}).json()["job_id"]
            wait(lambda: runner.job(job_id).process)
            self.assertEqual(self.client.post(f"/api/json-visualizer/run/{job_id}/cancel").status_code, 200)
            done = self.finished(job_id)
        self.assertEqual(done["state"], "cancelled")
        self.assertLess(done["seconds"], 20)

    def test_overview(self):
        overview = self.client.get("/api/json-visualizer/runner").json()
        self.assertEqual(overview["aws_profiles"], [])
        self.assertIn("us-east-1", overview["aws_regions"])
        self.assertEqual(set(overview["tools"]), {"aws", "az", "kubectl", "gcloud"})
        with open(self.folder + "/aws-config", "w") as file:
            file.write("[default]\nregion=us-east-1\n[profile team-prod]\nregion=eu-west-1\n")
        with patch.object(runner, "kube_contexts", return_value=["ctx"]):
            overview = self.client.get("/api/json-visualizer/runner").json()
        self.assertEqual((overview["aws_profiles"], overview["kube_contexts"]), (["default", "team-prod"], ["ctx"]))
        self.assertEqual(self.client.post("/api/json-visualizer/commands", json={"name": "x", "tool": "kubectl", "args": ["delete", "pod"]}).status_code, 400)
        created = self.client.post("/api/json-visualizer/commands", json={"name": "Pods", "tool": "kubectl", "args": ["get", "pods"], "context": "c1"}).json()
        self.assertEqual((created["context"], created["last_run_at"]), ("c1", None))


class FetchTests(Base):
    def test_rejects_bad_urls(self):
        for url in ("ftp://x/y.json", "file:///etc/passwd", "javascript:alert(1)", "https://user:pw@host/x.json", "http:///x"):
            response = self.client.post("/api/json-visualizer/fetch", json={"url": url})
            self.assertEqual(response.status_code, 400, url)

    def test_streams_the_body(self):
        real = httpx.AsyncClient

        def upstream(request):
            if request.url.path == "/missing":
                return httpx.Response(404)
            if request.url.path == "/old":
                return httpx.Response(302, headers={"Location": "https://example.test/data/vpcs%20all.json?x=1"})
            self.assertNotIn("cookie", request.headers)
            return httpx.Response(200, content=b'{"Vpcs": []}')

        with patch("app.api.json_visualizer.httpx.AsyncClient", side_effect=lambda **kw: real(transport=httpx.MockTransport(upstream), **kw)):
            response = self.client.post("/api/json-visualizer/fetch", json={"url": "https://example.test/data/vpcs%20all.json?x=1"}, cookies={"session": "s"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.content, b'{"Vpcs": []}')
            self.assertEqual(response.headers["x-file-name"], "vpcs all.json")
            self.assertEqual(response.headers["x-file-size"], "12")
            self.assertEqual(response.headers["content-type"], "application/octet-stream")
            self.assertEqual(self.client.post("/api/json-visualizer/fetch", json={"url": "https://example.test/old"}).content, b'{"Vpcs": []}')
            self.assertEqual(self.client.post("/api/json-visualizer/fetch", json={"url": "https://example.test/"}).headers["x-file-name"], "download.json")
            self.assertEqual(self.client.post("/api/json-visualizer/fetch", json={"url": "https://example.test/missing"}).status_code, 502)


# A reference for search.js: the walk over the whole parsed document.
def reference_search(document, pattern, scope="both"):
    keys, values = scope != "values", scope != "keys"
    matches = []

    def visit(value, path, key, parented):
        if keys and isinstance(key, str) and pattern.search(key):
            matches.append({"path": path, "on": "key"})
        if isinstance(value, dict):
            for name, item in value.items():
                visit(item, path + [name], name, True)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, path + [index], index, True)
        elif values and parented and pattern.search(filters.js_string(value)):
            matches.append({"path": path, "on": "value"})
    visit(document, [], None, False)
    return matches


def instances(count):
    return [{
        "InstanceId": f"i-{index:04x}", "InstanceType": ["m5.large", "t3.micro", "c5.xlarge"][index % 3],
        "State": {"Name": "running" if index % 4 else "stopped", "Code": 16},
        "Tags": [{"Key": "Name", "Value": f"web-{index}"}], "CpuOptions": {"CoreCount": index % 5 + 1},
        "Price": [1.5, 1.50, 1e2, 0.0000001][index % 4], "Note": "café \"quoted\"" if index == 7 else "plain",
    } for index in range(count)]


class ServerDocumentTests(Base):
    def make(self, data, name="doc.json", window=64):
        path = os.path.join(self.folder, "upload.tmp")
        with open(path, "wb") as file:
            file.write(data)
        return serverdoc.add(name, path, len(data), window=window, background=False)

    def api(self, document, endpoint, **params):
        if "path" in params:
            params["path"] = json.dumps(params["path"])
        return self.client.get(f"/api/json-visualizer/documents/{document.id}/{endpoint}", params=params)

    def test_array_root_with_tiny_windows(self):
        items = [{"id": index, "name": f"item {index}", "text": "x" * (index % 50), "n": [index, None, True]} for index in range(2000)]
        data = b"\xef\xbb\xbf  " + json.dumps(items, indent=1).encode()
        document = self.make(data)
        self.assertEqual(document.state, "ready", document.error)
        self.assertEqual(document.status()["root"], {"type": "array", "count": 2000})
        self.assertEqual([document.load(o, l) for o, l in zip(document.region.offsets, document.region.lengths)], items)
        for window in (1, 2, 3, 7, 33):
            small = self.make(json.dumps([1, 22, 333.5, "a,]b", {"k": [1, 2]}, [], -0.5e3, True, None, 12345678901]).encode(), window=window)
            self.assertEqual(small.state, "ready", small.error)
            self.assertEqual([small.load(o, l) for o, l in zip(small.region.offsets, small.region.lengths)],
                             [1, 22, 333.5, "a,]b", {"k": [1, 2]}, [], -500.0, True, None, 12345678901])
        children = self.api(document, "children", path=[], offset=10, limit=3).json()
        self.assertEqual(children["total"], 2000)
        self.assertEqual([(child["key"], child["type"], child["count"], child["preview"]) for child in children["children"]],
                         [(10, "object", 4, "{4 keys}"), (11, "object", 4, "{4 keys}"), (12, "object", 4, "{4 keys}")])
        node = self.api(document, "node", path=[5, "n"]).json()
        self.assertEqual(node, {"path": [5, "n"], "type": "array", "preview": "[3 items]", "count": 3, "value": [5, None, True]})
        leaves = self.api(document, "children", path=[5]).json()["children"]
        self.assertEqual(leaves[1], {"key": "name", "type": "string", "preview": "item 5", "value": "item 5"})
        self.assertEqual(self.api(document, "value", path=[3]).json(), items[3])
        self.assertEqual(self.api(document, "node", path=[]).json()["value"], items)
        self.assertEqual(self.api(document, "node", path=[2000]).status_code, 404)
        self.assertEqual(self.api(document, "node", path=["x"]).status_code, 404)
        self.assertEqual(self.client.get(f"/api/json-visualizer/documents/{document.id}/node", params={"path": "not json"}).status_code, 400)
        self.assertEqual(self.client.get(f"/api/json-visualizer/documents/{document.id}/node", params={"path": "{}"}).status_code, 400)

    def test_errors_while_indexing(self):
        for data, message in ((b"[1, 2,]", "Invalid JSON"), (b"[1 2]", "Expecting ','"), (b"", "empty"),
                              (b"[1] [2]", "Unexpected text"), (b'{"a": 1,}', "Expecting a key"), (b'["abc', "Invalid JSON")):
            document = self.make(data, window=4)
            self.assertEqual(document.state, "error", data)
            self.assertIn(message, document.error)

    def test_object_root_rows_filters_sort_summary(self):
        reservations = [{"ReservationId": f"r-{r}", "Instances": instances(30)[r * 3:(r + 1) * 3]} for r in range(10)]
        reservations.append({"ReservationId": "r-empty"})
        data = json.dumps({"Reservations": reservations, "NextToken": "abc", "Meta": {"a": 1}}).encode()
        document = self.make(data, window=100)
        self.assertEqual(document.state, "ready", document.error)
        root = self.client.get(f"/api/json-visualizer/documents/{document.id}/status").json()["root"]
        self.assertEqual(root["keys"], [
            {"key": "Reservations", "type": "array", "preview": "[11 items]", "count": 11},
            {"key": "NextToken", "type": "string", "preview": "abc"},
            {"key": "Meta", "type": "object", "preview": "{1 key}", "count": 1},
        ])
        top = self.api(document, "children", path=[]).json()
        self.assertEqual(top["children"][1]["value"], "abc")
        self.assertEqual(self.api(document, "node", path=["Reservations", 2, "Instances", 1, "InstanceId"]).json()["value"], "i-0007")
        page = self.api(document, "rows", path=["Reservations"], flatten="Instances", offset=0, limit=5, columns=1).json()
        self.assertEqual((page["total"], page["unfiltered"]), (30, 30))
        self.assertEqual(page["rows"][4]["path"], ["Reservations", 1, "Instances", 1])
        self.assertEqual(page["columns"][:3], ["InstanceId", "InstanceType", "State.Name"])
        expected = [item for item in instances(30) if item["State"]["Name"] == "running" and "m5" in item["InstanceType"]]
        expected.sort(key=lambda item: -item["CpuOptions"]["CoreCount"])
        query = {"filters": json.dumps({"State.Name": "=running", "InstanceType": "m5"}),
                 "sort": json.dumps([{"key": "CpuOptions.CoreCount", "dir": -1}])}
        first = self.api(document, "rows", path=["Reservations"], flatten="Instances", limit=3, **query).json()
        second = self.api(document, "rows", path=["Reservations"], flatten="Instances", offset=3, limit=100, **query).json()
        self.assertEqual(first["total"], len(expected))
        self.assertEqual([row["value"] for row in first["rows"] + second["rows"]], expected)
        named = self.api(document, "rows", path=["Reservations"], flatten="Instances", filters=json.dumps({"@name": "=web-12"})).json()
        self.assertEqual([row["path"] for row in named["rows"]], [["Reservations", 4, "Instances", 0]])
        ranged = self.api(document, "rows", path=["Reservations"], flatten="Instances", filters=json.dumps({"CpuOptions.CoreCount": "2..3"})).json()
        self.assertEqual(ranged["total"], 12)
        plain = self.api(document, "rows", path=["Reservations"], offset=10, limit=5).json()
        self.assertEqual((plain["total"], [row["path"] for row in plain["rows"]]), (11, [["Reservations", 10]]))
        inner = self.api(document, "rows", path=["Reservations", 0, "Instances"]).json()
        self.assertEqual(inner["total"], 3)
        self.assertEqual(self.api(document, "rows", path=["NextToken"]).status_code, 400)
        self.assertEqual(self.api(document, "rows", path=["Reservations"], sort="[{\"key\": 1}]").status_code, 400)
        summary = self.api(document, "summary", path=["Reservations"], flatten="Instances", fields="State.Name,InstanceType").json()
        self.assertEqual(summary["total"], 30)
        self.assertEqual(summary["breakdowns"]["State.Name"], [["running", 22], ["stopped", 8]])
        self.assertEqual(summary["breakdowns"]["InstanceType"], [["m5.large", 10], ["t3.micro", 10], ["c5.xlarge", 10]])

    def test_jsonl_with_bad_lines(self):
        lines = [json.dumps({"n": index}) for index in range(5)]
        data = ("\r\n".join(lines[:2]) + "\n\n{oops\n" + "\n".join(lines[2:]) + "\nnot json\n").encode()
        document = self.make(data, name="events.log")
        self.assertEqual((document.state, document.format), ("ready", "jsonl"), document.error)
        status = document.status()
        self.assertEqual((status["root"], status["error_count"]), ({"type": "array", "count": 5}, 2))
        self.assertEqual([(item["line"], item["text"]) for item in status["errors"]], [(4, "{oops"), (8, "not json")])
        self.assertEqual(self.api(document, "value", path=[]).json(), [{"n": index} for index in range(5)])
        self.assertEqual(self.make(b'{"a": 1}\n', name="one.json").format, "json")
        self.assertEqual(self.make(b'{"a": 1}\n', name="one.ndjson").format, "jsonl")

    def test_gzip_upload_over_http(self):
        items = instances(50)
        packed = gzip.compress(json.dumps(items).encode())
        response = self.client.post("/api/json-visualizer/documents", params={"name": "big.json.gz"}, content=packed)
        self.assertEqual(response.status_code, 201, response.text)
        document_id = response.json()["id"]
        status = wait(lambda: (lambda s: s if s["state"] != "indexing" else None)(
            self.client.get(f"/api/json-visualizer/documents/{document_id}/status").json()))
        self.assertEqual((status["state"], status["root"]["count"]), ("ready", 50), status)
        self.assertEqual(status["progress"]["bytes"], status["progress"]["total"])
        self.assertEqual(self.client.get(f"/api/json-visualizer/documents/{document_id}/value", params={"path": "[49]"}).json(), items[49])
        self.assertEqual(self.client.post("/api/json-visualizer/documents", params={"name": "x.gz"}, content=packed[:40]).status_code, 400)
        self.assertEqual(self.client.delete(f"/api/json-visualizer/documents/{document_id}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/json-visualizer/documents/{document_id}/status").status_code, 404)

    def test_size_limits_and_idle_documents(self):
        with patch.object(serverdoc, "VALUE_LIMIT", 100), patch.object(serverdoc, "NODE_VALUE_LIMIT", 50):
            document = self.make(json.dumps([{"a": "x" * 120}, 1]).encode())
            self.assertEqual(self.api(document, "value", path=[]).status_code, 413)
            self.assertEqual(self.api(document, "value", path=[1]).json(), 1)
            node = self.api(document, "node", path=[0]).json()
            self.assertTrue(node["truncated"])
            self.assertNotIn("value", node)
            big = self.make(json.dumps({"list": [1], "blob": "y" * 200}).encode())
            self.assertEqual(big.state, "error")
            self.assertIn("larger than", big.error)
        document.used -= serverdoc.IDLE_SECONDS + 1
        serverdoc.sweep()
        self.assertEqual(self.client.get(f"/api/json-visualizer/documents/{document.id}/status").status_code, 404)
        self.assertFalse(os.path.exists(document.path))

    def test_search_matches_search_js(self):
        doc = {"Reservations": [{"Instances": instances(12), "OwnerId": "1234"} for _ in range(3)],
               "NextToken": "web", "Name": None, "Count": 1.0}
        document = self.make(json.dumps(doc).encode(), window=50)
        cases = [
            ("web", {}), ("WEB", {"case_sensitive": True}), ("web", {"whole_word": True}),
            ("^web-1\\d$", {"regex": True}), ("name", {"scope": "keys"}), ("name", {"scope": "values"}),
            ("1.5", {}), ("100", {}), ("1e-7", {}), ("café", {}), ('"quoted"', {}), ("null", {}), ("1", {"whole_word": True}),
        ]
        for query, options in cases:
            scope = options.pop("scope", "both")
            pattern = filters.matcher(query, **options)
            expected = reference_search(doc, pattern, scope)
            search = serverdoc.start_search(document, query, scope=scope, background=False,
                                            case_sensitive=options.get("case_sensitive", False),
                                            whole_word=options.get("whole_word", False), regex=options.get("regex", False))
            self.assertEqual(search.matches, expected, (query, options, scope))
        array_doc = self.make(json.dumps(instances(20)).encode())
        for query in ("stopped", "1.5", "100", "i-000a"):
            search = serverdoc.start_search(array_doc, query, background=False)
            self.assertEqual(search.matches, reference_search(instances(20), filters.matcher(query)), query)
        response = self.client.post(f"/api/json-visualizer/documents/{document.id}/search", json={"query": "running", "scope": "values"})
        search_id = response.json()["search_id"]
        result = wait(lambda: (lambda r: r if r["done"] else None)(
            self.client.get(f"/api/json-visualizer/documents/{document.id}/search/{search_id}").json()))
        self.assertEqual(result["count"], len(reference_search(doc, filters.matcher("running"), "values")))
        later = self.client.get(f"/api/json-visualizer/documents/{document.id}/search/{search_id}", params={"after": 5}).json()
        self.assertEqual(later["matches"], result["matches"][5:])
        self.assertEqual(self.client.delete(f"/api/json-visualizer/documents/{document.id}/search/{search_id}").status_code, 200)
        self.assertEqual(self.client.post(f"/api/json-visualizer/documents/{document.id}/search", json={"query": "(", "regex": True}).status_code, 400)
        with patch.object(serverdoc, "MAX_MATCHES", 3):
            capped = serverdoc.start_search(document, "e", background=False)
        self.assertEqual((len(capped.matches), capped.capped), (3, True))


class FilterPortTests(unittest.TestCase):
    def test_cells_numbers_filters_and_sorting(self):
        M = filters.MISSING
        self.assertEqual([filters.js_number(v) for v in (1.0, 1.5, 1e21, 1e-7, 0.000001, 123456789.125, -0.0, 10**20 * 1.0)],
                         ["1", "1.5", "1e+21", "1e-7", "0.000001", "123456789.125", "0", "100000000000000000000"])
        self.assertEqual([filters.cell_text(v) for v in (M, None, [1, None, "a"], [{"a": 1}], {"a": 1.0, "b": [True]}, False)],
                         ["", "null", "1, , a", "[1 item]", '{"a":1,"b":[true]}', "false"])
        test = filters.filter_test
        self.assertTrue(test(" =Running ")("running"))
        self.assertFalse(test("!=running")("Running"))
        self.assertTrue(test(">10")(11) and not test(">10")("abc") and not test(">10")(M))
        self.assertTrue(test(">b")("c"))
        self.assertTrue(test("1..3")("2") and not test("1..3")(""))
        self.assertTrue(test("empty")(M) and test("!empty")(0))
        self.assertTrue(test("web")(["a", "Web-1"]))
        self.assertTrue(test("")(M))
        values = ["b10", "B2", "", "a", 10, 9, "é"]
        import functools
        ordered = sorted(values, key=functools.cmp_to_key(filters.compare_cells))
        self.assertEqual(ordered, [9, 10, "a", "B2", "b10", "é", ""])
        record = {"Tags": [{"Key": "Name", "Value": "web"}], "State": {"Name": "running"}, "a.b": 1, "list": [5, 6]}
        self.assertEqual(filters.get_field(record, "@name"), "web")
        self.assertEqual(filters.get_field(record, "State.Name"), "running")
        self.assertEqual(filters.get_field(record, "a.b"), 1)
        self.assertEqual(filters.get_field(record, "list.1"), 6)
        self.assertIs(filters.get_field(record, "State.Name.x"), M)
        self.assertEqual(filters.display_name({"metadata": {"name": "pod-1"}}), "pod-1")
        self.assertEqual(filters.display_name({"InstanceId": "i-1", "Name": ""}), "i-1")
        self.assertEqual(filters.display_name({"Tags": [], "VpcId": "vpc-1"}), "vpc-1")
        self.assertEqual(filters.column_keys([{"a": 1, "s": {"x": 1}}, {"b": 2, "a": 3}]), ["a", "s.x", "b"])


if __name__ == "__main__":
    unittest.main()
