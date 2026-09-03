from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.core.management import call_command
from django.db import DatabaseError

from bitbucket_search.management.commands import bitbucket_pdf_writer
from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFExtractionJob,
    PDFExtractionJobPhase,
    PDFExtractionJobStatus,
)
from bitbucket_search.services import pdf_indexing

pytestmark = pytest.mark.django_db


def _publication_job() -> PDFExtractionJob:
    repository = BitbucketRepository.objects.create(
        display_name="Writer efficiency",
        canonical_remote_key="example.invalid/owl/writer-efficiency",
        remote_url="https://example.invalid/owl/writer-efficiency.git",
    )
    document = PDFDocument.objects.create(
        repository=repository,
        filename="queued.pdf",
        relative_path="docs/queued.pdf",
    )
    return PDFExtractionJob.objects.create(
        document=document,
        target_git_blob_id="a" * 40,
        target_source_commit="b" * 40,
        target_relative_path=document.relative_path,
        target_extractor_version="writer-efficiency-test",
        status=PDFExtractionJobStatus.RUNNING,
        phase=PDFExtractionJobPhase.PUBLISHING,
    )


def test_empty_publication_poll_skips_claim_gate_and_sqlite_write_reservation(monkeypatch):
    claim_gate = Mock()
    reserve_write = Mock()
    monkeypatch.setattr(pdf_indexing, "pdf_extraction_claim_lock", claim_gate)
    monkeypatch.setattr(pdf_indexing, "_reserve_sqlite_write", reserve_write)

    assert pdf_indexing.work_one_publication_job() is None

    claim_gate.assert_not_called()
    reserve_write.assert_not_called()


def test_unsealed_publication_job_does_not_bypass_jsonl_queue(monkeypatch):
    job = _publication_job()
    reserve_write = Mock()
    gate_entries: list[int] = []

    @contextmanager
    def competing_claim_gate():
        gate_entries.append(1)
        yield

    monkeypatch.setattr(pdf_indexing, "pdf_extraction_claim_lock", competing_claim_gate)
    monkeypatch.setattr(pdf_indexing, "_reserve_sqlite_write", reserve_write)

    assert pdf_indexing.work_one_publication_job() is None

    job.refresh_from_db()
    assert gate_entries == []
    reserve_write.assert_not_called()
    assert job.worker_pid is None


def test_writer_empty_polling_backs_off_to_a_bounded_delay(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(
        bitbucket_pdf_writer,
        "resident_supervisor_is_alive",
        Mock(side_effect=(True, True, True, True, True, False)),
    )
    work = Mock(return_value=None)
    monkeypatch.setattr(bitbucket_pdf_writer, "work_one_publication_job", work)
    monkeypatch.setattr(bitbucket_pdf_writer.time, "sleep", sleeps.append)

    call_command(
        "bitbucket_pdf_writer",
        poll_interval=0.25,
        max_poll_interval=1.0,
    )

    assert work.call_count == 5
    assert sleeps == [0.25, 0.5, 1.0, 1.0, 1.0]


def test_writer_resets_idle_backoff_after_publishing_work(monkeypatch):
    sleeps: list[float] = []
    published = SimpleNamespace(pk=42, get_status_display=lambda: "Succeeded")
    monkeypatch.setattr(
        bitbucket_pdf_writer,
        "resident_supervisor_is_alive",
        Mock(side_effect=(True, True, True, True, False)),
    )
    work = Mock(side_effect=(None, published, None, None))
    monkeypatch.setattr(bitbucket_pdf_writer, "work_one_publication_job", work)
    monkeypatch.setattr(bitbucket_pdf_writer.time, "sleep", sleeps.append)

    call_command(
        "bitbucket_pdf_writer",
        poll_interval=0.25,
        max_poll_interval=1.0,
    )

    assert work.call_count == 4
    assert sleeps == [0.25, 0.25, 0.5]


def test_writer_uses_short_delay_after_database_error(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(
        bitbucket_pdf_writer,
        "resident_supervisor_is_alive",
        Mock(side_effect=(True, True, True, False)),
    )
    work = Mock(side_effect=(None, DatabaseError("busy"), None))
    monkeypatch.setattr(bitbucket_pdf_writer, "work_one_publication_job", work)
    monkeypatch.setattr(bitbucket_pdf_writer.time, "sleep", sleeps.append)

    call_command(
        "bitbucket_pdf_writer",
        poll_interval=0.25,
        max_poll_interval=1.0,
    )

    assert work.call_count == 3
    assert sleeps == [0.25, 0.25, 0.5]


def test_writer_exits_a_caught_database_error_loop_for_supervisor_recovery(
    monkeypatch,
    settings,
):
    settings.PDF_PIPELINE_COMPONENT_ERROR_LOOP_THRESHOLD = 2
    monkeypatch.setattr(
        bitbucket_pdf_writer,
        "resident_supervisor_is_alive",
        Mock(return_value=True),
    )
    work = Mock(side_effect=DatabaseError("synthetic busy loop"))
    monkeypatch.setattr(bitbucket_pdf_writer, "work_one_publication_job", work)
    monkeypatch.setattr(bitbucket_pdf_writer.time, "sleep", Mock())

    with pytest.raises(DatabaseError):
        call_command("bitbucket_pdf_writer", poll_interval=0.25, max_poll_interval=1.0)

    assert work.call_count == 2
