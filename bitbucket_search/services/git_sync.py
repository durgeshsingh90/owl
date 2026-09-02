"""Safe Git clone/refresh operations for OWL-managed repositories."""

from __future__ import annotations

import base64
import errno
import logging
import os
import queue
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

from django.conf import settings
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    PDFLocalPolicy,
    PDFLocalPolicyState,
    RepositorySyncPhase,
)
from bitbucket_search.services.filesystem_paths import display_path, filesystem_path
from bitbucket_search.services.git_output import emit_git_output
from bitbucket_search.services.logging_events import get_logger, log_event, logging_context
from bitbucket_search.services.path_safety import has_disallowed_path_characters
from bitbucket_search.services.repository_lock import repository_checkout_lock

ProgressCallback = Callable[[str, int, str], None]
HeartbeatCallback = Callable[[], None]
_GIT_PERCENT = re.compile(r"(?<!\d)(\d{1,3})%")
_DOCUMENT_PATTERNS = ("*.[pP][dD][fF]", "*.[vV][sS][dD][xX]")
_GIT_LFS_DOCUMENT_INCLUDE = ",".join(_DOCUMENT_PATTERNS)
_HEARTBEAT_INTERVAL_SECONDS = 1.0
_GIT_LFS_POINTER_VERSION = b"version https://git-lfs.github.com/spec/v1"
_GIT_LFS_POINTER_OID = re.compile(rb"^oid sha256:[0-9a-f]{64}$", re.MULTILINE)
_GIT_LFS_POINTER_SIZE = re.compile(rb"^size [0-9]+$", re.MULTILINE)
_GIT_LFS_POINTER_MAX_BYTES = 8_192
_IS_WINDOWS = os.name == "nt"
# Windows can keep a directory handle briefly after Git exits (for example,
# while Defender or an indexer inspects the new files). Keep this bounded, but
# long enough to outlast the common short-lived sharing violation.
_WINDOWS_PUBLISH_RETRY_DELAYS = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)
_WINDOWS_TRANSIENT_PUBLISH_ERRORS = frozenset({5, 32, 33})
_PUBLISH_RECOVERY_NAME = re.compile(r"\.owl-checkout-publish-[0-9a-f]{32}")
_COPY_HEARTBEAT_BYTES = 8 * 1024 * 1024
_CONSOLE_LINE_LIMIT = 16_384
logger = get_logger("git")
_GIT_COMMAND_OPERATIONS = frozenset(
    {
        "clone",
        "fetch",
        "merge",
        "checkout",
        "sparse-checkout",
        "status",
        "rev-parse",
        "symbolic-ref",
        "remote",
        "check-ref-format",
        "rev-list",
        "ls-files",
        "ls-tree",
        "ls-remote",
        "log",
        "lfs",
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class GitHTTPSCredential:
    """One in-memory Basic credential for an HTTPS repository operation."""

    username: str
    token: str


@dataclass(frozen=True, slots=True, repr=False)
class _GitHTTPSAuthorization:
    scope_url: str
    header: str
    secret_values: tuple[str, ...]


_ACTIVE_GIT_HTTPS_AUTHORIZATION: ContextVar[_GitHTTPSAuthorization | None] = ContextVar(
    "active_git_https_authorization", default=None
)


def _invalid_https_credential() -> RepositorySyncError:
    return RepositorySyncError(
        "invalid_https_credential",
        "OWL could not prepare the saved HTTPS credential. Replace it in Settings and retry.",
    )


def _https_host_scope(remote_url: str) -> str | None:
    """Return one normalized HTTPS origin suitable for Git's URL-scoped config."""

    try:
        parsed = urlsplit(remote_url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise RepositorySyncError(
            "invalid_repository_url",
            "The repository URL is invalid. Correct it before using a saved credential.",
        ) from exc
    if parsed.scheme.casefold() != "https":
        return None
    hostname = parsed.hostname
    if not hostname:
        raise RepositorySyncError(
            "invalid_repository_url",
            "The repository URL is invalid. Correct it before using a saved credential.",
        )
    normalized_host = hostname.casefold()
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"
    authority = f"{normalized_host}:{port}" if port is not None else normalized_host
    return f"https://{authority}/"


def _build_https_authorization(
    remote_url: str, credential: GitHTTPSCredential | None
) -> _GitHTTPSAuthorization | None:
    if credential is None:
        return None
    scope_url = _https_host_scope(remote_url)
    if scope_url is None:
        return None
    username = credential.username
    secret = credential.token
    if (
        not isinstance(username, str)
        or not isinstance(secret, str)
        or not username.strip()
        or not secret.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in username + secret)
    ):
        raise _invalid_https_credential()
    basic_value = base64.b64encode(f"{username}:{secret}".encode()).decode("ascii")
    header = f"Authorization: Basic {basic_value}"
    secret_values = tuple(
        sorted(
            {username, secret, f"{username}:{secret}", basic_value, header},
            key=len,
            reverse=True,
        )
    )
    return _GitHTTPSAuthorization(
        scope_url=scope_url,
        header=header,
        secret_values=secret_values,
    )


@contextmanager
def _git_https_authorization(
    remote_url: str, credential: GitHTTPSCredential | None
) -> Iterator[None]:
    """Bind a credential to this execution context and reliably remove it afterward."""

    authorization = _build_https_authorization(remote_url, credential)
    context_token = _ACTIVE_GIT_HTTPS_AUTHORIZATION.set(authorization)
    try:
        yield
    finally:
        _ACTIVE_GIT_HTTPS_AUTHORIZATION.reset(context_token)


def _redact_active_git_credential(text: str) -> str:
    authorization = _ACTIVE_GIT_HTTPS_AUTHORIZATION.get()
    if authorization is None:
        return text
    for secret_value in authorization.secret_values:
        text = text.replace(secret_value, "[credential removed]")
    return text


class _ConsoleLines:
    """Send complete lines only; never expose a clipped secret-bearing tail."""

    def __init__(self, operation: str, *, level: str = "info") -> None:
        self.operation = "connection" if operation == "ls-remote" else operation
        self.level = level
        self.characters: list[str] = []
        self.omitted = False

    def feed(self, text: str) -> None:
        for character in text:
            if character in "\r\n":
                self.finish()
            elif not self.omitted:
                if len(self.characters) == _CONSOLE_LINE_LIMIT:
                    self.characters.clear()
                    self.omitted = True
                else:
                    self.characters.append(character)

    def finish(self) -> None:
        if self.omitted:
            emit_git_output(
                "[Git output line omitted: too long]", operation=self.operation, level="warning"
            )
        elif self.characters:
            line = _redact_active_git_credential("".join(self.characters))
            prefix = line.lstrip().casefold()
            level = self.level
            if prefix.startswith(("fatal:", "error:")):
                level = "error"
            elif prefix.startswith("warning:"):
                level = "warning"
            emit_git_output(line, operation=self.operation, level=level)
        self.characters.clear()
        self.omitted = False


def _emit_captured_stderr(text: str, *, operation: str, level: str = "info") -> None:
    lines = _ConsoleLines(operation, level=level)
    lines.feed(text)
    lines.finish()


def _git_command_operation(arguments: Sequence[str]) -> str:
    """Select a fixed Git command label without exposing its arguments."""

    position = 3 if len(arguments) > 1 and arguments[1] == "-C" else 1
    if len(arguments) > position and arguments[position] in _GIT_COMMAND_OPERATIONS:
        return arguments[position]
    return "git_command"


class RepositorySyncError(RuntimeError):
    """A safe, user-actionable repository synchronization failure."""

    def __init__(self, code: str, summary: str, *, blocked_dirty: bool = False) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = " ".join(summary.split())[:500]
        self.blocked_dirty = blocked_dirty


@dataclass(frozen=True, slots=True)
class DocumentStats:
    pdf_count: int
    vsdx_count: int
    document_bytes: int


@dataclass(frozen=True, slots=True)
class RepositorySyncResult:
    branch: str
    source_commit: str
    result_commit: str
    documents: DocumentStats


def _progress_heartbeat(
    progress_callback: ProgressCallback,
    *,
    phase: str,
    progress: int,
    status_message: str,
) -> HeartbeatCallback:
    return lambda: progress_callback(phase, progress, status_message)


def managed_repository_path(repository: BitbucketRepository) -> Path:
    """Derive the only allowed checkout path from OWL-owned values."""

    root = Path(settings.BITBUCKET_REPOSITORIES_ROOT).resolve()
    safe_name = re.sub(r"[^a-z0-9]+", "-", repository.display_name.casefold()).strip("-")
    safe_name = safe_name[:70] or "repository"
    candidate = (root / f"{repository.pk}-{safe_name}").resolve(strict=False)
    if candidate.parent != root:
        raise RepositorySyncError(
            "invalid_local_path",
            "OWL could not derive a safe local repository folder.",
        )
    return candidate


def _validated_existing_path(repository: BitbucketRepository) -> Path:
    expected = managed_repository_path(repository)
    configured = (
        Path(repository.local_path).resolve(strict=False) if repository.local_path else None
    )
    native_expected = filesystem_path(expected)
    if (
        configured != expected
        or not native_expected.is_dir()
        or not (native_expected / ".git").is_dir()
    ):
        raise RepositorySyncError(
            "invalid_local_checkout",
            "The managed repository checkout is missing or is not valid. Retry after repairing it.",
        )
    return expected


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith("GIT_TRACE") or name == "GIT_CURL_VERBOSE":
            environment.pop(name)
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "SSH_ASKPASS_REQUIRE": "never",
            "LC_ALL": "C",
        }
    )
    config_entries: list[tuple[str, str]] = []
    if _IS_WINDOWS:
        # Apply this to OWL's child Git processes only, including their helpers.
        # Never change the user's global Git configuration or Windows registry.
        config_entries.append(("core.longpaths", "true"))
    authorization = _ACTIVE_GIT_HTTPS_AUTHORIZATION.get()
    if authorization is not None:
        # An empty value resets Git's inherited credential-helper list. The
        # request-specific Authorization header then remains the sole HTTPS
        # credential source for this non-interactive child process.
        config_entries.extend(
            (
                ("credential.helper", ""),
                (f"http.{authorization.scope_url}.extraHeader", authorization.header),
            )
        )
    if config_entries:
        try:
            config_count = int(environment.get("GIT_CONFIG_COUNT", "0"))
            if config_count < 0:
                raise ValueError
        except ValueError as exc:
            raise RepositorySyncError(
                "invalid_git_environment",
                "Git's inherited configuration is invalid. Check GIT_CONFIG_COUNT and retry.",
            ) from exc
        for key, value in config_entries:
            environment[f"GIT_CONFIG_KEY_{config_count}"] = key
            environment[f"GIT_CONFIG_VALUE_{config_count}"] = value
            config_count += 1
        environment["GIT_CONFIG_COUNT"] = str(config_count)
    return environment


