"""Read-only cloud CLI commands (aws, az, kubectl, gcloud) whose JSON output becomes a
snapshot. Commands run without a shell, from an allowlist: only listing and describing
operations, never ones that return secrets, change resources or write local files.
"""

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from app.aws import profiles
from app.aws.connection import PROFILE
from app.core.database import connection
from app.jsonviz import snapshots, storage

TOOLS = ("aws", "az", "kubectl", "gcloud")
AWS_REGIONS = [
    "us-east-1", "us-east-2", "us-west-1", "us-west-2", "ca-central-1", "ca-west-1",
    "sa-east-1", "mx-central-1", "eu-west-1", "eu-west-2", "eu-west-3", "eu-central-1",
    "eu-central-2", "eu-north-1", "eu-south-1", "eu-south-2", "me-south-1", "me-central-1",
    "il-central-1", "af-south-1", "ap-south-1", "ap-south-2", "ap-east-1", "ap-southeast-1",
    "ap-southeast-2", "ap-southeast-3", "ap-southeast-4", "ap-southeast-5", "ap-southeast-7",
    "ap-northeast-1", "ap-northeast-2", "ap-northeast-3", "us-gov-east-1", "us-gov-west-1",
    "cn-north-1", "cn-northwest-1",
]
JOBS_KEPT = 50
STDERR_KEPT = 1024 * 1024
STDERR_SHOWN = 20 * 1024
PARSE_LIMIT = 100 * 1024 * 1024  # Larger output is checked by its first character only.

WORD = re.compile(r"^[a-z0-9][a-z0-9-]*$")
REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d{1,2}$")
AWS_PREFIXES = ("describe-", "list-", "get-")
AWS_EXTRA = {"lookup-events", "search-resources", "filter-log-events"}
# Read-looking operations that return secrets or credentials, or write local files.
AWS_DENIED = {
    "get-secret-value", "get-password-data", "get-authorization-token", "get-login-password",
    "get-login", "get-session-token", "get-federation-token", "get-credentials-for-identity",
    "get-cluster-credentials", "get-cluster-credentials-with-iam", "get-role-credentials",
    "get-open-id-token", "get-open-id-token-for-developer-identity", "get-token",
    "get-temporary-glue-table-credentials", "get-temporary-glue-partition-credentials",
    "get-instance-access-details", "get-random-password", "get-data-key",
    # These take an output file name.
    "get-object", "get-object-torrent", "get-export", "get-sdk", "get-job-output",
    "get-media", "get-clip", "get-media-for-fragment-list", "get-images",
}
AWS_DENIED_WORDS = re.compile(r"credential|password|secret-value|private-key|-token$|auth-token|access-token")
AWS_DECRYPTING = {"get-parameter", "get-parameters", "get-parameters-by-path"}
AZ_DENIED_WORDS = re.compile(r"secret|credential|keys?$|password|token|connection-string")
KUBECTL_SECRETS = {"secret", "secrets"}


def _flag(arg, *names):
    """Whether arg is one of the flags, also as --flag=value (or -ovalue for short ones)."""
    for name in names:
        if arg == name or arg.startswith(name + "="):
            return True
        if len(name) == 2 and arg.startswith(name):
            return True
    return False


def _positionals(args):
    words = []
    for arg in args:
        if arg.startswith("-"):
            break
        words.append(arg)
    return words


def validate(tool, args):
    """The checked argument list (a copy); raises ValueError for anything not allowed."""
    if tool not in TOOLS:
        raise ValueError("tool must be aws, az, kubectl or gcloud.")
    if not isinstance(args, list) or not args:
        raise ValueError("Enter the command's arguments, for example: ec2 describe-instances.")
    if len(args) > 100:
        raise ValueError("Use at most 100 arguments.")
    for arg in args:
        if not isinstance(arg, str) or not arg:
            raise ValueError("Every argument must be non-empty text.")
        if "\x00" in arg or "\n" in arg or "\r" in arg:
            raise ValueError("Arguments cannot contain line breaks or NUL characters.")
        if len(arg) > 4096:
            raise ValueError("Arguments can be at most 4096 characters long.")
        if "file://" in arg.lower() or "fileb://" in arg.lower():
            raise ValueError("Arguments cannot read local files (file://).")
    return {"aws": _aws, "az": _az, "kubectl": _kubectl, "gcloud": _gcloud}[tool](list(args))


