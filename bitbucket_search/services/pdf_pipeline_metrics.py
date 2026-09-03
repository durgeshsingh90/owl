"""Low-overhead, redacted telemetry for OWL's durable PDF pipeline.

High-frequency samples live in one bounded process-local ring and an optional
atomically replaced snapshot below ``OWL_DATA_ROOT``.  SQLite stores only the
durable job boundaries that already matter to correctness; it is not used as a
per-second time-series database.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db.models import Case, Count, Exists, Max, Min, OuterRef, Q, Sum, Value, When
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFDocumentLifecycle,
    PDFExtractionJob,
    PDFExtractionJobPhase,
    PDFExtractionJobStatus,
    PDFPipelineCompletionKind,
    PDFPipelineRecovery,
    PDFPipelineRecoveryEvent,
    PDFPipelineRecoveryState,
    PDFPipelineRunState,
    PDFPipelineTuningEvent,
    RepositoryRemovalRecovery,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncPhase,
    RepositorySyncState,
)
from bitbucket_search.services.foreground_metrics import foreground_latency_snapshot
from bitbucket_search.services.pdf_jsonl_staging import staging_directory, staging_snapshot
from bitbucket_search.services.pdf_pipeline_controller import (
    ControllerEvaluationError,
    PDFPipelineController,
    controller_snapshot,
    observation_from_metrics,
)
from bitbucket_search.services.pdf_pipeline_runs import (
    TERMINAL_RUN_STATES,
    latest_current_run,
)
from bitbucket_search.services.pdf_runtime_metrics import publisher_runtime_snapshot

SCHEMA_VERSION = 1
SNAPSHOT_FILENAME = "metrics-v1.json"
_SERIES_ID = uuid.uuid4().hex
_SERIES_STARTED_AT = timezone.now()
_RING_LOCK = threading.RLock()
_MAX_RING_SAMPLES = 2_048
_SAMPLES: deque[dict[str, Any]] = deque()
_HISTORY_TRUNCATED = False
_DARWIN_MEMORY_CACHE_LOCK = threading.Lock()
_DARWIN_MEMORY_CACHE: tuple[float, int | None] | None = None

try:  # ``resource`` is unavailable on Windows.
    import resource as _resource
except ImportError:  # pragma: no cover - exercised by Windows CI.
    _resource = None

_ACTIVITY_LABELS = {
    "idle": "Idle",
    "queued": "Added to queue",
    "checking_connection": "Checking connection",
    "cloning": "Cloning",
    "pulling": "Pulling",
    "discovering": "Discovering PDFs",
    "validating": "Validating",
    "hashing": "Hashing",
    "extracting": "Extracting",
    "writing": "Writing",
    "extracting_and_writing": "Extracting + writing",
    "reusing_cached": "Reusing cached text",
    "backpressured": "Backpressured",
    "source_blocked": "Waiting for repository input",
    "retry_wait": "Waiting to retry",
    "recovering": "Recovering",
    "paused": "Paused",
    "completing": "Completing",
    "complete": "Complete",
    "completed_with_errors": "Completed with errors",
    "cancelled": "Cancelled",
}


def _configured_max_samples() -> int:
    sample_seconds = max(
        1,
        int(getattr(settings, "PDF_PIPELINE_METRICS_SAMPLE_SECONDS", 5)),
    )
    retention_seconds = max(
        sample_seconds,
        int(getattr(settings, "PDF_PIPELINE_METRICS_RETENTION_SECONDS", 1_800)),
    )
    return min(_MAX_RING_SAMPLES, max(12, math.ceil(retention_seconds / sample_seconds)))


def format_eta_duration(seconds: int | float | None) -> str | None:
    """Render an unbounded, nonnegative duration as ``HH:MM:SS``."""

    if seconds is None or isinstance(seconds, bool):
        return None
    try:
        rounded = max(0, int(math.ceil(float(seconds))))
    except (TypeError, ValueError, OverflowError):
        return None
    hours, remainder = divmod(rounded, 3_600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def monotonic_counter_delta(previous: int | None, current: int | None) -> int | None:
    """Return a safe counter delta; a reset starts a new series."""

    if previous is None or current is None or previous < 0 or current < 0:
        return None
    if current < previous:
        return None
    return current - previous


def percentile(values: Iterable[float], percentile_value: float) -> float | None:
    samples = sorted(float(value) for value in values)
    if not samples:
        return None
    percentile_value = max(0.0, min(float(percentile_value), 100.0))
    position = (len(samples) - 1) * percentile_value / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return samples[lower]
    fraction = position - lower
    return samples[lower] + (samples[upper] - samples[lower]) * fraction


def rolling_rate(
    event_times: Iterable[datetime],
    *,
    now: datetime,
    history_started_at: datetime,
    window_seconds: int = 60,
    minimum_partial_seconds: int = 30,
    minimum_partial_events: int = 3,
) -> dict[str, Any]:
    """Calculate one honest common-window event rate with warming semantics."""

    if timezone.is_naive(now) or timezone.is_naive(history_started_at):
        raise ValueError("Rate timestamps must include a timezone.")
    window = max(1, int(window_seconds))
    history_age = max(0.0, (now - history_started_at).total_seconds())
    elapsed = min(float(window), history_age)
    lower = max(history_started_at, now - timedelta(seconds=window))
    events = tuple(
        event
        for event in event_times
        if isinstance(event, datetime) and not timezone.is_naive(event) and lower <= event <= now
    )
    last_event_at = max(events).isoformat() if events else None
    complete_window = history_age >= window
    partial_ready = elapsed >= max(1, minimum_partial_seconds) and len(events) >= max(
        1, minimum_partial_events
    )
    if not complete_window and not partial_ready:
        return {
            "state": "warming",
            "confidence": "warming",
            "elapsedSeconds": round(elapsed, 3),
            "eventCount": len(events),
            "lastEventAt": last_event_at,
            "perSecond": None,
            "perMinute": None,
            "unavailableReason": "insufficient_elapsed_time_or_events",
            "asOf": now.isoformat(),
        }
    denominator = float(window) if complete_window else max(elapsed, 1.0)
    per_second = len(events) / denominator
    # A complete window with no events is strong evidence of a measured zero.
    # One or two events are real but necessarily low-sample observations.
    confidence = "high" if complete_window and len(events) not in {1, 2} else "low"
    return {
        "state": "available",
        "confidence": confidence,
        "elapsedSeconds": round(elapsed, 3),
        "eventCount": len(events),
        "lastEventAt": last_event_at,
        "perSecond": round(per_second, 6),
        "perMinute": round(per_second * 60, 2),
        "unavailableReason": None,
        "asOf": now.isoformat(),
    }


def _schedulable_cpu_count() -> int | None:
    try:
        if hasattr(os, "sched_getaffinity"):
            affinity_count = len(os.sched_getaffinity(0))
            if affinity_count > 0:
                return affinity_count
    except (OSError, TypeError):
        pass
    count = os.cpu_count()
    return count if isinstance(count, int) and count > 0 else None


def _memory_total_bytes() -> tuple[int | None, str | None]:
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        total_pages = int(os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, TypeError, ValueError):
        return None, None
    value = page_size * total_pages
    return (value, "posix_sysconf") if value > 0 else (None, None)


def _darwin_memory_available_bytes() -> int | None:
    """Read reclaimable memory from Apple's bounded ``vm_stat`` output."""

    global _DARWIN_MEMORY_CACHE
    sampled_monotonic = time.monotonic()
    with _DARWIN_MEMORY_CACHE_LOCK:
        if _DARWIN_MEMORY_CACHE is not None and sampled_monotonic - _DARWIN_MEMORY_CACHE[0] < 5.0:
            return _DARWIN_MEMORY_CACHE[1]
    try:
        result = subprocess.run(
            ["/usr/bin/vm_stat"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1,
            env={"LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        value = None
    else:
        output = result.stdout if result.returncode == 0 else ""
        page_match = re.search(r"page size of ([0-9]+) bytes", output)
        counters = {
            name: int(match.group(1))
            for name in ("Pages free", "Pages inactive", "Pages speculative")
            if (
                match := re.search(
                    rf"(?m)^{re.escape(name)}:\s*([0-9]+)\.\s*$",
                    output,
                )
            )
        }
        value = (
            int(page_match.group(1)) * sum(counters.values())
            if page_match is not None and len(counters) == 3
            else None
        )
    with _DARWIN_MEMORY_CACHE_LOCK:
        _DARWIN_MEMORY_CACHE = (sampled_monotonic, value)
    return value


def _memory_available_bytes() -> tuple[int | None, str | None]:
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        available_pages = int(os.sysconf("SC_AVPHYS_PAGES"))
    except (AttributeError, OSError, TypeError, ValueError):
        if sys.platform == "darwin":
            value = _darwin_memory_available_bytes()
            return (value, "darwin_vm_stat") if value is not None else (None, None)
        return None, None
    value = page_size * available_pages
    return (value, "posix_sysconf") if value >= 0 else (None, None)


def _process_peak_rss_bytes() -> int | None:
    if _resource is None:
        return None
    try:
        value = int(_resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss)
    except (OSError, TypeError, ValueError):
        return None
    # Darwin reports bytes; Linux and the BSDs traditionally report KiB.
    if sys.platform == "darwin":
        return max(0, value)
    return max(0, value * 1_024)


def _process_table_snapshot() -> tuple[float, float, int] | None:
    """Return normalized host/tree CPU and current tree RSS from bounded ``ps`` output."""

    if os.name == "nt":
        return None
    try:
        result = subprocess.run(
            ("ps", "-axo", "pid=,ppid=,%cpu=,rss="),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1,
            env={"LC_ALL": "C", "PATH": os.environ.get("PATH", "")},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or len(result.stdout) > 8_000_000:
        return None
    rows: list[tuple[int, int, float, int]] = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 4:
            continue
        try:
            pid, parent_pid = int(fields[0]), int(fields[1])
            cpu_pct, rss_kib = float(fields[2]), int(fields[3])
        except (TypeError, ValueError):
            continue
        if pid > 0 and parent_pid >= 0 and math.isfinite(cpu_pct) and rss_kib >= 0:
            rows.append((pid, parent_pid, max(0.0, cpu_pct), rss_kib))
    if not rows:
        return None
    descendants = {os.getpid()}
    changed = True
    while changed:
        changed = False
        for pid, parent_pid, _cpu_pct, _rss_kib in rows:
            if parent_pid in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    cpu_count = _schedulable_cpu_count() or 1
    host_cpu = min(100.0, sum(row[2] for row in rows) / cpu_count)
    tree_cpu = min(100.0, sum(row[2] for row in rows if row[0] in descendants) / cpu_count)
    tree_rss = sum(row[3] * 1_024 for row in rows if row[0] in descendants)
    return round(host_cpu, 2), round(tree_cpu, 2), tree_rss


def _semantic_resource_snapshot(
    resident_roles: Mapping[str, object] | None,
) -> tuple[int, int, int, int] | None:
    """Return configured/live/active/backlog semantic counts when the app is available."""

    try:
        from semantic_search.models import SemanticIndexJob, SemanticIndexJobStatus
    except (ImportError, LookupError):
        return None
    live_roles, _exited = _live_roles(resident_roles)
    configured = (
        int(getattr(settings, "SEMANTIC_MAX_WORKERS", 0))
        if bool(getattr(settings, "SEMANTIC_SEARCH_ENABLED", False))
        else 0
    )
    live = sum(1 for role in live_roles if role.startswith("semantic-index-"))
    try:
        active = SemanticIndexJob.objects.filter(status=SemanticIndexJobStatus.RUNNING).count()
        backlog = SemanticIndexJob.objects.filter(
            status__in=(SemanticIndexJobStatus.QUEUED, SemanticIndexJobStatus.RUNNING)
        ).count()
    except Exception:
        return None
    return configured, live, active, backlog


def resource_snapshot(*, resident_roles: Mapping[str, object] | None = None) -> dict[str, Any]:
    """Collect portable signals and mark unsupported host gauges unavailable."""

    availability: dict[str, str] = {}
    cpu_count = _schedulable_cpu_count()
    memory_total, memory_total_source = _memory_total_bytes()
    memory_available, memory_available_source = _memory_available_bytes()
    peak_rss = _process_peak_rss_bytes()
    process_table = _process_table_snapshot() if resident_roles is not None else None
    semantic = _semantic_resource_snapshot(resident_roles)
    live_roles, _exited_roles = _live_roles(resident_roles)
    try:
        disk_available = shutil.disk_usage(Path(settings.OWL_DATA_ROOT)).free
    except OSError:
        disk_available = None
    values = {
        "schedulableCpuCount": cpu_count,
        "hostCpuPct": process_table[0] if process_table is not None else None,
        "owlProcessTreeCpuPct": process_table[1] if process_table is not None else None,
        "hostMemoryTotalBytes": memory_total,
        "hostMemoryUsedPct": (
            round(
                max(0.0, min(100.0, (memory_total - memory_available) * 100 / memory_total)),
                2,
            )
            if memory_total and memory_available is not None
            else None
        ),
        "hostMemoryAvailableBytes": memory_available,
        # ``ru_maxrss`` is a high-water mark for this process only.  It must
        # never be presented as current RSS for OWL's whole process tree.
        "owlProcessTreeRssBytes": process_table[2] if process_table is not None else None,
        "owlCurrentProcessPeakRssBytes": peak_rss,
        "diskAvailableBytes": disk_available,
        "semanticWorkerTarget": semantic[0] if semantic is not None else None,
        "semanticWorkersLive": semantic[1] if semantic is not None else None,
        "semanticWorkersActive": semantic[2] if semantic is not None else None,
        "semanticBacklogJobs": semantic[3] if semantic is not None else None,
        "gitWorkersActive": sum(1 for role in live_roles if role.startswith("repository-sync-"))
        if resident_roles is not None
        else None,
        "thermalState": None,
    }
    for key, value in values.items():
        if value is None:
            availability[key] = "platform_signal_unavailable"
    if process_table is None:
        availability["owlProcessTreeRssBytes"] = "process_tree_sampler_unavailable"
    values["availability"] = availability
    values["sources"] = {
        "hostMemoryTotalBytes": memory_total_source,
        "hostMemoryAvailableBytes": memory_available_source,
        "owlCurrentProcessPeakRssBytes": "getrusage_peak",
        "hostCpuPct": "ps_process_table_normalized_by_schedulable_cpu"
        if process_table is not None
        else None,
        "owlProcessTreeCpuPct": "ps_descendant_sum_normalized_by_schedulable_cpu"
        if process_table is not None
        else None,
        "owlProcessTreeRssBytes": "ps_descendant_rss_sum" if process_table is not None else None,
    }
    return values


def _stage_files() -> tuple[int, int | None]:
    root = staging_directory()
    try:
        files = tuple(
            path
            for path in root.iterdir()
            if path.is_file()
            and (
                path.name.startswith("job-")
                or path.name == "current.jsonl"
                or (path.name.startswith("chunk_") and path.name.endswith(".jsonl"))
            )
        )
    except (FileNotFoundError, OSError):
        return 0, None
    total_bytes = 0
    oldest_ns: int | None = None
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        total_bytes += max(0, stat.st_size)
        oldest_ns = stat.st_mtime_ns if oldest_ns is None else min(oldest_ns, stat.st_mtime_ns)
    oldest_age = (
        max(0, int(time.time() - oldest_ns / 1_000_000_000)) if oldest_ns is not None else None
    )
    return total_bytes, oldest_age


def _eligible_input_snapshot() -> tuple[int, datetime | None]:
    """Count globally claimable work without taking the claimant's write gate."""

    active = PDFExtractionJob.objects.exclude(
        document__repository_id__in=RepositoryRemovalRecovery.objects.values("repository_id")
    ).filter(status=PDFExtractionJobStatus.RUNNING)
    running_by_repository = tuple(
        active.exclude(phase=PDFExtractionJobPhase.PUBLISHING)
        .order_by()
        .values("document__repository_id")
        .annotate(total=Count("id"))
    )
    active_sync = RepositorySyncJob.objects.filter(
        repository_id=OuterRef("document__repository_id"),
        status__in=(RepositorySyncJobStatus.QUEUED, RepositorySyncJobStatus.RUNNING),
    )
    repository_running = Case(
        *(
            When(
                document__repository_id=row["document__repository_id"],
                then=Value(row["total"]),
            )
            for row in running_by_repository
        ),
        default=Value(0),
    )
    candidates = (
        PDFExtractionJob.objects.exclude(
            document__repository_id__in=RepositoryRemovalRecovery.objects.values("repository_id")
        )
        .filter(
            status=PDFExtractionJobStatus.QUEUED,
            document__lifecycle_state=PDFDocumentLifecycle.ACTIVE,
            document__repository__enabled=True,
            document__repository__sync_state=RepositorySyncState.READY,
            document__local_policy__isnull=True,
        )
        .annotate(
            repository_sync_active=Exists(active_sync),
            repository_running=repository_running,
        )
        .filter(
            repository_sync_active=False,
            repository_running__lt=int(settings.PDF_MAX_EXTRACTION_WORKERS_PER_REPOSITORY),
        )
    )
    result = candidates.aggregate(total=Count("id"), oldest=Min("requested_at"))
    return int(result["total"] or 0), result["oldest"]


def _sample_datetime(sample: Mapping[str, Any]) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(sample.get("at", "")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return value if not timezone.is_naive(value) else None


def _gauge_growth_rate(
    field: str,
    current: int | float | None,
    *,
    now: datetime,
    window_seconds: int,
) -> float | None:
    if current is None:
        return None
    cutoff = now - timedelta(seconds=max(1, int(window_seconds)))
    with _RING_LOCK:
        candidates = tuple(_SAMPLES)
    for sample in candidates:
        sampled_at = _sample_datetime(sample)
        previous = sample.get(field)
        if (
            sampled_at is None
            or sampled_at < cutoff
            or sampled_at >= now
            or not isinstance(previous, (int, float))
            or isinstance(previous, bool)
        ):
            continue
        elapsed = (now - sampled_at).total_seconds()
        if elapsed > 0:
            return round((float(current) - float(previous)) / elapsed, 6)
    return None


def _queue_snapshot(now: datetime) -> dict[str, Any]:
    fresh_cutoff = now - timedelta(
        seconds=int(getattr(settings, "PDF_PIPELINE_METRICS_STALE_SECONDS", 15))
    )
    active = PDFExtractionJob.objects.filter(
        status__in=(PDFExtractionJobStatus.QUEUED, PDFExtractionJobStatus.RUNNING)
    )
    queued = active.filter(status=PDFExtractionJobStatus.QUEUED)
    extracting_phases = (
        PDFExtractionJobPhase.VALIDATING,
        PDFExtractionJobPhase.HASHING,
        PDFExtractionJobPhase.EXTRACTING,
    )
    active_extracting = active.filter(
        status=PDFExtractionJobStatus.RUNNING,
        phase__in=extracting_phases,
        heartbeat_at__gte=fresh_cutoff,
    )
    staged = active.filter(
        status=PDFExtractionJobStatus.RUNNING,
        phase=PDFExtractionJobPhase.PUBLISHING,
    )
    publication_in_flight = staged.filter(
        worker_pid__isnull=False,
        heartbeat_at__gte=fresh_cutoff,
    )
    staged_waiting = staged.exclude(pk__in=publication_in_flight.values("pk"))
    staged_bytes, oldest_stage_age = _stage_files()
    jsonl = staging_snapshot(now=now)
    eligible_input, oldest_eligible = _eligible_input_snapshot()
    window = int(getattr(settings, "PDF_PIPELINE_RATE_WINDOW_SECONDS", 60))
    depth = staged.count()
    depth_growth = _gauge_growth_rate(
        "backpressureDepthJobs", depth, now=now, window_seconds=window
    )
    bytes_growth = _gauge_growth_rate("stagedBytes", staged_bytes, now=now, window_seconds=window)
    return {
        "inputQueuedJobs": queued.count(),
        "eligibleInputJobs": eligible_input,
        "activeExtractionJobs": active_extracting.count(),
        "stagedWaitingJobs": staged_waiting.count(),
        "publicationInFlightJobs": publication_in_flight.count(),
        "backpressureDepthJobs": depth,
        "backpressureThresholdJobs": int(settings.PDF_MAX_STAGED_PUBLICATIONS),
        "stagedBytes": staged_bytes,
        "backpressureDepthGrowthPerSecond": depth_growth,
        "stagedBytesGrowthPerSecond": bytes_growth,
        "growthWindowSeconds": window,
        "oldestEligibleWaitSeconds": (
            max(0, round((now - oldest_eligible).total_seconds(), 3)) if oldest_eligible else None
        ),
        "oldestStagedWaitSeconds": oldest_stage_age,
        "jsonl": jsonl,
        "availability": {
            **(
                {"backpressureDepthGrowth": "insufficient_sample_history"}
                if depth_growth is None
                else {}
            ),
            **(
                {"stagedBytesGrowth": "insufficient_sample_history"} if bytes_growth is None else {}
            ),
        },
    }


def _live_roles(resident_roles: Mapping[str, object] | None) -> tuple[set[str], set[str]]:
    if resident_roles is None:
        return set(), set()
    live: set[str] = set()
    exited: set[str] = set()
    for role, process in resident_roles.items():
        try:
            running = process.poll() is None
        except (AttributeError, OSError):
            running = False
        (live if running else exited).add(str(role))
    return live, exited


def _worker_snapshot(
    queue: Mapping[str, Any],
    *,
    resident_roles: Mapping[str, object] | None,
    requested_target: int | None = None,
) -> dict[str, Any]:
    expected = int(settings.PDF_MAX_EXTRACTION_WORKERS)
    requested = min(
        expected,
        max(
            0,
            int(
                requested_target
                if requested_target is not None
                else getattr(settings, "PDF_PIPELINE_INITIAL_TARGET", expected)
            ),
        ),
    )
    live_roles, _exited = _live_roles(resident_roles)
    live_extractors = {role for role in live_roles if role.startswith("pdf-index-")}
    live_count = min(expected, len(live_extractors))
    backpressured = 0
    waiting = 0
    idle = 0
    paused_controller = max(0, live_count - requested)
    admitted_live = max(0, live_count - paused_controller)
    active = min(admitted_live, int(queue["activeExtractionJobs"]))
    remaining_live = max(0, admitted_live - active)
    if int(queue["inputQueuedJobs"]) > 0:
        waiting = remaining_live
    else:
        idle = remaining_live
    paused_scopes = tuple(
        PDFPipelineRecovery.objects.filter(
            Q(scope__in=("pipeline", "controller", "extraction_pool"))
            | Q(scope__startswith="extraction_slot:"),
            state=PDFPipelineRecoveryState.PAUSED,
        ).values_list("scope", flat=True)
    )
    non_live_slots = max(0, expected - live_count)
    if {"pipeline", "controller", "extraction_pool"} & set(paused_scopes):
        paused_recovery = non_live_slots
    else:
        paused_recovery = min(
            non_live_slots,
            len({scope for scope in paused_scopes if scope.startswith("extraction_slot:")}),
        )
    unavailable = max(0, expected - live_count - paused_recovery)
    process_snapshot_fresh = resident_roles is not None
    return {
        "expectedResident": expected,
        "live": live_count,
        "admitted": admitted_live,
        "active": active,
        "occupancyPct": round(active * 100 / admitted_live, 1) if admitted_live else None,
        "idleNoDemand": idle,
        "waitingForEligibleInput": waiting,
        "backpressured": backpressured,
        "pausedByController": paused_controller,
        "pausedByRecovery": paused_recovery,
        "unavailable": unavailable,
        "processSnapshotFresh": process_snapshot_fresh,
        "heartbeatFresh": None,
        "aggregateReason": (
            "fresh_supervisor_process_snapshot"
            if process_snapshot_fresh
            else "supervisor_process_snapshot_unavailable"
        ),
        "availability": {
            "controllerHeartbeat": "dedicated_controller_heartbeat_not_instrumented",
            **(
                {}
                if resident_roles is not None
                else {"processState": "supervisor_snapshot_unavailable"}
            ),
        },
    }


def _publisher_duty_cycle(
    current_state: str,
    *,
    now: datetime,
    window_seconds: int,
) -> dict[str, Any]:
    """Partition measured live publisher time across mutually exclusive states."""

    live_states = ("busy", "starved", "idle_no_demand", "blocked")
    cutoff = now - timedelta(seconds=max(1, int(window_seconds)))
    with _RING_LOCK:
        prior = tuple(_SAMPLES)
    points: list[tuple[datetime, str]] = []
    for sample in prior:
        sampled_at = _sample_datetime(sample)
        state = str(sample.get("publisherState", ""))
        if sampled_at is not None and cutoff <= sampled_at < now and state in live_states:
            points.append((sampled_at, state))
    points.sort(key=lambda item: item[0])
    if current_state in live_states:
        points.append((now, current_state))
    totals = {state: 0.0 for state in live_states}
    for index in range(len(points) - 1):
        sampled_at, state = points[index]
        next_at = points[index + 1][0]
        totals[state] += max(0.0, (next_at - sampled_at).total_seconds())
    measured_seconds = sum(totals.values())
    if measured_seconds <= 0:
        return {
            "busyPct": None,
            "starvedPct": None,
            "noDemandPct": None,
            "blockedPct": None,
            "dutyCycleWindowSeconds": window_seconds,
            "dutyCycleMeasuredSeconds": 0,
            "dutyCycleSampleCount": len(points),
        }
    percentages = {state: round(totals[state] * 100 / measured_seconds, 2) for state in live_states}
    # Keep the public partition exact after decimal rounding.
    largest = max(live_states, key=lambda state: totals[state])
    percentages[largest] = round(percentages[largest] + 100.0 - sum(percentages.values()), 2)
    return {
        "busyPct": percentages["busy"],
        "starvedPct": percentages["starved"],
        "noDemandPct": percentages["idle_no_demand"],
        "blockedPct": percentages["blocked"],
        "dutyCycleWindowSeconds": window_seconds,
        "dutyCycleMeasuredSeconds": round(measured_seconds, 3),
        "dutyCycleSampleCount": len(points),
    }


def _publisher_snapshot(
    queue: Mapping[str, Any],
    *,
    now: datetime | None = None,
    resident_roles: Mapping[str, object] | None,
) -> dict[str, Any]:
    live_roles, _exited = _live_roles(resident_roles)
    live = "pdf-writer-1" in live_roles
    window = int(getattr(settings, "PDF_PIPELINE_RATE_WINDOW_SECONDS", 60))
    runtime = publisher_runtime_snapshot(window_seconds=window)
    jsonl = queue.get("jsonl") if isinstance(queue.get("jsonl"), Mapping) else {}
    lock_blocked_ms = int(getattr(settings, "PDF_PIPELINE_SQLITE_LOCK_BLOCKED_MS", 250))
    lock_contended = bool(
        (runtime["sqliteBusyErrors"] or 0) > 0
        or (
            runtime["sqliteLockWaitP95Ms"] is not None
            and runtime["sqliteLockWaitP95Ms"] >= lock_blocked_ms
        )
    )
    paused = PDFPipelineRecovery.objects.filter(
        scope__in=("publisher", "pipeline"),
        state=PDFPipelineRecoveryState.PAUSED,
    ).exists()
    if paused:
        state = "paused_by_recovery"
    elif not live:
        state = "unavailable"
    elif lock_contended and jsonl.get("queuedChunks"):
        state = "blocked"
    elif jsonl.get("writerState") == "WRITING" or jsonl.get("sealedChunksWaiting"):
        state = "busy"
    elif (
        jsonl.get("currentSizeBytes")
        or jsonl.get("incomingRecords")
        or queue["activeExtractionJobs"]
        or queue["inputQueuedJobs"]
    ):
        state = "starved"
    else:
        state = "idle_no_demand"
    duty_cycle = _publisher_duty_cycle(
        state,
        now=now or timezone.now(),
        window_seconds=window,
    )
    return {
        "live": live,
        "expected": True,
        "heartbeatFresh": (
            bool(queue["publicationInFlightJobs"])
            if resident_roles is not None and queue["publicationInFlightJobs"]
            else None
        ),
        "state": state,
        "reasonCode": "blocked_sqlite" if state == "blocked" and lock_contended else None,
        "writerState": jsonl.get("writerState", "IDLE"),
        "currentChunk": jsonl.get("currentChunk"),
        **duty_cycle,
        **{key: value for key, value in runtime.items() if key != "availability"},
        "sqliteLockBlockedThresholdMs": lock_blocked_ms,
        "availability": {
            **(
                {"dutyCycle": "insufficient_sample_history"}
                if duty_cycle["dutyCycleMeasuredSeconds"] <= 0
                else {}
            ),
            **runtime["availability"],
        },
    }


def _recovery_snapshot() -> dict[str, Any]:
    recovery = (
        PDFPipelineRecovery.objects.exclude(state=PDFPipelineRecoveryState.HEALTHY)
        .order_by("-paused_at", "-last_failure_at", "scope")
        .first()
    )
    threshold = int(getattr(settings, "PDF_PIPELINE_RECOVERY_PAUSE_AFTER_ATTEMPTS", 25))
    if recovery is None:
        return {
            "state": "healthy",
            "halfOpen": False,
            "episodeId": None,
            "generation": 0,
            "pauseGeneration": 0,
            "scope": None,
            "reasonCode": None,
            "consecutiveFailedAttempts": 0,
            "lifetimeAttempts": 0,
            "pauseAfterAttempts": threshold,
            "lastAttemptAt": None,
            "nextRetryAt": None,
            "pausedAt": None,
            "resumable": False,
            "resumeSafety": "not_applicable",
            "resumeBlockedReason": None,
            "resumeAction": None,
        }
    # Import locally because the recovery preflight reads resource telemetry.
    # The action itself is fixed, same-origin, HMAC-bound and server produced.
    from bitbucket_search.services.pdf_recovery import recovery_payload

    payload = recovery_payload(recovery)
    if payload["resumable"]:
        payload["resumeSafety"] = "requires_preflight"
    return payload


def _recovery_events() -> list[dict[str, Any]]:
    """Return bounded transition history without exporting stored free-form text."""

    return [
        {
            "id": str(event.event_id),
            "correlationId": (
                str(event.correlation_id) if event.correlation_id is not None else None
            ),
            "at": event.occurred_at.isoformat(),
            "scope": event.recovery.scope,
            "kind": event.kind,
            "reasonCode": event.reason_code or None,
            "outcome": "Recorded recovery transition.",
            "generation": event.generation,
            "pauseGeneration": event.pause_generation,
        }
        for event in PDFPipelineRecoveryEvent.objects.select_related("recovery").order_by(
            "-occurred_at", "-id"
        )[:50]
    ]


def _pipeline_count_snapshot() -> dict[str, int]:
    """Expose stable corpus/job totals without treating retries as hidden loss."""

    active_documents = PDFDocument.objects.filter(lifecycle_state=PDFDocumentLifecycle.ACTIVE)
    active_jobs = PDFExtractionJob.objects.filter(
        document__lifecycle_state=PDFDocumentLifecycle.ACTIVE
    )
    extracting_phases = (
        PDFExtractionJobPhase.VALIDATING,
        PDFExtractionJobPhase.HASHING,
        PDFExtractionJobPhase.EXTRACTING,
    )
    return {
        "pdfsDiscovered": active_documents.count(),
        "pdfsPendingExtraction": active_jobs.filter(status=PDFExtractionJobStatus.QUEUED).count(),
        "pdfsCurrentlyExtracting": active_jobs.filter(
            status=PDFExtractionJobStatus.RUNNING,
            phase__in=extracting_phases,
        ).count(),
        "pdfsSuccessfullyExtracted": active_jobs.filter(staged_at__isnull=False)
        .order_by()
        .values("document_id")
        .distinct()
        .count(),
        "extractionFailures": active_jobs.filter(
            status__in=(PDFExtractionJobStatus.FAILED, PDFExtractionJobStatus.INTERRUPTED)
        ).count(),
        "pdfsSuccessfullyWritten": active_documents.filter(indexed_revision__isnull=False).count(),
    }


def _rate_event_rows(now: datetime, window_seconds: int) -> tuple[dict[str, Any], ...]:
    lower = max(_SERIES_STARTED_AT, now - timedelta(seconds=window_seconds))
    return tuple(
        PDFExtractionJob.objects.filter(
            Q(staged_at__gte=lower, staged_at__lte=now)
            | Q(published_at__gte=lower, published_at__lte=now)
            | Q(completed_at__gte=lower, completed_at__lte=now)
        )
        .values(
            "status",
            "error_code",
            "target_file_size",
            "pages_processed",
            "characters_extracted",
            "requested_at",
            "started_at",
            "staged_at",
            "publication_started_at",
            "published_at",
            "completed_at",
            "completion_kind",
            "retry_count",
            "phase",
        )
        .iterator(chunk_size=256)
    )


def _rate_event_times(
    now: datetime, window_seconds: int
) -> tuple[tuple[datetime, ...], tuple[datetime, ...], int]:
    lower = max(_SERIES_STARTED_AT, now - timedelta(seconds=window_seconds))
    rows = _rate_event_rows(now, window_seconds)
    extracted = tuple(
        row["staged_at"]
        for row in rows
        if row["staged_at"] is not None and lower <= row["staged_at"] <= now
    )
    written = tuple(
        row["published_at"]
        for row in rows
        if row["completion_kind"] == PDFPipelineCompletionKind.NORMAL_PUBLICATION
        and row["published_at"] is not None
        and lower <= row["published_at"] <= now
    )
    cache_reuse = sum(
        1
        for row in rows
        if row["completion_kind"] == PDFPipelineCompletionKind.CACHE_REUSE
        and row["published_at"] is not None
        and lower <= row["published_at"] <= now
    )
    return extracted, written, cache_reuse


def _duration_ms(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None or end < start:
        return None
    return max(0.0, (end - start).total_seconds() * 1_000)


def _latency_summary(values: Iterable[float]) -> dict[str, float | int | None]:
    samples = tuple(float(value) for value in values if math.isfinite(float(value)))
    if not samples:
        return {"sampleCount": 0, "meanMs": None, "p50Ms": None, "p95Ms": None}
    return {
        "sampleCount": len(samples),
        "meanMs": round(sum(samples) / len(samples), 3),
        "p50Ms": round(percentile(samples, 50) or 0.0, 3),
        "p95Ms": round(percentile(samples, 95) or 0.0, 3),
    }


def _throughput_snapshot(now: datetime) -> dict[str, Any]:
    window = int(getattr(settings, "PDF_PIPELINE_RATE_WINDOW_SECONDS", 60))
    lower = max(_SERIES_STARTED_AT, now - timedelta(seconds=window))
    rows = _rate_event_rows(now, window)
    extracted_rows = tuple(
        row for row in rows if row["staged_at"] is not None and lower <= row["staged_at"] <= now
    )
    written_rows = tuple(
        row
        for row in rows
        if row["completion_kind"] == PDFPipelineCompletionKind.NORMAL_PUBLICATION
        and row["published_at"] is not None
        and lower <= row["published_at"] <= now
    )
    cache_rows = tuple(
        row
        for row in rows
        if row["completion_kind"] == PDFPipelineCompletionKind.CACHE_REUSE
        and row["published_at"] is not None
        and lower <= row["published_at"] <= now
    )
    failed_rows = tuple(
        row
        for row in rows
        if row["status"] in {PDFExtractionJobStatus.FAILED, PDFExtractionJobStatus.INTERRUPTED}
        and row["completed_at"] is not None
        and lower <= row["completed_at"] <= now
    )
    extracted_times = tuple(row["staged_at"] for row in extracted_rows)
    written_times = tuple(row["published_at"] for row in written_rows)
    arguments = {
        "now": now,
        "history_started_at": _SERIES_STARTED_AT,
        "window_seconds": window,
        "minimum_partial_seconds": int(
            getattr(settings, "PDF_PIPELINE_RATE_MIN_ELAPSED_SECONDS", 30)
        ),
        "minimum_partial_events": int(getattr(settings, "PDF_PIPELINE_RATE_MIN_EVENTS", 3)),
    }
    extracted_rate = rolling_rate(extracted_times, **arguments)
    written_rate = rolling_rate(written_times, **arguments)
    elapsed = max(1.0, min(window, (now - _SERIES_STARTED_AT).total_seconds()))
    extraction_latency = _latency_summary(
        duration
        for row in extracted_rows
        if (duration := _duration_ms(row["started_at"], row["staged_at"])) is not None
    )
    publication_latency = _latency_summary(
        duration
        for row in written_rows
        if (duration := _duration_ms(row["publication_started_at"], row["published_at"]))
        is not None
    )
    end_to_end_latency = _latency_summary(
        duration
        for row in (*written_rows, *cache_rows)
        if (duration := _duration_ms(row.get("requested_at"), row["published_at"])) is not None
    )
    # Keep synthetic/injected legacy rows useful without manufacturing a
    # negative or otherwise invalid end-to-end duration.
    if not end_to_end_latency["sampleCount"]:
        end_to_end_latency = _latency_summary(
            duration
            for row in (*written_rows, *cache_rows)
            if (duration := _duration_ms(row["started_at"], row["published_at"])) is not None
        )
    staged_wait_latency = _latency_summary(
        duration
        for row in written_rows
        if (duration := _duration_ms(row["staged_at"], row["publication_started_at"])) is not None
    )
    pages_persisted = sum(int(row["pages_processed"] or 0) for row in written_rows)
    pages_extracted = sum(int(row["pages_processed"] or 0) for row in extracted_rows)
    characters_persisted = sum(int(row["characters_extracted"] or 0) for row in written_rows)
    characters_extracted = sum(int(row["characters_extracted"] or 0) for row in extracted_rows)
    source_bytes = sum(int(row["target_file_size"] or 0) for row in extracted_rows)
    timeout_failures = sum(
        1 for row in failed_rows if "timeout" in str(row["error_code"] or "").casefold()
    )
    interruptions = sum(
        1 for row in failed_rows if row["status"] == PDFExtractionJobStatus.INTERRUPTED
    )
    retries = sum(int(row["retry_count"] or 0) for row in rows)
    series_query = PDFExtractionJob.objects.filter(
        Q(staged_at__gte=_SERIES_STARTED_AT)
        | Q(published_at__gte=_SERIES_STARTED_AT)
        | Q(completed_at__gte=_SERIES_STARTED_AT)
    )
    series_totals = series_query.aggregate(
        extracted=Count("id", filter=Q(staged_at__gte=_SERIES_STARTED_AT)),
        written=Count(
            "id",
            filter=Q(
                completion_kind=PDFPipelineCompletionKind.NORMAL_PUBLICATION,
                published_at__gte=_SERIES_STARTED_AT,
            ),
        ),
        cache_reuse=Count(
            "id",
            filter=Q(
                completion_kind=PDFPipelineCompletionKind.CACHE_REUSE,
                published_at__gte=_SERIES_STARTED_AT,
            ),
        ),
        pages=Sum(
            "pages_processed",
            filter=Q(
                completion_kind=PDFPipelineCompletionKind.NORMAL_PUBLICATION,
                published_at__gte=_SERIES_STARTED_AT,
            ),
        ),
        source_bytes=Sum("target_file_size", filter=Q(staged_at__gte=_SERIES_STARTED_AT)),
        characters=Sum(
            "characters_extracted",
            filter=Q(
                completion_kind=PDFPipelineCompletionKind.NORMAL_PUBLICATION,
                published_at__gte=_SERIES_STARTED_AT,
            ),
        ),
    )
    completed_per_second = ((written_rate["eventCount"] or 0) + len(cache_rows)) / elapsed
    cache_per_second = len(cache_rows) / elapsed
    pages_per_second = pages_persisted / elapsed
    extracted_pages_per_second = pages_extracted / elapsed
    chars_extracted_per_second = characters_extracted / elapsed
    chars_persisted_per_second = characters_persisted / elapsed
    bytes_per_second = source_bytes / elapsed
    return {
        "rateWindowSeconds": window,
        "extractedRate": extracted_rate,
        "writtenRate": written_rate,
        "cacheReuseCompletionsPerSecond": round(cache_per_second, 6),
        "cacheReuseCompletionsPerMinute": round(cache_per_second * 60, 3),
        "documentsCompletedPerSecond": round(completed_per_second, 6),
        "documentsCompletedPerMinute": round(completed_per_second * 60, 3),
        "pagesPersistedPerSecond": round(pages_per_second, 6),
        "pagesPersistedPerMinute": round(pages_per_second * 60, 3),
        "pagesExtractedPerSecond": round(extracted_pages_per_second, 6),
        "pagesExtractedPerMinute": round(extracted_pages_per_second * 60, 3),
        "charactersExtractedPerSecond": round(chars_extracted_per_second, 6),
        "charactersExtractedPerMinute": round(chars_extracted_per_second * 60, 3),
        "charactersPersistedPerSecond": round(chars_persisted_per_second, 6),
        "charactersPersistedPerMinute": round(chars_persisted_per_second * 60, 3),
        "sourceBytesProcessedPerSecond": round(bytes_per_second, 6),
        "sourceBytesProcessedPerMinute": round(bytes_per_second * 60, 3),
        "failedPerSecond": round(len(failed_rows) / elapsed, 6),
        "failedPerMinute": round(len(failed_rows) * 60 / elapsed, 3),
        "timeoutPerSecond": round(timeout_failures / elapsed, 6),
        "timeoutPerMinute": round(timeout_failures * 60 / elapsed, 3),
        "interruptedPerSecond": round(interruptions / elapsed, 6),
        "interruptedPerMinute": round(interruptions * 60 / elapsed, 3),
        "retriesPerSecond": round(retries / elapsed, 6),
        "retriesPerMinute": round(retries * 60 / elapsed, 3),
        "extractionLatencyMeanMs": extraction_latency["meanMs"],
        "extractionLatencyP50Ms": extraction_latency["p50Ms"],
        "extractionLatencyP95Ms": extraction_latency["p95Ms"],
        "publicationLatencyMeanMs": publication_latency["meanMs"],
        "publicationLatencyP50Ms": publication_latency["p50Ms"],
        "publicationLatencyP95Ms": publication_latency["p95Ms"],
        "stagedWaitLatencyMeanMs": staged_wait_latency["meanMs"],
        "stagedWaitLatencyP50Ms": staged_wait_latency["p50Ms"],
        "stagedWaitLatencyP95Ms": staged_wait_latency["p95Ms"],
        "endToEndLatencyP95Ms": end_to_end_latency["p95Ms"],
        "latencySampleCounts": {
            "extraction": extraction_latency["sampleCount"],
            "publication": publication_latency["sampleCount"],
            "stagedWait": staged_wait_latency["sampleCount"],
            "endToEnd": end_to_end_latency["sampleCount"],
        },
        "seriesTotals": {
            "extracted": int(series_totals["extracted"] or 0),
            "written": int(series_totals["written"] or 0),
            "cacheReuse": int(series_totals["cache_reuse"] or 0),
            "pagesPersisted": int(series_totals["pages"] or 0),
            "sourceBytesProcessed": int(series_totals["source_bytes"] or 0),
            "charactersPersisted": int(series_totals["characters"] or 0),
        },
        "availability": {
            **(
                {}
                if extraction_latency["sampleCount"]
                else {"extractionLatency": "no_completed_extractions_in_window"}
            ),
            **(
                {}
                if publication_latency["sampleCount"]
                else {"publicationLatency": "no_completed_publications_in_window"}
            ),
            **(
                {}
                if staged_wait_latency["sampleCount"]
                else {"stagedWaitLatency": "no_completed_publications_in_window"}
            ),
            **(
                {}
                if end_to_end_latency["sampleCount"]
                else {"endToEndLatency": "no_completed_documents_in_window"}
            ),
        },
    }


def _terminal_eta(state: str, now: datetime) -> dict[str, Any] | None:
    labels = {
        PDFPipelineRunState.COMPLETE: "Complete",
        PDFPipelineRunState.COMPLETED_WITH_ERRORS: "Completed with errors",
        PDFPipelineRunState.CANCELLED: "Cancelled",
    }
    if state not in labels:
        return None
    cancelled = state == PDFPipelineRunState.CANCELLED
    return {
        "state": state,
        "etaSeconds": None if cancelled else 0,
        "display": labels[state],
        "confidence": "high",
        "lowerSeconds": None if cancelled else 0,
        "upperSeconds": None if cancelled else 0,
        "asOf": now.isoformat(),
        "reasonCode": f"run_{state}",
    }


def estimate_pipeline_eta(
    *,
    remaining_extraction: int,
    remaining_publication: int,
    extracted_rate_per_second: float | None,
    written_rate_per_second: float | None,
    inventory_final: bool,
    now: datetime,
    limiting_phase: str | None = None,
) -> dict[str, Any]:
    """Estimate overlapping extraction/publication makespan, never a naive sum."""

    remaining_extraction = max(0, int(remaining_extraction))
    remaining_publication = max(0, int(remaining_publication))
    if not inventory_final:
        reason = "repository_inventory_incomplete"
    elif remaining_extraction <= 0 and remaining_publication <= 0:
        return {
            "state": "completing",
            "etaSeconds": None,
            "display": "Completing…",
            "confidence": "low",
            "lowerSeconds": None,
            "upperSeconds": None,
            "asOf": now.isoformat(),
            "reasonCode": "terminal_reconciliation_pending",
            "limitingPhase": limiting_phase,
        }
    elif (remaining_extraction > 0 and not extracted_rate_per_second) or (
        remaining_publication > 0 and not written_rate_per_second
    ):
        reason = "insufficient_pipeline_rate"
    else:
        extraction_seconds = (
            remaining_extraction / extracted_rate_per_second
            if remaining_extraction > 0 and extracted_rate_per_second
            else 0.0
        )
        publication_seconds = (
            remaining_publication / written_rate_per_second
            if remaining_publication > 0 and written_rate_per_second
            else 0.0
        )
        seconds = max(extraction_seconds, publication_seconds)
        rounded = max(0, int(math.ceil(seconds)))
        return {
            "state": "available",
            "etaSeconds": rounded,
            "display": f"ETA ~{format_eta_duration(rounded)}",
            "confidence": "medium",
            "lowerSeconds": max(0, int(math.floor(rounded * 0.8))),
            "upperSeconds": max(rounded, int(math.ceil(rounded * 1.25))),
            "asOf": now.isoformat(),
            "reasonCode": "overlapping_recent_pipeline_rates",
            "limitingPhase": (
                "extraction" if extraction_seconds >= publication_seconds else "publication"
            ),
        }
    return {
        "state": "warming",
        "etaSeconds": None,
        "display": "Calculating total ETA",
        "confidence": "low",
        "lowerSeconds": None,
        "upperSeconds": None,
        "asOf": now.isoformat(),
        "reasonCode": reason,
        "limitingPhase": limiting_phase,
    }


def _eta_with_context(
    *,
    remaining: int,
    staged: int,
    inventory_final: bool,
    throughput: Mapping[str, Any],
    now: datetime,
    state: str,
) -> dict[str, Any]:
    terminal = _terminal_eta(state, now)
    if terminal is not None:
        eta = terminal
    elif state == PDFPipelineRunState.PAUSED:
        eta = {
            "state": "paused",
            "etaSeconds": None,
            "display": "ETA paused",
            "confidence": "low",
            "lowerSeconds": None,
            "upperSeconds": None,
            "asOf": now.isoformat(),
            "reasonCode": "run_paused",
            "limitingPhase": None,
        }
    else:
        extracted_measurement = throughput["extractedRate"]
        written_measurement = throughput["writtenRate"]
        extraction_remaining = max(0, remaining - staged)
        publication_remaining = max(0, remaining)
        minimum_completions = int(getattr(settings, "PDF_PIPELINE_ETA_MIN_COMPLETIONS", 3))
        stale_seconds = int(getattr(settings, "PDF_PIPELINE_ETA_STALE_SECONDS", 30))

        def measurement_stale(measurement: Mapping[str, Any], required: bool) -> bool:
            if not required or not measurement.get("perSecond"):
                return False
            raw = measurement.get("lastEventAt")
            try:
                last_event = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return True
            return (
                timezone.is_naive(last_event) or (now - last_event).total_seconds() > stale_seconds
            )

        if measurement_stale(
            extracted_measurement,
            extraction_remaining > 0,
        ) or measurement_stale(written_measurement, publication_remaining > 0):
            eta = {
                "state": "stale",
                "etaSeconds": None,
                "display": "ETA unavailable — throughput is stale",
                "confidence": "low",
                "lowerSeconds": None,
                "upperSeconds": None,
                "asOf": now.isoformat(),
                "reasonCode": "throughput_series_stale",
                "limitingPhase": None,
            }
        else:
            extracted_rate = (
                extracted_measurement.get("perSecond")
                if int(extracted_measurement.get("eventCount") or 0) >= minimum_completions
                else None
            )
            written_rate = (
                written_measurement.get("perSecond")
                if int(written_measurement.get("eventCount") or 0) >= minimum_completions
                else None
            )
            eta = estimate_pipeline_eta(
                remaining_extraction=extraction_remaining,
                remaining_publication=publication_remaining,
                extracted_rate_per_second=extracted_rate,
                written_rate_per_second=written_rate,
                inventory_final=inventory_final,
                now=now,
            )
    with _RING_LOCK:
        completed_samples = len(_SAMPLES)
    eta.update(
        {
            "windowSeconds": throughput["rateWindowSeconds"],
            "completedSamples": completed_samples,
            "completedWork": {
                "documents": int(throughput["writtenRate"].get("eventCount") or 0),
                "pages": None,
                "sourceBytes": None,
            },
            "seriesId": _SERIES_ID,
            "inventoryFinal": inventory_final,
            "effectiveExtractionTarget": min(
                int(settings.PDF_MAX_EXTRACTION_WORKERS),
                int(
                    getattr(
                        settings, "PDF_PIPELINE_INITIAL_TARGET", settings.PDF_MAX_EXTRACTION_WORKERS
                    )
                ),
            ),
        }
    )
    return eta


def _run_snapshot(now: datetime, throughput: Mapping[str, Any]) -> dict[str, Any] | None:
    run = latest_current_run()
    if run is None:
        return None
    memberships = tuple(run.repository_memberships.order_by("accepted_at", "id"))
    repository_counts = {state: 0 for state in PDFPipelineRunState.values}
    repository_progress = []
    total = successful = failed = cancelled = remaining = unresolved = 0
    run_staged = 0
    known_inventory = 0
    fresh_cutoff = now - timedelta(
        seconds=int(getattr(settings, "PDF_PIPELINE_METRICS_STALE_SECONDS", 15))
    )
    publication_counts_by_membership = {
        int(row["run_repository_id"]): row
        for row in PDFExtractionJob.objects.filter(
            run_repository_id__in=[membership.pk for membership in memberships],
            status=PDFExtractionJobStatus.RUNNING,
            phase=PDFExtractionJobPhase.PUBLISHING,
        )
        .values("run_repository_id")
        .annotate(
            total=Count("id"),
            publishing=Count(
                "id",
                filter=Q(worker_pid__isnull=False, heartbeat_at__gte=fresh_cutoff),
            ),
        )
    }
    for membership in memberships:
        repository_counts[membership.lifecycle_state] = (
            repository_counts.get(membership.lifecycle_state, 0) + 1
        )
        if membership.inventory_final:
            known_inventory += 1
        total += membership.total_pdfs
        successful += membership.successful_pdfs
        failed += membership.permanent_failed_pdfs
        cancelled += membership.cancelled_pdfs
        remaining += membership.remaining_pdfs
        unresolved += membership.unresolved_failures
        publication_counts = publication_counts_by_membership.get(membership.pk, {})
        staged_total = int(publication_counts.get("total") or 0)
        publishing = int(publication_counts.get("publishing") or 0)
        staged = max(0, staged_total - publishing)
        run_staged += staged_total
        eta = _eta_with_context(
            remaining=membership.remaining_pdfs,
            staged=staged_total,
            inventory_final=membership.inventory_final,
            throughput=throughput,
            now=now,
            state=membership.lifecycle_state,
        )
        repository_progress.append(
            {
                "repositoryId": membership.repository_id,
                "runId": str(run.pk),
                "revisionId": (
                    membership.repository_revision
                    if re.fullmatch(r"[0-9A-Fa-f]{7,64}", membership.repository_revision)
                    else None
                ),
                "lifecycleState": membership.lifecycle_state,
                "phase": membership.phase,
                "acceptedAt": membership.accepted_at.isoformat(),
                "activatedAt": (
                    membership.activated_at.isoformat() if membership.activated_at else None
                ),
                "lastProgressAt": (
                    membership.last_progress_at.isoformat() if membership.last_progress_at else None
                ),
                "completedAt": (
                    membership.completed_at.isoformat() if membership.completed_at else None
                ),
                "inventoryFinal": membership.inventory_final,
                "totalPdfs": membership.total_pdfs,
                "successfulPdfs": membership.successful_pdfs,
                "permanentFailedPdfs": membership.permanent_failed_pdfs,
                "cancelledPdfs": membership.cancelled_pdfs,
                "remainingPdfs": membership.remaining_pdfs,
                "stagedPdfs": staged,
                "publishingPdfs": publishing,
                "unresolvedFailures": membership.unresolved_failures,
                "terminalOutcome": membership.terminal_outcome or None,
                "eta": eta,
            }
        )
    inventory_final = bool(memberships) and known_inventory == len(memberships)
    total_eta = _eta_with_context(
        remaining=remaining,
        staged=run_staged,
        inventory_final=inventory_final,
        throughput=throughput,
        now=now,
        state=run.state,
    )
    total_eta["inventoryCoverage"] = known_inventory / len(memberships) if memberships else 1.0
    return {
        "id": str(run.pk),
        "state": run.state,
        "acceptedAt": run.accepted_at.isoformat(),
        "lastProgressAt": run.last_progress_at.isoformat() if run.last_progress_at else None,
        "completedAt": run.completed_at.isoformat() if run.completed_at else None,
        "repositories": {
            "accepted": len(memberships),
            "queued": repository_counts.get(PDFPipelineRunState.QUEUED, 0),
            "active": repository_counts.get(PDFPipelineRunState.ACTIVE, 0),
            "completed": repository_counts.get(PDFPipelineRunState.COMPLETE, 0),
            "completedWithErrors": repository_counts.get(
                PDFPipelineRunState.COMPLETED_WITH_ERRORS, 0
            ),
            "paused": repository_counts.get(PDFPipelineRunState.PAUSED, 0),
            "cancelled": repository_counts.get(PDFPipelineRunState.CANCELLED, 0),
            "health": {"degraded": 0, "unavailable": 0},
        },
        "pdfs": {
            "total": total,
            "successful": successful,
            "permanentFailed": failed,
            "cancelled": cancelled,
            "remaining": remaining,
            "unresolvedFailures": unresolved,
            "inventoryFinal": inventory_final,
            "inventoryRepositoriesKnown": known_inventory,
            "inventoryRepositoriesAccepted": len(memberships),
        },
        "totalEta": total_eta,
        "repositoryProgress": repository_progress,
    }


def _forecast_error_rows(
    samples: Iterable[Mapping[str, Any]],
    *,
    completed_at: datetime,
) -> tuple[dict[str, float], ...]:
    rows: list[dict[str, float]] = []
    for sample in samples:
        sampled_at = _sample_datetime(sample)
        eta = sample.get("eta")
        if sampled_at is None or sampled_at >= completed_at or not isinstance(eta, Mapping):
            continue
        predicted = eta.get("etaSeconds")
        if eta.get("state") != "available" or not isinstance(predicted, (int, float)):
            continue
        actual = max(0.0, (completed_at - sampled_at).total_seconds())
        if actual < 1.0:
            continue
        error = float(predicted) - actual
        rows.append(
            {
                "predictedSeconds": round(float(predicted), 3),
                "actualSeconds": round(actual, 3),
                "absolutePercentageError": round(abs(error) * 100 / actual, 3),
                "biasPercentage": round(error * 100 / actual, 3),
            }
        )
    return tuple(rows)


def _error_summary(rows: Iterable[Mapping[str, float]]) -> dict[str, Any]:
    values = tuple(rows)
    if not values:
        return {
            "checkpointCount": 0,
            "medianAbsolutePercentageError": None,
            "meanBiasPercentage": None,
            "overEstimateCount": 0,
            "underEstimateCount": 0,
        }
    absolute = [float(row["absolutePercentageError"]) for row in values]
    bias = [float(row["biasPercentage"]) for row in values]
    return {
        "checkpointCount": len(values),
        "medianAbsolutePercentageError": round(percentile(absolute, 50) or 0.0, 3),
        "meanBiasPercentage": round(sum(bias) / len(bias), 3),
        "overEstimateCount": sum(1 for value in bias if value > 0),
        "underEstimateCount": sum(1 for value in bias if value < 0),
    }


def _eta_accuracy_snapshot(run: Mapping[str, Any]) -> dict[str, Any]:
    """Score completed shadow forecasts retained in the bounded sample ring."""

    run_id = str(run.get("id") or "")
    total_pdfs = int(run.get("pdfs", {}).get("total") or 0)
    workload_class = "small" if total_pdfs < 100 else "medium" if total_pdfs < 1_000 else "large"
    completed_raw = run.get("completedAt")
    try:
        completed_at = datetime.fromisoformat(str(completed_raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        completed_at = None
    with _RING_LOCK:
        samples = tuple(
            sample
            for sample in _SAMPLES
            if isinstance(sample.get("run"), Mapping)
            and str(sample["run"].get("id") or "") == run_id
        )
    if completed_at is None or timezone.is_naive(completed_at):
        return {
            "state": "collecting",
            "workloadClass": workload_class,
            **_error_summary(()),
            "repositorySummaries": [],
        }
    total_rows = _forecast_error_rows(
        ({**sample, "eta": sample["run"].get("totalEta")} for sample in samples),
        completed_at=completed_at,
    )
    repository_summaries = []
    for repository in run.get("repositoryProgress", []):
        repository_id = repository.get("repositoryId")
        completed_value = repository.get("completedAt")
        try:
            repository_completed_at = datetime.fromisoformat(
                str(completed_value).replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            continue
        repository_rows = _forecast_error_rows(
            (
                {
                    **sample,
                    "eta": next(
                        (
                            item.get("eta")
                            for item in sample["run"].get("repositories", [])
                            if item.get("repositoryId") == repository_id
                        ),
                        None,
                    ),
                }
                for sample in samples
            ),
            completed_at=repository_completed_at,
        )
        repository_summaries.append(
            {"repositoryId": repository_id, **_error_summary(repository_rows)}
        )
    summary = _error_summary(total_rows)
    return {
        "state": "available" if summary["checkpointCount"] else "insufficient_history",
        "workloadClass": workload_class,
        **summary,
        "repositorySummaries": repository_summaries,
    }


def _activity_snapshot(
    now: datetime,
    run: Mapping[str, Any] | None,
    queues: Mapping[str, Any],
    recovery: Mapping[str, Any],
) -> dict[str, Any]:
    fresh_cutoff = now - timedelta(
        seconds=int(getattr(settings, "PDF_PIPELINE_METRICS_STALE_SECONDS", 15))
    )
    sync_rows = tuple(
        RepositorySyncJob.objects.filter(
            status=RepositorySyncJobStatus.RUNNING,
            worker_pid__isnull=False,
            heartbeat_at__gte=fresh_cutoff,
        ).values("operation", "phase", "heartbeat_at")
    )
    extraction_rows = tuple(
        PDFExtractionJob.objects.filter(
            status=PDFExtractionJobStatus.RUNNING,
            worker_pid__isnull=False,
            phase__in=(
                PDFExtractionJobPhase.VALIDATING,
                PDFExtractionJobPhase.HASHING,
                PDFExtractionJobPhase.EXTRACTING,
            ),
            heartbeat_at__gte=fresh_cutoff,
        ).values("phase", "heartbeat_at")
    )
    extraction_phases = {row["phase"] for row in extraction_rows}
    writing_heartbeat = PDFExtractionJob.objects.filter(
        status=PDFExtractionJobStatus.RUNNING,
        phase=PDFExtractionJobPhase.PUBLISHING,
        worker_pid__isnull=False,
        heartbeat_at__gte=fresh_cutoff,
    ).aggregate(value=Max("heartbeat_at"))["value"]
    writing = writing_heartbeat is not None
    recovery_state = recovery["state"]
    if recovery_state == PDFPipelineRecoveryState.PAUSED:
        code = "paused"
    elif recovery_state in {
        PDFPipelineRecoveryState.RETRY_WAIT,
        PDFPipelineRecoveryState.RECOVERING,
        PDFPipelineRecoveryState.RESUME_REQUESTED,
        PDFPipelineRecoveryState.RECOVERING_HALF_OPEN,
    }:
        code = (
            "retry_wait" if recovery_state == PDFPipelineRecoveryState.RETRY_WAIT else "recovering"
        )
    elif extraction_phases and writing:
        code = "extracting_and_writing"
    elif writing:
        code = "writing"
    elif PDFExtractionJobPhase.EXTRACTING in extraction_phases:
        code = "extracting"
    elif PDFExtractionJobPhase.HASHING in extraction_phases:
        code = "hashing"
    elif PDFExtractionJobPhase.VALIDATING in extraction_phases:
        code = "validating"
    elif sync_rows:
        phases = {row["phase"] for row in sync_rows}
        operations = {row["operation"] for row in sync_rows}
        if RepositorySyncPhase.CHECKING_CONNECTION in phases:
            code = "checking_connection"
        elif RepositorySyncPhase.DISCOVERING in phases or RepositorySyncPhase.FINALIZING in phases:
            code = "discovering"
        elif RepositorySyncOperation.CLONE in operations:
            code = "cloning"
        else:
            code = "pulling"
    elif run and run["state"] in TERMINAL_RUN_STATES:
        code = run["state"]
    elif run and run["repositories"]["queued"]:
        code = "queued"
    elif queues["inputQueuedJobs"] and not queues["eligibleInputJobs"]:
        code = "source_blocked"
    elif queues["inputQueuedJobs"]:
        code = "queued"
    elif run and run["state"] == PDFPipelineRunState.ACTIVE:
        code = "completing"
    else:
        code = "idle"
    secondary = []
    if code in {"extracting", "writing", "extracting_and_writing"} and sync_rows:
        secondary.append({"code": "pulling", "repositoryCount": len(sync_rows)})
    evidence_times = [
        row["heartbeat_at"] for row in (*sync_rows, *extraction_rows) if row["heartbeat_at"]
    ]
    if writing_heartbeat is not None:
        evidence_times.append(writing_heartbeat)
    return {
        "code": code,
        "label": _ACTIVITY_LABELS[code],
        "secondary": secondary,
        "reasonCode": f"activity_{code}",
        "evidenceAt": max(evidence_times).isoformat() if evidence_times else None,
        "evidence": {
            "activeExtractionJobs": queues["activeExtractionJobs"],
            "publisherState": "busy" if writing else "not_busy",
            "repositoriesSyncing": len(sync_rows),
        },
    }


def _indicator_snapshot(
    now: datetime,
    activity: Mapping[str, Any],
    run: Mapping[str, Any] | None,
    repository_count: int,
    actionable_repository_count: int,
    recovery: Mapping[str, Any],
) -> dict[str, Any]:
    code = activity["code"]
    running_codes = {
        "checking_connection",
        "cloning",
        "pulling",
        "discovering",
        "validating",
        "hashing",
        "extracting",
        "writing",
        "extracting_and_writing",
    }
    if repository_count == 0 and run is None:
        state = "hidden"
    elif code == "idle":
        state = "idle_actionable" if actionable_repository_count else "idle_unavailable"
    elif code == "queued" or code in {"backpressured", "source_blocked", "completing"}:
        state = "queued"
    elif code == "retry_wait":
        state = "retry_wait"
    elif code == "recovering":
        state = "recovering" if recovery.get("activeAttemptId") else "retry_wait"
    elif code == "paused":
        state = "paused"
    elif code in TERMINAL_RUN_STATES:
        state = "terminal"
    elif code in running_codes:
        state = "running"
    else:
        state = "unknown"
    has_fresh = state == "running" or (
        state == "recovering" and bool(recovery.get("activeAttemptId"))
    )
    evidence_at = (
        recovery.get("lastAttemptAt") if state == "recovering" else activity.get("evidenceAt")
    )
    return {
        "state": state,
        "hasFreshRunningWork": has_fresh,
        "evidenceCodes": [activity["reasonCode"]] if has_fresh else [],
        "evidenceAt": evidence_at if has_fresh else None,
        "freshForSeconds": int(getattr(settings, "PDF_PIPELINE_METRICS_STALE_SECONDS", 15)),
    }


def _state_snapshot(
    activity: Mapping[str, Any],
    queues: Mapping[str, Any],
    workers: Mapping[str, Any],
    publisher: Mapping[str, Any],
    recovery: Mapping[str, Any],
    resources: Mapping[str, Any] | None = None,
    throughput: Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    resources = resources or {}
    throughput = throughput or {}
    now = now or timezone.now()
    window_seconds = int(throughput.get("rateWindowSeconds") or 60)
    window_start = now - timedelta(seconds=window_seconds)
    detected: list[str] = []
    host_cpu = resources.get("hostCpuPct")
    memory_available = resources.get("hostMemoryAvailableBytes")
    disk_available = resources.get("diskAvailableBytes")
    if (
        isinstance(host_cpu, (int, float))
        and not isinstance(host_cpu, bool)
        and host_cpu >= float(getattr(settings, "PDF_PIPELINE_CONTROLLER_MAX_HOST_CPU_PCT", 85.0))
    ):
        detected.append("cpu_limited")
    if (
        isinstance(memory_available, int)
        and not isinstance(memory_available, bool)
        and memory_available
        < int(
            getattr(
                settings,
                "PDF_PIPELINE_CONTROLLER_MIN_AVAILABLE_MEMORY_BYTES",
                8 * 1_024**3,
            )
        )
    ):
        detected.append("memory_limited")
    if (
        isinstance(disk_available, int)
        and not isinstance(disk_available, bool)
        and disk_available
        < int(
            getattr(
                settings,
                "PDF_PIPELINE_CONTROLLER_MIN_AVAILABLE_DISK_BYTES",
                10 * 1_024**3,
            )
        )
    ):
        detected.append("disk_limited")
    sqlite_contended = bool(
        (publisher.get("sqliteBusyErrors") or 0) > 0
        or (
            isinstance(publisher.get("sqliteLockWaitP95Ms"), (int, float))
            and publisher["sqliteLockWaitP95Ms"]
            >= publisher.get("sqliteLockBlockedThresholdMs", 250)
        )
    )
    if sqlite_contended:
        detected.append("sqlite_contended")
    if activity["code"] == "source_blocked":
        detected.append("source_blocked")
    jsonl = queues.get("jsonl") if isinstance(queues.get("jsonl"), Mapping) else {}
    if publisher["state"] == "blocked" or int(jsonl.get("queuedChunks") or 0) > 0:
        detected.append("publication_limited")
    if publisher["state"] == "starved":
        detected.append("extraction_limited")

    if recovery["state"] == PDFPipelineRecoveryState.PAUSED:
        code, reason_code = "recovery_paused", "component_recovery_circuit_open"
    elif recovery["state"] != PDFPipelineRecoveryState.HEALTHY:
        code, reason_code = "recovering", "component_recovery_active"
    elif "disk_limited" in detected:
        code, reason_code = "disk_limited", "available_disk_below_safety_floor"
    elif "memory_limited" in detected:
        code, reason_code = "memory_limited", "available_memory_below_safety_floor"
    elif "cpu_limited" in detected:
        code, reason_code = "cpu_limited", "host_cpu_above_safety_ceiling"
    elif publisher["state"] == "unavailable" and int(jsonl.get("queuedChunks") or 0) > 0:
        code, reason_code = "degraded", "publisher_unavailable_with_backlog"
    elif "source_blocked" in detected:
        code, reason_code = "source_blocked", "queued_input_not_progressing"
    elif workers["unavailable"] and queues["inputQueuedJobs"]:
        code, reason_code = "degraded", "extractor_process_unavailable"
    elif "sqlite_contended" in detected:
        code, reason_code = "sqlite_contended", "sqlite_lock_wait_or_busy_error"
    elif activity["code"] == "idle":
        code, reason_code = "idle", "no_current_demand"
    elif "publication_limited" in detected:
        code, reason_code = "publication_limited", "durable_publication_backlog"
    elif "extraction_limited" in detected:
        code, reason_code = "extraction_limited", "publisher_awaiting_extractor_output"
    elif (
        throughput.get("extractedRate", {}).get("state") == "available"
        and throughput.get("writtenRate", {}).get("state") == "available"
    ):
        code, reason_code = "balanced", "measured_flow_within_guardrails"
    else:
        code, reason_code = "warming_up", "collecting_observation_window"
    labels = {
        "recovery_paused": "PDF pipeline paused",
        "recovering": "PDF pipeline recovering",
        "balanced": "PDF pipeline balanced",
        "degraded": "PDF pipeline degraded",
        "source_blocked": "PDF source blocked",
        "idle": "PDF pipeline idle",
        "extraction_limited": "PDF extraction limited",
        "publication_limited": "PDF publication limited",
        "sqlite_contended": "SQLite contended",
        "cpu_limited": "Host CPU constrained",
        "memory_limited": "Available memory constrained",
        "disk_limited": "Available disk constrained",
        "warming_up": "Measuring PDF pipeline",
    }
    evidence = {
        "recovery_paused": f"Recovery circuit is open after {int(recovery.get('consecutiveFailedAttempts') or 0)} failed attempts.",
        "recovering": f"Recovery state is {recovery['state']}.",
        "disk_limited": f"Available disk is {disk_available} bytes, below the configured safety floor.",
        "memory_limited": f"Available memory is {memory_available} bytes, below the configured safety floor.",
        "cpu_limited": f"Normalized host CPU is {host_cpu}% over the {window_seconds}-second observation window.",
        "degraded": "Required resident pipeline capacity is unavailable while durable work remains.",
        "source_blocked": f"{queues['inputQueuedJobs']} queued jobs have no currently eligible repository input.",
        "sqlite_contended": (
            f"SQLite lock-wait p95 is {publisher.get('sqliteLockWaitP95Ms')} ms with "
            f"{publisher.get('sqliteBusyErrors')} busy errors in {window_seconds} seconds."
        ),
        "idle": "No current eligible extraction or publication demand is measured.",
        "publication_limited": (
            f"{int(jsonl.get('queuedChunks') or 0)} durable JSONL chunks remain and the "
            f"SQLite writer is {publisher['state']}; extraction admission remains independent."
        ),
        "extraction_limited": "The live publisher is awaiting extractor output.",
        "balanced": f"Extraction and publication flow are measured within guardrails over {window_seconds} seconds.",
        "warming_up": f"The {window_seconds}-second observation window does not yet contain stable flow evidence.",
    }
    constraints = [constraint for constraint in dict.fromkeys(detected) if constraint != code]
    return {
        "code": code,
        "label": labels[code],
        "reasonCode": reason_code,
        "reason": evidence[code],
        "confidence": (
            "warming"
            if code == "warming_up"
            else "high"
            if code in {"idle", "recovery_paused", "disk_limited", "memory_limited"}
            else "medium"
        ),
        "constraints": constraints,
        "window": {
            "startedAt": window_start.isoformat(),
            "endedAt": now.isoformat(),
            "durationSeconds": window_seconds,
        },
    }


def _controller_snapshot(
    resources: Mapping[str, Any],
    *,
    recovery_state: str = PDFPipelineRecoveryState.HEALTHY,
    recovery_scope: str | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    return controller_snapshot(
        resources,
        recovery_state=recovery_state,
        recovery_scope=recovery_scope,
        at=at,
    )


def _tuning_events() -> list[dict[str, Any]]:
    reason_text = {
        "measured_extraction_headroom": "Eligible demand occupied the admitted parsers while the publisher had measured headroom.",
        "host_cpu_pressure": "Measured host CPU crossed the configured background-work guardrail.",
        "foreground_latency_pressure": "Measured foreground latency crossed its configured guardrail.",
        "sqlite_contention": "SQLite reported contention during the observation window.",
        "failure_rate_pressure": "The measured terminal failure rate crossed its guardrail.",
        "publication_backlog_rising": "Durable staged output was at the threshold and still increasing.",
        "increase_no_measured_benefit": "The prior increase did not produce the required durable-throughput gain.",
        "critical_memory_headroom": "Available memory fell below the critical headroom boundary.",
        "critical_disk_headroom": "Available disk fell below the critical headroom boundary.",
        "critical_foreground_latency": "Foreground latency crossed the critical pause boundary.",
        "recovery_freeze": "Component recovery froze admission tuning.",
    }
    expected_effect_text = {
        "improve_durable_throughput": "Test whether one additional admitted parser improves durable written throughput.",
        "relieve_measured_pressure": "Reduce parser admission to relieve the measured limiting pressure.",
        "protect_integrity_and_foreground_work": "Pause new parser admission while preserving in-flight durable work.",
        "restore_last_effective_target": "Restore the prior target after an increase did not help.",
        "preserve_target_pending_evidence": "Keep admission unchanged until sufficient evidence is available.",
    }

    def safe_code(value: object) -> str:
        candidate = str(value or "")
        return candidate if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", candidate) else "unknown"

    def numeric_evidence(value: object) -> dict[str, int | float | bool | None]:
        if not isinstance(value, Mapping):
            return {}
        return {
            key: item
            for raw_key, item in value.items()
            if (key := safe_code(raw_key)) != "unknown"
            and (item is None or type(item) in {bool, int, float})
            and not (isinstance(item, float) and not math.isfinite(item))
        }

    return [
        {
            "id": event.pk,
            "at": event.occurred_at.isoformat(),
            "mode": safe_code(event.mode),
            "action": safe_code(event.action),
            "previousTarget": event.previous_target,
            "proposedTarget": event.proposed_target,
            "reasonCode": safe_code(event.reason_code),
            # Human text is deliberately derived from the stable code at the
            # presentation layer.  A legacy/free-form database value can
            # contain a path or operator detail and is never exported here.
            "reason": reason_text.get(
                safe_code(event.reason_code),
                "Pipeline tuning evaluated measured safety and throughput guardrails.",
            ),
            "expectedEffectCode": safe_code(event.expected_effect_code)
            if event.expected_effect_code
            else None,
            "expectedEffect": expected_effect_text.get(safe_code(event.expected_effect_code))
            if event.expected_effect_code
            else None,
            "observationWindowSeconds": event.observation_window_seconds,
            "evidence": numeric_evidence(event.evidence),
            "confidence": safe_code(event.confidence),
            "cooldownUntil": event.cooldown_until.isoformat() if event.cooldown_until else None,
            "outcome": safe_code(event.outcome) if event.outcome else None,
        }
        for event in PDFPipelineTuningEvent.objects.order_by("-occurred_at", "-id")[:50]
    ]


def build_pipeline_metrics_payload(
    *,
    at: datetime | None = None,
    resident_roles: Mapping[str, object] | None = None,
    include_samples: bool = True,
) -> dict[str, Any]:
    """Build one redacted local payload from durable state and bounded gauges."""

    now = at or timezone.now()
    queues = _queue_snapshot(now)
    resources = resource_snapshot(resident_roles=resident_roles)
    recovery = _recovery_snapshot()
    controller = _controller_snapshot(
        resources,
        recovery_state=recovery["state"],
        recovery_scope=recovery["scope"],
        at=now,
    )
    workers = _worker_snapshot(
        queues,
        resident_roles=resident_roles,
        requested_target=controller["effectiveAdmissionTarget"],
    )
    publisher = _publisher_snapshot(queues, now=now, resident_roles=resident_roles)
    throughput = _throughput_snapshot(now)
    counts = _pipeline_count_snapshot()
    jsonl = queues["jsonl"]
    if int(jsonl["queuedChunks"]):
        flow_balance = {
            "state": "SQLITE_BACKLOG",
            "label": "SQLite backlog",
            "reason": "SQLite ingestion has sealed JSONL chunks waiting on disk.",
        }
    elif publisher["state"] == "starved":
        flow_balance = {
            "state": "EXTRACTION_STARVING_SQLITE",
            "label": "Extraction starving SQLite",
            "reason": "The SQLite writer is waiting for the next sealed extraction chunk.",
        }
    elif not counts["pdfsPendingExtraction"] and not counts["pdfsCurrentlyExtracting"]:
        flow_balance = {
            "state": "IDLE",
            "label": "Idle",
            "reason": "No extraction or ingestion work is waiting.",
        }
    else:
        flow_balance = {
            "state": "BALANCED",
            "label": "Balanced",
            "reason": "No durable extractor or SQLite queue constraint is currently visible.",
        }
    run = _run_snapshot(now, throughput)
    if run is not None:
        run["etaAccuracy"] = _eta_accuracy_snapshot(run)
    repository_count = BitbucketRepository.objects.count()
    actionable_repository_count = BitbucketRepository.objects.filter(
        enabled=True,
        exclude_from_refresh=False,
    ).count()
    activity = _activity_snapshot(now, run, queues, recovery)
    indicator = _indicator_snapshot(
        now,
        activity,
        run,
        repository_count,
        actionable_repository_count,
        recovery,
    )
    state = _state_snapshot(
        activity,
        queues,
        workers,
        publisher,
        recovery,
        resources,
        throughput,
        now=now,
    )
    with _RING_LOCK:
        samples = list(_SAMPLES) if include_samples else []
        history_complete = not _HISTORY_TRUNCATED
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": now.isoformat(),
        "seriesId": _SERIES_ID,
        "seriesStartedAt": _SERIES_STARTED_AT.isoformat(),
        "historyStartedAt": (samples[0]["at"] if samples else _SERIES_STARTED_AT.isoformat()),
        "historyComplete": history_complete,
        "sampleIntervalSeconds": int(getattr(settings, "PDF_PIPELINE_METRICS_SAMPLE_SECONDS", 5)),
        "windowSeconds": int(getattr(settings, "PDF_PIPELINE_RATE_WINDOW_SECONDS", 60)),
        "state": state,
        "activity": activity,
        "topBarActivityIndicator": indicator,
        "run": run,
        "controller": controller,
        "workers": workers,
        "publisher": publisher,
        "counts": counts,
        "jsonlStaging": jsonl,
        "flowBalance": flow_balance,
        "recovery": recovery,
        "recoveryEvents": _recovery_events(),
        "queues": queues,
        "throughput": throughput,
        "resources": resources,
        "foreground": foreground_latency_snapshot(),
        "samples": samples,
        "tuningEvents": _tuning_events(),
    }


def _sample_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    throughput = payload["throughput"]
    run = payload.get("run")
    return {
        "at": payload["generatedAt"],
        "intervalSeconds": payload["sampleIntervalSeconds"],
        "workers": {
            "requestedTarget": payload["controller"]["requestedTarget"],
            "effectiveAdmissionTarget": payload["controller"]["effectiveAdmissionTarget"],
            "active": payload["workers"]["active"],
            "idleNoDemand": payload["workers"]["idleNoDemand"],
            "waitingForEligibleInput": payload["workers"]["waitingForEligibleInput"],
            "backpressured": payload["workers"]["backpressured"],
            "pausedByController": payload["workers"]["pausedByController"],
            "pausedByRecovery": payload["workers"]["pausedByRecovery"],
            "unavailable": payload["workers"]["unavailable"],
        },
        "publisherState": payload["publisher"]["state"],
        "backpressureDepthJobs": payload["queues"]["backpressureDepthJobs"],
        "backpressureThresholdJobs": payload["queues"]["backpressureThresholdJobs"],
        "stagedBytes": payload["queues"]["stagedBytes"],
        "extractorOutputs": throughput["extractedRate"]["eventCount"],
        "writerPublications": throughput["writtenRate"]["eventCount"],
        "extractorOutputsPerSecond": throughput["extractedRate"]["perSecond"],
        "writerPublicationsPerSecond": throughput["writtenRate"]["perSecond"],
        "hostCpuPct": payload["resources"]["hostCpuPct"],
        "hostMemoryAvailableBytes": payload["resources"]["hostMemoryAvailableBytes"],
        "run": (
            {
                "id": run["id"],
                "state": run["state"],
                "pdfs": {
                    "total": run["pdfs"]["total"],
                    "successful": run["pdfs"]["successful"],
                    "remaining": run["pdfs"]["remaining"],
                    "inventoryFinal": run["pdfs"]["inventoryFinal"],
                },
                "totalEta": {
                    key: run["totalEta"].get(key)
                    for key in (
                        "state",
                        "etaSeconds",
                        "confidence",
                        "reasonCode",
                        "inventoryFinal",
                    )
                },
                "repositories": [
                    {
                        "repositoryId": repository["repositoryId"],
                        "lifecycleState": repository["lifecycleState"],
                        "remainingPdfs": repository["remainingPdfs"],
                        "eta": {
                            key: repository["eta"].get(key)
                            for key in (
                                "state",
                                "etaSeconds",
                                "confidence",
                                "reasonCode",
                                "inventoryFinal",
                            )
                        },
                    }
                    for repository in run["repositoryProgress"]
                ],
            }
            if isinstance(run, Mapping)
            else None
        ),
        "availability": {
            **payload["resources"].get("availability", {}),
            **payload["queues"].get("availability", {}),
        },
    }


def _snapshot_path() -> Path:
    return Path(settings.PDF_PIPELINE_STATE_ROOT).resolve() / SNAPSHOT_FILENAME


def write_metrics_snapshot(payload: Mapping[str, Any]) -> Path:
    """Atomically persist a bounded 0600 snapshot for other local processes."""

    target = _snapshot_path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    if len(encoded) > 4 * 1024 * 1024:
        raise ValueError("The bounded metrics snapshot exceeded its size limit.")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def read_metrics_snapshot(*, at: datetime | None = None) -> dict[str, Any] | None:
    """Read and validate the last supervisor snapshot without mutating state."""

    now = at or timezone.now()
    target = _snapshot_path()
    try:
        if target.stat().st_size > 4 * 1024 * 1024:
            return None
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schemaVersion") != SCHEMA_VERSION:
        return None
    try:
        generated_at = datetime.fromisoformat(str(payload["generatedAt"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    if timezone.is_naive(generated_at) or generated_at > now + timedelta(seconds=5):
        return None
    age = max(0.0, (now - generated_at).total_seconds())
    payload["snapshotAgeSeconds"] = round(age, 3)
    payload["snapshotStale"] = age > int(
        getattr(settings, "PDF_PIPELINE_METRICS_STALE_SECONDS", 15)
    )
    return payload


def _invalidate_nonterminal_eta(
    payload: dict[str, Any],
    *,
    state: str,
    reason_code: str,
    display: str,
    at: datetime,
) -> None:
    run = payload.get("run")
    if not isinstance(run, dict):
        return

    def invalidate(target_state: object, eta: object) -> None:
        if target_state in TERMINAL_RUN_STATES or not isinstance(eta, dict):
            return
        eta.update(
            {
                "state": state,
                "etaSeconds": None,
                "display": display,
                "confidence": "low",
                "lowerSeconds": None,
                "upperSeconds": None,
                "asOf": at.isoformat(),
                "reasonCode": reason_code,
            }
        )

    invalidate(run.get("state"), run.get("totalEta"))
    progress = run.get("repositoryProgress")
    if isinstance(progress, list):
        for repository in progress:
            if isinstance(repository, dict):
                invalidate(repository.get("lifecycleState"), repository.get("eta"))


def sample_pipeline_metrics(
    *,
    at: datetime | None = None,
    resident_roles: Mapping[str, object] | None = None,
    controller: PDFPipelineController | None = None,
) -> dict[str, Any]:
    """Record one bounded sample and optionally publish the cross-process snapshot."""

    if not bool(getattr(settings, "PDF_PIPELINE_METRICS_ENABLED", True)):
        return build_pipeline_metrics_payload(
            at=at,
            resident_roles=resident_roles,
            include_samples=False,
        )
    payload = build_pipeline_metrics_payload(
        at=at,
        resident_roles=resident_roles,
        include_samples=False,
    )
    controller_error: Exception | None = None
    if controller is not None:
        try:
            observation = observation_from_metrics(payload, at=at or timezone.now())
            payload["controller"] = controller.evaluate(observation)
        except Exception as exc:
            # Metrics and tuning are advisory to durable queue correctness. A
            # failed evaluation is persisted as a conservative snapshot before
            # the supervisor receives a redacted failure and opens its circuit.
            controller_error = exc
            payload["controller"] = controller_snapshot(
                payload["resources"],
                recovery_state=payload["recovery"]["state"],
                recovery_scope=payload["recovery"]["scope"],
                at=at or timezone.now(),
            )
        payload["workers"] = _worker_snapshot(
            payload["queues"],
            resident_roles=resident_roles,
            requested_target=payload["controller"]["effectiveAdmissionTarget"],
        )
        payload["state"] = _state_snapshot(
            payload["activity"],
            payload["queues"],
            payload["workers"],
            payload["publisher"],
            payload["recovery"],
            payload["resources"],
            payload["throughput"],
            now=at or timezone.now(),
        )
    sample = _sample_from_payload(payload)
    global _HISTORY_TRUNCATED
    with _RING_LOCK:
        maximum = _configured_max_samples()
        while len(_SAMPLES) >= maximum:
            _SAMPLES.popleft()
            _HISTORY_TRUNCATED = True
        _SAMPLES.append(sample)
        payload["samples"] = list(_SAMPLES)
        payload["historyStartedAt"] = payload["samples"][0]["at"]
        payload["historyComplete"] = not _HISTORY_TRUNCATED
    if bool(getattr(settings, "PDF_PIPELINE_METRICS_SNAPSHOT_ENABLED", True)):
        write_metrics_snapshot(payload)
    if controller_error is not None:
        raise ControllerEvaluationError(
            "PDF admission controller evaluation failed."
        ) from controller_error
    return payload


def metrics_payload_for_request(*, at: datetime | None = None) -> dict[str, Any]:
    """Prefer the owner snapshot; return a truthful stale/unavailable fallback."""

    now = at or timezone.now()
    snapshot = read_metrics_snapshot(at=now)
    if snapshot is not None:
        snapshot["foreground"] = foreground_latency_snapshot()
        if snapshot.get("snapshotStale"):
            snapshot["topBarActivityIndicator"] = {
                "state": "unknown",
                "hasFreshRunningWork": False,
                "evidenceCodes": ["metrics_snapshot_stale"],
                "evidenceAt": snapshot.get("generatedAt"),
                "freshForSeconds": int(getattr(settings, "PDF_PIPELINE_METRICS_STALE_SECONDS", 15)),
            }
            snapshot["state"] = {
                **snapshot.get("state", {}),
                "code": "degraded",
                "label": "Pipeline status unavailable",
                "reasonCode": "metrics_snapshot_stale",
                "reason": "The pipeline owner has not published a fresh metrics snapshot.",
                "confidence": "low",
            }
            _invalidate_nonterminal_eta(
                snapshot,
                state="stale",
                reason_code="metrics_snapshot_stale",
                display="ETA unavailable — pipeline status is stale",
                at=now,
            )
        return snapshot
    payload = build_pipeline_metrics_payload(at=now, resident_roles=None)
    payload["snapshotAgeSeconds"] = None
    payload["snapshotStale"] = True
    if (
        payload["activity"]["code"] not in TERMINAL_RUN_STATES
        and payload["activity"]["code"] != "idle"
    ):
        payload["topBarActivityIndicator"] = {
            "state": "unknown",
            "hasFreshRunningWork": False,
            "evidenceCodes": ["metrics_owner_unavailable"],
            "evidenceAt": None,
            "freshForSeconds": int(getattr(settings, "PDF_PIPELINE_METRICS_STALE_SECONDS", 15)),
        }
        _invalidate_nonterminal_eta(
            payload,
            state="unavailable",
            reason_code="metrics_owner_unavailable",
            display="ETA unavailable",
            at=now,
        )
    return payload
