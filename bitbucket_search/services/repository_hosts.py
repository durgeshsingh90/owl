"""Durable, non-secret trust policy for repository hosts.

This module deliberately keeps host approval separate from repository registration
and HTTPS credential storage.  It performs no DNS, HTTP, or Git work.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from django.apps import apps
from django.conf import settings
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.models import F

from bitbucket_search.models import (
    BitbucketHTTPSCredential,
    BitbucketHTTPSCredentialState,
    BitbucketRepository,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    TrustedRepositoryHostSource,
)

MAX_REPOSITORY_HOST_ORIGIN_LENGTH = 2_048
BUILT_IN_REPOSITORY_HOSTS = ("bitbucket.org", "github.com")
ACTIVE_SYNC_JOB_STATUSES = (
    RepositorySyncJobStatus.QUEUED,
    RepositorySyncJobStatus.RUNNING,
)


class RepositoryHostError(RuntimeError):
    """Base class for errors safe to expose on the local Settings surface."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RepositoryHostValidationError(RepositoryHostError):
    """A host origin is outside OWL's strict trust boundary."""


class RepositoryHostPolicyManagedExternally(RepositoryHostError):
    """The environment owns the effective host policy."""


class RepositoryHostNotAllowed(RepositoryHostError):
    """A hostname or HTTPS origin is not in the effective policy."""


class RepositoryHostNotFound(RepositoryHostError):
    """A requested UI-managed host does not exist."""


class RepositoryHostReadOnly(RepositoryHostError):
    """A built-in or externally managed host cannot be mutated in Settings."""


class RepositoryHostConflict(RepositoryHostError):
    """A UI-managed host still has durable dependants."""

    def __init__(self, dependencies: RepositoryHostDependencies) -> None:
        super().__init__(
            "repository_host_in_use",
            "This repository host is still in use. Remove its repositories, active jobs, "
            "and HTTPS credential before removing the host.",
        )
        self.dependencies = dependencies


@dataclass(frozen=True, slots=True)
class NormalizedRepositoryHost:
    canonical_origin: str
    hostname: str
    port: int


@dataclass(frozen=True, slots=True)
class EffectiveRepositoryHost:
    canonical_origin: str
    hostname: str
    port: int
    source: str


@dataclass(frozen=True, slots=True)
class EffectiveRepositoryHostPolicy:
    """One immutable policy snapshot shared by validators and workers.

    Built-in/environment entries are legacy hostname grants and therefore accept
    an exact matching hostname at any valid port. UI entries are exact HTTPS
    origins; their hostname also grants the corresponding SSH host.
    """

    source: str
    externally_managed: bool
    entries: tuple[EffectiveRepositoryHost, ...]
    database_available: bool = True

    @property
    def hostnames(self) -> frozenset[str]:
        return frozenset(entry.hostname for entry in self.entries)

    @property
    def https_origins(self) -> frozenset[str]:
        return frozenset(entry.canonical_origin for entry in self.entries)

    @property
    def broad_https_hostnames(self) -> frozenset[str]:
        return frozenset(
            entry.hostname for entry in self.entries if entry.source in {"built_in", "environment"}
        )

    def allows_hostname(self, value: object) -> bool:
        try:
            hostname = normalize_repository_hostname(value)
        except RepositoryHostValidationError:
            return False
        return hostname in self.hostnames

    def allows_https_origin(self, value: object) -> bool:
        try:
            origin = normalize_repository_host_origin(value)
        except RepositoryHostValidationError:
            return False
        return bool(
            origin.canonical_origin in self.https_origins
            or origin.hostname in self.broad_https_hostnames
        )


@dataclass(frozen=True, slots=True)
class RepositoryHostDependencies:
    repository_count: int = 0
    active_job_count: int = 0
    credential_count: int = 0
    last_repository_access_verified_at: datetime | None = None

    @property
    def has_dependencies(self) -> bool:
        return bool(self.repository_count or self.active_job_count or self.credential_count)


@dataclass(frozen=True, slots=True)
class RepositoryHostSummary:
    canonical_origin: str
    hostname: str
    port: int
    source: str
    enabled: bool
    available: bool
    state: str
    credential_state: str
    dependencies: RepositoryHostDependencies


