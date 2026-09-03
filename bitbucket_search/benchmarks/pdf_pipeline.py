"""Repeatable synthetic benchmark harness for the existing PDF pipeline.

The public command is an orchestrator only. Every measured trial starts a fresh
Django process whose ``OWL_DATA_ROOT`` points below the repository's ignored
``var/benchmarks`` runtime directory. This prevents a benchmark from reading or
writing the user's canonical OWL database, repositories, or extracted content.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management import call_command
from django.db import DatabaseError, close_old_connections, connection, connections

BENCHMARK_SCHEMA_VERSION = 2
BENCHMARK_KIND = "owl.synthetic-pdf-pipeline-benchmark"
DEFAULT_WORKER_TARGETS = (4, 6, 8)
FULL_WORKER_MATRIX = (1, 2, 4, 6, 8)
MAX_TESTED_WORKERS = 8
SQLITE_JOURNAL_MODES = ("delete", "wal")
FOREGROUND_PROBE_INTERVAL_SECONDS = 0.2
_SYNTHETIC_SECRET_KEY = (
    "synthetic-pdf-benchmark-secret-key-never-for-real-use-0123456789-abcdefghij"
)
_TIER_CHARACTER_TARGETS = (96, 768, 6_144, 24_576)


@dataclass(frozen=True)
class BenchmarkPlan:
    """Validated, fixed inputs shared by every isolated trial."""

    worker_targets: tuple[int, ...]
    repetitions: int
    document_count: int
    repository_count: int
    pages_per_document: int
    seed: int
    trial_timeout_seconds: int
    keep_trial_data: bool = False
    sqlite_journal_mode: str = "delete"
    metrics_sampling_enabled: bool = False


def utc_now() -> datetime:
    return datetime.now(UTC)


def benchmark_runtime_root() -> Path:
    """Return the only production location in which this harness may write."""

    ignored_runtime = (Path(settings.BASE_DIR) / "var").resolve()
    root = (ignored_runtime / "benchmarks").resolve()
    if not root.is_relative_to(ignored_runtime):  # pragma: no cover - defensive invariant
        raise ValueError("Benchmark runtime path escaped OWL's ignored var directory.")
    return root


def validate_plan(plan: BenchmarkPlan) -> BenchmarkPlan:
    if not plan.worker_targets:
        raise ValueError("At least one fixed PDF worker target is required.")
    if len(set(plan.worker_targets)) != len(plan.worker_targets):
        raise ValueError("PDF worker targets must not contain duplicates.")
    if any(
        isinstance(value, bool) or not 1 <= value <= MAX_TESTED_WORKERS
        for value in plan.worker_targets
    ):
        raise ValueError(f"PDF worker targets must be between 1 and {MAX_TESTED_WORKERS}.")
    if not 1 <= plan.repetitions <= 20:
        raise ValueError("Repetitions must be between 1 and 20.")
    if not 1 <= plan.document_count <= 25_000:
        raise ValueError("Document count must be between 1 and 25,000.")
    if not 1 <= plan.repository_count <= min(plan.document_count, 1_000):
        raise ValueError("Repository count must be between 1 and the document count.")
    if not 1 <= plan.pages_per_document <= 1_000:
        raise ValueError("Pages per document must be between 1 and 1,000.")
    if not 0 <= plan.seed <= 2**31 - 1:
        raise ValueError("Seed must be between 0 and 2,147,483,647.")
    if not 30 <= plan.trial_timeout_seconds <= 86_400:
        raise ValueError("Trial timeout must be between 30 and 86,400 seconds.")
    if plan.sqlite_journal_mode not in SQLITE_JOURNAL_MODES:
        raise ValueError("SQLite journal mode must be delete or wal.")
    if not isinstance(plan.metrics_sampling_enabled, bool):
        raise ValueError("Metrics sampling selection must be true or false.")
    return plan


def new_run_id() -> str:
    timestamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:12]}"


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        path.chmod(0o700)
    except OSError:
        if os.name != "nt":
            raise
    return path


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            temporary.chmod(0o600)
        except OSError:
            if os.name != "nt":
                raise
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_remove_trial(path: Path, *, run_directory: Path) -> None:
    resolved_path = path.resolve()
    resolved_run_directory = run_directory.resolve()
    if (
        resolved_path == resolved_run_directory
        or not resolved_path.is_relative_to(resolved_run_directory)
        or not resolved_path.name.startswith("trial-")
    ):
        raise ValueError("Refusing to remove a path outside the generated benchmark run.")
    shutil.rmtree(resolved_path)


def _child_environment(
    *,
    data_root: Path,
    worker_count: int,
    metrics_sampling_enabled: bool,
) -> dict[str, str]:
    """Build a minimal child environment without inheriting integration secrets."""

    inherited_names = (
        "PATH",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "SYSTEMROOT",
        "WINDIR",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    )
    environment = {name: os.environ[name] for name in inherited_names if name in os.environ}
    environment.update(
        {
            "DJANGO_SETTINGS_MODULE": "owl.settings",
            "DJANGO_DEBUG": "false",
            "DJANGO_SECRET_KEY": _SYNTHETIC_SECRET_KEY,
            "OWL_DATA_ROOT": str(data_root),
            "OWL_ENV_FILE": str(data_root / "no-environment-file"),
            "OWL_ALLOW_NON_LOOPBACK": "false",
            "OWL_ALLOW_LIVE_EXTERNAL_TESTS": "false",
            "OWL_ALLOW_SYNTHETIC_CONFLUENCE_TARGETS": "false",
            "OWL_ALLOW_IN_MEMORY_SECRET_STORE": "true",
            "OWL_PDF_BENCHMARK_INTERNAL": "1",
            "CONFLUENCE_SECRET_BACKEND": "memory",
            "CONFLUENCE_BASE_URL": "",
            "CONFLUENCE_PAT": "",
            "BITBUCKET_ALLOWED_HOSTS": "",
            "PDF_MAX_EXTRACTION_WORKERS": str(worker_count),
            "PDF_MAX_EXTRACTION_WORKERS_PER_REPOSITORY": str(worker_count),
            "PDF_MAX_ACTIVE_EXTRACTION_REPOSITORIES": "1",
            "PDF_PIPELINE_CONTROLLER_MODE": "fixed",
            "PDF_PIPELINE_ADAPTIVE_ENABLED": "false",
            "PDF_PIPELINE_CONTROLLER_KILL_SWITCH": "false",
            "PDF_PIPELINE_MANUAL_FIXED_TARGET": str(worker_count),
            "PDF_PIPELINE_INITIAL_TARGET": str(worker_count),
            "PDF_PIPELINE_METRICS_ENABLED": (
                "true" if metrics_sampling_enabled else "false"
            ),
            "PDF_PIPELINE_METRICS_SNAPSHOT_ENABLED": (
                "true" if metrics_sampling_enabled else "false"
            ),
            "SEMANTIC_SEARCH_ENABLED": "false",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "NO_PROXY": "*",
        }
    )
    return environment


def _trial_command(
    *,
    plan: BenchmarkPlan,
    worker_count: int,
    repetition: int,
    result_path: Path,
) -> list[str]:
    command = [
        sys.executable,
        str(Path(settings.BASE_DIR) / "manage.py"),
        "bitbucket_pdf_pipeline_benchmark",
        "--internal-trial",
        "--workers",
        str(worker_count),
        "--repetitions",
        "1",
        "--documents",
        str(plan.document_count),
        "--repositories",
        str(plan.repository_count),
        "--pages-per-document",
        str(plan.pages_per_document),
        "--seed",
        str(plan.seed),
        "--sqlite-journal-mode",
        plan.sqlite_journal_mode,
        "--trial-timeout-seconds",
        str(plan.trial_timeout_seconds),
        "--result-path",
        str(result_path),
    ]
    if plan.metrics_sampling_enabled:
        command.append("--metrics-sampling")
    return command


def _load_trial_result(path: Path, *, worker_count: int, repetition: int) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("The isolated benchmark child did not create a valid result.") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schemaVersion") != BENCHMARK_SCHEMA_VERSION
        or payload.get("kind") != BENCHMARK_KIND
        or payload.get("workerCount") != worker_count
    ):
        raise ValueError("The isolated benchmark child returned an incompatible result.")
    payload["repetition"] = repetition
    return payload


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * fraction)


def summarize_trials(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    successful = [trial for trial in trials if trial.get("status") == "complete"]
    for worker_count in sorted({int(trial["workerCount"]) for trial in successful}):
        group = [trial for trial in successful if trial["workerCount"] == worker_count]
        durations = [float(trial["pipeline"]["durationSeconds"]) for trial in group]
        rates = [float(trial["pipeline"]["persistedDocumentsPerMinute"]) for trial in group]
        mean_rate = statistics.fmean(rates)
        standard_deviation = statistics.pstdev(rates) if len(rates) > 1 else 0.0
        summaries.append(
            {
                "workerCount": worker_count,
                "successfulTrials": len(group),
                "durationSeconds": {
                    "median": round(float(statistics.median(durations)), 6),
                    "p95": round(float(_percentile(durations, 0.95) or 0.0), 6),
                },
                "persistedDocumentsPerMinute": {
                    "median": round(float(statistics.median(rates)), 6),
                    "p95": round(float(_percentile(rates, 0.95) or 0.0), 6),
                    "populationStandardDeviation": round(standard_deviation, 6),
                    "coefficientOfVariation": (
                        round(standard_deviation / mean_rate, 6) if mean_rate else None
                    ),
                },
            }
        )
    return summaries


def run_benchmark(plan: BenchmarkPlan) -> tuple[dict[str, Any], Path]:
    """Run each fixed target in a fresh process/root and consolidate one report."""

    plan = validate_plan(plan)
    runtime_root = benchmark_runtime_root()
    runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    reports_directory = runtime_root / "reports"
    reports_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_id = new_run_id()
    run_directory = _private_directory(runtime_root / "runs" / run_id)
    report_path = reports_directory / f"{run_id}.json"
    started_at = utc_now()
    trials: list[dict[str, Any]] = []

    for worker_count in plan.worker_targets:
        for repetition in range(1, plan.repetitions + 1):
            trial_directory = _private_directory(
                run_directory / f"trial-w{worker_count:02d}-r{repetition:02d}"
            )
            data_root = trial_directory / "owl-data"
            result_path = trial_directory / "trial-result.json"
            command = _trial_command(
                plan=plan,
                worker_count=worker_count,
                repetition=repetition,
                result_path=result_path,
            )
            try:
                completed = subprocess.run(
                    command,
                    cwd=settings.BASE_DIR,
                    env=_child_environment(data_root=data_root, worker_count=worker_count),
                    capture_output=True,
                    text=True,
                    timeout=plan.trial_timeout_seconds,
                    check=False,
                )
                if completed.returncode != 0:
                    trials.append(
                        {
                            "schemaVersion": BENCHMARK_SCHEMA_VERSION,
                            "kind": BENCHMARK_KIND,
                            "status": "failed",
                            "failureCode": "trial_process_failed",
                            "returnCode": completed.returncode,
                            "workerCount": worker_count,
                            "repetition": repetition,
                        }
                    )
                else:
                    trials.append(
                        _load_trial_result(
                            result_path,
                            worker_count=worker_count,
                            repetition=repetition,
                        )
                    )
            except subprocess.TimeoutExpired:
                trials.append(
                    {
                        "schemaVersion": BENCHMARK_SCHEMA_VERSION,
                        "kind": BENCHMARK_KIND,
                        "status": "failed",
                        "failureCode": "trial_timeout",
                        "workerCount": worker_count,
                        "repetition": repetition,
                    }
                )
            except OSError:
                trials.append(
                    {
                        "schemaVersion": BENCHMARK_SCHEMA_VERSION,
                        "kind": BENCHMARK_KIND,
                        "status": "failed",
                        "failureCode": "trial_launch_failed",
                        "workerCount": worker_count,
                        "repetition": repetition,
                    }
                )
            except ValueError:
                trials.append(
                    {
                        "schemaVersion": BENCHMARK_SCHEMA_VERSION,
                        "kind": BENCHMARK_KIND,
                        "status": "failed",
                        "failureCode": "invalid_trial_result",
                        "workerCount": worker_count,
                        "repetition": repetition,
                    }
                )
            finally:
                if not plan.keep_trial_data:
                    _safe_remove_trial(trial_directory, run_directory=run_directory)

    failed_trials = sum(trial.get("status") != "complete" for trial in trials)
    report = {
        "schemaVersion": BENCHMARK_SCHEMA_VERSION,
        "kind": BENCHMARK_KIND,
        "runId": run_id,
        "status": "complete" if failed_trials == 0 else "completed_with_errors",
        "startedAt": started_at.isoformat(),
        "completedAt": utc_now().isoformat(),
        "controllerMode": "fixed",
        "productionPipelineModified": False,
        "isolation": {
            "freshDataRootAndDatabasePerTrial": True,
            "externalNetworkAllowed": False,
            "semanticIndexingEnabled": False,
            "trialDataRetained": plan.keep_trial_data,
            "reportsBelowIgnoredRuntimeData": True,
        },
        "workload": {
            "synthetic": True,
            "documentCount": plan.document_count,
            "repositoryCount": plan.repository_count,
            "pagesPerDocument": plan.pages_per_document,
            "seed": plan.seed,
            "workerTargets": list(plan.worker_targets),
            "repetitions": plan.repetitions,
        },
        "machine": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "logicalCpuCount": os.cpu_count(),
        },
        "summary": summarize_trials(trials),
        "trials": trials,
        "failedTrialCount": failed_trials,
        "limitations": [
            "This calibration scaffold measures the existing PDF extraction, durable staging, and publication path.",
            "Semantic concurrency, concurrent browser/search load, cold-cache control, host I/O, thermal state, and representative 20,000-25,000 PDF evidence require the later benchmark phase.",
            "A report from this harness is not by itself permission to enable adaptive mode or exceed the tested ceiling of eight extractors.",
        ],
    }
    _atomic_json(report_path, report)
    if not plan.keep_trial_data:
        run_directory.rmdir()
    return report, report_path


def _escape_pdf_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _write_text_pdf(path: Path, page_texts: list[str]) -> None:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    for text in page_texts:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference})}
        )
        stream = DecodedStreamObject()
        stream.set_data(
            f"BT /F1 10 Tf 20 720 Td ({_escape_pdf_literal(text)}) Tj ET".encode("ascii")
        )
        page[NameObject("/Contents")] = writer._add_object(stream)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("xb") as target:
        writer.write(target)


def _synthetic_page_text(
    *, document_number: int, page_number: int, seed: int, target_characters: int
) -> str:
    prefix = (
        f"owl synthetic pdf benchmark seed {seed} document {document_number} "
        f"page {page_number} benchmarktoken{document_number:06d} "
    )
    filler = "durable stage isolated parser controlled sqlite publisher "
    return (prefix + (filler * ((target_characters // len(filler)) + 1)))[:target_characters]


def _seed_synthetic_corpus(plan: BenchmarkPlan) -> dict[str, Any]:
    from bitbucket_search.models import (
        BitbucketRepository,
        PDFDocument,
        RepositorySyncState,
    )
    from bitbucket_search.services.git_sync import managed_repository_path
    from bitbucket_search.services.pdf_indexing import queue_repository_pdf_extractions

    repositories = []
    for repository_number in range(plan.repository_count):
        commit = hashlib.sha1(f"owl-benchmark-{plan.seed}-{repository_number}".encode()).hexdigest()
        repository = BitbucketRepository.objects.create(
            display_name=f"Synthetic benchmark repository {repository_number + 1}",
            canonical_remote_key=(
                f"benchmark.example.invalid/synthetic/repository-{repository_number + 1}"
            ),
            remote_url=(
                "https://benchmark.example.invalid/synthetic/"
                f"repository-{repository_number + 1}.git"
            ),
            sync_state=RepositorySyncState.READY,
            last_synced_commit=commit,
        )
        checkout = managed_repository_path(repository)
        (checkout / ".git").mkdir(mode=0o700, parents=True)
        repository.local_path = str(checkout)
        repository.save(update_fields=("local_path", "updated_at"))
        repositories.append((repository, checkout, commit))

    corpus_digest = hashlib.sha256()
    tier_counts: Counter[str] = Counter()
    duplicate_count = 0
    prior_bytes: bytes | None = None
    source_bytes = 0
    for document_number in range(plan.document_count):
        repository, checkout, commit = repositories[document_number % len(repositories)]
        tier_index = document_number % len(_TIER_CHARACTER_TARGETS)
        tier_name = ("tiny", "medium", "large", "very_large")[tier_index]
        tier_counts[tier_name] += 1
        relative_path = f"documents/{document_number:06d}-{tier_name}.pdf"
        target = checkout / relative_path
        if prior_bytes is not None and (document_number + 1) % 9 == 0:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_bytes(prior_bytes)
            duplicate_count += 1
        else:
            page_texts = [
                _synthetic_page_text(
                    document_number=document_number,
                    page_number=page_number,
                    seed=plan.seed,
                    target_characters=_TIER_CHARACTER_TARGETS[tier_index],
                )
                for page_number in range(1, plan.pages_per_document + 1)
            ]
            _write_text_pdf(target, page_texts)
            prior_bytes = target.read_bytes()
        content = target.read_bytes()
        source_bytes += len(content)
        corpus_digest.update(hashlib.sha256(content).digest())
        PDFDocument.objects.create(
            repository=repository,
            filename=target.name,
            relative_path=relative_path,
            file_size=len(content),
            git_blob_id=hashlib.sha1(content).hexdigest(),
            last_seen_commit=commit,
        )

    queued_count = 0
    for repository, _checkout, _commit in repositories:
        queued_count += len(queue_repository_pdf_extractions(repository).queued_job_ids)
    return {
        "corpusSha256": corpus_digest.hexdigest(),
        "sourceBytes": source_bytes,
        "queuedDocuments": queued_count,
        "duplicateDocumentCount": duplicate_count,
        "tierCounts": dict(sorted(tier_counts.items())),
    }


def _resource_usage() -> dict[str, float | int | None]:
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return {
            "selfCpuSeconds": None,
            "childCpuSeconds": None,
            "selfPeakRssRaw": None,
            "childPeakRssRaw": None,
        }
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {
        "selfCpuSeconds": own.ru_utime + own.ru_stime,
        "childCpuSeconds": children.ru_utime + children.ru_stime,
        "selfPeakRssRaw": own.ru_maxrss,
        "childPeakRssRaw": children.ru_maxrss,
    }


def _usage_delta(
    before: dict[str, float | int | None], after: dict[str, float | int | None]
) -> dict[str, float | int | None]:
    result = dict(after)
    for field in ("selfCpuSeconds", "childCpuSeconds"):
        earlier = before[field]
        later = after[field]
        result[field] = (
            round(float(later) - float(earlier), 6)
            if earlier is not None and later is not None
            else None
        )
    return result


def run_internal_worker(*, role: str, timeout_seconds: int) -> None:
    """Run one benchmark-owned controller process without the resident watchdog."""

    from django.db import close_old_connections, connections

    from bitbucket_search.models import PDFExtractionJob, PDFExtractionJobStatus
    from bitbucket_search.services.pdf_indexing import (
        work_one_extraction_job,
        work_one_publication_job,
    )

    if role not in {"extractor", "publisher"}:
        raise ValueError("Internal benchmark worker role is invalid.")
    if not 30 <= timeout_seconds <= 86_400:
        raise ValueError("Internal benchmark timeout is invalid.")
    data_root = Path(settings.OWL_DATA_ROOT).resolve()
    if (
        os.environ.get("OWL_PDF_BENCHMARK_INTERNAL") != "1"
        or data_root.name != "owl-data"
        or not data_root.parent.is_relative_to(benchmark_runtime_root())
    ):
        raise ValueError("Internal benchmark workers require a generated isolated data root.")
    deadline = time.monotonic() + timeout_seconds
    try:
        while time.monotonic() < deadline:
            close_old_connections()
            completed = (
                work_one_publication_job() if role == "publisher" else work_one_extraction_job()
            )
            close_old_connections()
            if completed is not None:
                continue
            active = PDFExtractionJob.objects.filter(
                status__in=(PDFExtractionJobStatus.QUEUED, PDFExtractionJobStatus.RUNNING)
            ).exists()
            close_old_connections()
            if not active:
                return
            time.sleep(0.02)
    finally:
        connections.close_all()
    raise RuntimeError("The benchmark worker exceeded its trial deadline.")


def _stop_trial_processes(processes: list[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _drain_existing_pipeline(*, timeout_seconds: int) -> float:
    from django.db import close_old_connections, connections

    from bitbucket_search.models import PDFExtractionJob, PDFExtractionJobStatus

    connections.close_all()
    started = time.monotonic()
    base_command = [
        sys.executable,
        str(Path(settings.BASE_DIR) / "manage.py"),
        "bitbucket_pdf_pipeline_benchmark",
        "--trial-timeout-seconds",
        str(timeout_seconds),
    ]
    processes: list[subprocess.Popen[bytes]] = []
    deadline = started + timeout_seconds
    try:
        for _number in range(settings.PDF_MAX_EXTRACTION_WORKERS):
            processes.append(
                subprocess.Popen(
                    [*base_command, "--internal-worker-role", "extractor"],
                    cwd=settings.BASE_DIR,
                    env=os.environ.copy(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
            )
        processes.append(
            subprocess.Popen(
                [*base_command, "--internal-worker-role", "publisher"],
                cwd=settings.BASE_DIR,
                env=os.environ.copy(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        )
        while time.monotonic() < deadline:
            if any(process.poll() not in (None, 0) for process in processes):
                raise RuntimeError("An isolated benchmark worker process failed.")
            close_old_connections()
            active = PDFExtractionJob.objects.filter(
                status__in=(PDFExtractionJobStatus.QUEUED, PDFExtractionJobStatus.RUNNING)
            ).exists()
            close_old_connections()
            if not active:
                break
            time.sleep(0.02)
        else:
            raise RuntimeError("The isolated benchmark pipeline exceeded its trial deadline.")

        remaining_wait = max(1.0, min(10.0, deadline - time.monotonic()))
        for process in processes:
            try:
                return_code = process.wait(timeout=remaining_wait)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("An isolated benchmark worker did not stop cleanly.") from exc
            if return_code != 0:
                raise RuntimeError("An isolated benchmark worker process failed.")
    finally:
        _stop_trial_processes(processes)
        connections.close_all()
    return time.monotonic() - started


def _database_bytes() -> int:
    database_root = Path(settings.DATABASE_ROOT)
    return sum(path.stat().st_size for path in database_root.rglob("*") if path.is_file())


def run_internal_trial(
    *, plan: BenchmarkPlan, worker_count: int, result_path: Path
) -> dict[str, Any]:
    """Create and measure one corpus inside an already isolated child process."""

    validate_plan(plan)
    data_root = Path(settings.OWL_DATA_ROOT).resolve()
    trial_directory = data_root.parent.resolve()
    resolved_result_path = result_path.resolve()
    if (
        os.environ.get("OWL_PDF_BENCHMARK_INTERNAL") != "1"
        or data_root.name != "owl-data"
        or resolved_result_path.parent != trial_directory
        or not trial_directory.is_relative_to(benchmark_runtime_root())
    ):
        raise ValueError("Internal benchmark output must stay inside its isolated trial root.")
    if worker_count != settings.PDF_MAX_EXTRACTION_WORKERS:
        raise ValueError("The child worker setting does not match the measured fixed target.")

    trial_started_at = utc_now()
    call_command("migrate", interactive=False, verbosity=0)
    setup_started = time.monotonic()
    workload = _seed_synthetic_corpus(plan)
    setup_duration = time.monotonic() - setup_started
    before_usage = _resource_usage()
    pipeline_duration = _drain_existing_pipeline(timeout_seconds=plan.trial_timeout_seconds)
    after_usage = _resource_usage()

    from bitbucket_search.models import (
        PDFDocument,
        PDFExtractionJob,
        PDFExtractionJobStatus,
        PDFTextPage,
        PDFTextRevision,
    )

    jobs = list(PDFExtractionJob.objects.all())
    job_durations = [
        (job.completed_at - job.started_at).total_seconds()
        for job in jobs
        if job.started_at is not None and job.completed_at is not None
    ]
    persisted_documents = PDFDocument.objects.filter(indexed_revision__isnull=False).count()
    succeeded = sum(job.status == PDFExtractionJobStatus.SUCCEEDED for job in jobs)
    failed = sum(job.status == PDFExtractionJobStatus.FAILED for job in jobs)
    cancelled = sum(job.status == PDFExtractionJobStatus.CANCELLED for job in jobs)
    active = sum(
        job.status in (PDFExtractionJobStatus.QUEUED, PDFExtractionJobStatus.RUNNING)
        for job in jobs
    )
    persisted_pages = PDFTextPage.objects.count()
    extracted_characters = sum(
        PDFDocument.objects.values_list("extracted_character_count", flat=True)
    )
    rate = (persisted_documents / pipeline_duration) * 60 if pipeline_duration else 0.0
    correctness_ok = (
        len(jobs) == plan.document_count
        and active == 0
        and succeeded + failed + cancelled == plan.document_count
        and persisted_documents == succeeded
    )
    payload = {
        "schemaVersion": BENCHMARK_SCHEMA_VERSION,
        "kind": BENCHMARK_KIND,
        "status": "complete" if correctness_ok else "failed",
        "workerCount": worker_count,
        "startedAt": trial_started_at.isoformat(),
        "workload": workload,
        "setupDurationSeconds": round(setup_duration, 6),
        "pipeline": {
            "durationSeconds": round(pipeline_duration, 6),
            "persistedDocuments": persisted_documents,
            "persistedPages": persisted_pages,
            "sourceBytes": workload["sourceBytes"],
            "extractedCharacters": extracted_characters,
            "persistedDocumentsPerMinute": round(rate, 6),
            "jobLatencySeconds": {
                "p50": round(float(_percentile(job_durations, 0.50) or 0.0), 6),
                "p95": round(float(_percentile(job_durations, 0.95) or 0.0), 6),
                "sampleCount": len(job_durations),
            },
            "statusCounts": {
                "succeeded": succeeded,
                "failed": failed,
                "cancelled": cancelled,
                "active": active,
            },
            "revisionCount": PDFTextRevision.objects.count(),
            "databaseBytes": _database_bytes(),
        },
        "resources": _usage_delta(before_usage, after_usage),
        "correctness": {
            "allJobsTerminal": active == 0,
            "oneJobPerDocument": len(jobs) == plan.document_count,
            "persistedDocumentCountMatchesSuccesses": persisted_documents == succeeded,
            "passed": correctness_ok,
        },
        "completedAt": utc_now().isoformat(),
    }
    _atomic_json(resolved_result_path, payload)
    return payload
