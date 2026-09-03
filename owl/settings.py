"""Django settings for OWL's loopback-only, local-first runtime."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.utils.csp import CSP
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


def _configured_path(raw_value: str | None, default: Path) -> Path:
    """Return an absolute path, resolving relative configuration below BASE_DIR."""
    candidate = Path(raw_value).expanduser() if raw_value else default
    if not candidate.is_absolute():
        candidate = BASE_DIR / candidate
    return candidate.resolve()


ENV_FILE = _configured_path(os.getenv("OWL_ENV_FILE"), BASE_DIR / ".env")
if ENV_FILE.is_file():
    # The process environment always wins over values in the local ignored file.
    load_dotenv(dotenv_path=ENV_FILE, override=False)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ImproperlyConfigured(f"{name} must be one of true/false, yes/no, on/off, or 1/0.")


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default

    try:
        parsed = int(value)
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be an integer.") from exc
    if parsed < minimum:
        raise ImproperlyConfigured(f"{name} must be at least {minimum}.")
    return parsed


def _env_optional_int(name: str, *, minimum: int = 0) -> int | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be an integer or blank.") from exc
    if parsed < minimum:
        raise ImproperlyConfigured(f"{name} must be at least {minimum}.")
    return parsed


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be a number.") from exc
    if minimum is not None and parsed < minimum:
        raise ImproperlyConfigured(f"{name} must be at least {minimum}.")
    if maximum is not None and parsed > maximum:
        raise ImproperlyConfigured(f"{name} must be at most {maximum}.")
    return parsed


def _env_csv(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None:
        return default
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _env_choice(name: str, default: str, choices: tuple[str, ...]) -> str:
    value = os.getenv(name, default).strip().casefold() or default
    if value not in choices:
        raise ImproperlyConfigured(f"{name} must be one of: {', '.join(choices)}.")
    return value


def _env_log_level(name: str, default: str) -> str:
    value = os.getenv(name, default).strip().upper() or default
    allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if value not in allowed:
        raise ImproperlyConfigured(f"{name} must be one of: {', '.join(sorted(allowed))}.")
    return value


def _create_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ImproperlyConfigured(
            f"OWL cannot create or access its local data directory: {path}"
        ) from exc


SAFE_REPOSITORY_DATA_DIRECTORIES = {"data", "media", "var"}


def _validate_data_root(path: Path) -> Path:
    """Reject broad or trackable locations before OWL writes any runtime data."""

    if BASE_DIR.is_relative_to(path):
        raise ImproperlyConfigured(
            "OWL_DATA_ROOT must be a dedicated data directory, not the repository "
            "or one of its parent directories."
        )

    if path.is_relative_to(BASE_DIR):
        relative_path = path.relative_to(BASE_DIR)
        if (
            not relative_path.parts
            or relative_path.parts[0] not in SAFE_REPOSITORY_DATA_DIRECTORIES
        ):
            allowed = ", ".join(sorted(SAFE_REPOSITORY_DATA_DIRECTORIES))
            raise ImproperlyConfigured(
                "An OWL_DATA_ROOT inside the repository must be beneath one of "
                f"these ignored directories: {allowed}."
            )

    broad_external_roots = {
        Path(path.anchor).resolve(),
        Path("/tmp").resolve(),
        Path("/var").resolve(),
        Path("/var/tmp").resolve(),
        Path("/Users").resolve(),
        Path("/Volumes").resolve(),
    }
    if path in broad_external_roots:
        raise ImproperlyConfigured(
            "OWL_DATA_ROOT must name a dedicated subdirectory, not a broad system directory."
        )
    return path


OWL_DATA_ROOT = _validate_data_root(_configured_path(os.getenv("OWL_DATA_ROOT"), BASE_DIR / "var"))
_create_private_directory(OWL_DATA_ROOT)

DATABASE_ROOT = OWL_DATA_ROOT / "database"
LOG_ROOT = OWL_DATA_ROOT / "logs"
MEDIA_ROOT = OWL_DATA_ROOT / "media"
STATIC_ROOT = OWL_DATA_ROOT / "staticfiles"
SECRET_ROOT = OWL_DATA_ROOT / "secrets"
REPOSITORIES_ROOT = OWL_DATA_ROOT / "repositories"
BITBUCKET_MEDIA_ROOT = MEDIA_ROOT / "bitbucket"
BITBUCKET_REPOSITORIES_ROOT = BITBUCKET_MEDIA_ROOT / "repositories"
BITBUCKET_TEMP_ROOT = BITBUCKET_MEDIA_ROOT / "tmp"
IMPORTS_ROOT = OWL_DATA_ROOT / "imports"
BACKUPS_ROOT = OWL_DATA_ROOT / "backups"
INDEXES_ROOT = OWL_DATA_ROOT / "indexes"
TEMP_ROOT = OWL_DATA_ROOT / "tmp"
MODEL_ROOT = OWL_DATA_ROOT / "models"
SEMANTIC_MODEL_CACHE_ROOT = MODEL_ROOT / "semantic"
PDF_PIPELINE_STATE_ROOT = OWL_DATA_ROOT / "pdf-pipeline"

for runtime_directory in (
    DATABASE_ROOT,
    LOG_ROOT,
    MEDIA_ROOT,
    SECRET_ROOT,
    REPOSITORIES_ROOT,
    BITBUCKET_MEDIA_ROOT,
    BITBUCKET_REPOSITORIES_ROOT,
    BITBUCKET_TEMP_ROOT,
    IMPORTS_ROOT,
    BACKUPS_ROOT,
    INDEXES_ROOT,
    TEMP_ROOT,
    MODEL_ROOT,
    SEMANTIC_MODEL_CACHE_ROOT,
    PDF_PIPELINE_STATE_ROOT,
):
    _create_private_directory(runtime_directory)


def _validate_secret_key(value: str, source: str) -> str:
    if len(value) < 50 or len(set(value)) < 5 or value.startswith("django-insecure-"):
        raise ImproperlyConfigured(
            f"{source} must contain a strong Django secret key of at least 50 "
            "characters and must not use Django's insecure development prefix."
        )
    return value


def _load_or_create_secret_key() -> str:
    configured = os.getenv("DJANGO_SECRET_KEY", "").strip()
    if configured:
        return _validate_secret_key(configured, "DJANGO_SECRET_KEY")

    secret_path = SECRET_ROOT / "django-secret-key"
    try:
        stored = secret_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        stored = ""
    except OSError as exc:
        raise ImproperlyConfigured(
            f"OWL cannot read its local Django secret key: {secret_path}"
        ) from exc

    if stored:
        return _validate_secret_key(stored, str(secret_path))

    generated = secrets.token_urlsafe(64)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(secret_path, flags, 0o600)
    except FileExistsError:
        # Another OWL process created the key between the read and create calls.
        try:
            stored = secret_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ImproperlyConfigured(
                f"OWL cannot read its local Django secret key: {secret_path}"
            ) from exc
        return _validate_secret_key(stored, str(secret_path))
    except OSError as exc:
        raise ImproperlyConfigured(
            f"OWL cannot create its local Django secret key: {secret_path}"
        ) from exc

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as secret_file:
            secret_file.write(f"{generated}\n")
    except OSError as exc:
        secret_path.unlink(missing_ok=True)
        raise ImproperlyConfigured(
            f"OWL cannot write its local Django secret key: {secret_path}"
        ) from exc

    return generated


SECRET_KEY = _load_or_create_secret_key()
DEBUG = _env_bool("DJANGO_DEBUG", True)

# OWL is intentionally loopback-only. A network deployment requires a separate
# authentication, HTTPS, and threat-model decision.
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]
INTERNAL_IPS = ["127.0.0.1", "::1"]
OWL_ALLOW_NON_LOOPBACK = _env_bool("OWL_ALLOW_NON_LOOPBACK", False)


# A complete environment-managed Confluence profile takes precedence over the
# UI-managed encrypted profile. These settings must never enter template context.
CONFLUENCE_BASE_URL = os.getenv("CONFLUENCE_BASE_URL", "").strip()
CONFLUENCE_PAT = os.getenv("CONFLUENCE_PAT", "").strip()
CONFLUENCE_AUTH_MODE = os.getenv("CONFLUENCE_AUTH_MODE", "bearer").strip().casefold() or "bearer"
CONFLUENCE_SECRET_BACKEND = (
    os.getenv("CONFLUENCE_SECRET_BACKEND", "auto").strip().casefold() or "auto"
)
CONFLUENCE_REQUEST_TIMEOUT_SECONDS = _env_int("CONFLUENCE_REQUEST_TIMEOUT_SECONDS", 30, minimum=1)
CONFLUENCE_MAX_RESPONSE_BYTES = _env_int("CONFLUENCE_MAX_RESPONSE_BYTES", 1_048_576, minimum=1_024)
CONFLUENCE_ACTION_COOLDOWN_SECONDS = _env_int("CONFLUENCE_ACTION_COOLDOWN_SECONDS", 2, minimum=0)
CONFLUENCE_MAX_WORKERS = _env_int("CONFLUENCE_MAX_WORKERS", 5, minimum=1)
CONFLUENCE_REFRESH_INTERVAL_SECONDS = _env_int(
    "CONFLUENCE_REFRESH_INTERVAL_SECONDS",
    604_800,
    minimum=60,
)
CONFLUENCE_REFRESH_RETRY_SECONDS = _env_int(
    "CONFLUENCE_REFRESH_RETRY_SECONDS",
    7_200,
    minimum=60,
)
CONFLUENCE_REFRESH_SCHEDULER_POLL_SECONDS = _env_int(
    "CONFLUENCE_REFRESH_SCHEDULER_POLL_SECONDS",
    60,
    minimum=5,
)

# The memory backend is an injectable fake, never an automatic fallback when
# the operating-system credential store is unavailable.
OWL_ALLOW_IN_MEMORY_SECRET_STORE = _env_bool("OWL_ALLOW_IN_MEMORY_SECRET_STORE", False)
OWL_ALLOW_LIVE_EXTERNAL_TESTS = _env_bool("OWL_ALLOW_LIVE_EXTERNAL_TESTS", False)
OWL_ALLOW_SYNTHETIC_CONFLUENCE_TARGETS = _env_bool("OWL_ALLOW_SYNTHETIC_CONFLUENCE_TARGETS", False)
OWL_ALLOW_SYNTHETIC_GIT_REMOTES = _env_bool("OWL_ALLOW_SYNTHETIC_GIT_REMOTES", False)

_BITBUCKET_ALLOWED_HOSTS_RAW = os.getenv("BITBUCKET_ALLOWED_HOSTS")
BITBUCKET_ALLOWED_HOSTS_EXPLICIT = _BITBUCKET_ALLOWED_HOSTS_RAW is not None
BITBUCKET_ALLOWED_HOSTS_SOURCE = (
    "explicit_blank"
    if BITBUCKET_ALLOWED_HOSTS_EXPLICIT and not _BITBUCKET_ALLOWED_HOSTS_RAW.strip()
    else "explicit"
    if BITBUCKET_ALLOWED_HOSTS_EXPLICIT
    else "unset"
)
BITBUCKET_ALLOWED_HOSTS = _env_csv(
    "BITBUCKET_ALLOWED_HOSTS",
    ("bitbucket.org", "github.com"),
)
BITBUCKET_SECRET_BACKEND = (
    os.getenv("BITBUCKET_SECRET_BACKEND", CONFLUENCE_SECRET_BACKEND).strip().casefold() or "auto"
)
BITBUCKET_HISTORY_YEARS = _env_int("BITBUCKET_HISTORY_YEARS", 3, minimum=1)
# Four independent repository controllers keep the Git side of the pipeline
# moving while completed repositories feed the separate PDF worker pool.
BITBUCKET_MAX_REPO_WORKERS = 4
BITBUCKET_GIT_TIMEOUT_SECONDS = _env_int("BITBUCKET_GIT_TIMEOUT_SECONDS", 3_600, minimum=60)
BITBUCKET_CONNECTION_TIMEOUT_SECONDS = min(
    _env_int("BITBUCKET_CONNECTION_TIMEOUT_SECONDS", 20, minimum=1), 120
)
BITBUCKET_WORKER_IDLE_SECONDS = _env_int("BITBUCKET_WORKER_IDLE_SECONDS", 15, minimum=1)
BITBUCKET_REPOSITORY_JOB_LEASE_SECONDS = _env_int(
    "BITBUCKET_REPOSITORY_JOB_LEASE_SECONDS",
    90,
    minimum=60,
)
BITBUCKET_REPOSITORY_WORKER_MAX_RETRIES = _env_int(
    "BITBUCKET_REPOSITORY_WORKER_MAX_RETRIES",
    1,
    minimum=0,
)
# Repository deletion may need to wait for an isolated PDF parser to observe
# cancellation and release its shared checkout lock. Keep this as a Django
# setting so local installations can tune it without an environment file.
BITBUCKET_REPOSITORY_REMOVAL_WAIT_SECONDS = 120
BITBUCKET_DAILY_REFRESH_ENABLED = _env_bool("BITBUCKET_DAILY_REFRESH_ENABLED", True)
BITBUCKET_DAILY_REFRESH_LOCAL_HOUR = _env_int(
    "BITBUCKET_DAILY_REFRESH_LOCAL_HOUR",
    11,
    minimum=0,
)
if BITBUCKET_DAILY_REFRESH_LOCAL_HOUR > 23:
    raise ImproperlyConfigured("BITBUCKET_DAILY_REFRESH_LOCAL_HOUR must be between 0 and 23.")
BITBUCKET_DAILY_REFRESH_RETRY_SECONDS = _env_int(
    "BITBUCKET_DAILY_REFRESH_RETRY_SECONDS",
    7_200,
    minimum=60,
)
BITBUCKET_DAILY_REFRESH_MAX_RETRIES = _env_int(
    "BITBUCKET_DAILY_REFRESH_MAX_RETRIES",
    3,
    minimum=0,
)
BITBUCKET_PDF_PAGE_SIZE = min(
    _env_int("BITBUCKET_PDF_PAGE_SIZE", 500, minimum=10),
    500,
)
BITBUCKET_SEARCH_PAGE_SIZE = min(
    _env_int("BITBUCKET_SEARCH_PAGE_SIZE", 100, minimum=10),
    100,
)
# Sixteen isolated parsers feed one JSONL stager and one dedicated SQLite
# publisher. Local installs can tune the pool without changing source; retain a
# defensive cap so one accidental value cannot exhaust RAM.
PDF_MAX_EXTRACTION_WORKERS = min(
    _env_int("PDF_MAX_EXTRACTION_WORKERS", 16, minimum=1),
    32,
)
# Retained for dashboard/backward-configuration compatibility only. Durable
# JSONL chunks may grow beyond this soft warning threshold and never throttle
# extraction.
PDF_MAX_STAGED_PUBLICATIONS = min(
    _env_int("PDF_MAX_STAGED_PUBLICATIONS", 16, minimum=1),
    1_000_000,
)
# Prefer repository locality while permitting bounded, fair spillover when the
# preferred set cannot fill the parser pool.
PDF_MAX_ACTIVE_EXTRACTION_REPOSITORIES = _env_int(
    "PDF_MAX_ACTIVE_EXTRACTION_REPOSITORIES",
    1,
    minimum=1,
)
PDF_PIPELINE_REPOSITORY_FAIRNESS_MAX_WAIT_SECONDS = _env_int(
    "PDF_PIPELINE_REPOSITORY_FAIRNESS_MAX_WAIT_SECONDS",
    120,
    minimum=1,
)
PDF_PIPELINE_REPOSITORY_WORK_CONSERVING = _env_bool(
    "PDF_PIPELINE_REPOSITORY_WORK_CONSERVING",
    True,
)
PDF_MAX_EXTRACTION_WORKERS_PER_REPOSITORY = _env_int(
    "PDF_MAX_EXTRACTION_WORKERS_PER_REPOSITORY",
    PDF_MAX_EXTRACTION_WORKERS,
    minimum=1,
)
if PDF_MAX_ACTIVE_EXTRACTION_REPOSITORIES > PDF_MAX_EXTRACTION_WORKERS:
    raise ImproperlyConfigured(
        "PDF_MAX_ACTIVE_EXTRACTION_REPOSITORIES cannot exceed PDF_MAX_EXTRACTION_WORKERS."
    )
if PDF_MAX_EXTRACTION_WORKERS_PER_REPOSITORY > PDF_MAX_EXTRACTION_WORKERS:
    raise ImproperlyConfigured(
        "PDF_MAX_EXTRACTION_WORKERS_PER_REPOSITORY cannot exceed PDF_MAX_EXTRACTION_WORKERS."
    )
PDF_PIPELINE_REUSE_PARENT_FINGERPRINT = _env_bool(
    "PDF_PIPELINE_REUSE_PARENT_FINGERPRINT",
    True,
)
PDF_PUBLICATION_PAGE_BATCH_SIZE = _env_int(
    "PDF_PUBLICATION_PAGE_BATCH_SIZE",
    100,
    minimum=1,
)
if PDF_PUBLICATION_PAGE_BATCH_SIZE > 1_000:
    raise ImproperlyConfigured("PDF_PUBLICATION_PAGE_BATCH_SIZE cannot exceed 1000.")
PDF_JSONL_CHUNK_SIZE_BYTES = min(
    _env_int("PDF_JSONL_CHUNK_SIZE_BYTES", 50 * 1024 * 1024, minimum=1),
    1024 * 1024 * 1024,
)
# Blank deliberately follows BITBUCKET_TEMP_ROOT at access time. This keeps
# test/runtime data-root overrides isolated without exposing extracted text.
PDF_JSONL_STAGING_DIRECTORY = os.getenv("PDF_JSONL_STAGING_DIRECTORY", "").strip()
PDF_JSONL_RETENTION_DAYS = _env_int("PDF_JSONL_RETENTION_DAYS", 7, minimum=0)
PDF_EXTRACTION_TIMEOUT_SECONDS = _env_int(
    "PDF_EXTRACTION_TIMEOUT_SECONDS",
    600,
    minimum=10,
)
PDF_EXTRACTION_JOB_LEASE_SECONDS = _env_int(
    "PDF_EXTRACTION_JOB_LEASE_SECONDS",
    90,
    minimum=60,
)
PDF_EXTRACTION_WORKER_IDLE_SECONDS = _env_int(
    "PDF_EXTRACTION_WORKER_IDLE_SECONDS",
    15,
    minimum=1,
)
PDF_EXTRACTION_MAX_AUTOMATIC_RETRIES = _env_int(
    "PDF_EXTRACTION_MAX_AUTOMATIC_RETRIES",
    2,
    minimum=0,
)
# The controller defaults to observe-only and maps its target to the legacy
# worker setting.  Upgrading OWL therefore never changes extraction admission.
PDF_PIPELINE_CONTROLLER_MODE = _env_choice(
    "PDF_PIPELINE_CONTROLLER_MODE",
    "observe",
    ("fixed", "observe", "shadow", "adaptive"),
)
PDF_PIPELINE_ADAPTIVE_ENABLED = _env_bool("PDF_PIPELINE_ADAPTIVE_ENABLED", False)
PDF_PIPELINE_CONTROLLER_KILL_SWITCH = _env_bool(
    "PDF_PIPELINE_CONTROLLER_KILL_SWITCH",
    False,
)
PDF_PIPELINE_MANUAL_FIXED_TARGET = _env_optional_int(
    "PDF_PIPELINE_MANUAL_FIXED_TARGET",
    minimum=0,
)
PDF_PIPELINE_CONFIGURED_MIN_TARGET = _env_int(
    "PDF_PIPELINE_CONFIGURED_MIN_TARGET",
    1,
    minimum=0,
)
PDF_PIPELINE_INITIAL_TARGET = _env_int(
    "PDF_PIPELINE_INITIAL_TARGET",
    PDF_MAX_EXTRACTION_WORKERS,
    minimum=0,
)
PDF_PIPELINE_TESTED_HARD_MAX = min(
    _env_int("PDF_PIPELINE_TESTED_HARD_MAX", 16, minimum=1),
    32,
)
if PDF_PIPELINE_CONFIGURED_MIN_TARGET > PDF_MAX_EXTRACTION_WORKERS:
    raise ImproperlyConfigured(
        "PDF_PIPELINE_CONFIGURED_MIN_TARGET cannot exceed PDF_MAX_EXTRACTION_WORKERS."
    )
if PDF_PIPELINE_INITIAL_TARGET > PDF_MAX_EXTRACTION_WORKERS:
    raise ImproperlyConfigured(
        "PDF_PIPELINE_INITIAL_TARGET cannot exceed PDF_MAX_EXTRACTION_WORKERS."
    )
if (
    PDF_PIPELINE_MANUAL_FIXED_TARGET is not None
    and PDF_PIPELINE_MANUAL_FIXED_TARGET > PDF_MAX_EXTRACTION_WORKERS
):
    raise ImproperlyConfigured(
        "PDF_PIPELINE_MANUAL_FIXED_TARGET cannot exceed PDF_MAX_EXTRACTION_WORKERS."
    )
PDF_PIPELINE_BACKGROUND_CPU_BUDGET_FRACTION = _env_float(
    "PDF_PIPELINE_BACKGROUND_CPU_BUDGET_FRACTION",
    0.80,
    minimum=0.0,
    maximum=1.0,
)
if PDF_PIPELINE_BACKGROUND_CPU_BUDGET_FRACTION <= 0:
    raise ImproperlyConfigured(
        "PDF_PIPELINE_BACKGROUND_CPU_BUDGET_FRACTION must be greater than zero."
    )
PDF_PIPELINE_METRICS_ENABLED = _env_bool("PDF_PIPELINE_METRICS_ENABLED", True)
PDF_PIPELINE_METRICS_SAMPLE_SECONDS = _env_int(
    "PDF_PIPELINE_METRICS_SAMPLE_SECONDS",
    5,
    minimum=1,
)
PDF_PIPELINE_METRICS_RETENTION_SECONDS = _env_int(
    "PDF_PIPELINE_METRICS_RETENTION_SECONDS",
    1_800,
    minimum=60,
)
PDF_PIPELINE_METRICS_SNAPSHOT_ENABLED = _env_bool(
    "PDF_PIPELINE_METRICS_SNAPSHOT_ENABLED",
    True,
)
PDF_PIPELINE_METRICS_STALE_SECONDS = _env_int(
    "PDF_PIPELINE_METRICS_STALE_SECONDS",
    15,
    minimum=5,
)
PDF_PIPELINE_RATE_WINDOW_SECONDS = _env_int(
    "PDF_PIPELINE_RATE_WINDOW_SECONDS",
    60,
    minimum=10,
)
PDF_PIPELINE_RATE_MIN_ELAPSED_SECONDS = _env_int(
    "PDF_PIPELINE_RATE_MIN_ELAPSED_SECONDS",
    30,
    minimum=1,
)
PDF_PIPELINE_RATE_MIN_EVENTS = _env_int(
    "PDF_PIPELINE_RATE_MIN_EVENTS",
    3,
    minimum=1,
)
PDF_PIPELINE_SQLITE_LOCK_BLOCKED_MS = _env_int(
    "PDF_PIPELINE_SQLITE_LOCK_BLOCKED_MS",
    250,
    minimum=1,
)
if PDF_PIPELINE_RATE_MIN_ELAPSED_SECONDS > PDF_PIPELINE_RATE_WINDOW_SECONDS:
    raise ImproperlyConfigured(
        "PDF_PIPELINE_RATE_MIN_ELAPSED_SECONDS cannot exceed PDF_PIPELINE_RATE_WINDOW_SECONDS."
    )
PDF_PIPELINE_ETA_MIN_COMPLETIONS = _env_int(
    "PDF_PIPELINE_ETA_MIN_COMPLETIONS",
    3,
    minimum=1,
)
PDF_PIPELINE_ETA_STALE_SECONDS = _env_int(
    "PDF_PIPELINE_ETA_STALE_SECONDS",
    30,
    minimum=5,
)
PDF_PIPELINE_CONTROLLER_OBSERVATION_SECONDS = _env_int(
    "PDF_PIPELINE_CONTROLLER_OBSERVATION_SECONDS",
    60,
    minimum=30,
)
PDF_PIPELINE_CONTROLLER_COOLDOWN_SECONDS = _env_int(
    "PDF_PIPELINE_CONTROLLER_COOLDOWN_SECONDS",
    120,
    minimum=30,
)
PDF_PIPELINE_CONTROLLER_HYSTERESIS_SAMPLES = _env_int(
    "PDF_PIPELINE_CONTROLLER_HYSTERESIS_SAMPLES",
    3,
    minimum=1,
)
PDF_PIPELINE_CONTROLLER_MIN_DOCUMENTS = _env_int(
    "PDF_PIPELINE_CONTROLLER_MIN_DOCUMENTS",
    3,
    minimum=1,
)
PDF_PIPELINE_CONTROLLER_MIN_PAGES = _env_int(
    "PDF_PIPELINE_CONTROLLER_MIN_PAGES",
    10,
    minimum=1,
)
PDF_PIPELINE_CONTROLLER_MIN_BYTES = _env_int(
    "PDF_PIPELINE_CONTROLLER_MIN_BYTES",
    1_048_576,
    minimum=1,
)
PDF_PIPELINE_CONTROLLER_MAX_ORDINARY_DECREASE = _env_int(
    "PDF_PIPELINE_CONTROLLER_MAX_ORDINARY_DECREASE",
    2,
    minimum=1,
)
PDF_PIPELINE_CONTROLLER_MIN_THROUGHPUT_IMPROVEMENT = _env_float(
    "PDF_PIPELINE_CONTROLLER_MIN_THROUGHPUT_IMPROVEMENT",
    0.05,
    minimum=0.0,
    maximum=1.0,
)
PDF_PIPELINE_CONTROLLER_MAX_HOST_CPU_PCT = _env_float(
    "PDF_PIPELINE_CONTROLLER_MAX_HOST_CPU_PCT",
    85.0,
    minimum=1.0,
    maximum=100.0,
)
PDF_PIPELINE_CONTROLLER_MIN_AVAILABLE_MEMORY_BYTES = _env_int(
    "PDF_PIPELINE_CONTROLLER_MIN_AVAILABLE_MEMORY_BYTES",
    8 * 1_024**3,
    minimum=1,
)
PDF_PIPELINE_CONTROLLER_MIN_AVAILABLE_DISK_BYTES = _env_int(
    "PDF_PIPELINE_CONTROLLER_MIN_AVAILABLE_DISK_BYTES",
    10 * 1_024**3,
    minimum=1,
)
PDF_PIPELINE_CONTROLLER_MAX_FOREGROUND_P95_MS = _env_int(
    "PDF_PIPELINE_CONTROLLER_MAX_FOREGROUND_P95_MS",
    500,
    minimum=1,
)
PDF_PIPELINE_ADAPTIVE_BENCHMARK_GATE_PATH = _configured_path(
    os.getenv("PDF_PIPELINE_ADAPTIVE_BENCHMARK_GATE_PATH"),
    PDF_PIPELINE_STATE_ROOT / "adaptive-enablement-v1.json",
)
PDF_PIPELINE_RECOVERY_ENABLED = _env_bool("PDF_PIPELINE_RECOVERY_ENABLED", True)
PDF_PIPELINE_RECOVERY_PAUSE_AFTER_ATTEMPTS = _env_int(
    "PDF_PIPELINE_RECOVERY_PAUSE_AFTER_ATTEMPTS",
    25,
    minimum=0,
)
PDF_PIPELINE_RECOVERY_BACKOFF_BASE_SECONDS = _env_int(
    "PDF_PIPELINE_RECOVERY_BACKOFF_BASE_SECONDS",
    1,
    minimum=1,
)
PDF_PIPELINE_RECOVERY_BACKOFF_MAX_SECONDS = _env_int(
    "PDF_PIPELINE_RECOVERY_BACKOFF_MAX_SECONDS",
    300,
    minimum=1,
)
if PDF_PIPELINE_RECOVERY_BACKOFF_BASE_SECONDS > PDF_PIPELINE_RECOVERY_BACKOFF_MAX_SECONDS:
    raise ImproperlyConfigured(
        "PDF_PIPELINE_RECOVERY_BACKOFF_BASE_SECONDS cannot exceed its maximum."
    )
PDF_PIPELINE_RECOVERY_JITTER_FRACTION = _env_float(
    "PDF_PIPELINE_RECOVERY_JITTER_FRACTION",
    0.20,
    minimum=0.0,
    maximum=1.0,
)
PDF_PIPELINE_RECOVERY_STABILITY_SECONDS = _env_int(
    "PDF_PIPELINE_RECOVERY_STABILITY_SECONDS",
    60,
    minimum=5,
)
PDF_PIPELINE_RECOVERY_CORRELATION_WINDOW_SECONDS = _env_int(
    "PDF_PIPELINE_RECOVERY_CORRELATION_WINDOW_SECONDS",
    10,
    minimum=1,
)
PDF_PIPELINE_RECOVERY_ESCALATION_SLOT_COUNT = _env_int(
    "PDF_PIPELINE_RECOVERY_ESCALATION_SLOT_COUNT",
    2,
    minimum=2,
)
if PDF_PIPELINE_RECOVERY_ESCALATION_SLOT_COUNT > 8:
    raise ImproperlyConfigured("PDF_PIPELINE_RECOVERY_ESCALATION_SLOT_COUNT cannot exceed 8.")
PDF_PIPELINE_COMPONENT_ERROR_LOOP_THRESHOLD = _env_int(
    "PDF_PIPELINE_COMPONENT_ERROR_LOOP_THRESHOLD",
    5,
    minimum=1,
)
if PDF_PIPELINE_COMPONENT_ERROR_LOOP_THRESHOLD > 100:
    raise ImproperlyConfigured("PDF_PIPELINE_COMPONENT_ERROR_LOOP_THRESHOLD cannot exceed 100.")
BITBUCKET_SUPERVISOR_POLL_SECONDS = _env_int(
    "BITBUCKET_SUPERVISOR_POLL_SECONDS",
    5,
    minimum=1,
)
OWL_KEEP_DISPLAY_AWAKE_DURING_BACKGROUND_WORK = _env_bool(
    "OWL_KEEP_DISPLAY_AWAKE_DURING_BACKGROUND_WORK",
    True,
)
PDF_MAX_FILE_BYTES = _env_int(
    "PDF_MAX_FILE_BYTES",
    2_147_483_648,
    minimum=1_024,
)
PDF_MAX_PAGES = _env_int("PDF_MAX_PAGES", 10_000, minimum=1)
PDF_MAX_PAGE_TEXT_CHARS = _env_int(
    "PDF_MAX_PAGE_TEXT_CHARS",
    2_000_000,
    minimum=1_000,
)
PDF_MAX_TOTAL_TEXT_CHARS = _env_int(
    "PDF_MAX_TOTAL_TEXT_CHARS",
    100_000_000,
    minimum=10_000,
)
PDF_MAX_PROCESS_MEMORY_BYTES = _env_int(
    "PDF_MAX_PROCESS_MEMORY_BYTES",
    1_073_741_824,
    minimum=67_108_864,
)
SEMANTIC_SEARCH_ENABLED = _env_bool("SEMANTIC_SEARCH_ENABLED", True)
SEMANTIC_MODEL_ID = os.getenv("SEMANTIC_MODEL_ID", "BAAI/bge-small-en-v1.5").strip()
SEMANTIC_MODEL_REPOSITORY = os.getenv(
    "SEMANTIC_MODEL_REPOSITORY",
    "Qdrant/bge-small-en-v1.5-onnx-Q",
).strip()
SEMANTIC_MODEL_REVISION = os.getenv(
    "SEMANTIC_MODEL_REVISION",
    "c32e6154d1bb7a0e47c5e745fd895e7700f44385",
).strip()
SEMANTIC_MODEL_PATH = os.getenv("SEMANTIC_MODEL_PATH", "").strip()
SEMANTIC_MODEL_OFFLINE = _env_bool("SEMANTIC_MODEL_OFFLINE", False)
if not SEMANTIC_MODEL_ID or not SEMANTIC_MODEL_REPOSITORY or not SEMANTIC_MODEL_REVISION:
    raise ImproperlyConfigured(
        "SEMANTIC_MODEL_ID, SEMANTIC_MODEL_REPOSITORY, and SEMANTIC_MODEL_REVISION cannot be blank."
    )
SEMANTIC_MODEL_VERSION = (
    f"fastembed-0.8.0:{SEMANTIC_MODEL_ID}:{SEMANTIC_MODEL_REPOSITORY}@{SEMANTIC_MODEL_REVISION}"
)
SEMANTIC_MAX_WORKERS = _env_int(
    "SEMANTIC_MAX_WORKERS",
    2,
    minimum=1,
)
if SEMANTIC_MAX_WORKERS > 4:
    raise ImproperlyConfigured(
        "SEMANTIC_MAX_WORKERS must be at most 4 so local model copies do not exhaust memory."
    )
SEMANTIC_EMBEDDING_BATCH_SIZE = _env_int("SEMANTIC_EMBEDDING_BATCH_SIZE", 64, minimum=1)
SEMANTIC_CHUNK_MAX_CHARACTERS = _env_int("SEMANTIC_CHUNK_MAX_CHARACTERS", 1_800, minimum=256)
SEMANTIC_CHUNK_OVERLAP_CHARACTERS = _env_int("SEMANTIC_CHUNK_OVERLAP_CHARACTERS", 180, minimum=0)
if SEMANTIC_CHUNK_OVERLAP_CHARACTERS >= SEMANTIC_CHUNK_MAX_CHARACTERS:
    raise ImproperlyConfigured(
        "SEMANTIC_CHUNK_OVERLAP_CHARACTERS must be smaller than SEMANTIC_CHUNK_MAX_CHARACTERS."
    )
SEMANTIC_CHUNKER_VERSION = (
    "semantic-chunks-v1:"
    f"max={SEMANTIC_CHUNK_MAX_CHARACTERS}:"
    f"overlap={SEMANTIC_CHUNK_OVERLAP_CHARACTERS}"
)
SEMANTIC_WORKER_IDLE_SECONDS = _env_int("SEMANTIC_WORKER_IDLE_SECONDS", 15, minimum=1)
SEMANTIC_JOB_LEASE_SECONDS = _env_int("SEMANTIC_JOB_LEASE_SECONDS", 900, minimum=60)
SEMANTIC_JOB_MAX_AUTOMATIC_RETRIES = _env_int("SEMANTIC_JOB_MAX_AUTOMATIC_RETRIES", 2, minimum=0)
SEMANTIC_JOB_RETRY_SECONDS = _env_int("SEMANTIC_JOB_RETRY_SECONDS", 300, minimum=5)
SEMANTIC_FAILED_RETRY_SECONDS = _env_int("SEMANTIC_FAILED_RETRY_SECONDS", 3_600, minimum=60)
SEMANTIC_SWEEP_BATCH_SIZE = _env_int("SEMANTIC_SWEEP_BATCH_SIZE", 500, minimum=1)
SEMANTIC_RECONCILE_SECONDS = _env_int("SEMANTIC_RECONCILE_SECONDS", 60, minimum=10)
SEMANTIC_SEARCH_TOP_K = _env_int("SEMANTIC_SEARCH_TOP_K", 250, minimum=10)
SEMANTIC_RERANK_SOURCE_CANDIDATES = _env_int(
    "SEMANTIC_RERANK_SOURCE_CANDIDATES",
    300,
    minimum=10,
)
SEMANTIC_SEARCH_MIN_SCORE = _env_float("SEMANTIC_SEARCH_MIN_SCORE", 0.55, minimum=-1.0, maximum=1.0)
NEW_DURATION_DAYS = _env_int("NEW_DURATION_DAYS", 30, minimum=1)
UPDATED_DURATION_DAYS = _env_int("UPDATED_DURATION_DAYS", 30, minimum=1)
OPEN_ALL_CONFIRM_THRESHOLD = _env_int("OPEN_ALL_CONFIRM_THRESHOLD", 10, minimum=1)
DATA_UPLOAD_MAX_MEMORY_SIZE = _env_int("DATA_UPLOAD_MAX_MEMORY_BYTES", 2_621_440, minimum=1_024)
FILE_UPLOAD_MAX_MEMORY_SIZE = _env_int("FILE_UPLOAD_MAX_MEMORY_BYTES", 2_621_440, minimum=1_024)
FILE_UPLOAD_TEMP_DIR = TEMP_ROOT
FILE_UPLOAD_PERMISSIONS = 0o600
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o700
DJANGO_LOG_LEVEL = _env_log_level("DJANGO_LOG_LEVEL", "INFO")
OWL_LOG_LEVEL = _env_log_level("OWL_LOG_LEVEL", "INFO")
BITBUCKET_LOG_LEVEL = _env_log_level("BITBUCKET_LOG_LEVEL", "DEBUG")
BOOKMARK_LOG_LEVEL = _env_log_level("BOOKMARK_LOG_LEVEL", "DEBUG")
SEMANTIC_LOG_LEVEL = _env_log_level("SEMANTIC_LOG_LEVEL", "DEBUG")
OWL_LOG_MAX_BYTES = _env_int("OWL_LOG_MAX_BYTES", 5_242_880, minimum=1_024)
OWL_LOG_BACKUP_COUNT = _env_int("OWL_LOG_BACKUP_COUNT", 3, minimum=1)


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core.apps.CoreConfig",
    "bookmark_manager.apps.BookmarkManagerConfig",
    "bitbucket_search.apps.BitbucketSearchConfig",
    "semantic_search.apps.SemanticSearchConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "bitbucket_search.services.foreground_metrics.ForegroundLatencyMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "core.middleware.LoopbackOpaqueOriginCsrfMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
]

ROOT_URLCONF = "owl.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.csp",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "owl.wsgi.application"
ASGI_APPLICATION = "owl.asgi.application"


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATABASE_ROOT / "owl.sqlite3",
        "OPTIONS": {
            "timeout": _env_int("SQLITE_TIMEOUT_SECONDS", 30, minimum=1),
        },
    },
}


AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


LANGUAGE_CODE = "en-gb"
TIME_ZONE = os.getenv("OWL_TIME_ZONE", "Europe/Dublin").strip() or "Europe/Dublin"
USE_I18N = True
USE_TZ = True


STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "/media/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
DEFAULT_EXCEPTION_REPORTER_FILTER = "core.debug.OWLExceptionReporterFilter"
DEFAULT_EXCEPTION_REPORTER = "core.debug.OWLExceptionReporter"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "expected_loopback_disconnect": {
            "()": "core.logging.ExpectedLoopbackDisconnectFilter",
        },
    },
    "formatters": {
        "safe": {
            "()": "core.logging.SecretSafeFormatter",
            "format": "{asctime} {levelname} {name}: {message}",
            "style": "{",
        },
        "bitbucket": {
            "()": "core.logging.SecretSafeFormatter",
            "format": "{asctime} {levelname} {name} pid={process} thread={threadName}: {message}",
            "style": "{",
        },
        "bookmarks": {
            "()": "core.logging.SecretSafeFormatter",
            "format": "{asctime} {levelname} {name} pid={process} thread={threadName}: {message}",
            "style": "{",
        },
        "semantic": {
            "()": "core.logging.SecretSafeFormatter",
            "format": "{asctime} {levelname} {name} pid={process} thread={threadName}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "safe",
            "filters": ["expected_loopback_disconnect"],
        },
        "local_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": LOG_ROOT / "owl.log",
            "maxBytes": OWL_LOG_MAX_BYTES,
            "backupCount": OWL_LOG_BACKUP_COUNT,
            "encoding": "utf-8",
            "delay": True,
            "formatter": "safe",
            "filters": ["expected_loopback_disconnect"],
        },
        "bitbucket_console": {
            "class": "logging.StreamHandler",
            "level": OWL_LOG_LEVEL,
            "formatter": "bitbucket",
        },
        "bitbucket_file": {
            "class": "core.logging.ProcessSafeRotatingFileHandler",
            "filename": LOG_ROOT / "bitbucket.log",
            "level": BITBUCKET_LOG_LEVEL,
            "maxBytes": OWL_LOG_MAX_BYTES,
            "backupCount": OWL_LOG_BACKUP_COUNT,
            "encoding": "utf-8",
            "delay": True,
            "formatter": "bitbucket",
        },
        "bitbucket_errors": {
            "class": "core.logging.ProcessSafeRotatingFileHandler",
            "filename": LOG_ROOT / "bitbucket-errors.log",
            "level": "ERROR",
            "maxBytes": OWL_LOG_MAX_BYTES,
            "backupCount": OWL_LOG_BACKUP_COUNT,
            "encoding": "utf-8",
            "delay": True,
            "formatter": "bitbucket",
        },
        "bookmarks_console": {
            "class": "logging.StreamHandler",
            "level": OWL_LOG_LEVEL,
            "formatter": "bookmarks",
        },
        "bookmarks_file": {
            "class": "core.logging.ProcessSafeRotatingFileHandler",
            "filename": LOG_ROOT / "bookmarks.log",
            "level": BOOKMARK_LOG_LEVEL,
            "maxBytes": OWL_LOG_MAX_BYTES,
            "backupCount": OWL_LOG_BACKUP_COUNT,
            "encoding": "utf-8",
            "delay": True,
            "formatter": "bookmarks",
        },
        "bookmarks_errors": {
            "class": "core.logging.ProcessSafeRotatingFileHandler",
            "filename": LOG_ROOT / "bookmarks-errors.log",
            "level": "ERROR",
            "maxBytes": OWL_LOG_MAX_BYTES,
            "backupCount": OWL_LOG_BACKUP_COUNT,
            "encoding": "utf-8",
            "delay": True,
            "formatter": "bookmarks",
        },
        "semantic_console": {
            "class": "logging.StreamHandler",
            "level": OWL_LOG_LEVEL,
            "formatter": "semantic",
        },
        "semantic_file": {
            "class": "core.logging.ProcessSafeRotatingFileHandler",
            "filename": LOG_ROOT / "semantic.log",
            "level": SEMANTIC_LOG_LEVEL,
            "maxBytes": OWL_LOG_MAX_BYTES,
            "backupCount": OWL_LOG_BACKUP_COUNT,
            "encoding": "utf-8",
            "delay": True,
            "formatter": "semantic",
        },
        "semantic_errors": {
            "class": "core.logging.ProcessSafeRotatingFileHandler",
            "filename": LOG_ROOT / "semantic-errors.log",
            "level": "ERROR",
            "maxBytes": OWL_LOG_MAX_BYTES,
            "backupCount": OWL_LOG_BACKUP_COUNT,
            "encoding": "utf-8",
            "delay": True,
            "formatter": "semantic",
        },
    },
    "loggers": {
        "owl.bookmarks": {
            "handlers": ["bookmarks_console", "bookmarks_file", "bookmarks_errors"],
            "level": "DEBUG",
            "propagate": False,
        },
        "bookmark_manager": {
            "handlers": ["bookmarks_console", "bookmarks_file", "bookmarks_errors"],
            "level": "DEBUG",
            "propagate": False,
        },
        "owl.bitbucket": {
            "handlers": ["bitbucket_console", "bitbucket_file", "bitbucket_errors"],
            # Keep ERROR events available to their own handler even if the
            # diagnostic file is configured to show only CRITICAL events.
            "level": "DEBUG",
            "propagate": False,
        },
        "bitbucket_search": {
            "handlers": ["bitbucket_console", "bitbucket_file", "bitbucket_errors"],
            "level": "DEBUG",
            "propagate": False,
        },
        "owl.semantic": {
            "handlers": ["semantic_console", "semantic_file", "semantic_errors"],
            "level": "DEBUG",
            "propagate": False,
        },
        "semantic_search": {
            "handlers": ["semantic_console", "semantic_file", "semantic_errors"],
            "level": "DEBUG",
            "propagate": False,
        },
        "django": {
            "handlers": ["console", "local_file"],
            "level": DJANGO_LOG_LEVEL,
            "propagate": False,
        },
        "owl": {
            "handlers": ["console", "local_file"],
            "level": OWL_LOG_LEVEL,
            "propagate": False,
        },
    },
    "root": {
        "handlers": ["console", "local_file"],
        "level": OWL_LOG_LEVEL,
    },
}


# SecurityMiddleware and browser-facing security defaults for loopback HTTP.
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
SECURE_REFERRER_POLICY = "same-origin"
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "SAMEORIGIN"

# Django's built-in CSP support. Nonces allow deliberately marked inline code;
# all ordinary assets must be bundled locally and loaded from this OWL origin.
SECURE_CSP = {
    "default-src": [CSP.SELF],
    "base-uri": [CSP.NONE],
    "connect-src": [CSP.SELF],
    "font-src": [CSP.SELF, "data:"],
    "form-action": [CSP.SELF],
    # Same-origin framing supports OWL's local PDF preview while preventing
    # another site from embedding the application.
    "frame-ancestors": [CSP.SELF],
    "frame-src": [CSP.SELF, "blob:"],
    "img-src": [CSP.SELF, "blob:", "data:"],
    "media-src": [CSP.SELF, "blob:"],
    "object-src": [CSP.SELF, "blob:"],
    "script-src": [CSP.SELF, CSP.NONCE],
    "style-src": [CSP.SELF, CSP.NONCE],
    "worker-src": [CSP.SELF, "blob:"],
}