@dataclass(frozen=True, slots=True)
class RepositoryHostMutationResult:
    canonical_origin: str
    hostname: str
    port: int
    created: bool = False
    changed: bool = False
    removed: bool = False


def _safe_validation_error() -> RepositoryHostValidationError:
    # Deliberately generic: a rejected value may contain pasted credentials.
    return RepositoryHostValidationError(
        "invalid_repository_host_origin",
        "Enter a credential-free HTTPS repository host URL without a path, query, or fragment.",
    )


def _contains_unsafe_character(value: str) -> bool:
    return any(
        character.isspace()
        or ord(character) == 127
        or unicodedata.category(character) in {"Cc", "Cf", "Cs", "Co", "Cn"}
        for character in value
    )


def normalize_repository_hostname(value: object) -> str:
    """Return one canonical exact hostname without accepting URL syntax."""

    try:
        raw = unicodedata.normalize("NFKC", str(value or ""))
    except (TypeError, ValueError):
        raise _safe_validation_error() from None
    if (
        not raw
        or raw != raw.strip()
        or len(raw) > 253
        or _contains_unsafe_character(raw)
        or "*" in raw
        or any(character in raw for character in "/\\@?#%")
    ):
        raise _safe_validation_error()

    if raw.endswith(".."):
        raise _safe_validation_error()
    candidate = raw[:-1] if raw.endswith(".") else raw
    if not candidate:
        raise _safe_validation_error()

    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        address = None
    if address is not None:
        return address.compressed.casefold()
    if candidate.isascii() and ":" not in candidate:
        try:
            socket.inet_aton(candidate)
        except OSError:
            pass
        else:
            # Reject legacy short, octal, hexadecimal, and integer IPv4 forms;
            # different resolvers can map them to surprising destinations.
            raise _safe_validation_error()

    if any(
        not (character in {".", "-"} or unicodedata.category(character)[0] in {"L", "M", "N"})
        for character in candidate
    ):
        raise _safe_validation_error()
    try:
        ascii_hostname = candidate.encode("idna").decode("ascii").casefold()
        # Reject transitional/ambiguous IDN mappings rather than silently making
        # a different Unicode label authoritative (for example, mapping one
        # spelling onto an unrelated ASCII spelling).
        unicode_round_trip = ascii_hostname.encode("ascii").decode("idna")
    except UnicodeError:
        raise _safe_validation_error() from None
    if not candidate.isascii() and (
        unicodedata.normalize("NFC", unicode_round_trip).lower()
        != unicodedata.normalize("NFC", candidate).lower()
    ):
        raise _safe_validation_error()
    try:
        if unicode_round_trip.encode("idna").decode("ascii").casefold() != ascii_hostname:
            raise _safe_validation_error()
    except UnicodeError:
        raise _safe_validation_error() from None

    labels = ascii_hostname.split(".")
    if len(ascii_hostname) > 253 or any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not all(character.isalnum() or character == "-" for character in label)
        for label in labels
    ):
        raise _safe_validation_error()
    return ascii_hostname


def _origin_netloc(hostname: str, port: int) -> str:
    rendered_hostname = f"[{hostname}]" if ":" in hostname else hostname
    return f"{rendered_hostname}:{port}"


def normalize_repository_host_origin(value: object) -> NormalizedRepositoryHost:
    """Normalize a strict credential-free HTTPS origin with an effective port."""

    try:
        raw = unicodedata.normalize("NFKC", str(value or ""))
    except (TypeError, ValueError):
        raise _safe_validation_error() from None
    if (
        not raw
        or raw != raw.strip()
        or len(raw) > MAX_REPOSITORY_HOST_ORIGIN_LENGTH
        or _contains_unsafe_character(raw)
    ):
        raise _safe_validation_error()
    try:
        parsed = urlsplit(raw)
        parsed_port = parsed.port
        port = 443 if parsed_port is None else parsed_port
    except (TypeError, ValueError):
        raise _safe_validation_error() from None
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not 1 <= port <= 65_535
    ):
        raise _safe_validation_error()
    # urlsplit treats an empty explicit port like no port; it is not a canonical
    # origin and must not silently become 443.
    authority = parsed.netloc.rsplit("@", 1)[-1]
    if authority.endswith(":"):
        raise _safe_validation_error()

    hostname = normalize_repository_hostname(parsed.hostname)
    return NormalizedRepositoryHost(
        canonical_origin=f"https://{_origin_netloc(hostname, port)}",
        hostname=hostname,
        port=port,
    )


