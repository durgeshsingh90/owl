from __future__ import annotations

import json
import os
import subprocess
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from pypdf import PdfReader

from bitbucket_search.benchmarks import pdf_pipeline


def _plan(**overrides) -> pdf_pipeline.BenchmarkPlan:
    values = {
        "worker_targets": (1,),
        "repetitions": 1,
        "document_count": 1,
        "repository_count": 1,
        "pages_per_document": 1,
        "seed": 1100,
        "trial_timeout_seconds": 30,
        "keep_trial_data": False,
    }
    values.update(overrides)
    return pdf_pipeline.BenchmarkPlan(**values)


def test_plan_enforces_current_tested_bounds_and_repeatable_workload():
    assert pdf_pipeline.validate_plan(_plan()).worker_targets == (1,)

    with pytest.raises(ValueError, match="between 1 and 8"):
        pdf_pipeline.validate_plan(_plan(worker_targets=(9,)))
    with pytest.raises(ValueError, match="must not contain duplicates"):
        pdf_pipeline.validate_plan(_plan(worker_targets=(2, 2)))
    with pytest.raises(ValueError, match="between 1 and the document count"):
        pdf_pipeline.validate_plan(_plan(document_count=2, repository_count=3))
    with pytest.raises(ValueError, match="between 30 and 86,400"):
        pdf_pipeline.validate_plan(_plan(trial_timeout_seconds=29))


def test_synthetic_pdf_generator_creates_machine_readable_pages(tmp_path):
    target = tmp_path / "synthetic.pdf"
    pdf_pipeline._write_text_pdf(target, ["synthetic first page", "synthetic second page"])

    reader = PdfReader(target)
    assert [page.extract_text() for page in reader.pages] == [
        "synthetic first page",
        "synthetic second page",
    ]


def test_benchmark_orchestrates_fresh_sanitized_roots_and_keeps_only_report(tmp_path, monkeypatch):
    runtime_root = tmp_path / "var" / "benchmarks"
    monkeypatch.setattr(pdf_pipeline, "benchmark_runtime_root", lambda: runtime_root)
    monkeypatch.setattr(pdf_pipeline, "new_run_id", lambda: "20260903T120000Z-testonly0001")
    monkeypatch.setenv("CONFLUENCE_PAT", "must-not-reach-child")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-reach-child")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        worker_count = int(command[command.index("--workers") + 1])
        result_path = Path(command[command.index("--result-path") + 1])
        payload = {
            "schemaVersion": pdf_pipeline.BENCHMARK_SCHEMA_VERSION,
            "kind": pdf_pipeline.BENCHMARK_KIND,
            "status": "complete",
            "workerCount": worker_count,
            "pipeline": {
                "durationSeconds": float(10 / worker_count),
                "persistedDocumentsPerMinute": float(worker_count * 6),
            },
        }
        pdf_pipeline._atomic_json(result_path, payload)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(pdf_pipeline.subprocess, "run", fake_run)
    output = StringIO()
    call_command(
        "bitbucket_pdf_pipeline_benchmark",
        "--workers",
        "1",
        "--workers",
        "2",
        "--repetitions",
        "2",
        "--documents",
        "2",
        "--repositories",
        "2",
        "--pages-per-document",
        "1",
        "--trial-timeout-seconds",
        "30",
        stdout=output,
    )

    assert len(calls) == 4
    child_roots = []
    for _command, kwargs in calls:
        environment = kwargs["env"]
        data_root = Path(environment["OWL_DATA_ROOT"])
        child_roots.append(data_root)
        assert data_root.is_relative_to(runtime_root)
        assert data_root.name == "owl-data"
        assert environment["CONFLUENCE_PAT"] == ""
        assert "AWS_SECRET_ACCESS_KEY" not in environment
        assert environment["SEMANTIC_SEARCH_ENABLED"] == "false"
        assert kwargs["check"] is False
    assert len(set(child_roots)) == 4
    assert all(not root.exists() for root in child_roots)
    assert {command[command.index("--seed") + 1] for command, _kwargs in calls} == {"1100"}

    reports = list((runtime_root / "reports").glob("*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["status"] == "complete"
    assert report["failedTrialCount"] == 0
    assert [item["workerCount"] for item in report["summary"]] == [1, 2]
    assert not (runtime_root / "runs" / report["runId"]).exists()
    if os.name != "nt":
        assert reports[0].stat().st_mode & 0o777 == 0o600
    assert str(reports[0]) in output.getvalue()


def test_command_rejects_ambiguous_matrix_selection():
    with pytest.raises(CommandError, match="either --full-matrix"):
        call_command(
            "bitbucket_pdf_pipeline_benchmark",
            "--full-matrix",
            "--workers",
            "4",
        )


def test_internal_trial_refuses_non_generated_output_before_migrating(tmp_path):
    with pytest.raises(ValueError, match="isolated trial root"):
        pdf_pipeline.run_internal_trial(
            plan=_plan(),
            worker_count=1,
            result_path=tmp_path / "outside.json",
        )


def test_internal_worker_refuses_to_touch_the_normal_test_data_root():
    with pytest.raises(ValueError, match="generated isolated data root"):
        pdf_pipeline.run_internal_worker(role="extractor", timeout_seconds=30)


def test_trial_cleanup_refuses_the_run_root_and_unrelated_paths(tmp_path):
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()

    with pytest.raises(ValueError, match="Refusing to remove"):
        pdf_pipeline._safe_remove_trial(run_directory, run_directory=run_directory)
    with pytest.raises(ValueError, match="Refusing to remove"):
        pdf_pipeline._safe_remove_trial(unrelated, run_directory=run_directory)
