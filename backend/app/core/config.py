"""Configurable Bitbucket origin and encrypted local credential storage."""

import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

from cryptography.fernet import Fernet
from pydantic import BaseModel, Field, SecretStr, field_validator


class Settings(BaseModel):
    base_url: str
    username: str = Field(min_length=1, max_length=300)
    token: SecretStr
    verify_ssl: bool = False
    max_workers: int = Field(default=4, ge=1, le=10)

    @field_validator("verify_ssl", mode="before")
    @classmethod
    def disable_ssl_verification(cls, value):
        return False

    @field_validator("base_url")
    @classmethod
    def normalize(cls, value):
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Use an HTTP(S) Bitbucket server URL without credentials, query or fragment."
            )
        path = re.sub(r"/rest/api/(?:1\.0|latest)/?$", "", parsed.path.rstrip("/"))
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    @field_validator("token")
    @classmethod
    def nonempty(cls, value):
        if not value.get_secret_value():
            raise ValueError("Token is required.")
        return value


def config_dir():
    return Path(
        os.environ.get("OWL_CONFIG_DIR", Path(__file__).resolve().parents[2] / "data")
    )


def atomic_private(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_settings(settings):
    directory = config_dir()
    key_path = directory / "secret.key"
    if not key_path.exists():
        atomic_private(key_path, Fernet.generate_key())
    data = settings.model_dump(mode="json")
    data["token"] = settings.token.get_secret_value()
    atomic_private(
        directory / "settings.enc",
        Fernet(key_path.read_bytes()).encrypt(json.dumps(data).encode()),
    )


def load_settings():
    directory = config_dir()
    path = directory / "settings.enc"
    if not path.exists():
        raise ValueError("Save Bitbucket connection settings first.")
    return Settings.model_validate_json(
        Fernet((directory / "secret.key").read_bytes()).decrypt(path.read_bytes())
    )


def parse_project(url, settings):
    parsed = urlsplit(url.strip())
    base = urlsplit(settings.base_url)
    prefix = base.path.rstrip("/") + "/projects/"
    if (
        (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc)
        or not parsed.path.startswith(prefix)
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Project URL must belong to the configured Bitbucket server.")
    key = unquote(parsed.path[len(prefix) :].split("/")[0])
    if not re.fullmatch(r"[A-Za-z0-9_~-]+", key):
        raise ValueError("Invalid project key.")
    return key, settings.base_url + "/projects/" + key