def _connection_timeout_error() -> RepositorySyncError:
    return RepositorySyncError(
        "connection_timeout",
        "The Git repository connection timed out. Check the network and, if this host "
        "requires it, your VPN connection, then retry.",
    )


def _connection_failure(stderr: str) -> RepositorySyncError:
    """Classify Git's private diagnostics into fixed, actionable public text."""

    detail = stderr.casefold()
    if any(
        phrase in detail
        for phrase in (
            "certificate verify failed",
            "certificate verification failed",
            "server certificate verification failed",
            "ssl certificate problem",
            "ssl peer certificate",
            "ssl connect error",
            "tls handshake",
            "gnutls_handshake",
            "host key verification failed",
            "remote host identification has changed",
        )
    ):
        return RepositorySyncError(
            "connection_tls_failed",
            "Git could not verify the server's TLS certificate or SSH host key. "
            "Check the trusted certificates or known-host entry; do not disable verification.",
        )
    if any(
        phrase in detail
        for phrase in (
            "authentication failed",
            "permission denied",
            "access denied",
            "not authorized",
            "repository not found",
            "could not read username",
            "could not read password",
            "terminal prompts disabled",
            "returned error: 401",
            "returned error: 403",
            "returned error: 404",
        )
    ):
        return RepositorySyncError(
            "connection_auth_failed",
            "Git could not access this repository with the available credentials. "
            "Check the repository URL, permissions, and Git credential manager or SSH agent.",
        )
    if "timed out" in detail or "timeout was reached" in detail:
        return _connection_timeout_error()
    if any(
        phrase in detail
        for phrase in (
            "could not resolve host",
            "could not resolve proxy",
            "could not resolve hostname",
            "failed to connect",
            "couldn't connect",
            "connection refused",
            "connection reset",
            "network is unreachable",
            "no route to host",
            "name or service not known",
            "temporary failure in name resolution",
        )
    ):
        return RepositorySyncError(
            "connection_unreachable",
            "Git could not reach the repository host. Check the network, DNS or proxy and, "
            "if this host requires it, your VPN connection, then retry.",
        )
    return RepositorySyncError(
        "connection_check_failed",
        "Git could not verify repository access. Check the repository URL and the Git console "
        "details, then retry.",
    )


def _check_connection(
    repository: BitbucketRepository,
    progress_callback: ProgressCallback,
    *,
    repository_path: Path | None = None,
) -> None:
    """Read remote HEAD using Git's real credentials before checkout writes."""

    phase = RepositorySyncPhase.CHECKING_CONNECTION
    status_message = "Checking repository connection…"
    progress_callback(phase, 3, status_message)
    emit_git_output("Checking repository connection with Git…", operation="connection")
    arguments = ("git",)
    if repository_path is not None:
        arguments += ("-C", str(repository_path))
    _run_capture(
        (*arguments, "ls-remote", "--symref", "--", repository.remote_url, "HEAD"),
        failure_code="connection_check_failed",
        failure_summary="Git could not check repository access. Check Git installation and retry.",
        heartbeat_callback=_progress_heartbeat(
            progress_callback, phase=phase, progress=3, status_message=status_message
        ),
        timeout_seconds=settings.BITBUCKET_CONNECTION_TIMEOUT_SECONDS,
        failure_classifier=_connection_failure,
        timeout_error=_connection_timeout_error(),
        isolate_process=True,
    )
    emit_git_output("Repository connection verified.", operation="connection")
    progress_callback(phase, 4, "Repository connection verified.")


def test_repository_connection(
    repository: BitbucketRepository,
    *,
    https_credential: GitHTTPSCredential | None = None,
) -> None:
    """Perform only Git's read-only remote HEAD check for one repository."""

    with _git_https_authorization(repository.remote_url, https_credential):
        _check_connection(repository, lambda _phase, _progress, _message: None)


def probe_bitbucket_ssh_connection() -> None:
    """Verify Bitbucket Cloud SSH authentication with the user's configured keys."""

    timeout = settings.BITBUCKET_CONNECTION_TIMEOUT_SECONDS
    emit_git_output("Checking Bitbucket SSH key authentication…", operation="connection")
    _run_capture(
        (
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectionAttempts=1",
            "-o",
            f"ConnectTimeout={max(1, int(timeout))}",
            "git@bitbucket.org",
        ),
        failure_code="connection_check_failed",
        failure_summary=(
            "SSH could not test Bitbucket authentication. Check that SSH is installed and retry."
        ),
        timeout_seconds=timeout,
        failure_classifier=_connection_failure,
        timeout_error=_connection_timeout_error(),
        isolate_process=True,
        accepted_return_codes=(0, 1),
    )
    emit_git_output("Bitbucket SSH key authentication verified.", operation="connection")


