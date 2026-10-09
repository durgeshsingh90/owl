"""AWS accounts from the AWS CLI config file (~/.aws/config), grouped by category."""

import json
import os
from datetime import datetime, timezone
from typing import Literal

from app.aws import connection as aws
from app.aws import profiles
from app.core.database import connection
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

router = APIRouter(prefix="/api")


def _source(db):
    row = db.execute("SELECT * FROM aws_config_source WHERE id=1").fetchone()
    return dict(row) if row else {"path": "", "mtime": None, "loaded_at": None, "error": ""}


def sync_config():
    """Read the AWS config file again when it changed; the accounts page shows it."""
    with connection() as db:
        source = _source(db)
    path = source["path"] or profiles.default_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        error = f"No AWS config file at {path}. Set its location in Settings."
        if error != source["error"]:
            with connection() as db:
                db.execute(
                    "INSERT INTO aws_config_source(id,path,error) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET error=excluded.error",
                    (source["path"], error),
                )
        return
    if mtime == source["mtime"] and not source["error"]:
        return
    try:
        payload = profiles.inventory(path)
        error = ""
    except (OSError, UnicodeError, ValueError) as problem:
        payload, error = None, str(problem)[:500] or "The AWS config file could not be read."
    now = datetime.now(timezone.utc).isoformat()
    with connection() as db:
        if payload is not None:
            db.execute(
                "INSERT INTO aws_accounts(id,payload,imported_at) VALUES(1,?,?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,imported_at=excluded.imported_at",
                (json.dumps(payload), now),
            )
        db.execute(
            "INSERT INTO aws_config_source(id,path,mtime,loaded_at,error) VALUES(1,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET mtime=excluded.mtime,loaded_at=CASE WHEN ? THEN excluded.loaded_at ELSE loaded_at END,error=excluded.error",
            (source["path"], mtime if payload is not None else None, now, error, payload is not None),
        )


def source_state():
    with connection() as db:
        source = _source(db)
    path = source["path"] or profiles.default_path()
    return {
        "path": path,
        "custom": bool(source["path"]),
        "default_path": profiles.default_path(),
        "exists": os.path.exists(path),
        "loaded_at": source["loaded_at"],
        "error": source["error"],
    }


@router.get("/aws-accounts")
def inventory():
    sync_config()
    with connection() as db:
        row = db.execute(
            "SELECT payload,imported_at FROM aws_accounts WHERE id=1"
        ).fetchone()
        copies = db.execute("SELECT kind,value,count FROM aws_account_copies").fetchall()
        stars = [
            row[0]
            for row in db.execute("SELECT profile FROM aws_account_stars ORDER BY starred_at")
        ]
        projects = _projects(db)
        custom = [row[0] for row in db.execute("SELECT name FROM aws_custom_categories ORDER BY created_at,name")]
        overrides = {row[0]: row[1] for row in db.execute("SELECT profile,category FROM aws_category_overrides")}
    counts = {"profile": {}, "account_id": {}, "role": {}}
    for copy in copies:
        counts[copy["kind"]][copy["value"]] = copy["count"]
    source = source_state()
    if row is None:
        return {"imported": False, "copies": counts, "stars": stars, "projects": projects, "config": source}
    payload = json.loads(row["payload"])
    payload["categories"] = _apply_categories(payload.get("categories", {}), custom, overrides)
    return {
        "imported": True,
        "imported_at": row["imported_at"],
        **payload,
        "custom_categories": custom,
        "category_overrides": overrides,
        "copies": counts,
        "stars": stars,
        "projects": projects,
        "config": source,
    }


def _apply_categories(categories, custom, overrides):
    """Accounts moved by hand go to their chosen category; categories made by hand
    show even while empty. Each category keeps its accounts' order."""
    result = {name: [account for account in accounts if account["profile"] not in overrides] for name, accounts in categories.items()}
    for name in custom:
        result.setdefault(name, [])
    moved = [account for accounts in categories.values() for account in accounts if account["profile"] in overrides]
    for account in moved:
        result.setdefault(overrides[account["profile"]], []).append(account)
    # Automatic categories left empty disappear; hand-made ones stay.
    keep = {name.lower() for name in custom}
    return {name: accounts for name, accounts in result.items() if accounts or name.lower() in keep}


class ConfigPath(BaseModel):
    path: str = Field(default="", max_length=1000)


@router.put("/aws-accounts/config")
def set_config(value: ConfigPath):
    """Where the AWS config file is; empty means the default (~/.aws/config)."""
    path = os.path.expandvars(os.path.expanduser(value.path.strip().strip('"')))
    if path and not os.path.isfile(path):
        raise HTTPException(400, f"No file at {path}.")
    with connection() as db:
        db.execute(
            "INSERT INTO aws_config_source(id,path,mtime,error) VALUES(1,?,NULL,'') "
            "ON CONFLICT(id) DO UPDATE SET path=excluded.path,mtime=NULL,error=''",
            (path,),
        )
    sync_config()
    state = source_state()
    if state["error"]:
        raise HTTPException(400, state["error"])
    return state