def https_origin_from_repository_url(value: object) -> NormalizedRepositoryHost:
    """Extract the exact HTTPS origin from a credential-free repository URL."""

    try:
        raw = unicodedata.normalize("NFKC", str(value or ""))
        if (
            not raw
            or raw != raw.strip()
            or len(raw) > MAX_REPOSITORY_HOST_ORIGIN_LENGTH
            or _contains_unsafe_character(raw)
        ):
            raise _safe_validation_error()
        parsed = urlsplit(raw)
        parsed_port = parsed.port
        port = 443 if parsed_port is None else parsed_port
    except (TypeError, ValueError):
        raise _safe_validation_error() from None
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not 1 <= port <= 65_535
    ):
        raise _safe_validation_error()
    hostname = normalize_repository_hostname(parsed.hostname)
    return NormalizedRepositoryHost(
        canonical_origin=f"https://{_origin_netloc(hostname, port)}",
        hostname=hostname,
        port=port,
    )


def _trusted_host_model():
    return apps.get_model("bitbucket_search", "TrustedRepositoryHost")


def _normalized_policy_hostnames(values: Iterable[object]) -> tuple[str, ...]:
    normalized: set[str] = set()
    for value in values:
        try:
            normalized.add(normalize_repository_hostname(value))
        except RepositoryHostValidationError:
            # Invalid configuration never expands trust. Startup validation can
            # report the operator error independently.
            continue
    return tuple(sorted(normalized))


def _built_in_hostnames() -> tuple[str, ...]:
    values = getattr(settings, "BITBUCKET_BUILT_IN_HOSTS", BUILT_IN_REPOSITORY_HOSTS)
    return _normalized_policy_hostnames(values)


def repository_host_policy_is_external() -> bool:
    """Preserve explicit-empty provenance as well as normal environment use."""

    for setting_name in (
        "BITBUCKET_ALLOWED_HOSTS_EXPLICIT",
        "BITBUCKET_ALLOWED_HOSTS_EXTERNALLY_MANAGED",
        "BITBUCKET_ALLOWED_HOSTS_ENV_CONFIGURED",
    ):
        if hasattr(settings, setting_name):
            return bool(getattr(settings, setting_name))
    source = str(getattr(settings, "BITBUCKET_ALLOWED_HOSTS_SOURCE", "")).strip().casefold()
    if source:
        return source in {"environment", "external", "explicit", "explicit_blank"}
    if "BITBUCKET_ALLOWED_HOSTS" in os.environ:
        return True

    # This also makes Django override_settings useful in isolated service tests:
    # changing the legacy tuple away from the built-ins represents an explicit
    # policy even when no process environment was involved.
    configured = _normalized_policy_hostnames(
        getattr(settings, "BITBUCKET_ALLOWED_HOSTS", BUILT_IN_REPOSITORY_HOSTS)
    )
    return configured != _built_in_hostnames()


def effective_repository_host_policy() -> EffectiveRepositoryHostPolicy:
    """Resolve one fail-closed effective host policy without caching DB state."""

    if repository_host_policy_is_external():
        hostnames = _normalized_policy_hostnames(getattr(settings, "BITBUCKET_ALLOWED_HOSTS", ()))
        return EffectiveRepositoryHostPolicy(
            source="environment",
            externally_managed=True,
            entries=tuple(
                EffectiveRepositoryHost(
                    canonical_origin=f"https://{_origin_netloc(hostname, 443)}",
                    hostname=hostname,
                    port=443,
                    source="environment",
                )
                for hostname in hostnames
            ),
        )

    built_in_entries = tuple(
        EffectiveRepositoryHost(
            canonical_origin=f"https://{_origin_netloc(hostname, 443)}",
            hostname=hostname,
            port=443,
            source="built_in",
        )
        for hostname in _built_in_hostnames()
    )
    try:
        model = _trusted_host_model()
        rows = tuple(
            model.objects.filter(enabled=True)
            .order_by("canonical_origin", "pk")
            .values("canonical_origin", "hostname", "port", "source")
        )
    except (LookupError, DatabaseError):
        return EffectiveRepositoryHostPolicy(
            source="built_in_and_ui",
            externally_managed=False,
            entries=built_in_entries,
            database_available=False,
        )

    ui_entries: dict[str, EffectiveRepositoryHost] = {}
    for row in rows:
        try:
            normalized = normalize_repository_host_origin(row["canonical_origin"])
        except RepositoryHostValidationError:
            continue
        # Fail closed if a corrupt row's redundant columns disagree.
        if normalized.hostname != row["hostname"] or normalized.port != row["port"]:
            continue
        ui_entries[normalized.canonical_origin] = EffectiveRepositoryHost(
            canonical_origin=normalized.canonical_origin,
            hostname=normalized.hostname,
            port=normalized.port,
            source=(
                row["source"]
                if row["source"] in TrustedRepositoryHostSource.values
                else TrustedRepositoryHostSource.LEGACY
            ),
        )

    entries = {entry.canonical_origin: entry for entry in built_in_entries}
    entries.update(ui_entries)
    return EffectiveRepositoryHostPolicy(
        source="built_in_and_ui",
        externally_managed=False,
        entries=tuple(entries[key] for key in sorted(entries)),
    )