def _abort_capture(process, *, isolate_process: bool) -> str:
    """Stop owned Git/transport children and bound cleanup after timeout/cancellation."""

    running = process.poll() is None
    pid = getattr(process, "pid", None)
    if (
        isolate_process
        and isinstance(pid, int)
        and not isinstance(pid, bool)
        and pid > 0
        and (running or os.name != "nt")
    ):
        try:
            if os.name == "nt":
                subprocess.run(
                    ("taskkill", "/PID", str(pid), "/T", "/F"),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
            else:
                # Git can exit before a helper closes its inherited pipes.
                # Its isolated process group still owns those live helpers.
                os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except (OSError, subprocess.TimeoutExpired) as exc:
            log_event(logger, logging.ERROR, "git_transport_cleanup_failed", error=exc)
    if running:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except OSError as exc:
            log_event(logger, logging.ERROR, "git_transport_cleanup_failed", error=exc)
    try:
        _stdout, stderr = process.communicate(timeout=2)
        return stderr
    except subprocess.TimeoutExpired as exc:
        # A helper that escaped the process group must not keep the worker
        # blocked on inherited pipe handles after the preflight timeout.
        log_event(logger, logging.ERROR, "git_transport_pipe_cleanup_failed", error=exc)
        if os.name != "nt":
            # Windows communicate() has reader threads; closing their TextIO
            # handles here can itself wait forever on an escaped helper's pipe.
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError as close_error:
                        log_event(
                            logger, logging.ERROR, "git_transport_close_failed", error=close_error
                        )
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired) as wait_error:
            log_event(logger, logging.ERROR, "git_transport_reap_failed", error=wait_error)
        return ""
    except OSError as exc:
        log_event(logger, logging.ERROR, "git_transport_pipe_cleanup_failed", error=exc)
        return ""


def _run_capture(
    arguments: Sequence[str],
    *,
    failure_code: str,
    failure_summary: str,
    heartbeat_callback: HeartbeatCallback | None = None,
    input_text: str | None = None,
    timeout_seconds: float | None = None,
    failure_classifier: Callable[[str], RepositorySyncError] | None = None,
    timeout_error: RepositorySyncError | None = None,
    isolate_process: bool = False,
    accepted_return_codes: Sequence[int] = (0,),
) -> str:
    started_at = time.monotonic()
    operation = _git_command_operation(arguments)
    log_event(logger, logging.DEBUG, "git_command_started", stage="capture", operation=operation)
    process_options = {}
    if isolate_process:
        process_options = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
            if os.name == "nt"
            else {"start_new_session": True}
        )
    try:
        process = subprocess.Popen(
            list(arguments),
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_git_environment(),
            close_fds=True,
            **process_options,
        )
    except OSError as exc:
        log_event(
            logger,
            logging.ERROR,
            "git_process_spawn_failed",
            error=exc,
            error_code=failure_code,
            stage="capture",
            operation=operation,
        )
        raise RepositorySyncError(failure_code, failure_summary) from exc

    timeout_seconds = (
        settings.BITBUCKET_GIT_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    )
    deadline = time.monotonic() + timeout_seconds
    stdout = ""
    stderr = ""
    pending_input = input_text
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(
                    list(arguments),
                    timeout_seconds,
                )
            try:
                input_arguments = {"input": pending_input} if pending_input is not None else {}
                stdout, stderr = process.communicate(
                    timeout=min(_HEARTBEAT_INTERVAL_SECONDS, remaining),
                    **input_arguments,
                )
            except subprocess.TimeoutExpired:
                pending_input = None
                if heartbeat_callback is not None:
                    heartbeat_callback()
                continue
            break
    except subprocess.TimeoutExpired as exc:
        stderr = _abort_capture(process, isolate_process=isolate_process)
        _emit_captured_stderr(stderr, operation=operation, level="error")
        failure = timeout_error or RepositorySyncError(failure_code, failure_summary)
        log_event(
            logger,
            logging.ERROR,
            "git_process_timeout",
            error=exc,
            operation=operation,
            error_code=failure.code,
            elapsed_ms=round((time.monotonic() - started_at) * 1000),
        )
        raise failure from exc
    except BaseException:
        _abort_capture(process, isolate_process=isolate_process)
        raise

    accepted_codes = frozenset(int(code) for code in accepted_return_codes)
    succeeded = process.returncode in accepted_codes
    _emit_captured_stderr(stderr, operation=operation, level="info" if succeeded else "error")
    if not succeeded:
        failure = (
            failure_classifier(stderr[-32_768:])
            if failure_classifier is not None
            else RepositorySyncError(failure_code, failure_summary)
        )
        log_event(
            logger,
            logging.ERROR,
            "git_process_failed",
            error_code=failure.code,
            operation=operation,
            return_code=process.returncode,
            elapsed_ms=round((time.monotonic() - started_at) * 1000),
        )
        raise failure
    log_event(
        logger,
        logging.DEBUG,
        "git_command_completed",
        stage="capture",
        operation=operation,
        elapsed_ms=round((time.monotonic() - started_at) * 1000),
    )
    return stdout.strip()


def _run_streaming(
    arguments: Sequence[str],
    *,
    phase: str,
    progress_start: int,
    progress_end: int,
    status_message: str,
    progress_callback: ProgressCallback,
    failure_code: str,
    failure_summary: str,
) -> None:
    """Run Git without a shell while emitting progress and silent-period heartbeats."""

    started_at = time.monotonic()
    operation = _git_command_operation(arguments)
    log_event(
        logger,
        logging.DEBUG,
        "git_command_started",
        stage="streaming",
        phase=phase,
        operation=operation,
    )
    try:
        process = subprocess.Popen(
            list(arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_git_environment(),
            close_fds=True,
        )
    except OSError as exc:
        log_event(
            logger,
            logging.ERROR,
            "git_process_spawn_failed",
            error=exc,
            error_code=failure_code,
            phase=phase,
            operation=operation,
        )
        raise RepositorySyncError(failure_code, failure_summary) from exc

    chunks: queue.Queue[str | None] = queue.Queue(maxsize=128)
    stop_reader = threading.Event()

    def enqueue(chunk: str | None) -> None:
        while not stop_reader.is_set():
            try:
                chunks.put(chunk, timeout=0.1)
                return
            except queue.Full:
                continue

    def read_output() -> None:
        assert process.stdout is not None
        try:
            # Text-mode universal newlines also turn Git's carriage-return
            # progress updates into complete lines without waiting for EOF.
            while not stop_reader.is_set() and (chunk := process.stdout.readline(4096)):
                enqueue(chunk)
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                "git_progress_read_failed",
                error=exc,
                operation=operation,
                phase=phase,
            )
        finally:
            enqueue(None)
            try:
                process.stdout.close()
            except OSError as exc:
                log_event(logger, logging.ERROR, "git_progress_close_failed", error=exc)

    reader = None
    reader_started = False
    try:
        # A failed reader startup or first DB-backed heartbeat must still reap
        # Git before releasing the repository's checkout lock.
        reader_context = copy_context()
        # Preserve repository/job logging context in the reader, but do not
        # retain a credential in a thread that never launches Git and could be
        # kept alive briefly by a transport child holding the output pipe.
        reader_context.run(_ACTIVE_GIT_HTTPS_AUTHORIZATION.set, None)
        reader = threading.Thread(target=reader_context.run, args=(read_output,), daemon=True)
        reader.start()
        reader_started = True
        deadline = time.monotonic() + settings.BITBUCKET_GIT_TIMEOUT_SECONDS
        latest_progress = progress_start
        buffer = ""
        console_lines = _ConsoleLines(operation)
        stream_finished = False
        progress_callback(phase, latest_progress, status_message)
        while process.poll() is None or not stream_finished:
            if time.monotonic() >= deadline:
                process.kill()
                process.wait()
                log_event(
                    logger,
                    logging.ERROR,
                    "git_process_timeout",
                    error_code=failure_code,
                    operation=operation,
                    phase=phase,
                    elapsed_ms=round((time.monotonic() - started_at) * 1000),
                )
                raise RepositorySyncError(failure_code, failure_summary)
            try:
                chunk = chunks.get(timeout=1)
            except queue.Empty:
                progress_callback(phase, latest_progress, status_message)
                continue
            if chunk is None:
                stream_finished = True
                console_lines.finish()
                continue
            console_lines.feed(chunk)
            buffer = (buffer + chunk)[-4096:]
            percentages = [min(int(match.group(1)), 100) for match in _GIT_PERCENT.finditer(buffer)]
            if percentages:
                observed = max(percentages)
                mapped = progress_start + round((progress_end - progress_start) * observed / 100)
                latest_progress = max(latest_progress, min(mapped, progress_end))
            progress_callback(phase, latest_progress, status_message)
    finally:
        stop_reader.set()
        if process.poll() is None:
            process.kill()
        process.wait()
        if reader_started:
            reader.join(timeout=2)
        # The reader owns its pipe. Closing it from here while a transport child
        # still holds the write end could block on the reader's TextIO lock.
        if not reader_started and process.stdout is not None:
            process.stdout.close()

    if process.returncode != 0:
        log_event(
            logger,
            logging.ERROR,
            "git_process_failed",
            error_code=failure_code,
            return_code=process.returncode,
            phase=phase,
            operation=operation,
            elapsed_ms=round((time.monotonic() - started_at) * 1000),
        )
        raise RepositorySyncError(failure_code, failure_summary)
    progress_callback(phase, progress_end, status_message)
    log_event(
        logger,
        logging.DEBUG,
        "git_command_completed",
        stage="streaming",
        phase=phase,
        operation=operation,
        elapsed_ms=round((time.monotonic() - started_at) * 1000),
    )