def _projects(db):
    projects = {
        row["id"]: {"id": row["id"], "name": row["name"], "accounts": []}
        for row in db.execute("SELECT id,name FROM aws_projects ORDER BY created_at,id")
    }
    for row in db.execute(
        "SELECT project_id,profile FROM aws_project_accounts ORDER BY added_at,rowid"
    ):
        projects[row["project_id"]]["accounts"].append(row["profile"])
    return list(projects.values())


def _project(db, project_id):
    if not db.execute("SELECT 1 FROM aws_projects WHERE id=?", (project_id,)).fetchone():
        raise HTTPException(404, "Project not found.")


@router.get("/aws-accounts/export")
def export_inventory():
    with connection() as db:
        row = db.execute("SELECT payload FROM aws_accounts WHERE id=1").fetchone()
    if row is None:
        raise HTTPException(404, "No AWS accounts have been imported.")
    payload = json.loads(row["payload"])
    payload["total_accounts"] = sum(len(accounts) for accounts in payload["categories"].values())
    return JSONResponse(
        payload,
        headers={"Content-Disposition": 'attachment; filename="aws_accounts_by_category.json"'},
    )


class Copy(BaseModel):
    kind: Literal["profile", "account_id", "role"]
    value: str = Field(min_length=1, max_length=500)


@router.post("/aws-accounts/copies")
def record_copy(value: Copy):
    with connection() as db:
        db.execute(
            "INSERT INTO aws_account_copies(kind,value,count,last_copied) VALUES(?,?,1,?) "
            "ON CONFLICT(kind,value) DO UPDATE SET count=count+1,last_copied=excluded.last_copied",
            (value.kind, value.value, datetime.now(timezone.utc).isoformat()),
        )
        count = db.execute(
            "SELECT count FROM aws_account_copies WHERE kind=? AND value=?",
            (value.kind, value.value),
        ).fetchone()[0]
    return {"count": count}


class Star(BaseModel):
    profile: str = Field(min_length=1, max_length=500)
    starred: bool


@router.put("/aws-accounts/stars")
def star_account(value: Star):
    with connection() as db:
        if value.starred:
            db.execute(
                "INSERT OR IGNORE INTO aws_account_stars(profile,starred_at) VALUES(?,?)",
                (value.profile, datetime.now(timezone.utc).isoformat()),
            )
        else:
            db.execute("DELETE FROM aws_account_stars WHERE profile=?", (value.profile,))
    return {"ok": True, "starred": value.starred}


class ProjectName(BaseModel):
    name: str = Field(max_length=100)

    @field_validator("name")
    @classmethod
    def clean(cls, value):
        value = " ".join(value.split())
        if not value:
            raise ValueError("Enter a project name.")
        return value


class ProjectAccount(BaseModel):
    profile: str = Field(min_length=1, max_length=500)


def _unique(db, name, project_id=None):
    row = db.execute("SELECT id FROM aws_projects WHERE name=?", (name,)).fetchone()
    if row and row["id"] != project_id:
        raise HTTPException(409, f'A project named "{name}" already exists.')


@router.post("/aws-accounts/projects")
def create_project(value: ProjectName):
    with connection() as db:
        _unique(db, value.name)
        project_id = db.execute(
            "INSERT INTO aws_projects(name,created_at) VALUES(?,?)",
            (value.name, datetime.now(timezone.utc).isoformat()),
        ).lastrowid
    return {"id": project_id, "name": value.name, "accounts": []}


@router.patch("/aws-accounts/projects/{project_id}")
def rename_project(project_id: int, value: ProjectName):
    with connection() as db:
        _project(db, project_id)
        _unique(db, value.name, project_id)
        db.execute("UPDATE aws_projects SET name=? WHERE id=?", (value.name, project_id))
    return {"ok": True, "name": value.name}


@router.delete("/aws-accounts/projects/{project_id}")
def delete_project(project_id: int):
    with connection() as db:
        _project(db, project_id)
        db.execute("DELETE FROM aws_project_accounts WHERE project_id=?", (project_id,))
        db.execute("DELETE FROM aws_projects WHERE id=?", (project_id,))
    return {"ok": True}


@router.put("/aws-accounts/projects/{project_id}/accounts")
def add_project_account(project_id: int, value: ProjectAccount):
    with connection() as db:
        _project(db, project_id)
        added = db.execute(
            "INSERT OR IGNORE INTO aws_project_accounts(project_id,profile,added_at) VALUES(?,?,?)",
            (project_id, value.profile, datetime.now(timezone.utc).isoformat()),
        ).rowcount
    return {"ok": True, "added": bool(added)}


@router.delete("/aws-accounts/projects/{project_id}/accounts")
def remove_project_account(project_id: int, profile: str = Query(min_length=1, max_length=500)):
    with connection() as db:
        _project(db, project_id)
        db.execute(
            "DELETE FROM aws_project_accounts WHERE project_id=? AND profile=?",
            (project_id, profile),
        )
    return {"ok": True}


class Profile(BaseModel):
    profile: str = Field(max_length=128)


