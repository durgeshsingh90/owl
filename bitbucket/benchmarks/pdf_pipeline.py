"""Repeatable synthetic benchmark harness for the existing PDF pipeline.

The public command is an orchestrator only. Every measured trial starts a fresh
Django process whose ``OWL_DATA_ROOT`` points below the repository's ignored
``var/benchmarks`` runtime directory. This prevents a benchmark from reading or
writing the user's canonical OWL database, repositories, or extracted content.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management import call_command
from django.db import connection, connections

BENCHMARK_SCHEMA_VERSION = 2
BENCHMARK_KIND = "owl.synthetic-pdf-pipeline-benchmark"
DEFAULT_WORKER_TARGETS = (8, 12, 16)
FULL_WORKER_MATRIX = (1, 2, 4, 8, 12, 16)
MAX_TESTED_WORKERS = 16
SQLITE_JOURNAL_MODES = ("delete", "wal")
FOREGROUND_PROBE_INTERVAL_SECONDS = 0.2
_SYNTHETIC_SECRET_KEY = "synthetic-test-secret-key-only-not-for-real-use-pdf-benchmark-0123456789"
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
    per_repository_worker_limit: int | None = None
    repository_work_conserving: bool = True
    reuse_parent_fingerprint: bool = True
    publication_page_batch_size: int = 100
    duplicate_page_index: bool = False
    source_padding_bytes_per_document: int = 0
    include_failure_fixtures: bool = False


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
    if plan.per_repository_worker_limit is not None and (
        isinstance(plan.per_repository_worker_limit, bool)
        or not 1 <= plan.per_repository_worker_limit <= MAX_TESTED_WORKERS
    ):
        raise ValueError(f"Per-repository workers must be between 1 and {MAX_TESTED_WORKERS}.")
    if not isinstance(plan.repository_work_conserving, bool):
        raise ValueError("Repository work-conservation selection must be true or false.")
    if not isinstance(plan.reuse_parent_fingerprint, bool):
        raise ValueError("Parent-fingerprint reuse selection must be true or false.")
    if (
        isinstance(plan.publication_page_batch_size, bool)
        or not 1 <= plan.publication_page_batch_size <= 1_000
    ):
        raise ValueError("Publication page batch size must be between 1 and 1000.")
    if not isinstance(plan.duplicate_page_index, bool):
        raise ValueError("Duplicate page-index selection must be true or false.")
    if (
        isinstance(plan.source_padding_bytes_per_document, bool)
        or not 0 <= plan.source_padding_bytes_per_document <= 10_000_000
    ):
        raise ValueError("Source padding per document must be between 0 and 10000000 bytes.")
    if not isinstance(plan.include_failure_fixtures, bool):
        raise ValueError("Failure-fixture selection must be true or false.")
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
    per_repository_worker_limit: int | None = None,
    repository_work_conserving: bool = True,
    reuse_parent_fingerprint: bool = True,
    publication_page_batch_size: int = 100,
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
            "PDF_MAX_EXTRACTION_WORKERS": str(worker_count),
            "PDF_MAX_EXTRACTION_WORKERS_PER_REPOSITORY": str(
                min(worker_count, per_repository_worker_limit or worker_count)
            ),
            "PDF_MAX_ACTIVE_EXTRACTION_REPOSITORIES": "1",
            "PDF_PIPELINE_REPOSITORY_WORK_CONSERVING": (
                "true" if repository_work_conserving else "false"
            ),
            "PDF_PIPELINE_REUSE_PARENT_FINGERPRINT": (
                "true" if reuse_parent_fingerprint else "false"
            ),
            "PDF_PUBLICATION_PAGE_BATCH_SIZE": str(publication_page_batch_size),
            "PDF_PIPELINE_CONTROLLER_MODE": "fixed",
            "PDF_PIPELINE_ADAPTIVE_ENABLED": "false",
            "PDF_PIPELINE_CONTROLLER_KILL_SWITCH": "false",
            "PDF_PIPELINE_MANUAL_FIXED_TARGET": str(worker_count),
            "PDF_PIPELINE_INITIAL_TARGET": str(worker_count),
            "PDF_PIPELINE_METRICS_ENABLED": ("true" if metrics_sampling_enabled else "false"),
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
        "bitbucket_app_pdf_pipeline_benchmark",
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
    if plan.per_repository_worker_limit is not None:
        command.extend(("--per-repository-workers", str(plan.per_repository_worker_limit)))
    if not plan.repository_work_conserving:
        command.append("--strict-repository-locality")
    if not plan.reuse_parent_fingerprint:
        command.append("--repeat-child-prehash")
    command.extend(("--publication-page-batch-size", str(plan.publication_page_batch_size)))
    if plan.duplicate_page_index:
        command.append("--with-duplicate-page-index")
    if plan.source_padding_bytes_per_document:
        command.extend(
            (
                "--source-padding-bytes-per-document",
                str(plan.source_padding_bytes_per_document),
            )
        )
    if plan.include_failure_fixtures:
        command.append("--include-failure-fixtures")
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
                    env=_child_environment(
                        data_root=data_root,
                        worker_count=worker_count,
                        metrics_sampling_enabled=plan.metrics_sampling_enabled,
                        per_repository_worker_limit=plan.per_repository_worker_limit,
                        repository_work_conserving=plan.repository_work_conserving,
                        reuse_parent_fingerprint=plan.reuse_parent_fingerprint,
                        publication_page_batch_size=plan.publication_page_batch_size,
                    ),
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
            "sqliteJournalMode": plan.sqlite_journal_mode,
            "metricsSamplingEnabled": plan.metrics_sampling_enabled,
            "perRepositoryWorkerLimit": plan.per_repository_worker_limit,
            "repositoryWorkConserving": plan.repository_work_conserving,
            "reuseParentFingerprint": plan.reuse_parent_fingerprint,
            "publicationPageBatchSize": plan.publication_page_batch_size,
            "duplicatePageIndex": plan.duplicate_page_index,
            "sourcePaddingBytesPerDocument": plan.source_padding_bytes_per_document,
            "failureFixturesIncluded": plan.include_failure_fixtures,
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
            "This harness measures the existing PDF extraction, durable staging, and publication path with concurrent exact-search and dashboard-payload probes.",
            "The foreground probes call the production read services directly; visible browser rendering and network latency require separate UI validation.",
            "Semantic-model concurrency, reproducible cold-cache control, thermal state, controlled recovery injection, and representative 20,000-25,000 PDF / roughly 50 GB evidence remain required before the gate can pass.",
            "A report from this harness is not by itself permission to enable adaptive mode or exceed the tested ceiling of eight extractors.",
        ],
    }
    _atomic_json(report_path, report)
    if not plan.keep_trial_data:
        run_directory.rmdir()
    return report, report_path


def _escape_pdf_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _write_text_pdf(
    path: Path,
    page_texts: list[str],
    *,
    source_padding_bytes: int = 0,
    encrypted: bool = False,
) -> None:
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
    if source_padding_bytes:
        padding = DecodedStreamObject()
        padding.set_data(b"0" * source_padding_bytes)
        writer._add_object(padding)
    if encrypted:
        writer.encrypt("placeholder")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("xb") as target:
        writer.write(target)


def _write_blank_pdf(path: Path, *, page_count: int, source_padding_bytes: int = 0) -> None:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject

    writer = PdfWriter()
    for _page_number in range(page_count):
        writer.add_blank_page(width=612, height=792)
    if source_padding_bytes:
        padding = DecodedStreamObject()
        padding.set_data(b"0" * source_padding_bytes)
        writer._add_object(padding)
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
    from bitbucket.models import (
        BitbucketRepository,
        PDFDocument,
        RepositorySyncState,
    )
    from bitbucket.services.git_sync import managed_repository_path
    from bitbucket.services.pdf_indexing import queue_repository_pdf_extractions

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
    failure_fixture_counts: Counter[str] = Counter()
    prior_bytes: bytes | None = None
    source_bytes = 0
    for document_number in range(plan.document_count):
        repository, checkout, commit = repositories[document_number % len(repositories)]
        tier_index = document_number % len(_TIER_CHARACTER_TARGETS)
        tier_name = ("tiny", "medium", "large", "very_large")[tier_index]
        tier_counts[tier_name] += 1
        relative_path = f"documents/{document_number:06d}-{tier_name}.pdf"
        target = checkout / relative_path
        fixture_offset = plan.document_count - document_number
        fixture_kind = (
            {1: "malformed", 2: "encrypted", 3: "blank"}.get(fixture_offset)
            if plan.include_failure_fixtures and plan.document_count >= 3
            else None
        )
        padding_multiplier = (0.25, 0.75, 1.25, 1.75)[tier_index]
        source_padding_bytes = round(plan.source_padding_bytes_per_document * padding_multiplier)
        if fixture_kind == "malformed":
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_bytes(b"%PDF-1.7\nmalformed benchmark fixture\n")
            failure_fixture_counts[fixture_kind] += 1
        elif fixture_kind == "encrypted":
            page_texts = [
                _synthetic_page_text(
                    document_number=document_number,
                    page_number=page_number,
                    seed=plan.seed,
                    target_characters=_TIER_CHARACTER_TARGETS[tier_index],
                )
                for page_number in range(1, plan.pages_per_document + 1)
            ]
            _write_text_pdf(
                target,
                page_texts,
                source_padding_bytes=source_padding_bytes,
                encrypted=True,
            )
            failure_fixture_counts[fixture_kind] += 1
        elif fixture_kind == "blank":
            _write_blank_pdf(
                target,
                page_count=plan.pages_per_document,
                source_padding_bytes=source_padding_bytes,
            )
            failure_fixture_counts[fixture_kind] += 1
        elif prior_bytes is not None and (document_number + 1) % 9 == 0:
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
            _write_text_pdf(
                target,
                page_texts,
                source_padding_bytes=source_padding_bytes,
            )
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
        "failureFixtureCounts": dict(sorted(failure_fixture_counts.items())),
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

    from bitbucket.models import PDFExtractionJob, PDFExtractionJobStatus
    from bitbucket.services.pdf_indexing import (
        work_one_extraction_job,
        work_one_publication_job,
        work_one_staging_job,
    )
    from bitbucket.services.pdf_jsonl_staging import JSONLStager

    if role not in {"extractor", "stager", "publisher"}:
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
    stager = JSONLStager() if role == "stager" else None
    try:
        while time.monotonic() < deadline:
            close_old_connections()
            if role == "publisher":
                completed = work_one_publication_job()
            elif role == "stager":
                completed = work_one_staging_job(stager)
            else:
                completed = work_one_extraction_job()
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
        if role == "publisher":
            from bitbucket.services.pdf_runtime_metrics import (
                flush_publisher_runtime_metrics,
            )

            flush_publisher_runtime_metrics()
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


def _drain_existing_pipeline(
    *, timeout_seconds: int
) -> tuple[float, dict[str, int | str | None], dict[str, Any]]:
    from django.db import close_old_connections, connections

    from bitbucket.models import PDFExtractionJob, PDFExtractionJobStatus

    connections.close_all()
    started = time.monotonic()
    base_command = [
        sys.executable,
        str(Path(settings.BASE_DIR) / "manage.py"),
        "bitbucket_app_pdf_pipeline_benchmark",
        "--trial-timeout-seconds",
        str(timeout_seconds),
    ]
    processes: list[subprocess.Popen[bytes]] = []
    resident_roles: dict[str, subprocess.Popen[bytes]] = {}
    metrics_enabled = bool(getattr(settings, "PDF_PIPELINE_METRICS_ENABLED", False))
    metrics_interval = max(1.0, float(getattr(settings, "PDF_PIPELINE_METRICS_SAMPLE_SECONDS", 5)))
    next_metrics_sample = started
    metrics_samples = 0
    metrics_errors = 0
    concurrent_probe = _new_concurrent_probe_state()
    next_foreground_probe = started
    deadline = started + timeout_seconds
    try:
        for worker_number in range(1, settings.PDF_MAX_EXTRACTION_WORKERS + 1):
            process = subprocess.Popen(
                [*base_command, "--internal-worker-role", "extractor"],
                cwd=settings.BASE_DIR,
                env=os.environ.copy(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            processes.append(process)
            resident_roles[f"pdf-index-{worker_number}"] = process
        stager = subprocess.Popen(
            [*base_command, "--internal-worker-role", "stager"],
            cwd=settings.BASE_DIR,
            env=os.environ.copy(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        processes.append(stager)
        resident_roles["pdf-stager-1"] = stager
        publisher = subprocess.Popen(
            [*base_command, "--internal-worker-role", "publisher"],
            cwd=settings.BASE_DIR,
            env=os.environ.copy(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        processes.append(publisher)
        resident_roles["pdf-writer-1"] = publisher
        while time.monotonic() < deadline:
            if any(process.poll() not in (None, 0) for process in processes):
                raise RuntimeError("An isolated benchmark worker process failed.")
            close_old_connections()
            active = PDFExtractionJob.objects.filter(
                status__in=(PDFExtractionJobStatus.QUEUED, PDFExtractionJobStatus.RUNNING)
            ).exists()
            close_old_connections()
            if metrics_enabled and time.monotonic() >= next_metrics_sample:
                try:
                    from bitbucket.services.pdf_pipeline_metrics import (
                        sample_pipeline_metrics,
                    )

                    sample_pipeline_metrics(resident_roles=resident_roles)
                except Exception:
                    metrics_errors += 1
                else:
                    metrics_samples += 1
                finally:
                    close_old_connections()
                    next_metrics_sample = time.monotonic() + metrics_interval
            if time.monotonic() >= next_foreground_probe:
                try:
                    _sample_concurrent_probe(
                        concurrent_probe,
                        resident_roles=resident_roles,
                        elapsed_seconds=time.monotonic() - started,
                    )
                finally:
                    close_old_connections()
                    next_foreground_probe = time.monotonic() + FOREGROUND_PROBE_INTERVAL_SECONDS
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
    duration = time.monotonic() - started
    return (
        duration,
        {
            "state": "enabled" if metrics_enabled else "disabled",
            "samples": metrics_samples,
            "errors": metrics_errors,
        },
        _concurrent_probe_summary(concurrent_probe, duration_seconds=duration),
    )


def _database_bytes() -> int:
    database_root = Path(settings.DATABASE_ROOT)
    return sum(path.stat().st_size for path in database_root.rglob("*") if path.is_file())


def _latency_summary(values: list[float]) -> dict[str, float | int]:
    return {
        "p50": round(float(_percentile(values, 0.50) or 0.0), 6),
        "p95": round(float(_percentile(values, 0.95) or 0.0), 6),
        "sampleCount": len(values),
    }


def _new_concurrent_probe_state() -> dict[str, Any]:
    return {
        "exactSearchMs": [],
        "exactSearchRequests": 0,
        "exactSearchFailures": 0,
        "exactSearchResultBearing": 0,
        "dashboardMs": [],
        "dashboardRequests": 0,
        "dashboardFailures": 0,
        "backpressureDepthJobs": [],
        "stagedBytes": [],
        "oldestStagedWaitSeconds": [],
        "hostCpuPct": [],
        "owlProcessTreeCpuPct": [],
        "owlProcessTreeRssBytes": [],
        "hostMemoryAvailableBytes": [],
        "diskAvailableBytes": [],
        "totalEtaForecasts": [],
        "repositoryEtaForecasts": {},
    }


def _append_numeric(target: list[float], value: object) -> None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    ):
        target.append(float(value))


def _sample_concurrent_probe(
    state: dict[str, Any],
    *,
    resident_roles: dict[str, subprocess.Popen[bytes]],
    elapsed_seconds: float,
) -> None:
    """Issue read-only foreground probes while the isolated writer is active."""

    from django.db.models import Count, Q

    from bitbucket.models import PDFExtractionJob
    from bitbucket.services.pdf_pipeline_metrics import (
        build_pipeline_metrics_payload,
    )
    from bitbucket.services.pdf_search import search_documents
    from bitbucket.services.pdf_search_query import PDFSearchQuery, PDFSearchScope

    state["exactSearchRequests"] += 1
    started = time.perf_counter()
    try:
        result = search_documents(
            PDFSearchQuery(
                chips=("benchmarktoken000000",),
                scopes=(PDFSearchScope.CONTENT,),
            )
        )
    except Exception:
        state["exactSearchFailures"] += 1
    else:
        state["exactSearchMs"].append((time.perf_counter() - started) * 1_000)
        if result.total:
            state["exactSearchResultBearing"] += 1

    state["dashboardRequests"] += 1
    started = time.perf_counter()
    try:
        payload = build_pipeline_metrics_payload(resident_roles=resident_roles)
    except Exception:
        state["dashboardFailures"] += 1
    else:
        state["dashboardMs"].append((time.perf_counter() - started) * 1_000)
        queues = payload.get("queues") if isinstance(payload, dict) else None
        resources = payload.get("resources") if isinstance(payload, dict) else None
        if isinstance(queues, dict):
            for field in (
                "backpressureDepthJobs",
                "stagedBytes",
                "oldestStagedWaitSeconds",
            ):
                _append_numeric(state[field], queues.get(field))
        if isinstance(resources, dict):
            for field in (
                "hostCpuPct",
                "owlProcessTreeCpuPct",
                "owlProcessTreeRssBytes",
                "hostMemoryAvailableBytes",
                "diskAvailableBytes",
            ):
                _append_numeric(state[field], resources.get(field))

    progress = PDFExtractionJob.objects.aggregate(
        total=Count("id"),
        completed=Count("id", filter=Q(completed_at__isnull=False)),
    )
    total = int(progress["total"] or 0)
    completed = int(progress["completed"] or 0)
    if completed >= 3 and completed < total and elapsed_seconds > 0:
        state["totalEtaForecasts"].append(
            {
                "elapsedSeconds": elapsed_seconds,
                "predictedSeconds": (total - completed) / (completed / elapsed_seconds),
            }
        )
    repository_rows = PDFExtractionJob.objects.values("document__repository_id").annotate(
        total=Count("id"),
        completed=Count("id", filter=Q(completed_at__isnull=False)),
    )
    repository_forecasts = state["repositoryEtaForecasts"]
    for row in repository_rows:
        repository_total = int(row["total"] or 0)
        repository_completed = int(row["completed"] or 0)
        if repository_completed < 2 or repository_completed >= repository_total:
            continue
        repository_id = str(row["document__repository_id"])
        repository_forecasts.setdefault(repository_id, []).append(
            {
                "elapsedSeconds": elapsed_seconds,
                "predictedSeconds": (
                    (repository_total - repository_completed)
                    / (repository_completed / elapsed_seconds)
                ),
            }
        )


def _forecast_summary(
    forecasts: list[dict[str, float]], *, duration_seconds: float
) -> dict[str, Any]:
    absolute_percentage_errors: list[float] = []
    bias_percentages: list[float] = []
    for forecast in forecasts:
        actual = duration_seconds - float(forecast["elapsedSeconds"])
        if actual <= 0.01:
            continue
        error = float(forecast["predictedSeconds"]) - actual
        absolute_percentage_errors.append(abs(error) * 100 / actual)
        bias_percentages.append(error * 100 / actual)
    return {
        "checkpointCount": len(absolute_percentage_errors),
        "medianAbsolutePercentageError": (
            round(float(statistics.median(absolute_percentage_errors)), 3)
            if absolute_percentage_errors
            else None
        ),
        "meanBiasPercentage": (
            round(float(statistics.fmean(bias_percentages)), 3) if bias_percentages else None
        ),
        "overEstimateCount": sum(value > 0 for value in bias_percentages),
        "underEstimateCount": sum(value < 0 for value in bias_percentages),
    }


def _concurrent_probe_summary(state: dict[str, Any], *, duration_seconds: float) -> dict[str, Any]:
    exact_requests = int(state["exactSearchRequests"])
    dashboard_requests = int(state["dashboardRequests"])
    repository_forecasts = state["repositoryEtaForecasts"]
    return {
        "sampleIntervalSeconds": FOREGROUND_PROBE_INTERVAL_SECONDS,
        "exactSearch": {
            **_latency_summary(state["exactSearchMs"]),
            "requestCount": exact_requests,
            "failureCount": int(state["exactSearchFailures"]),
            "availabilityPct": (
                round(
                    (exact_requests - int(state["exactSearchFailures"])) * 100 / exact_requests,
                    3,
                )
                if exact_requests
                else None
            ),
            "resultBearingCount": int(state["exactSearchResultBearing"]),
        },
        "dashboard": {
            **_latency_summary(state["dashboardMs"]),
            "requestCount": dashboard_requests,
            "failureCount": int(state["dashboardFailures"]),
            "availabilityPct": (
                round(
                    (dashboard_requests - int(state["dashboardFailures"]))
                    * 100
                    / dashboard_requests,
                    3,
                )
                if dashboard_requests
                else None
            ),
        },
        "queues": {
            "maximumBackpressureDepthJobs": int(max(state["backpressureDepthJobs"], default=0)),
            "maximumStagedBytes": int(max(state["stagedBytes"], default=0)),
            "maximumOldestStagedWaitSeconds": round(
                max(state["oldestStagedWaitSeconds"], default=0.0), 3
            ),
        },
        "resources": {
            "maximumHostCpuPct": (
                round(max(state["hostCpuPct"]), 3) if state["hostCpuPct"] else None
            ),
            "maximumOwlProcessTreeCpuPct": (
                round(max(state["owlProcessTreeCpuPct"]), 3)
                if state["owlProcessTreeCpuPct"]
                else None
            ),
            "maximumOwlProcessTreeRssBytes": (
                int(max(state["owlProcessTreeRssBytes"]))
                if state["owlProcessTreeRssBytes"]
                else None
            ),
            "minimumHostMemoryAvailableBytes": (
                int(min(state["hostMemoryAvailableBytes"]))
                if state["hostMemoryAvailableBytes"]
                else None
            ),
            "minimumDiskAvailableBytes": (
                int(min(state["diskAvailableBytes"])) if state["diskAvailableBytes"] else None
            ),
        },
        "etaCalibration": {
            "workloadClass": "mixed_tier_generated",
            "warmupSeconds": (
                round(float(state["totalEtaForecasts"][0]["elapsedSeconds"]), 3)
                if state["totalEtaForecasts"]
                else None
            ),
            "total": _forecast_summary(
                state["totalEtaForecasts"], duration_seconds=duration_seconds
            ),
            "repositories": {
                repository_id: _forecast_summary(
                    forecasts,
                    duration_seconds=duration_seconds,
                )
                for repository_id, forecasts in sorted(repository_forecasts.items())
            },
        },
    }


def _sqlite_connection_and_index_snapshot() -> dict[str, Any]:
    """Measure connection setup and record the exact SQLite/index contract."""

    connections.close_all()
    started = time.monotonic()
    connection.ensure_connection()
    connection_setup_ms = (time.monotonic() - started) * 1_000
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA busy_timeout")
        busy_timeout_ms = int(cursor.fetchone()[0])
        cursor.execute("PRAGMA synchronous")
        synchronous = int(cursor.fetchone()[0])
        cursor.execute("PRAGMA journal_mode")
        journal_mode = str(cursor.fetchone()[0]).casefold()
        cursor.execute("PRAGMA index_list('bitbucket_pdftextpage')")
        raw_indexes = cursor.fetchall()
        indexes: list[dict[str, Any]] = []
        for _sequence, name, unique, origin, partial in raw_indexes:
            quoted_name = str(name).replace('"', '""')
            cursor.execute(f'PRAGMA index_info("{quoted_name}")')
            indexes.append(
                {
                    "name": str(name),
                    "unique": bool(unique),
                    "origin": str(origin),
                    "partial": bool(partial),
                    "columns": [str(row[2]) for row in cursor.fetchall()],
                }
            )
        cursor.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM bitbucket_pdftextpage "
            "WHERE revision_id = %s ORDER BY page_number",
            [1],
        )
        lookup_plan = [str(row[3]) for row in cursor.fetchall()]
        cursor.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = 'bitbucket_pdftextpage'"
        )
        fts_trigger_count = int(cursor.fetchone()[0])
    return {
        "connectionSetupMs": round(connection_setup_ms, 6),
        "busyTimeoutMs": busy_timeout_ms,
        "journalMode": journal_mode,
        "synchronous": synchronous,
        "pdfTextPageIndexes": indexes,
        "pdfTextPageLookupPlan": lookup_plan,
        "pdfTextPageTriggerCount": fts_trigger_count,
    }


def _wal_checkpoint_snapshot(journal_mode: str) -> dict[str, Any]:
    if journal_mode != "wal":
        return {"state": "not_applicable", "durationMs": None}
    started = time.monotonic()
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA wal_checkpoint(PASSIVE)")
        row = cursor.fetchone()
    duration_ms = (time.monotonic() - started) * 1_000
    return {
        "state": "measured",
        "durationMs": round(duration_ms, 6),
        "busy": int(row[0]),
        "logFrames": int(row[1]),
        "checkpointedFrames": int(row[2]),
    }


def _foreground_search_snapshot(
    *, document_count: int, include_failure_fixtures: bool
) -> dict[str, Any]:
    """Measure repeatable exact-search and page-lookup latency after publication."""

    from bitbucket.models import PDFTextPage, PDFTextRevision
    from bitbucket.services.pdf_search import search_documents
    from bitbucket.services.pdf_search_query import PDFSearchQuery, PDFSearchScope

    document_numbers = tuple(
        document_number
        for document_number in range(document_count)
        if (document_number + 1) % 9 != 0
        and not (
            include_failure_fixtures
            and document_count >= 3
            and document_number >= document_count - 3
        )
    )[:20]
    sample_count = len(document_numbers)
    if not document_numbers:
        return {
            "exactSearchMs": _latency_summary([]),
            "exactSearchFailures": 0,
            "pageLookupMs": _latency_summary([]),
            "availabilityPct": None,
        }
    exact_durations: list[float] = []
    exact_failures = 0
    page_lookup_durations: list[float] = []
    revision_ids = tuple(
        PDFTextRevision.objects.order_by("id").values_list("id", flat=True)[:sample_count]
    )

    # Warm the SQLite connection, FTS statement path, and ORM relation loading
    # outside the measurements. The corpus and query sequence stay identical
    # for before/after index experiments.
    search_documents(
        PDFSearchQuery(
            chips=(f"benchmarktoken{document_numbers[0]:06d}",),
            scopes=(PDFSearchScope.CONTENT,),
        )
    )
    if revision_ids:
        list(
            PDFTextPage.objects.filter(revision_id=revision_ids[0])
            .order_by("page_number")
            .values_list("page_number", flat=True)
        )

    for document_number in document_numbers:
        started = time.perf_counter()
        result = search_documents(
            PDFSearchQuery(
                chips=(f"benchmarktoken{document_number:06d}",),
                scopes=(PDFSearchScope.CONTENT,),
            )
        )
        exact_durations.append((time.perf_counter() - started) * 1_000)
        if result.total < 1:
            exact_failures += 1

    for revision_id in revision_ids:
        started = time.perf_counter()
        list(
            PDFTextPage.objects.filter(revision_id=revision_id)
            .order_by("page_number")
            .values_list("page_number", "extraction_state")
        )
        page_lookup_durations.append((time.perf_counter() - started) * 1_000)

    return {
        "exactSearchMs": _latency_summary(exact_durations),
        "exactSearchFailures": exact_failures,
        "pageLookupMs": _latency_summary(page_lookup_durations),
        "availabilityPct": round(
            ((sample_count - exact_failures) / sample_count) * 100,
            3,
        ),
    }


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
    if plan.duplicate_page_index:
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE INDEX bb_pdf_page_lookup_idx "
                "ON bitbucket_pdftextpage(revision_id, page_number)"
            )
    with connection.cursor() as cursor:
        cursor.execute(f"PRAGMA journal_mode={plan.sqlite_journal_mode}")
        row = cursor.fetchone()
    actual_journal_mode = str(row[0] if row else "").casefold()
    if actual_journal_mode != plan.sqlite_journal_mode:
        raise RuntimeError("The isolated SQLite journal mode did not match the benchmark plan.")
    sqlite_contract = _sqlite_connection_and_index_snapshot()
    setup_started = time.monotonic()
    workload = _seed_synthetic_corpus(plan)
    setup_duration = time.monotonic() - setup_started
    before_usage = _resource_usage()
    pipeline_duration, metrics_sampling, concurrent_foreground = _drain_existing_pipeline(
        timeout_seconds=plan.trial_timeout_seconds
    )
    after_usage = _resource_usage()

    from bitbucket.models import (
        PDFDocument,
        PDFExtractionJob,
        PDFExtractionJobStatus,
        PDFTextPage,
        PDFTextRevision,
    )

    jobs = list(PDFExtractionJob.objects.select_related("document"))
    end_to_end_durations = [
        (job.completed_at - job.requested_at).total_seconds()
        for job in jobs
        if job.completed_at is not None
    ]
    extraction_durations = [
        (job.staged_at - job.started_at).total_seconds()
        for job in jobs
        if job.started_at is not None and job.staged_at is not None
    ]
    staged_wait_durations = [
        (job.publication_started_at - job.staged_at).total_seconds()
        for job in jobs
        if job.staged_at is not None and job.publication_started_at is not None
    ]
    publication_durations = [
        (job.published_at - job.publication_started_at).total_seconds()
        for job in jobs
        if job.publication_started_at is not None and job.published_at is not None
    ]
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
    minute_factor = 60 / pipeline_duration if pipeline_duration else 0.0
    repository_waits: dict[int, list[float]] = {}
    for job in jobs:
        if job.started_at is None:
            continue
        repository_waits.setdefault(job.document.repository_id, []).append(
            (job.started_at - job.requested_at).total_seconds()
        )
    all_repository_waits = [value for values in repository_waits.values() for value in values]
    from bitbucket.services.pdf_runtime_metrics import publisher_runtime_snapshot

    publisher_runtime = publisher_runtime_snapshot(window_seconds=plan.trial_timeout_seconds)
    wal_checkpoint = _wal_checkpoint_snapshot(actual_journal_mode)
    foreground = _foreground_search_snapshot(
        document_count=plan.document_count,
        include_failure_fixtures=plan.include_failure_fixtures,
    )
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
        "sqliteJournalMode": actual_journal_mode,
        "metricsSampling": metrics_sampling,
        "pipeline": {
            "durationSeconds": round(pipeline_duration, 6),
            "persistedDocuments": persisted_documents,
            "persistedPages": persisted_pages,
            "sourceBytes": workload["sourceBytes"],
            "extractedCharacters": extracted_characters,
            "persistedDocumentsPerMinute": round(rate, 6),
            "persistedPagesPerMinute": round(persisted_pages * minute_factor, 6),
            "sourceBytesPerMinute": round(workload["sourceBytes"] * minute_factor, 6),
            "extractedCharactersPerMinute": round(extracted_characters * minute_factor, 6),
            "latencySeconds": {
                "extraction": _latency_summary(extraction_durations),
                "stagedWait": _latency_summary(staged_wait_durations),
                "publication": _latency_summary(publication_durations),
                "workerJob": _latency_summary(job_durations),
                "endToEnd": _latency_summary(end_to_end_durations),
            },
            "repositoryWaitSeconds": {
                "p50": round(
                    float(
                        _percentile(
                            all_repository_waits,
                            0.50,
                        )
                        or 0.0
                    ),
                    6,
                ),
                "p95": round(
                    float(
                        _percentile(
                            all_repository_waits,
                            0.95,
                        )
                        or 0.0
                    ),
                    6,
                ),
                "oldestByRepository": {
                    str(repository_id): round(max(values), 6)
                    for repository_id, values in sorted(repository_waits.items())
                },
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
        "sqlite": {
            **sqlite_contract,
            "publisherRuntime": publisher_runtime,
            "walCheckpoint": wal_checkpoint,
        },
        "foreground": foreground,
        "concurrentForeground": concurrent_foreground,
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
