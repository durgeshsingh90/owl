"""Encrypted, exact-origin Bitbucket Data Center HTTP access tokens."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import transaction

from bitbucket.models import HTTPSCredential, Repository
from bitbucket.services.repository_urls import RepositoryURL, parse_repository_url

MAX_TOKEN_CHARACTERS = 16_384


class CredentialError(RuntimeError):
    """A safe credential error which never contains token material."""


@dataclass(frozen=True, slots=True)
class ActiveCredential:
    origin: str
    username: str
    token: str = field(repr=False)


def _fernet() -> Fernet:
    digest = hashlib.sha256(f"owl.bitbucket-api-token.v1:{settings.SECRET_KEY}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _validated_token(value: object) -> str:
    raw = str(value or "")
    token = raw.strip()
    if (
        not token
        or token != raw
        or len(token) > MAX_TOKEN_CHARACTERS
        or any(ord(character) < 32 or ord(character) == 127 for character in token)
    ):
        raise CredentialError("Enter an HTTP access token without surrounding whitespace.")
    return token


def has_credential(origin: str) -> bool:
    return HTTPSCredential.objects.filter(origin=origin).exclude(token_ciphertext="").exists()


def save_credential(
    parsed: RepositoryURL,
    token_value: object,
    *,
    username: object = "",
) -> None:
    token = _validated_token(token_value)
    normalized_username = str(username or "").strip()
    if len(normalized_username) > 255 or any(
        ord(character) < 32 or ord(character) == 127 for character in normalized_username
    ):
        raise CredentialError("Enter a valid Bitbucket username.")
    ciphertext = _fernet().encrypt(token.encode()).decode("ascii")
    with transaction.atomic():
        HTTPSCredential.objects.update_or_create(
            origin=parsed.origin,
            defaults={
                "username": normalized_username,
                "token_ciphertext": ciphertext,
            },
        )


def resolve_credential(repository: Repository) -> ActiveCredential:
    parsed = parse_repository_url(repository.url)
    record = HTTPSCredential.objects.filter(origin=parsed.origin).first()
    if record is None or not record.token_ciphertext:
        raise CredentialError("Configure an HTTP access token for this Bitbucket server.")
    try:
        token = _fernet().decrypt(record.token_ciphertext.encode("ascii")).decode()
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise CredentialError(
            "The saved HTTP access token cannot be read. Replace it in Bitbucket settings."
        ) from exc
    return ActiveCredential(origin=parsed.origin, username=record.username, token=token)


def credential_summaries() -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "origin": record.origin,
            "configured": bool(record.token_ciphertext),
            "username": record.username,
            "updatedAt": record.updated_at.isoformat(),
        }
        for record in HTTPSCredential.objects.all()
    )