class CategoryName(BaseModel):
    name: str = Field(max_length=100)

    @field_validator("name")
    @classmethod
    def clean(cls, value):
        value = " ".join(value.split())
        if not value:
            raise ValueError("Enter a category name.")
        return value


class CategoryMove(BaseModel):
    profiles: list[str] = Field(min_length=1, max_length=5000)
    # None puts the accounts back in the category OWL chose from their names.
    category: str | None = Field(default=None, max_length=100)


@router.post("/aws-accounts/categories")
def create_category(value: CategoryName):
    with connection() as db:
        if db.execute("SELECT 1 FROM aws_custom_categories WHERE name=?", (value.name,)).fetchone():
            raise HTTPException(409, f'A category named "{value.name}" already exists.')
        db.execute(
            "INSERT INTO aws_custom_categories(name,created_at) VALUES(?,?)",
            (value.name, datetime.now(timezone.utc).isoformat()),
        )
    return {"name": value.name}


@router.patch("/aws-accounts/categories/{name}")
def rename_category(name: str, value: CategoryName):
    """Rename a category: a hand-made one, or an automatic one (which then becomes
    hand-made and keeps its accounts)."""
    with connection() as db:
        if value.name.lower() != name.lower() and db.execute("SELECT 1 FROM aws_custom_categories WHERE name=?", (value.name,)).fetchone():
            raise HTTPException(409, f'A category named "{value.name}" already exists.')
        payload = db.execute("SELECT payload FROM aws_accounts WHERE id=1").fetchone()
        automatic = json.loads(payload[0]).get("categories", {}).get(name, []) if payload else []
        custom = db.execute("SELECT 1 FROM aws_custom_categories WHERE name=?", (name,)).fetchone()
        if not custom and not automatic:
            raise HTTPException(404, "Category not found.")
        db.execute("DELETE FROM aws_custom_categories WHERE name=?", (name,))
        db.execute(
            "INSERT OR IGNORE INTO aws_custom_categories(name,created_at) VALUES(?,?)",
            (value.name, datetime.now(timezone.utc).isoformat()),
        )
        db.execute("UPDATE aws_category_overrides SET category=? WHERE category=? COLLATE NOCASE", (value.name, name))
        overridden = {row[0] for row in db.execute("SELECT profile FROM aws_category_overrides")}
        db.executemany(
            "INSERT OR IGNORE INTO aws_category_overrides(profile,category) VALUES(?,?)",
            [(account["profile"], value.name) for account in automatic if account["profile"] not in overridden],
        )
    return {"name": value.name}


@router.delete("/aws-accounts/categories/{name}")
def delete_category(name: str):
    """Delete a hand-made category; its accounts go back to their automatic categories."""
    with connection() as db:
        removed = db.execute("DELETE FROM aws_custom_categories WHERE name=?", (name,)).rowcount
        moved = db.execute("DELETE FROM aws_category_overrides WHERE category=? COLLATE NOCASE", (name,)).rowcount
    if not removed and not moved:
        raise HTTPException(404, "Category not found.")
    return {"ok": True, "returned": moved}


@router.put("/aws-accounts/category-accounts")
def move_accounts(value: CategoryMove):
    with connection() as db:
        if value.category is None:
            db.executemany("DELETE FROM aws_category_overrides WHERE profile=?", [(profile,) for profile in value.profiles])
        else:
            category = " ".join(value.category.split())
            if not category:
                raise HTTPException(400, "Choose a category.")
            db.executemany(
                "INSERT INTO aws_category_overrides(profile,category) VALUES(?,?) ON CONFLICT(profile) DO UPDATE SET category=excluded.category",
                [(profile, category) for profile in value.profiles],
            )
    return {"ok": True, "moved": len(value.profiles)}


@router.get("/aws-accounts/connection")
def connection_state(refresh: bool = False):
    return aws.check() if refresh else aws.state()


@router.put("/aws-accounts/connection")
def connection_profile(value: Profile):
    aws.set_profile(value.profile)
    return aws.check()


@router.post("/aws-accounts/connection/login")
def connection_login():
    try:
        return aws.login()
    except FileNotFoundError as error:
        raise HTTPException(400, str(error)) from None


class DeleteAll(BaseModel):
    confirmation: str = ""


@router.delete("/aws-accounts")
def delete_all(value: DeleteAll):
    """Delete every AWS Accounts record: copy counts, stars, projects and session. The
    accounts themselves are read again from the AWS config file."""
    if value.confirmation != "delete all":
        raise HTTPException(400, 'Type "delete all" to confirm.')
    aws.reset()
    with connection() as db:
        accounts = db.execute("DELETE FROM aws_accounts").rowcount
        db.execute("UPDATE aws_config_source SET mtime=NULL")
        copies = db.execute("DELETE FROM aws_account_copies").rowcount
        stars = db.execute("DELETE FROM aws_account_stars").rowcount
        db.execute("DELETE FROM aws_project_accounts")
        db.execute("DELETE FROM aws_category_overrides")
        db.execute("DELETE FROM aws_custom_categories")
        projects = db.execute("DELETE FROM aws_projects").rowcount
    return {
        "ok": True, "inventory": accounts, "copies": copies, "stars": stars, "projects": projects,
    }