def _git_value(
    repository_path: Path,
    *arguments: str,
    code: str,
    summary: str,
    heartbeat_callback: HeartbeatCallback | None = None,
) -> str:
    return _run_capture(
        ("git", "-C", str(repository_path), *arguments),
        failure_code=code,
        failure_summary=summary,
        heartbeat_callback=heartbeat_callback,
    )


def require_clean_document_checkout(
    repository: BitbucketRepository,
    *,
    repository_path: Path | None = None,
    heartbeat_callback: HeartbeatCallback | None = None,
) -> Path:
    """Protect local changes before a document policy changes the sparse tree."""

    target = repository_path or _validated_existing_path(repository)
    dirty = _git_value(
        target,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        code="status_failed",
        summary="OWL could not verify that the repository working tree is clean.",
        heartbeat_callback=heartbeat_callback,
    )
    if dirty:
        raise RepositorySyncError(
            "dirty_working_tree",
            "Local changes were found. OWL did not overwrite them; repair the checkout and retry.",
            blocked_dirty=True,
        )
    return target


def _literal_document_policy_patterns(relative_path: str) -> tuple[str, str]:
    relative = PurePosixPath(relative_path)
    if (
        not relative_path
        or "\\" in relative_path
        or has_disallowed_path_characters(relative_path)
        or relative.is_absolute()
        or PureWindowsPath(relative_path).drive
        or relative.suffix.casefold() != ".pdf"
        or any(part in {"", ".", ".."} for part in relative_path.split("/"))
    ):
        raise RepositorySyncError(
            "invalid_document_policy_path",
            "A local PDF policy has an unsafe path. OWL left the checkout unchanged.",
        )
    escaped = "".join(
        f"\\{character}" if character in "\\*?[]!#" else character for character in relative_path
    )
    # A slash anchors this exact path. A directory-only inclusion prevents a
    # future directory at the same PDF filename from suppressing its children.
    return f"!/{escaped}", f"/{escaped}/"


def apply_document_checkout_policy(
    repository: BitbucketRepository,
    *,
    repository_path: Path | None = None,
    heartbeat_callback: HeartbeatCallback | None = None,
) -> None:
    """Materialize selected documents while leaving frozen/deleted files out of Git's tree."""

    target = repository_path or _validated_existing_path(repository)
    excluded_paths = (
        PDFLocalPolicy.objects.filter(
            repository=repository,
            state__in=(PDFLocalPolicyState.EXCLUDED, PDFLocalPolicyState.DELETED),
        )
        .order_by("relative_path")
        .values_list("relative_path", flat=True)
    )
    patterns = list(_DOCUMENT_PATTERNS)
    for relative_path in excluded_paths:
        patterns.extend(_literal_document_policy_patterns(relative_path))
    log_event(
        logger,
        logging.DEBUG,
        "git_document_policy_selected",
        repository_id=repository.pk,
        skipped_count=(len(patterns) - len(_DOCUMENT_PATTERNS)) // 2,
    )
    _run_capture(
        ("git", "-C", str(target), "sparse-checkout", "set", "--no-cone", "--stdin"),
        failure_code="sparse_checkout_failed",
        failure_summary=(
            "Git could not restore the PDF and VSDX working tree safely. "
            "Check checkout permissions and local file conflicts, then retry."
        ),
        heartbeat_callback=heartbeat_callback,
        input_text="\n".join(patterns) + "\n",
    )


def _verify_origin(
    repository: BitbucketRepository,
    repository_path: Path,
    *,
    heartbeat_callback: HeartbeatCallback | None = None,
) -> None:
    origin = _git_value(
        repository_path,
        "remote",
        "get-url",
        "origin",
        code="invalid_origin",
        summary="OWL could not verify the repository's origin remote.",
        heartbeat_callback=heartbeat_callback,
    )
    if origin != repository.remote_url:
        raise RepositorySyncError(
            "origin_mismatch",
            "The local checkout points to a different remote. OWL left it unchanged.",
        )


def _branch(
    repository_path: Path,
    *,
    heartbeat_callback: HeartbeatCallback | None = None,
) -> str:
    branch = _git_value(
        repository_path,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        code="invalid_branch",
        summary="The repository is not on a named branch. OWL left it unchanged.",
        heartbeat_callback=heartbeat_callback,
    )
    _run_capture(
        ("git", "check-ref-format", "--branch", branch),
        failure_code="invalid_branch",
        failure_summary="The repository branch name is not safe to synchronize.",
        heartbeat_callback=heartbeat_callback,
    )
    return branch


def _commit(
    repository_path: Path,
    *,
    heartbeat_callback: HeartbeatCallback | None = None,
) -> str:
    return _git_value(
        repository_path,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        code="invalid_commit",
        summary="OWL could not verify the repository's current commit.",
        heartbeat_callback=heartbeat_callback,
    )


def _is_git_lfs_pointer(candidate: Path, *, size: int) -> bool:
    """Recognize a small canonical Git LFS v1 pointer without reading document data."""

    if size <= 0 or size > _GIT_LFS_POINTER_MAX_BYTES:
        return False
    try:
        content = candidate.read_bytes()
    except OSError as exc:
        log_event(logger, logging.ERROR, "git_lfs_pointer_read_failed", error=exc)
        raise _document_scan_error(exc, path=candidate) from exc
    normalized = content.replace(b"\r\n", b"\n")
    return (
        normalized.startswith(_GIT_LFS_POINTER_VERSION + b"\n")
        and _GIT_LFS_POINTER_OID.search(normalized) is not None
        and _GIT_LFS_POINTER_SIZE.search(normalized) is not None
    )


def _document_scan_error(error: OSError, *, path: Path | None = None) -> RepositorySyncError:
    """Classify local access failures without exposing paths or OS error text."""

    windows_error = getattr(error, "winerror", None)
    path_length = len(str(display_path(path))) if path is not None else 0
    if error.errno == errno.ENAMETOOLONG or windows_error == 206:
        return RepositorySyncError(
            "document_path_too_long",
            "A downloaded document path is too long. Use a shorter OWL media location "
            "or check Windows long-path support, then retry.",
        )
    if error.errno in {errno.EACCES, errno.EPERM} or windows_error in {5, 32, 33}:
        return RepositorySyncError(
            "document_access_denied",
            "OWL could not read a downloaded PDF/VSDX file or folder. Check permissions "
            "or another program locking the checkout, then retry.",
        )
    if error.errno in {errno.ENOENT, errno.ENOTDIR} or windows_error in {2, 3}:
        if windows_error in {2, 3} and path_length >= 260:
            return RepositorySyncError(
                "document_path_unavailable",
                "Windows could not find a downloaded document in a path of "
                f"{path_length} characters. This may be a long-path handling issue. "
                "Check Windows long-path support or use a shorter OWL media location, then retry.",
            )
        return RepositorySyncError(
            "document_missing",
            "A downloaded PDF/VSDX file or folder could not be found during scanning. "
            "Check storage availability and Windows long-path support, then retry.",
        )
    return RepositorySyncError(
        "document_scan_failed",
        "OWL could not read the downloaded document checkout. Check storage access and retry.",
    )