def is_repository_hostname_allowed(value: object) -> bool:
    return effective_repository_host_policy().allows_hostname(value)


def is_repository_https_origin_allowed(value: object) -> bool:
    return effective_repository_host_policy().allows_https_origin(value)


def require_repository_hostname_allowed(value: object) -> str:
    try:
        hostname = normalize_repository_hostname(value)
    except RepositoryHostValidationError:
        raise RepositoryHostNotAllowed(
            "repository_host_not_allowed", "This repository host is not approved in OWL Settings."
        ) from None
    if hostname not in effective_repository_host_policy().hostnames:
        raise RepositoryHostNotAllowed(
            "repository_host_not_allowed", "This repository host is not approved in OWL Settings."
        )
    return hostname


def require_repository_https_origin_allowed(value: object) -> NormalizedRepositoryHost:
    try:
        origin = normalize_repository_host_origin(value)
    except RepositoryHostValidationError:
        raise RepositoryHostNotAllowed(
            "repository_host_not_allowed", "This repository host is not approved in OWL Settings."
        ) from None
    if not effective_repository_host_policy().allows_https_origin(origin.canonical_origin):
        raise RepositoryHostNotAllowed(
            "repository_host_not_allowed", "This repository host is not approved in OWL Settings."
        )
    return origin


def _repository_origin(repository: BitbucketRepository) -> NormalizedRepositoryHost | None:
    raw = str(repository.remote_url or "")
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme.casefold() == "https":
        try:
            return https_origin_from_repository_url(raw)
        except RepositoryHostValidationError:
            return None
    if parsed.scheme.casefold() == "ssh" and parsed.hostname:
        try:
            hostname = normalize_repository_hostname(parsed.hostname)
            parsed_port = parsed.port
        except (RepositoryHostValidationError, ValueError):
            return None
        port = 22 if parsed_port is None else parsed_port
        if not 1 <= port <= 65_535:
            return None
        return NormalizedRepositoryHost("", hostname, port)

    # Retain compatibility with already stored scp-style SSH URLs.
    if ":" in raw and "@" in raw and "://" not in raw:
        authority = raw.split(":", 1)[0]
        user, separator, hostname_value = authority.rpartition("@")
        if separator and user.casefold() == "git":
            try:
                hostname = normalize_repository_hostname(hostname_value)
            except RepositoryHostValidationError:
                return None
            return NormalizedRepositoryHost("", hostname, 22)
    return None


