"""Architecture data as JSON files beside OWL's database.

    architecture/
      snapshots/<id>/inventory.json   normalized AWS inventory
      snapshots/<id>/meta.json        name, time, counts, import errors and warnings
      projects/<id>.json              project, applications and requirements
      services/*.json                 shared and central services hosted in AWS
      external/*.json                 on-premises and third-party systems
      rules/*.json                    architecture standards (override the built-in ones by id)

The first use copies the sample Payment Platform in, with a snapshot imported from
the sample AWS export, so the app has something to show.
"""

import json
import re
import shutil
import threading
import time
from pathlib import Path

from app.architecture.models import ExternalSystem, Inventory, Project, Rule, Workload
from app.architecture.normalize import counts, normalize
from app.core.database import database_path

PACKAGE = Path(__file__).resolve().parent
SAMPLE = PACKAGE / "sample"
BUILT_IN_RULES = PACKAGE / "rules"
_lock = threading.Lock()
_cache = {}


def folder():
    return database_path().parent / "architecture"


def ensure():
    root = folder()
    with _lock:
        if (root / ".seeded").exists():
            return root
        for name in ("projects", "services", "external", "rules", "snapshots"):
            (root / name).mkdir(parents=True, exist_ok=True)
        for name in ("projects", "services", "external"):
            for source in sorted((SAMPLE / name).glob("*.json")):
                target = root / name / source.name
                if not target.exists():
                    shutil.copyfile(source, target)
        if not any((root / "snapshots").iterdir()):
            files = [(str(p.relative_to(SAMPLE / "aws")), json.loads(p.read_text())) for p in sorted((SAMPLE / "aws").rglob("*.json"))]
            _store_snapshot(root, "Sample export", files, created=time.time())
        (root / ".seeded").write_text("1")
    return root


def _read(path):
    return json.loads(Path(path).read_text())


def _items(name, model):
    """Every object in a folder; a file holds one object or a list of them."""
    out = {}
    for path in sorted((ensure() / name).glob("*.json")):
        data = _read(path)
        for item in data if isinstance(data, list) else [data]:
            value = model.model_validate(item)
            out[value.id] = value
    return out


def projects():
    return _items("projects", Project)


def project(project_id):
    found = projects().get(project_id)
    if not found:
        raise KeyError(f"Project {project_id} not found.")
    return found


def save_project(value: Project):
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", value.id):
        raise ValueError("Project ids use letters, digits, dot, dash and underscore.")
    path = ensure() / "projects" / f"{value.id}.json"
    path.write_text(json.dumps(value.model_dump(exclude_defaults=True), indent=2) + "\n")
    return value


def services():
    return list(_items("services", Workload).values())


def externals():
    return list(_items("external", ExternalSystem).values())


def rules():
    found = {}
    for path in sorted(BUILT_IN_RULES.glob("*.json")) + sorted((ensure() / "rules").glob("*.json")):
        data = _read(path)
        for item in data if isinstance(data, list) else [data]:
            rule = Rule.model_validate(item)
            found[rule.id] = rule
    return [found[k] for k in sorted(found)]


# Snapshots


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "snapshot"


def _store_snapshot(root, name, files, created):
    inventory, errors, warnings = normalize(files)
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(created))
    snapshot_id = f"{stamp}-{_slug(name)}"
    target = root / "snapshots" / snapshot_id
    suffix = 1
    while target.exists():
        suffix += 1
        target = root / "snapshots" / f"{snapshot_id}-{suffix}"
    target.mkdir(parents=True)
    (target / "inventory.json").write_text(inventory.model_dump_json(by_alias=True))
    meta = {
        "id": target.name, "name": name, "created": created, "files": len(files),
        "accounts": sorted(inventory.accounts),
        "counts": counts(inventory), "errors": errors, "warnings": warnings,
    }
    (target / "meta.json").write_text(json.dumps(meta, indent=2))
    return meta


def validate(files):
    inventory, errors, warnings = normalize(files)
    return {"counts": counts(inventory), "errors": errors, "warnings": warnings, "files": len(files)}


def import_snapshot(name, files):
    root = ensure()
    preview = validate(files)
    if preview["errors"]:
        raise ValueError("The import has errors; fix them and try again: " + "; ".join(preview["errors"][:5]))
    if not any(item["count"] for item in preview["counts"].values()):
        raise ValueError("No AWS resources were found in the files.")
    with _lock:
        return _store_snapshot(root, name or "Import", files, created=time.time())


def snapshots():
    root = ensure() / "snapshots"
    items = [_read(p / "meta.json") for p in root.iterdir() if (p / "meta.json").exists()]
    return sorted(items, key=lambda m: (-m["created"], m["id"]))


def snapshot_meta(snapshot_id=""):
    items = snapshots()
    if not items:
        raise KeyError("No snapshots have been imported.")
    if not snapshot_id:
        return items[0]
    found = next((m for m in items if m["id"] == snapshot_id), None)
    if not found:
        raise KeyError(f"Snapshot {snapshot_id} not found.")
    return found


def inventory(snapshot_id=""):
    meta = snapshot_meta(snapshot_id)
    path = ensure() / "snapshots" / meta["id"] / "inventory.json"
    stamp = path.stat().st_mtime
    cached = _cache.get(meta["id"])
    if cached and cached[0] == stamp:
        return meta, cached[1]
    value = Inventory.model_validate_json(path.read_text())
    _cache[meta["id"]] = (stamp, value)
    return meta, value


def delete_snapshot(snapshot_id):
    meta = snapshot_meta(snapshot_id)
    shutil.rmtree(ensure() / "snapshots" / meta["id"])
    _cache.pop(meta["id"], None)