def _scan_documents(
    repository_path: Path,
    *,
    heartbeat_callback: HeartbeatCallback | None = None,
) -> tuple[DocumentStats, int]:
    """Count materialized documents and unresolved LFS pointers without following symlinks."""

    repository_path = filesystem_path(repository_path.resolve())
    pdf_count = 0
    vsdx_count = 0
    document_bytes = 0
    lfs_pointer_count = 0
    last_heartbeat_at = time.monotonic()

    def heartbeat_if_due() -> None:
        nonlocal last_heartbeat_at
        if heartbeat_callback is None:
            return
        observed_at = time.monotonic()
        if observed_at - last_heartbeat_at >= _HEARTBEAT_INTERVAL_SECONDS:
            heartbeat_callback()
            last_heartbeat_at = observed_at

    def directory_error(error: OSError) -> None:
        log_event(logger, logging.ERROR, "git_document_directory_scan_failed", error=error)
        path = Path(error.filename) if error.filename is not None else repository_path
        raise _document_scan_error(error, path=path) from error

    for directory, directory_names, filenames in os.walk(
        repository_path, followlinks=False, onerror=directory_error
    ):
        heartbeat_if_due()
        current = Path(directory)
        directory_names[:] = [
            name
            for name in directory_names
            if name != ".git"
            and not (current / name).is_symlink()
            and not (current / name).is_junction()
        ]
        for filename in filenames:
            heartbeat_if_due()
            candidate = current / filename
            suffix = candidate.suffix.casefold()
            if suffix not in {".pdf", ".vsdx"}:
                continue
            try:
                metadata = candidate.stat(follow_symlinks=False)
            except OSError as exc:
                log_event(logger, logging.ERROR, "git_document_stat_failed", error=exc)
                raise _document_scan_error(exc, path=candidate) from exc
            if not stat.S_ISREG(metadata.st_mode):
                continue
            if _is_git_lfs_pointer(candidate, size=metadata.st_size):
                lfs_pointer_count += 1
                continue
            if suffix == ".pdf":
                pdf_count += 1
            else:
                vsdx_count += 1
            document_bytes += metadata.st_size
    log_event(
        logger,
        logging.DEBUG,
        "git_document_scan_completed",
        pdf_count=pdf_count,
        vsdx_count=vsdx_count,
        byte_count=document_bytes,
        count=lfs_pointer_count,
    )
    return DocumentStats(pdf_count, vsdx_count, document_bytes), lfs_pointer_count


def _lfs_pointer_noun(pointer_count: int) -> str:
    return "file" if pointer_count == 1 else "files"


def _hydrate_git_lfs_documents(
    repository_path: Path,
    *,
    pointer_count: int,
    progress_callback: ProgressCallback,
) -> None:
    """Fetch and check out only PDF/VSDX LFS objects for the current branch."""

    noun = _lfs_pointer_noun(pointer_count)
    if shutil.which("git-lfs") is None:
        raise RepositorySyncError(
            "git_lfs_unavailable",
            (
                f"{pointer_count} PDF/VSDX {noun} remains Git LFS pointer content. "
                "Install Git LFS and authenticate it, then select Refresh; OWL will retry "
                "the document-only LFS download."
            ),
        )
    _run_streaming(
        (
            "git",
            "-C",
            str(repository_path),
            "lfs",
            "pull",
            f"--include={_GIT_LFS_DOCUMENT_INCLUDE}",
            "--exclude=",
        ),
        phase=RepositorySyncPhase.DISCOVERING,
        progress_start=94,
        progress_end=97,
        status_message=f"Downloading {pointer_count} PDF/VSDX Git LFS {noun}…",
        progress_callback=progress_callback,
        failure_code="git_lfs_download_failed",
        failure_summary=(
            "Git LFS could not download the PDF/VSDX objects. Check LFS authentication "
            "and object availability, then select Refresh to retry."
        ),
    )


def discover_documents(
    repository_path: Path,
    *,
    heartbeat_callback: HeartbeatCallback | None = None,
    progress_callback: ProgressCallback | None = None,
) -> DocumentStats:
    """Materialize remaining document LFS objects, then return safe document totals."""

    documents, pointer_count = _scan_documents(
        repository_path,
        heartbeat_callback=heartbeat_callback,
    )
    if not pointer_count:
        return documents

    if progress_callback is None:

        def progress_callback(_phase: str, _progress: int, _message: str) -> None:
            if heartbeat_callback is not None:
                heartbeat_callback()

    _hydrate_git_lfs_documents(
        repository_path,
        pointer_count=pointer_count,
        progress_callback=progress_callback,
    )
    progress_callback(
        RepositorySyncPhase.DISCOVERING,
        98,
        "Verifying downloaded PDF and VSDX Git LFS files…",
    )
    documents, remaining_pointer_count = _scan_documents(
        repository_path,
        heartbeat_callback=heartbeat_callback,
    )
    if remaining_pointer_count:
        noun = _lfs_pointer_noun(remaining_pointer_count)
        verb = "is" if remaining_pointer_count == 1 else "are"
        raise RepositorySyncError(
            "git_lfs_objects_unavailable",
            (
                f"{remaining_pointer_count} PDF/VSDX {noun} {verb} still Git LFS pointer "
                "content after retrieval. Verify the files are tracked by Git LFS and their "
                "objects exist, then select Refresh to retry."
            ),
        )
    return documents


def _ahead_behind(
    repository_path: Path,
    remote_branch: str,
    *,
    heartbeat_callback: HeartbeatCallback | None = None,
) -> tuple[int, int]:
    """Return commits unique to local HEAD and the fetched remote branch."""

    comparison = _git_value(
        repository_path,
        "rev-list",
        "--left-right",
        "--count",
        f"HEAD...{remote_branch}",
        "--",
        code="history_comparison_failed",
        summary="OWL could not compare local and remote repository history safely.",
        heartbeat_callback=heartbeat_callback,
    )
    try:
        local_only, remote_only = comparison.split()
        return int(local_only), int(remote_only)
    except (TypeError, ValueError) as exc:
        raise RepositorySyncError(
            "history_comparison_failed",
            "OWL could not compare local and remote repository history safely.",
        ) from exc


def _remove_owned_tree(resolved: Path, *, failure_event: str) -> None:
    """Remove one already-boundary-checked private tree without following links."""

    native_root = filesystem_path(resolved)

    def remove_readonly(function, path, error):
        if isinstance(error, FileNotFoundError):
            return
        candidate = Path(path)
        # Git object files can have Windows' read-only bit. Only clear that bit
        # for deletion of an entry in this exact OWL-owned tree; do not follow
        # links, repair arbitrary ACLs, or retry non-deletion operations.
        if (
            not _IS_WINDOWS
            or not isinstance(error, PermissionError)
            or function not in {os.unlink, os.remove, os.rmdir}
            or candidate.is_symlink()
            or candidate.is_junction()
            or not candidate.resolve(strict=False).is_relative_to(native_root)
        ):
            raise error
        metadata = candidate.stat(follow_symlinks=False)
        if metadata.st_mode & stat.S_IWRITE:
            raise error
        candidate.chmod(stat.S_IMODE(metadata.st_mode) | stat.S_IWRITE)
        function(path)

    try:
        shutil.rmtree(native_root, onexc=remove_readonly)
    except FileNotFoundError:
        pass
    except Exception as exc:
        # Cleanup is secondary. In particular, its PermissionError must not
        # replace a checkout, scan, or publication error reported to the user.
        log_event(
            logger,
            logging.ERROR,
            failure_event,
            error=exc,
            error_code="staging_cleanup_failed",
        )


