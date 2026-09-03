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