def _aws(args):
    if len(args) < 2:
        raise ValueError("Enter a service and an operation, for example: ec2 describe-instances.")
    service, operation = args[0], args[1]
    if not WORD.match(service) or not WORD.match(operation):
        raise ValueError("Start with a service and an operation, for example: ec2 describe-instances.")
    if not (operation.startswith(AWS_PREFIXES) or operation in AWS_EXTRA):
        raise ValueError("Only describe-, list- and get- operations (and lookup-events, search-resources, filter-log-events) can run here.")
    if operation in AWS_DENIED or (operation.startswith("get-") and AWS_DENIED_WORDS.search(operation)):
        raise ValueError(f"{operation} is not allowed: it returns secrets or writes files.")
    if operation in AWS_DECRYPTING and any(_flag(arg, "--with-decryption") for arg in args):
        raise ValueError("--with-decryption is not allowed: it returns secret values.")
    for arg in args[2:]:
        if _flag(arg, "--output", "--profile", "--region", "--endpoint-url"):
            name = arg.split("=")[0]
            if name in ("--profile", "--region"):
                raise ValueError(f"Choose the {name[2:]} in its own field, not in the arguments.")
            raise ValueError(f"{name} is not allowed here.")
    return args


def _az(args):
    words = _positionals(args)
    if len(words) < 2 or not all(WORD.match(word) for word in words):
        raise ValueError("Enter a command group and list or show, for example: vm list.")
    if words[-1] not in ("list", "show"):
        raise ValueError("Only az commands that end with list or show can run here.")
    if any(AZ_DENIED_WORDS.search(word) for word in words):
        raise ValueError(f"az {' '.join(words)} is not allowed: it can return secrets or keys.")
    for arg in args[len(words):]:
        if _flag(arg, "--output", "-o"):
            raise ValueError("--output is not allowed here: the output is always JSON.")
        if _flag(arg, "--subscription"):
            raise ValueError("Choose the subscription in its own field, not in the arguments.")
        if arg.startswith("@"):
            raise ValueError("Arguments cannot read local files (@file).")
    return args


def _kubectl(args):
    if args[0] != "get":
        raise ValueError("Only kubectl get can run here.")
    for arg in args[1:]:
        if _flag(arg, "-o", "--output"):
            raise ValueError("--output is not allowed here: the output is always JSON.")
        if _flag(arg, "-w", "--watch", "--watch-only"):
            raise ValueError("--watch is not allowed here.")
        if _flag(arg, "--kubeconfig", "--token", "--as", "--as-group", "--as-uid", "--server", "-s",
                 "--username", "--password", "--client-key", "--client-certificate", "--certificate-authority"):
            raise ValueError(f"{arg.split('=')[0]} is not allowed here.")
        if _flag(arg, "--context"):
            raise ValueError("Choose the context in its own field, not in the arguments.")
        if not arg.startswith("-") and any(part.split("/")[0].split(".")[0].lower() in KUBECTL_SECRETS for part in arg.split(",")):
            raise ValueError("kubectl get secrets is not allowed: it returns secret values.")
    return args


def _gcloud(args):
    words = _positionals(args)
    # The command words end with list or describe; describe may name one resource after it.
    verb = next((index for index, word in enumerate(words) if word in ("list", "describe")), None)
    if verb is None or (words[-1] != "list" and verb < len(words) - (2 if words[verb] == "describe" else 1)):
        raise ValueError("Only gcloud commands that end with list or describe (and a name) can run here.")
    if not verb or not all(WORD.match(word) for word in words[:verb]):
        raise ValueError("Enter a command group and list or describe, for example: compute instances list.")
    for arg in args[len(words):]:
        if _flag(arg, "--format"):
            raise ValueError("--format is not allowed here: the output is always JSON.")
        if _flag(arg, "--project"):
            raise ValueError("Choose the project in its own field, not in the arguments.")
        if _flag(arg, "--flags-file", "--access-token-file", "--credential-file-override", "--impersonate-service-account"):
            raise ValueError(f"{arg.split('=')[0]} is not allowed here.")
    return args