def _remove_staging_directory(staging_path: Path) -> None:
    """Best-effort removal of this clone's private staging tree, never its error."""

    try:
        temp_root = Path(settings.BITBUCKET_TEMP_ROOT).resolve()
        if staging_path.is_symlink() or staging_path.is_junction():
            return
        resolved = staging_path.resolve(strict=False)
        if resolved.parent != temp_root or not re.fullmatch(
            r"repository-[0-9]+-[0-9a-f]{32}", resolved.name
        ):
            return
    except OSError as exc:
        log_event(
            logger,
            logging.ERROR,
            "git_staging_cleanup_failed",
            error=exc,
            error_code="staging_cleanup_failed",
        )
        return
    _remove_owned_tree(resolved, failure_event="git_staging_cleanup_failed")


def _remove_publish_recovery(recovery: Path, target_parent: Path) -> None:
    """Best-effort cleanup limited to this publication's private sibling tree."""

    try:
        expected_parent = target_parent.resolve()
        if recovery.is_symlink() or recovery.is_junction():
            return
        resolved = recovery.resolve(strict=False)
        if resolved.parent != expected_parent or not _PUBLISH_RECOVERY_NAME.fullmatch(
            resolved.name
        ):
            return
    except OSError as exc:
        log_event(
            logger,
            logging.ERROR,
            "git_checkout_publish_recovery_cleanup_failed",
            error=exc,
            error_code="staging_cleanup_failed",
        )
        return
    _remove_owned_tree(
        resolved,
        failure_event="git_checkout_publish_recovery_cleanup_failed",
    )


def _publish_wait(delay_seconds: float, heartbeat_callback: HeartbeatCallback) -> None:
    """Wait in heartbeat-sized slices so publication retries remain cancellable."""

    remaining = delay_seconds
    while remaining > 0:
        heartbeat_callback()
        delay = min(remaining, _HEARTBEAT_INTERVAL_SECONDS)
        time.sleep(delay)
        remaining -= delay
    heartbeat_callback()


def _is_transient_publish_error(error: OSError) -> bool:
    return _IS_WINDOWS and getattr(error, "winerror", None) in _WINDOWS_TRANSIENT_PUBLISH_ERRORS


def _replace_checkout_with_retries(
    source: Path,
    target: Path,
    *,
    heartbeat_callback: HeartbeatCallback,
) -> None:
    """Rename one private checkout into an absent target with bounded lock retries."""

    for attempt in range(len(_WINDOWS_PUBLISH_RETRY_DELAYS) + 1):
        if os.path.lexists(target):
            raise RepositorySyncError(
                "destination_exists",
                "The managed destination already exists. OWL left it unchanged.",
            )
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            if not _is_transient_publish_error(exc) or attempt == len(
                _WINDOWS_PUBLISH_RETRY_DELAYS
            ):
                raise
            if attempt == 0:
                emit_git_output(
                    "The managed-folder move is temporarily blocked; OWL is retrying it safely.",
                    operation="worker",
                    level="warning",
                )
            log_event(
                logger,
                logging.WARNING,
                "git_checkout_publish_retry",
                error=exc,
                retry_number=attempt + 1,
                retries_remaining=len(_WINDOWS_PUBLISH_RETRY_DELAYS) - attempt,
            )
            _publish_wait(_WINDOWS_PUBLISH_RETRY_DELAYS[attempt], heartbeat_callback)


def _copy_file_with_heartbeat(
    source: str | os.PathLike[str],
    target: str | os.PathLike[str],
    *,
    heartbeat_callback: HeartbeatCallback,
) -> str:
    """Copy a regular checkout file without leaving a long silent lease gap."""

    with open(source, "rb") as source_handle, open(target, "wb") as target_handle:
        while chunk := source_handle.read(_COPY_HEARTBEAT_BYTES):
            target_handle.write(chunk)
            heartbeat_callback()
    shutil.copystat(source, target, follow_symlinks=True)
    heartbeat_callback()
    return os.fspath(target)


def _recover_checkout_publication(
    staging: Path,
    target: Path,
    *,
    heartbeat_callback: HeartbeatCallback,
) -> None:
    """Copy to a private target-volume sibling, then publish that copy atomically."""

    recovery = target.parent / f".owl-checkout-publish-{uuid.uuid4().hex}"
    native_staging = filesystem_path(staging)
    native_recovery = filesystem_path(recovery)
    last_copy_heartbeat = 0.0

    def copy_heartbeat() -> None:
        nonlocal last_copy_heartbeat
        observed_at = time.monotonic()
        if observed_at - last_copy_heartbeat < _HEARTBEAT_INTERVAL_SECONDS:
            return
        heartbeat_callback()
        last_copy_heartbeat = observed_at

    try:
        if os.path.lexists(filesystem_path(target)):
            raise RepositorySyncError(
                "destination_exists",
                "The managed destination already exists. OWL left it unchanged.",
            )
        heartbeat_callback()
        shutil.copytree(
            native_staging,
            native_recovery,
            symlinks=True,
            copy_function=lambda source, destination: _copy_file_with_heartbeat(
                source,
                destination,
                heartbeat_callback=copy_heartbeat,
            ),
        )
        heartbeat_callback()
        _replace_checkout_with_retries(
            native_recovery,
            filesystem_path(target),
            heartbeat_callback=heartbeat_callback,
        )
    except BaseException:
        _remove_publish_recovery(recovery, target.parent)
        raise
    _remove_staging_directory(staging)


def _publish_staged_checkout(
    staging: Path,
    target: Path,
    *,
    heartbeat_callback: HeartbeatCallback,
) -> None:
    """Publish a clone with bounded lock retries and an atomic copy recovery."""

    native_staging = filesystem_path(staging)
    native_target = filesystem_path(target)
    try:
        native_target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        log_event(logger, logging.ERROR, "git_checkout_publish_root_failed", error=exc)
        raise RepositorySyncError(
            "checkout_publish_failed",
            "OWL could not prepare its managed repository folder. Check write and create-folder "
            "permissions for the OWL data folder, then retry.",
        ) from exc
    try:
        try:
            _replace_checkout_with_retries(
                native_staging,
                native_target,
                heartbeat_callback=heartbeat_callback,
            )
            return
        except OSError as direct_error:
            if direct_error.errno != errno.EXDEV and not _is_transient_publish_error(direct_error):
                raise
            reason = "cross_device" if direct_error.errno == errno.EXDEV else "transient_lock"
            log_event(
                logger,
                logging.WARNING,
                "git_checkout_publish_recovery_started",
                error=direct_error,
                reason=reason,
            )
            emit_git_output(
                "The direct checkout move was blocked; OWL is making a private recovery copy.",
                operation="worker",
                level="warning",
            )
            try:
                _recover_checkout_publication(
                    staging,
                    target,
                    heartbeat_callback=heartbeat_callback,
                )
            except RepositorySyncError:
                raise
            except OSError as recovery_error:
                log_event(
                    logger,
                    logging.ERROR,
                    "git_checkout_publish_recovery_failed",
                    error=recovery_error,
                    reason=reason,
                )
                log_event(
                    logger,
                    logging.ERROR,
                    "git_checkout_publish_failed",
                    error=recovery_error,
                    reason=reason,
                )
                if recovery_error.errno == errno.ENOSPC:
                    summary = (
                        "Git downloaded the checkout, but OWL did not have enough free space to "
                        "make its private publication copy. Free space in the OWL data folder, "
                        "then retry."
                    )
                elif reason == "cross_device":
                    summary = (
                        "OWL could not safely copy the downloaded checkout between the configured "
                        "drives. Check available space and write permissions, then retry."
                    )
                else:
                    summary = (
                        "Git downloaded the checkout, but Windows kept its folder locked after "
                        "OWL's bounded retries and private-copy recovery. Close Explorer preview "
                        "windows, PDF tools, sync tools, or scanners using the OWL data folder, "
                        "then retry."
                    )
                raise RepositorySyncError("checkout_publish_failed", summary) from recovery_error
            emit_git_output(
                "OWL recovered the checkout and published it to the managed folder.",
                operation="worker",
            )
            log_event(
                logger,
                logging.INFO,
                "git_checkout_publish_recovered",
                reason=reason,
            )
    except OSError as exc:
        log_event(logger, logging.ERROR, "git_checkout_publish_failed", error=exc)
        if exc.errno == errno.EXDEV:
            summary = (
                "OWL could not safely copy the downloaded checkout between the configured drives. "
                "Check available space and write permissions, then retry."
            )
        elif _is_transient_publish_error(exc):
            summary = (
                "Git downloaded the checkout, but Windows kept its folder locked after OWL's "
                "bounded retries and private-copy recovery. Close Explorer preview windows, PDF "
                "tools, sync tools, or scanners using the OWL data folder, then retry."
            )
        else:
            summary = (
                "Git downloaded the checkout, but OWL could not move it into its managed folder. "
                "Close programs locking that folder and check write/delete permissions, then retry."
            )
        raise RepositorySyncError("checkout_publish_failed", summary) from exc


