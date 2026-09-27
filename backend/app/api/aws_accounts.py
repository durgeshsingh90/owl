"""Imported AWS account inventory, grouped by category."""

import json
from datetime import datetime, timezone
from typing import Literal

from app.aws import connection as aws
from app.core.database import connection
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

router = APIRouter(prefix="/api")


class Account(BaseModel):
    model_config = ConfigDict(extra="allow")

    profile: str = Field(min_length=1, max_length=500)
    account_id: str = Field(default="", max_length=64)

    @field_validator("account_id", mode="before")
    @classmethod
    def text_id(cls, value):
        # Account IDs keep leading zeros only as strings; accept numbers too.
        return "" if value is None else str(value)


class Inventory(BaseModel):
    model_config = ConfigDict(extra="allow")

    generated_at: str | None = Field(default=None, max_length=64)
    total_accounts: int | None = None
    common_roles: dict[str, str] = Field(default_factory=dict)
    categories: dict[str, list[Account]] = Field(max_length=1000)

    @field_validator("categories")
    @classmethod
    def limits(cls, value):
        if not value:
            raise ValueError("The file has no categories.")
        if sum(len(accounts) for accounts in value.values()) > 50000:
            raise ValueError("The file has more than 50,000 accounts.")
        return value


@router.get("/aws-accounts")
def inventory():
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
    counts = {"profile": {}, "account_id": {}, "role": {}}
    for copy in copies:
        counts[copy["kind"]][copy["value"]] = copy["count"]
    if row is None:
        return {"imported": False, "copies": counts, "stars": stars, "projects": projects}
    return {
        "imported": True,
        "imported_at": row["imported_at"],
        **json.loads(row["payload"]),
        "copies": counts,
        "stars": stars,
        "projects": projects,
    }


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


@router.put("/aws-accounts")
def import_inventory(value: Inventory):
    imported_at = datetime.now(timezone.utc).isoformat()
    with connection() as db:
        db.execute(
            "INSERT INTO aws_accounts(id,payload,imported_at) VALUES(1,?,?) "
            "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,imported_at=excluded.imported_at",
            (json.dumps(value.model_dump()), imported_at),
        )
    return {
        "ok": True,
        "imported_at": imported_at,
        "accounts": sum(len(accounts) for accounts in value.categories.values()),
    }


class DeleteAll(BaseModel):
    confirmation: str = ""


@router.delete("/aws-accounts")
def delete_all(value: DeleteAll):
    """Delete every AWS Accounts record: inventory, copy counts, stars, projects and session."""
    if value.confirmation != "delete all":
        raise HTTPException(400, 'Type "delete all" to confirm.')
    aws.reset()
    with connection() as db:
        accounts = db.execute("DELETE FROM aws_accounts").rowcount
        copies = db.execute("DELETE FROM aws_account_copies").rowcount
        stars = db.execute("DELETE FROM aws_account_stars").rowcount
        db.execute("DELETE FROM aws_project_accounts")
        projects = db.execute("DELETE FROM aws_projects").rowcount
    return {
        "ok": True, "inventory": accounts, "copies": copies, "stars": stars, "projects": projects,
    }
