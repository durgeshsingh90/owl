"""Run the existing PDF extraction/publication path against isolated synthetic data."""

from __future__ import annotations

import argparse
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from bitbucket_search.benchmarks.pdf_pipeline import (
    DEFAULT_WORKER_TARGETS,
    FULL_WORKER_MATRIX,
    BenchmarkPlan,
    run_benchmark,
    run_internal_trial,
    run_internal_worker,
    validate_plan,
)


class Command(BaseCommand):
    help = (
        "Benchmark the fixed PDF extraction/staging/publication pipeline in a fresh "
        "synthetic OWL data root and database per trial."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--workers",
            action="append",
            type=int,
            dest="worker_targets",
            help="Fixed extractor target to test (repeatable; defaults to 4, 6, and 8).",
        )
        parser.add_argument(
            "--full-matrix",
            action="store_true",
            help="Run fixed targets 1, 2, 4, 6, and 8.",
        )
        parser.add_argument("--repetitions", type=int, default=3)
        parser.add_argument("--documents", type=int, default=16)
        parser.add_argument("--repositories", type=int, default=3)
        parser.add_argument("--pages-per-document", type=int, default=2)
        parser.add_argument("--seed", type=int, default=1100)
        parser.add_argument("--trial-timeout-seconds", type=int, default=900)
        parser.add_argument(
            "--sqlite-journal-mode",
            choices=("delete", "wal"),
            default="delete",
            help=(
                "SQLite journal mode for this isolated trial matrix. Keep this fixed "
                "while comparing worker targets."
            ),
        )
        parser.add_argument(
            "--metrics-sampling",
            action="store_true",
            help=(
                "Enable the normal bounded pipeline metrics sampler in each isolated "
                "trial so its overhead can be measured separately."
            ),
        )
        parser.add_argument(
            "--per-repository-workers",
            type=int,
            default=None,
            help=(
                "Optional per-repository parser cap for locality/work-conservation "
                "experiments; defaults to each fixed worker target."
            ),
        )
        parser.add_argument(
            "--strict-repository-locality",
            action="store_true",
            help="Disable work-conserving spillover for one isolated comparison run.",
        )
        parser.add_argument(
            "--repeat-child-prehash",
            action="store_true",
            help="Disable parent-fingerprint handoff for one isolated I/O comparison run.",
        )
        parser.add_argument(
            "--publication-page-batch-size",
            type=int,
            default=100,
            help="PDF page bulk-create batch size for this isolated trial only.",
        )
        parser.add_argument(
            "--with-duplicate-page-index",
            action="store_true",
            help=(
                "Recreate the retired duplicate PDF-page lookup index inside each "
                "disposable trial for a controlled before/after comparison."
            ),
        )
        parser.add_argument(
            "--source-padding-bytes-per-document",
            type=int,
            default=0,
            help=(
                "Add an unreferenced deterministic PDF stream of this average size; "
                "use only in disposable capacity/representative trials."
            ),
        )
        parser.add_argument(
            "--include-failure-fixtures",
            action="store_true",
            help=(
                "Replace the final three generated documents with blank, encrypted, "
                "and malformed fixtures in each disposable trial."
            ),
        )
        parser.add_argument(
            "--keep-trial-data",
            action="store_true",
            help="Keep synthetic trial databases/PDFs below ignored var/benchmarks.",
        )
        parser.add_argument("--internal-trial", action="store_true", help=argparse.SUPPRESS)
        parser.add_argument(
            "--internal-worker-role",
            choices=("extractor", "publisher"),
            help=argparse.SUPPRESS,
        )
        parser.add_argument("--result-path", type=Path, help=argparse.SUPPRESS)

    def handle(self, *args, **options):
        internal_worker_role = options["internal_worker_role"]
        if internal_worker_role:
            if options["internal_trial"] or options["result_path"] is not None:
                raise CommandError("The internal benchmark worker contract is invalid.")
            try:
                run_internal_worker(
                    role=internal_worker_role,
                    timeout_seconds=options["trial_timeout_seconds"],
                )
            except (RuntimeError, ValueError) as exc:
                raise CommandError(str(exc)) from exc
            return

        if options["full_matrix"] and options["worker_targets"]:
            raise CommandError("Use either --full-matrix or explicit --workers values, not both.")
        worker_targets = tuple(
            FULL_WORKER_MATRIX
            if options["full_matrix"]
            else options["worker_targets"] or DEFAULT_WORKER_TARGETS
        )
        plan = BenchmarkPlan(
            worker_targets=worker_targets,
            repetitions=options["repetitions"],
            document_count=options["documents"],
            repository_count=options["repositories"],
            pages_per_document=options["pages_per_document"],
            seed=options["seed"],
            trial_timeout_seconds=options["trial_timeout_seconds"],
            keep_trial_data=options["keep_trial_data"],
            sqlite_journal_mode=options["sqlite_journal_mode"],
            metrics_sampling_enabled=options["metrics_sampling"],
            per_repository_worker_limit=options["per_repository_workers"],
            repository_work_conserving=not options["strict_repository_locality"],
            reuse_parent_fingerprint=not options["repeat_child_prehash"],
            publication_page_batch_size=options["publication_page_batch_size"],
            duplicate_page_index=options["with_duplicate_page_index"],
            source_padding_bytes_per_document=options["source_padding_bytes_per_document"],
            include_failure_fixtures=options["include_failure_fixtures"],
        )
        try:
            validate_plan(plan)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        if options["internal_trial"]:
            result_path = options["result_path"]
            if result_path is None or len(worker_targets) != 1 or plan.repetitions != 1:
                raise CommandError("The internal trial contract is incomplete.")
            try:
                payload = run_internal_trial(
                    plan=plan,
                    worker_count=worker_targets[0],
                    result_path=result_path,
                )
            except (RuntimeError, ValueError) as exc:
                raise CommandError(str(exc)) from exc
            if payload["status"] != "complete":
                raise CommandError("The isolated trial failed its correctness checks.")
            return

        if options["result_path"] is not None:
            raise CommandError("--result-path is reserved for an isolated internal trial.")
        report, report_path = run_benchmark(plan)
        self.stdout.write(
            self.style.SUCCESS(
                f"Synthetic PDF pipeline benchmark {report['status']}: {report_path}"
            )
        )
        if report["failedTrialCount"]:
            raise CommandError(
                f"{report['failedTrialCount']} isolated benchmark trial(s) failed; "
                "see the sanitized report."
            )