def _clone(
    repository: BitbucketRepository,
    progress_callback: ProgressCallback,
) -> RepositorySyncResult:
    target = managed_repository_path(repository)
    if os.path.lexists(filesystem_path(target)):
        raise RepositorySyncError(
            "destination_exists",
            "The managed destination already exists but is not a valid completed checkout.",
        )
    _check_connection(repository, progress_callback)
    temp_root = Path(settings.BITBUCKET_TEMP_ROOT).resolve()
    filesystem_path(temp_root).mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = temp_root / f"repository-{repository.pk}-{uuid.uuid4().hex}"
    shallow_since = timezone.now().date() - timedelta(days=365 * settings.BITBUCKET_HISTORY_YEARS)
    try:
        preferred_clone_arguments = (
            "--no-checkout",
            "--single-branch",
            f"--shallow-since={shallow_since.isoformat()}",
            "--progress",
            "--",
            repository.remote_url,
            str(staging),
        )
        compatible_clone_arguments = preferred_clone_arguments
        last_resort_clone_arguments = (
            "--no-checkout",
            "--single-branch",
            "--depth=1",
            "--progress",
            "--",
            repository.remote_url,
            str(staging),
        )
        try:
            _run_streaming(
                ("git", "clone", "--filter=blob:none", *preferred_clone_arguments),
                phase=RepositorySyncPhase.CLONING,
                progress_start=5,
                progress_end=72,
                status_message="Downloading repository history in the background…",
                progress_callback=progress_callback,
                failure_code="clone_failed",
                failure_summary=(
                    "Git could not clone this repository. Check repository access, your SSH "
                    "agent or credential manager, and the configured host."
                ),
            )
        except RepositorySyncError as filtered_error:
            if filtered_error.code != "clone_failed":
                raise
            log_event(
                logger,
                logging.WARNING,
                "git_clone_compatibility_fallback",
                repository_id=repository.pk,
                reason="filtered_clone_failed",
            )
            _remove_staging_directory(staging)
            # A locked failed clone may survive best-effort cleanup on Windows.
            # Never retry into that partial directory or adopt its contents.
            staging = temp_root / f"repository-{repository.pk}-{uuid.uuid4().hex}"
            compatible_clone_arguments = (*compatible_clone_arguments[:-1], str(staging))
            progress_callback(
                RepositorySyncPhase.CLONING,
                8,
                "The server did not support partial clone; retrying with Git history…",
            )
            _check_connection(repository, progress_callback)
            try:
                _run_streaming(
                    ("git", "clone", *compatible_clone_arguments),
                    phase=RepositorySyncPhase.CLONING,
                    progress_start=8,
                    progress_end=72,
                    status_message="Downloading compatible repository history in the background…",
                    progress_callback=progress_callback,
                    failure_code="clone_failed",
                    failure_summary=filtered_error.summary,
                )
            except RepositorySyncError as history_error:
                if history_error.code != "clone_failed":
                    raise
                log_event(
                    logger,
                    logging.WARNING,
                    "git_clone_compatibility_fallback",
                    repository_id=repository.pk,
                    reason="dated_history_clone_failed",
                )
                _remove_staging_directory(staging)
                staging = temp_root / f"repository-{repository.pk}-{uuid.uuid4().hex}"
                last_resort_clone_arguments = (
                    *last_resort_clone_arguments[:-1],
                    str(staging),
                )
                progress_callback(
                    RepositorySyncPhase.CLONING,
                    10,
                    "Dated history is unavailable; downloading the latest revision…",
                )
                _check_connection(repository, progress_callback)
                _run_streaming(
                    ("git", "clone", *last_resort_clone_arguments),
                    phase=RepositorySyncPhase.CLONING,
                    progress_start=10,
                    progress_end=72,
                    status_message="Downloading a compatible checkout in the background…",
                    progress_callback=progress_callback,
                    failure_code="clone_failed",
                    failure_summary=history_error.summary,
                )
        clone_validation_heartbeat = _progress_heartbeat(
            progress_callback,
            phase=RepositorySyncPhase.CLONING,
            progress=72,
            status_message="Validating the downloaded repository…",
        )
        _verify_origin(
            repository,
            staging,
            heartbeat_callback=clone_validation_heartbeat,
        )
        branch = _branch(staging, heartbeat_callback=clone_validation_heartbeat)
        progress_callback(
            RepositorySyncPhase.UPDATING,
            76,
            "Preparing the PDF and VSDX working tree…",
        )
        sparse_checkout_heartbeat = _progress_heartbeat(
            progress_callback,
            phase=RepositorySyncPhase.UPDATING,
            progress=76,
            status_message="Preparing the PDF and VSDX working tree…",
        )
        _run_capture(
            ("git", "-C", str(staging), "sparse-checkout", "init", "--no-cone"),
            failure_code="sparse_checkout_failed",
            failure_summary="Git could not prepare the document-only working tree.",
            heartbeat_callback=sparse_checkout_heartbeat,
        )
        apply_document_checkout_policy(
            repository,
            repository_path=staging,
            heartbeat_callback=sparse_checkout_heartbeat,
        )
        _run_streaming(
            ("git", "-C", str(staging), "checkout", "-f", "HEAD"),
            phase=RepositorySyncPhase.UPDATING,
            progress_start=78,
            progress_end=90,
            status_message="Downloading PDF and VSDX files…",
            progress_callback=progress_callback,
            failure_code="checkout_failed",
            failure_summary="Git could not materialize the repository's PDF and VSDX files.",
        )
        source_commit = ""
        result_commit = _commit(
            staging,
            heartbeat_callback=_progress_heartbeat(
                progress_callback,
                phase=RepositorySyncPhase.UPDATING,
                progress=90,
                status_message="Validating the PDF and VSDX working tree…",
            ),
        )
        progress_callback(
            RepositorySyncPhase.DISCOVERING,
            93,
            "Counting downloaded PDF and VSDX files…",
        )
        documents = discover_documents(
            staging,
            heartbeat_callback=_progress_heartbeat(
                progress_callback,
                phase=RepositorySyncPhase.DISCOVERING,
                progress=93,
                status_message="Counting downloaded PDF and VSDX files…",
            ),
            progress_callback=progress_callback,
        )
        _publish_staged_checkout(
            staging,
            target,
            heartbeat_callback=_progress_heartbeat(
                progress_callback,
                phase=RepositorySyncPhase.DISCOVERING,
                progress=98,
                status_message="Finishing the downloaded repository checkout…",
            ),
        )
        return RepositorySyncResult(
            branch=branch,
            source_commit=source_commit,
            result_commit=result_commit,
            documents=documents,
        )
    except Exception:
        _remove_staging_directory(staging)
        raise