def repository_host_dependencies(
    origin: object,
    *,
    include_all_https_ports: bool = False,
) -> RepositoryHostDependencies:
    normalized = normalize_repository_host_origin(origin)
    repositories: list[BitbucketRepository] = []
    for repository in BitbucketRepository.objects.only(
        "pk", "remote_url", "last_sync_successful_at"
    ).iterator():
        repository_origin = _repository_origin(repository)
        if repository_origin is None:
            continue
        if repository_origin.canonical_origin:
            matches = (
                repository_origin.hostname == normalized.hostname
                if include_all_https_ports
                else repository_origin.canonical_origin == normalized.canonical_origin
            )
        else:
            matches = repository_origin.hostname == normalized.hostname
        if matches:
            repositories.append(repository)

    repository_ids = tuple(repository.pk for repository in repositories)
    active_job_count = (
        RepositorySyncJob.objects.filter(
            repository_id__in=repository_ids,
            status__in=ACTIVE_SYNC_JOB_STATUSES,
        ).count()
        if repository_ids
        else 0
    )
    verified_times = tuple(
        repository.last_sync_successful_at
        for repository in repositories
        if repository.last_sync_successful_at is not None
    )
    credential_rows = _credential_rows_for_origin(
        normalized.canonical_origin,
        include_all_https_ports=include_all_https_ports,
    )
    return RepositoryHostDependencies(
        repository_count=len(repositories),
        active_job_count=active_job_count,
        credential_count=len(credential_rows),
        last_repository_access_verified_at=max(verified_times) if verified_times else None,
    )


def _credential_rows_for_origin(
    origin: str,
    *,
    include_all_https_ports: bool = False,
) -> tuple[tuple[str, str], ...]:
    expected = normalize_repository_host_origin(origin)
    rows: list[tuple[str, str]] = []
    for raw_origin, state in BitbucketHTTPSCredential.objects.values_list("origin", "state"):
        try:
            normalized = https_origin_from_repository_url(raw_origin)
        except RepositoryHostValidationError:
            continue
        if (
            normalized.hostname == expected.hostname
            if include_all_https_ports
            else normalized.canonical_origin == expected.canonical_origin
        ):
            rows.append((raw_origin, state))
    return tuple(rows)


def _credential_state(origin: str, *, include_all_https_ports: bool = False) -> str:
    states = {
        state
        for _raw_origin, state in _credential_rows_for_origin(
            origin,
            include_all_https_ports=include_all_https_ports,
        )
    }
    if BitbucketHTTPSCredentialState.INVALID_CREDENTIAL in states:
        state = BitbucketHTTPSCredentialState.INVALID_CREDENTIAL
    elif BitbucketHTTPSCredentialState.CONNECTED in states:
        state = BitbucketHTTPSCredentialState.CONNECTED
    elif BitbucketHTTPSCredentialState.STORED_UNVERIFIED in states:
        state = BitbucketHTTPSCredentialState.STORED_UNVERIFIED
    else:
        state = ""
    return {
        BitbucketHTTPSCredentialState.STORED_UNVERIFIED: "stored_unverified",
        BitbucketHTTPSCredentialState.CONNECTED: "connected",
        BitbucketHTTPSCredentialState.INVALID_CREDENTIAL: "invalid",
    }.get(state, "not_configured")


def list_repository_host_summaries() -> tuple[RepositoryHostSummary, ...]:
    """Return compact, secret-free summaries, including unavailable stored UI rows."""

    policy = effective_repository_host_policy()
    effective_by_origin = {entry.canonical_origin: entry for entry in policy.entries}
    summaries: dict[str, RepositoryHostSummary] = {}

    for entry in policy.entries:
        hostname_managed = entry.source in {"built_in", "environment"}
        dependencies = repository_host_dependencies(
            entry.canonical_origin,
            include_all_https_ports=hostname_managed,
        )
        summaries[entry.canonical_origin] = RepositoryHostSummary(
            canonical_origin=entry.canonical_origin,
            hostname=entry.hostname,
            port=entry.port,
            source=entry.source,
            enabled=True,
            available=True,
            state=(
                "managed_externally"
                if entry.source == "environment"
                else "in_use"
                if dependencies.repository_count
                else "approved_unverified"
            ),
            credential_state=_credential_state(
                entry.canonical_origin,
                include_all_https_ports=hostname_managed,
            ),
            dependencies=dependencies,
        )

    try:
        model = _trusted_host_model()
        rows = tuple(model.objects.order_by("canonical_origin", "pk"))
    except (LookupError, DatabaseError):
        rows = ()
    for row in rows:
        try:
            normalized = normalize_repository_host_origin(row.canonical_origin)
        except RepositoryHostValidationError:
            continue
        dependencies = repository_host_dependencies(normalized.canonical_origin)
        effective = effective_by_origin.get(normalized.canonical_origin)
        stored_source = (
            row.source
            if row.source in TrustedRepositoryHostSource.values
            else TrustedRepositoryHostSource.LEGACY
        )
        available = bool(
            row.enabled
            and not policy.externally_managed
            and effective is not None
            and effective.source == stored_source
        )
        # Do not replace the authoritative built-in/environment summary with a
        # redundant historical UI row for the same canonical origin.
        if effective is not None and effective.source in {"built_in", "environment"}:
            continue
        summaries[normalized.canonical_origin] = RepositoryHostSummary(
            canonical_origin=normalized.canonical_origin,
            hostname=normalized.hostname,
            port=normalized.port,
            source=stored_source,
            enabled=bool(row.enabled),
            available=available,
            state=(
                "unavailable"
                if not available
                else "in_use"
                if dependencies.repository_count
                else "approved_unverified"
            ),
            credential_state=_credential_state(normalized.canonical_origin),
            dependencies=dependencies,
        )
    return tuple(summaries[key] for key in sorted(summaries))


