"""Encrypted, exact-origin Bitbucket Data Center HTTP access tokens."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import transaction

from bitbucket.models import HTTPSCredential, Repository
from bitbucket.services.repository_urls import (
    RepositoryURL,
    ServerURL,
    ServerURLValidationError,
    parse_api_base_url,
    parse_repository_url,
)

MAX_TOKEN_CHARACTERS = 16_384


class CredentialError(RuntimeError):
    """A safe credential error which never contains token material."""


@dataclass(frozen=True, slots=True)
class ActiveCredential:
    origin: str
    username: str
    api_base_url: str
    verify_ssl: bool
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
    parsed: RepositoryURL | ServerURL,
    token_value: object,
    *,
    username: object = "",
    api_base_url: str = "",
    verify_ssl: bool | None = None,
) -> None:
    token = _validated_token(token_value)
    normalized_username = str(username or "").strip()
    if len(normalized_username) > 255 or any(
        ord(character) < 32 or ord(character) == 127 for character in normalized_username
    ):
        raise CredentialError("Enter a valid Bitbucket username.")
    ciphertext = _fernet().encrypt(token.encode()).decode("ascii")
    resolved_api_base_url = api_base_url or getattr(parsed, "api_base_url", "")
    defaults: dict[str, object] = {
        "username": normalized_username,
        "token_ciphertext": ciphertext,
    }
    if resolved_api_base_url:
        defaults["api_base_url"] = resolved_api_base_url
    if verify_ssl is not None:
        defaults["verify_ssl"] = verify_ssl
    with transaction.atomic():
        HTTPSCredential.objects.update_or_create(
            origin=parsed.origin,
            defaults=defaults,
        )


def update_server_credential(
    parsed: ServerURL,
    *,
    username: object,
    token_value: object,
    verify_ssl: bool,
) -> None:
    token = str(token_value or "")
    existing = HTTPSCredential.objects.filter(origin=parsed.origin).first()
    if not token:
        if existing is None or not existing.token_ciphertext:
            raise CredentialError("Enter an HTTP access token for this Bitbucket server.")
        normalized_username = str(username or "").strip()
        if len(normalized_username) > 255 or any(
            ord(character) < 32 or ord(character) == 127 for character in normalized_username
        ):
            raise CredentialError("Enter a valid Bitbucket username.")
        HTTPSCredential.objects.filter(pk=existing.pk).update(
            api_base_url=parsed.api_base_url,
            username=normalized_username,
            verify_ssl=verify_ssl,
        )
        return
    save_credential(
        parsed,
        token,
        username=username,
        api_base_url=parsed.api_base_url,
        verify_ssl=verify_ssl,
    )


def credential_for_connection(
    parsed: ServerURL,
    *,
    username: object,
    token_value: object,
    verify_ssl: bool,
) -> ActiveCredential:
    raw_token = str(token_value or "")
    if raw_token:
        token = _validated_token(raw_token)
        normalized_username = str(username or "").strip()
        if len(normalized_username) > 255 or any(
            ord(character) < 32 or ord(character) == 127 for character in normalized_username
        ):
            raise CredentialError("Enter a valid Bitbucket username.")
        return ActiveCredential(
            origin=parsed.origin,
            username=normalized_username,
            api_base_url=parsed.api_base_url,
            verify_ssl=verify_ssl,
            token=token,
        )
    existing = resolve_origin_credential(parsed.origin)
    return ActiveCredential(
        origin=parsed.origin,
        username=str(username or "").strip() or existing.username,
        api_base_url=parsed.api_base_url,
        verify_ssl=verify_ssl,
        token=existing.token,
    )


def resolve_origin_credential(origin: str) -> ActiveCredential:
    record = HTTPSCredential.objects.filter(origin=origin).first()
    if record is None or not record.token_ciphertext:
        raise CredentialError("Configure an HTTP access token for this Bitbucket server.")
    try:
        token = _fernet().decrypt(record.token_ciphertext.encode("ascii")).decode()
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise CredentialError(
            "The saved HTTP access token cannot be read. Replace it in Bitbucket settings."
        ) from exc
    return ActiveCredential(
        origin=record.origin,
        username=record.username,
        api_base_url=record.api_base_url,
        verify_ssl=record.verify_ssl,
        token=token,
    )


def resolve_credential(repository: Repository) -> ActiveCredential:
    parsed = parse_repository_url(repository.url)
    return resolve_origin_credential(parsed.origin)


def credential_summaries() -> tuple[dict[str, object], ...]:
    summaries: list[dict[str, object]] = []
    for record in HTTPSCredential.objects.all():
        try:
            visible_base_url = parse_api_base_url(record.api_base_url).web_base_url
        except ServerURLValidationError:
            visible_base_url = record.origin
        summaries.append(
            {
                "origin": record.origin,
                "configured": bool(record.token_ciphertext),
                "baseUrl": visible_base_url,
                "apiBaseUrl": record.api_base_url,
                "username": record.username,
                "verifySsl": record.verify_ssl,
                "updatedAt": record.updated_at.isoformat(),
            }
        )
    return tuple(summaries)
