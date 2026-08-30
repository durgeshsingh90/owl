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


def _env_csv(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None:
        return default
    return tuple(item.strip() for item in value.split(",") if item.strip())


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

BITBUCKET_ALLOWED_HOSTS = _env_csv("BITBUCKET_ALLOWED_HOSTS", ("bitbucket.org",))
BITBUCKET_HISTORY_YEARS = _env_int("BITBUCKET_HISTORY_YEARS", 3, minimum=1)
BITBUCKET_MAX_REPO_WORKERS = _env_int("BITBUCKET_MAX_REPO_WORKERS", 5, minimum=1)
BITBUCKET_GIT_TIMEOUT_SECONDS = _env_int("BITBUCKET_GIT_TIMEOUT_SECONDS", 3_600, minimum=60)
BITBUCKET_WORKER_IDLE_SECONDS = _env_int("BITBUCKET_WORKER_IDLE_SECONDS", 15, minimum=1)
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
    _env_int("BITBUCKET_PDF_PAGE_SIZE", 200, minimum=10),
    200,
)
BITBUCKET_SEARCH_PAGE_SIZE = min(
    _env_int("BITBUCKET_SEARCH_PAGE_SIZE", 200, minimum=10),
    200,
)
PDF_MAX_EXTRACTION_WORKERS = _env_int(
    "PDF_MAX_EXTRACTION_WORKERS",
    max(1, min((os.cpu_count() or 2) - 1, 4)),
    minimum=1,
)
PDF_EXTRACTION_TIMEOUT_SECONDS = _env_int(
    "PDF_EXTRACTION_TIMEOUT_SECONDS",
    600,
    minimum=10,
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
BITBUCKET_SUPERVISOR_POLL_SECONDS = _env_int(
    "BITBUCKET_SUPERVISOR_POLL_SECONDS",
    5,
    minimum=1,
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
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
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
    "formatters": {
        "safe": {
            "()": "core.logging.SecretSafeFormatter",
            "format": "{asctime} {levelname} {name}: {message}",
            "style": "{",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "safe",
        },
        "local_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": LOG_ROOT / "owl.log",
            "maxBytes": OWL_LOG_MAX_BYTES,
            "backupCount": OWL_LOG_BACKUP_COUNT,
            "encoding": "utf-8",
            "delay": True,
            "formatter": "safe",
        },
    },
    "loggers": {
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