def _field(value, label):
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith("-") or len(value) > 256 or any(char < " " for char in value):
        raise ValueError(f"The {label} is not valid.")
    return value


FIELDS = {"aws": ("profile", "region"), "az": ("subscription",), "kubectl": ("context",), "gcloud": ("project",)}


def options(tool, values):
    """The tool's own fields (profile and region for aws, …), checked; empty ones dropped."""
    result = {}
    for name in ("profile", "region", "subscription", "context", "project"):
        value = _field(values.get(name), name)
        if not value:
            continue
        if name not in FIELDS[tool]:
            raise ValueError(f"{name} does not apply to {tool}.")
        if name == "profile" and not PROFILE.match(value):
            raise ValueError("Profile names may contain letters, digits, '.', '_' and '-'.")
        if name == "region" and not REGION.match(value):
            raise ValueError("Choose an AWS region such as us-east-1.")
        result[name] = value
    return result


def arguments(tool, args, fields):
    """The full argument list after the tool name: checked args, then JSON output and
    the chosen profile, region, subscription, context or project."""
    args = validate(tool, args)
    if tool == "aws":
        args += ["--output", "json"]
        args += ["--profile", fields["profile"]] if fields.get("profile") else []
        args += ["--region", fields["region"]] if fields.get("region") else []
    elif tool == "az":
        args += ["-o", "json"] + (["--subscription", fields["subscription"]] if fields.get("subscription") else [])
    elif tool == "kubectl":
        args += ["-o", "json"] + (["--context", fields["context"]] if fields.get("context") else [])
    else:
        args += ["--format=json"] + (["--project", fields["project"]] if fields.get("project") else [])
    return args


CMD_SPECIAL = re.compile(r'[&|<>^%!"]')


def launcher(path, args, windows=None):
    """The program and leading arguments to start path with.

    On Windows, az and gcloud are .cmd files, which Windows runs through cmd.exe, so
    an argument like "a & b" would start a second command. The Azure CLI's own Python
    is used directly when it is there; otherwise cmd.exe's special characters are refused.
    """
    windows = os.name == "nt" if windows is None else windows
    if not windows or not str(path).lower().endswith((".cmd", ".bat")):
        return [str(path)]
    root = Path(path).parent.parent
    python = root / "python.exe"
    if Path(path).stem.lower() == "az" and python.exists() and (root / "Lib" / "site-packages" / "azure" / "cli").exists():
        return [str(python), "-IBm", "azure.cli"]
    for arg in args:
        if CMD_SPECIAL.search(arg):
            raise ValueError(f"On Windows, arguments for {Path(path).name} cannot contain & | < > ^ % ! or double quotes.")
    return [str(path)]


def command(tool, args, fields):
    """(program arguments, the command as shown to people)."""
    full = arguments(tool, args, fields)
    path = shutil.which(tool)
    if not path:
        raise ValueError(f"The {tool} command was not found. Install it and make sure `{tool}` is on PATH.")
    return launcher(path, full) + full, display(tool, full)


def display(tool, args):
    # Quoted for reading only; the command never goes through a shell.
    return shlex.join([tool, *args])


def _platform():
    options = {"stdin": subprocess.DEVNULL, "env": {**os.environ, "AWS_PAGER": ""}}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True  # So a kill reaches the tool's children too.
    return options


def _kill(process):
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            # .cmd shims start the real tool as a child; /T ends the whole tree.
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True,
                           timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.kill()
    except OSError:
        pass


# Jobs

_jobs = OrderedDict()
_jobs_lock = threading.Lock()