def _refresh(
    repository: BitbucketRepository,
    progress_callback: ProgressCallback,
) -> RepositorySyncResult:
    target = _validated_existing_path(repository)
    validation_heartbeat = _progress_heartbeat(
        progress_callback,
        phase=RepositorySyncPhase.VALIDATING,
        progress=3,
        status_message="Validating the managed repository checkout…",
    )
    _verify_origin(repository, target, heartbeat_callback=validation_heartbeat)
    branch = _branch(target, heartbeat_callback=validation_heartbeat)
    if repository.default_branch and branch != repository.default_branch:
        raise RepositorySyncError(
            "branch_mismatch",
            "The checkout branch changed. OWL left the repository unchanged.",
        )
    dirty = _git_value(
        target,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        code="status_failed",
        summary="OWL could not verify that the repository working tree is clean.",
        heartbeat_callback=validation_heartbeat,
    )
    if dirty:
        raise RepositorySyncError(
            "dirty_working_tree",
            "Local changes were found. OWL did not overwrite them; repair the checkout and retry.",
            blocked_dirty=True,
        )
    source_commit = _commit(target, heartbeat_callback=validation_heartbeat)
    _check_connection(repository, progress_callback, repository_path=target)
    fetch_arguments = ["git", "-C", str(target), "fetch", "--prune", "--progress"]
    is_shallow = _git_value(
        target,
        "rev-parse",
        "--is-shallow-repository",
        code="history_state_failed",
        summary="OWL could not determine the available Git history coverage.",
        heartbeat_callback=validation_heartbeat,
    ) == "true"
    if is_shallow:
        shallow_since = timezone.now().date() - timedelta(
            days=365 * settings.BITBUCKET_HISTORY_YEARS
        )
        fetch_arguments.append(f"--shallow-since={shallow_since.isoformat()}")
    fetch_arguments.append("origin")
    try:
        _run_streaming(
            tuple(fetch_arguments),
            phase=RepositorySyncPhase.FETCHING,
            progress_start=8,
            progress_end=70,
            status_message=(
                "Refreshing repository data and available author history…"
                if is_shallow
                else "Refreshing repository data in the background…"
            ),
            progress_callback=progress_callback,
            failure_code="fetch_failed",
            failure_summary=(
                "Git could not refresh this repository. Check repository access and try again."
            ),
        )
    except RepositorySyncError as history_error:
        if not is_shallow or history_error.code != "fetch_failed":
            raise
        log_event(
            logger,
            logging.WARNING,
            "git_fetch_history_fallback",
            repository_id=repository.pk,
            reason="dated_history_fetch_failed",
        )
        _run_streaming(
            ("git", "-C", str(target), "fetch", "--prune", "--progress", "origin"),
            phase=RepositorySyncPhase.FETCHING,
            progress_start=10,
            progress_end=70,
            status_message="Refreshing repository data with available server history…",
            progress_callback=progress_callback,
            failure_code="fetch_failed",
            failure_summary=history_error.summary,
        )
    fetched_validation_heartbeat = _progress_heartbeat(
        progress_callback,
        phase=RepositorySyncPhase.FETCHING,
        progress=70,
        status_message="Validating the refreshed repository data…",
    )
    dirty = _git_value(
        target,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        code="status_failed",
        summary="OWL could not recheck the repository working tree.",
        heartbeat_callback=fetched_validation_heartbeat,
    )
    if dirty:
        raise RepositorySyncError(
            "dirty_working_tree",
            "Local changes appeared during refresh. OWL left them unchanged.",
            blocked_dirty=True,
        )
    remote_branch = f"refs/remotes/origin/{branch}"
    local_only, remote_only = _ahead_behind(
        target,
        remote_branch,
        heartbeat_callback=fetched_validation_heartbeat,
    )
    if local_only:
        if remote_only:
            code = "history_diverged"
            summary = (
                "Local and remote repository history diverged. OWL left the checkout unchanged."
            )
        else:
            code = "local_commits_detected"
            summary = (
                "Local commits not present on the remote were found. "
                "OWL left the checkout unchanged."
            )
        raise RepositorySyncError(code, summary, blocked_dirty=True)
    _run_streaming(
        (
            "git",
            "-C",
            str(target),
            "merge",
            "--ff-only",
            "--no-stat",
            "--progress",
            "--",
            remote_branch,
        ),
        phase=RepositorySyncPhase.UPDATING,
        progress_start=72,
        progress_end=90,
        status_message="Updating the PDF and VSDX working tree…",
        progress_callback=progress_callback,
        failure_code="fast_forward_failed",
        failure_summary=(
            "The repository could not be fast-forwarded safely. OWL left the checkout unchanged."
        ),
    )
    # An earlier/interrupted sparse checkout can exclude tracked documents while
    # Git still reports a clean tree. A no-op merge does not materialize those
    # files, so restore OWL's selection without forcing or resetting local work.
    apply_document_checkout_policy(
        repository,
        repository_path=target,
        heartbeat_callback=_progress_heartbeat(
            progress_callback,
            phase=RepositorySyncPhase.UPDATING,
            progress=91,
            status_message="Restoring the PDF and VSDX working tree selection…",
        ),
    )
    result_commit = _commit(
        target,
        heartbeat_callback=_progress_heartbeat(
            progress_callback,
            phase=RepositorySyncPhase.UPDATING,
            progress=92,
            status_message="Validating the updated working tree…",
        ),
    )
    progress_callback(
        RepositorySyncPhase.DISCOVERING,
        93,
        "Counting downloaded PDF and VSDX files…",
    )
    documents = discover_documents(
        target,
        heartbeat_callback=_progress_heartbeat(
            progress_callback,
            phase=RepositorySyncPhase.DISCOVERING,
            progress=93,
            status_message="Counting downloaded PDF and VSDX files…",
        ),
        progress_callback=progress_callback,
    )
    return RepositorySyncResult(
        branch=branch,
        source_commit=source_commit,
        result_commit=result_commit,
        documents=documents,
    )


def synchronize_repository(
    repository: BitbucketRepository,
    *,
    operation: str,
    progress_callback: ProgressCallback,
    https_credential: GitHTTPSCredential | None = None,
) -> RepositorySyncResult:
    """Clone once or safely fast-forward an existing OWL-managed checkout."""

    started_at = time.monotonic()
    log_event(
        logger,
        logging.INFO,
        "git_repository_sync_started",
        repository_id=repository.pk,
        operation=operation,
    )
    try:
        with (
            _git_https_authorization(getattr(repository, "remote_url", ""), https_credential),
            logging_context(repository_id=repository.pk),
        ):
            result = _synchronize_repository(
                repository, operation=operation, progress_callback=progress_callback
            )
    except Exception as exc:
        log_event(
            logger,
            logging.ERROR,
            "git_repository_sync_failed",
            error=exc,
            repository_id=repository.pk,
            operation=operation,
            error_code=exc.code if isinstance(exc, RepositorySyncError) else "git_sync_error",
            elapsed_ms=round((time.monotonic() - started_at) * 1000),
        )
        raise
    log_event(
        logger,
        logging.INFO,
        "git_repository_sync_completed",
        repository_id=repository.pk,
        operation=operation,
        pdf_count=result.documents.pdf_count,
        vsdx_count=result.documents.vsdx_count,
        byte_count=result.documents.document_bytes,
        elapsed_ms=round((time.monotonic() - started_at) * 1000),
    )
    return result


def _synchronize_repository(
    repository: BitbucketRepository,
    *,
    operation: str,
    progress_callback: ProgressCallback,
) -> RepositorySyncResult:
    # Native document actions take this same cross-process lock without
    # waiting. Keeping it for the complete Git operation prevents a checkout
    # path from being replaced after OWL validates it but before the OS opens
    # it. Searches remain available from the last published database index.
    with repository_checkout_lock(repository.pk, blocking=True):
        progress_callback(
            RepositorySyncPhase.VALIDATING,
            2,
            "Validating the repository and private media destination…",
        )
        if operation == "clone":
            return _clone(repository, progress_callback)
        if operation == "refresh":
            return _refresh(repository, progress_callback)
        raise RepositorySyncError("invalid_operation", "OWL received an invalid sync operation.")