def add_trusted_repository_host(value: object) -> RepositoryHostMutationResult:
    """Idempotently persist one UI host without contacting it or queuing work."""

    normalized = normalize_repository_host_origin(value)
    policy = effective_repository_host_policy()
    if policy.externally_managed:
        raise RepositoryHostPolicyManagedExternally(
            "repository_host_policy_managed_externally",
            "Repository hosts are managed by the OWL environment and are read-only in Settings.",
        )
    if any(
        entry.canonical_origin == normalized.canonical_origin and entry.source == "built_in"
        for entry in policy.entries
    ):
        return RepositoryHostMutationResult(
            normalized.canonical_origin, normalized.hostname, normalized.port
        )

    model = _trusted_host_model()
    try:
        with transaction.atomic():
            record, created = model.objects.get_or_create(
                canonical_origin=normalized.canonical_origin,
                defaults={
                    "hostname": normalized.hostname,
                    "port": normalized.port,
                    "enabled": True,
                    "source": TrustedRepositoryHostSource.UI,
                },
            )
            changed = False
            if not created and (
                record.hostname != normalized.hostname
                or record.port != normalized.port
                or not record.enabled
            ):
                record.hostname = normalized.hostname
                record.port = normalized.port
                record.enabled = True
                record.save(update_fields=("hostname", "port", "enabled", "updated_at"))
                changed = True
    except IntegrityError:
        # A concurrent identical submission won the unique canonical-origin race.
        record = model.objects.get(canonical_origin=normalized.canonical_origin)
        created = False
        changed = False
    return RepositoryHostMutationResult(
        normalized.canonical_origin,
        normalized.hostname,
        normalized.port,
        created=created,
        changed=changed,
    )


def remove_trusted_repository_host(value: object) -> RepositoryHostMutationResult:
    """Remove one unused UI row without cascading into repositories or credentials."""

    normalized = normalize_repository_host_origin(value)
    policy = effective_repository_host_policy()
    if policy.externally_managed:
        raise RepositoryHostPolicyManagedExternally(
            "repository_host_policy_managed_externally",
            "Repository hosts are managed by the OWL environment and are read-only in Settings.",
        )
    if any(
        entry.canonical_origin == normalized.canonical_origin and entry.source == "built_in"
        for entry in policy.entries
    ):
        raise RepositoryHostReadOnly(
            "repository_host_read_only", "Built-in repository hosts cannot be removed."
        )

    model = _trusted_host_model()
    with transaction.atomic():
        record = (
            model.objects.select_for_update()
            .filter(canonical_origin=normalized.canonical_origin)
            .first()
        )
        if record is None:
            raise RepositoryHostNotFound(
                "repository_host_not_found", "This UI-managed repository host no longer exists."
            )
        if record.source != TrustedRepositoryHostSource.UI:
            raise RepositoryHostReadOnly(
                "repository_host_read_only",
                "Migrated repository host approvals are read-only compatibility records.",
            )
        if connection.vendor == "sqlite":
            model.objects.filter(pk=record.pk).update(enabled=F("enabled"))
        dependencies = repository_host_dependencies(normalized.canonical_origin)
        if dependencies.has_dependencies:
            raise RepositoryHostConflict(dependencies)
        record.delete()
    return RepositoryHostMutationResult(
        normalized.canonical_origin,
        normalized.hostname,
        normalized.port,
        changed=True,
        removed=True,
    )