class Job:
    def __init__(self, tool, display_command, labels, name, timeout):
        self.id = uuid.uuid4().hex
        self.tool, self.command, self.labels, self.name, self.timeout = tool, display_command, labels, name, timeout
        self.state, self.started_at, self.finished_at = "running", time.time(), None
        self.returncode, self.error, self.snapshot_id = None, "", None
        self.stderr = bytearray()
        self.process = None
        self.cancelled = threading.Event()

    def view(self):
        snapshot = snapshots.get(self.snapshot_id) if self.snapshot_id else None
        return {
            "id": self.id, "state": self.state, "command": self.command, "started_at": self.started_at,
            "seconds": round((self.finished_at or time.time()) - self.started_at, 1),
            "returncode": self.returncode,
            "stderr": bytes(self.stderr[-STDERR_SHOWN:]).decode("utf-8", "replace"),
            "error": self.error, "snapshot_id": self.snapshot_id, "snapshot": snapshot,
        }


def _read_stderr(stream, job):
    for chunk in iter(lambda: stream.read(65536), b""):
        job.stderr += chunk
        if len(job.stderr) > STDERR_KEPT:
            del job.stderr[: len(job.stderr) - STDERR_KEPT]
    stream.close()


def check_output(path):
    """("json" | "jsonl") for the output file, or raise ValueError saying what it is."""
    size = os.path.getsize(path)
    with open(path, "rb") as file:
        head = file.read(300 * 4)
    text = head.decode("utf-8", "replace").lstrip("﻿").strip()
    if not text:
        raise ValueError("The command returned no output.")
    if size > PARSE_LIMIT:
        if text[0] in "[{":
            return "json"
        raise ValueError(f"The command did not return JSON: {text[:300]}")
    with open(path, "rb") as file:
        data = file.read().decode("utf-8", "replace").lstrip("﻿")
    try:
        json.loads(data)
        return "json"
    except ValueError:
        pass
    lines = [line for line in data.splitlines() if line.strip()]
    try:
        for line in lines:
            json.loads(line)
    except ValueError:
        raise ValueError(f"The command did not return JSON: {text[:300]}") from None
    return "jsonl"


def _execute(job, argv, command_id):
    output = None
    try:
        handle, output = tempfile.mkstemp(dir=storage.subfolder("runs"), prefix="run-", suffix=".out")
        with os.fdopen(handle, "wb") as stdout:
            job.process = subprocess.Popen(argv, stdout=stdout, stderr=subprocess.PIPE, **_platform())
            reader = threading.Thread(target=_read_stderr, args=(job.process.stderr, job), daemon=True)
            reader.start()
            if job.cancelled.is_set():
                _kill(job.process)
            try:
                job.process.wait(timeout=job.timeout)
            except subprocess.TimeoutExpired:
                _kill(job.process)
                job.process.wait()
                job.error = f"The command did not finish within {job.timeout} seconds and was stopped."
            reader.join(timeout=10)
        job.returncode = job.process.returncode
        if job.cancelled.is_set():
            job.state, job.error = "cancelled", "Cancelled."
        elif job.error:
            job.state = "failed"
        elif job.returncode:
            lines = [line.strip() for line in bytes(job.stderr).decode("utf-8", "replace").splitlines() if line.strip()]
            job.state = "failed"
            job.error = (lines[-1] if lines else f"The command failed with exit code {job.returncode}.")[:500]
        else:
            fmt = check_output(output)
            record = snapshots.store(output, job.name, labels=job.labels, format=fmt, origin="runner", command=job.command)
            output = None  # store() took the file.
            job.snapshot_id, job.state = record["id"], "done"
    except ValueError as error:
        job.state, job.error = "failed", str(error)
    except OSError as error:
        job.state, job.error = "failed", f"The command could not start: {error.strerror or error}."
    except Exception as error:  # noqa: BLE001 - the job must end with a reason
        job.state, job.error = "failed", f"The run failed: {type(error).__name__}."
    finally:
        job.finished_at = time.time()
        if output and os.path.exists(output):
            os.unlink(output)
    if command_id:
        touch_command(command_id)


