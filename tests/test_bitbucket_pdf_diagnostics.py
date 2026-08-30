from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from bitbucket_search.models import PDFExtractionJob, PDFExtractionJobStatus, PDFIndexState
from bitbucket_search.services import pdf_indexing
from bitbucket_search.services.pdf_extractor import (
    PDFExtractionDiagnostic,
    PDFExtractionState,
    _result,
)
from tests.test_bitbucket_pdf_indexing import indexed_pdf_target  # noqa: F401


def _failure_payload():
    return _result(
        PDFExtractionState.DEPENDENCY_UNAVAILABLE,
        diagnostic=PDFExtractionDiagnostic("load_parser", "module_not_found"),
    ).to_payload()


def test_failure_payload_preserves_fixed_diagnostic_metadata_and_overrides_child_text():
    payload = _failure_payload()
    payload["error_summary"] = "private parser message and source path"
    payload["error_code"] = "private_data"

    staged = pdf_indexing._parse_extractor_payload(payload)

    assert staged.diagnostic.stage == "load_parser"
    assert staged.diagnostic.reason == "module_not_found"
    assert staged.error_code == "pdf_dependency_unavailable"
    assert "Install the project requirements" in staged.error_summary
    assert "private" not in staged.error_summary


def test_older_extractor_payload_without_optional_diagnostic_remains_supported():
    payload = _failure_payload()
    del payload["diagnostic"]
    staged = pdf_indexing._parse_extractor_payload(payload)
    assert staged.diagnostic == PDFExtractionDiagnostic()


@pytest.mark.parametrize(
    "diagnostic",
    [
        "private path",
        [],
        {"stage": "load_parser", "reason": "module_not_found"},
        {"stage": "private_stage", "reason": "module_not_found", "errno": None, "winerror": None},
        {"stage": "load_parser", "reason": "private_reason", "errno": None, "winerror": None},
        {"stage": "load_parser", "reason": "os_error", "errno": "private", "winerror": None},
        {"stage": "load_parser", "reason": "os_error", "errno": True, "winerror": None},
        {"stage": "load_parser", "reason": "os_error", "errno": None, "winerror": 2**100},
        {"stage": "load_parser", "reason": "os_error", "errno": -1, "winerror": None},
    ],
)
def test_untrusted_diagnostic_payload_cannot_reach_backend_logs(diagnostic):
    payload = _failure_payload()
    payload["diagnostic"] = diagnostic
    with pytest.raises(pdf_indexing.PDFIndexingError) as caught:
        pdf_indexing._parse_extractor_payload(payload)
    assert caught.value.code == "invalid_extractor_response"
    assert "private" not in caught.value.summary


def test_unknown_child_state_is_rejected_instead_of_trusted_as_a_reason():
    payload = _failure_payload()
    payload["state"] = "private_state"
    with pytest.raises(pdf_indexing.PDFIndexingError) as caught:
        pdf_indexing._parse_extractor_payload(payload)
    assert caught.value.code == "invalid_extractor_response"


def test_parser_diagnostic_logs_safe_category_stage_and_windows_codes_at_error(caplog):
    payload = _failure_payload()
    payload["diagnostic"] = {
        "stage": "read_pdf",
        "reason": "os_error",
        "errno": 22,
        "winerror": 123,
    }
    staged = pdf_indexing._parse_extractor_payload(payload)
    job = SimpleNamespace(document=SimpleNamespace(repository_id=12), document_id=34, pk=56)
    with caplog.at_level(logging.ERROR, logger="owl.bitbucket.indexing"):
        pdf_indexing._log_extractor_diagnostic(staged, job=job)

    message = next(record for record in caplog.records if "pdf_parser_diagnostic" in record.message)
    assert message.levelno == logging.ERROR
    assert "repository_id=12 document_id=34 job_id=56" in message.message
    assert "stage=read_pdf reason=os_error" in message.message
    assert "errno=22 winerror=123" in message.message


@pytest.mark.django_db
def test_dependency_failure_is_persisted_with_repair_action_and_does_not_publish_text(
    indexed_pdf_target,  # noqa: F811 - pytest fixture import
    caplog,
):
    repository, document, _path = indexed_pdf_target
    pdf_indexing.queue_repository_pdf_extractions(repository)
    claimed = pdf_indexing.claim_next_extraction_job()
    staged = pdf_indexing._parse_extractor_payload(_failure_payload())

    with caplog.at_level(logging.ERROR, logger="owl.bitbucket.indexing"):
        pdf_indexing.execute_claimed_extraction_job(
            claimed.pk,
            extraction_runner=lambda _path, _heartbeat: staged,
        )

    document.refresh_from_db()
    completed = PDFExtractionJob.objects.get(pk=claimed.pk)
    assert completed.status == PDFExtractionJobStatus.FAILED
    assert completed.error_code == "pdf_dependency_unavailable"
    assert "Install the project requirements" in completed.error_summary
    assert document.index_state == PDFIndexState.FAILED
    assert document.indexed_revision_id is None
    assert "stage=load_parser reason=module_not_found" in caplog.text
    assert "error_code=pdf_dependency_unavailable" in caplog.text
    # An installation failure should not spin through automatic retry loops.
    assert pdf_indexing.queue_repository_pdf_extractions(repository).queued_job_ids == ()
    assert (
        len(
            pdf_indexing.queue_repository_pdf_extractions(
                repository, retry_failed=True
            ).queued_job_ids
        )
        == 1
    )