def default_name(tool, args):
    words = [arg for arg in args[:4] if not arg.startswith("-")][:3]
    return "-".join([tool, *words]) + ".json"


def start(tool, args, fields, *, timeout=120, name=None, command_id=None):
    """Start a run in the background and return its job."""
    argv, shown = command(tool, args, fields)
    labels = {key: value[:200] for key, value in {"command": shown, "tool": tool, **fields}.items()}
    job = Job(tool, shown, labels, snapshots.clean_name(name) if name else default_name(tool, args), timeout)
    with _jobs_lock:
        _jobs[job.id] = job
        for key in [key for key, item in _jobs.items() if item.state != "running"][: max(0, len(_jobs) - JOBS_KEPT)]:
            del _jobs[key]
    threading.Thread(target=_execute, args=(job, argv, command_id), daemon=True, name=f"jsonviz-run-{job.id[:8]}").start()
    return job


def job(job_id):
    with _jobs_lock:
        return _jobs.get(job_id)


def cancel(job_id):
    found = job(job_id)
    if found is None:
        return None
    if found.state == "running":
        found.cancelled.set()
        if found.process is not None:
            _kill(found.process)
    return found


# Tools, profiles and contexts


def aws_profiles():
    """Profiles in the AWS config file AWS Accounts uses; none when it is missing."""
    try:
        with connection() as db:
            row = db.execute("SELECT path FROM aws_config_source WHERE id=1").fetchone()
        path = (row["path"] if row else "") or profiles.default_path()
        return [account["profile"] for account in profiles.read(path)]
    except (OSError, UnicodeError, ValueError):
        return []


def kube_contexts():
    path = shutil.which("kubectl")
    if not path:
        return []
    try:
        result = subprocess.run([path, "config", "get-contexts", "-o", "name"], capture_output=True,
                                text=True, timeout=5, **_platform())
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def overview():
    return {
        "tools": {tool: shutil.which(tool) for tool in TOOLS},
        "aws_profiles": aws_profiles(),
        "aws_regions": AWS_REGIONS,
        "kube_contexts": kube_contexts(),
        "saved": list_commands(),
    }


# Saved commands


def _command_record(row):
    record = dict(row)
    record["args"] = json.loads(record["args"])
    record.update(json.loads(record.pop("extra") or "{}"))
    return record


def list_commands():
    with connection() as db:
        rows = db.execute("SELECT * FROM json_viz_commands ORDER BY COALESCE(last_run_at,created_at) DESC, id DESC").fetchall()
    return [_command_record(row) for row in rows]


def save_command(name, tool, args, fields, ran=False):
    """Save (or replace, by name) a command after checking it."""
    name = (name or "").strip()
    if not name or len(name) > 200:
        raise ValueError("Give the command a name of up to 200 characters.")
    validate(tool, args)
    fields = options(tool, fields)
    extra = {key: fields[key] for key in ("subscription", "context", "project") if key in fields}
    now = time.time()
    with connection() as db:
        db.execute(
            "INSERT INTO json_viz_commands(name,tool,args,profile,region,extra,created_at,last_run_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET tool=excluded.tool,args=excluded.args,profile=excluded.profile,"
            "region=excluded.region,extra=excluded.extra,last_run_at=COALESCE(excluded.last_run_at,last_run_at)",
            (name, tool, json.dumps(args), fields.get("profile", ""), fields.get("region", ""),
             json.dumps(extra), now, now if ran else None),
        )
        row = db.execute("SELECT * FROM json_viz_commands WHERE name=?", (name,)).fetchone()
    return _command_record(row)


def delete_command(command_id):
    with connection() as db:
        return db.execute("DELETE FROM json_viz_commands WHERE id=?", (command_id,)).rowcount > 0


def touch_command(command_id):
    with connection() as db:
        db.execute("UPDATE json_viz_commands SET last_run_at=? WHERE id=?", (time.time(), command_id))
