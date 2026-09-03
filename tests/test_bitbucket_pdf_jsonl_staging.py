from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bitbucket_search.services import pdf_jsonl_staging as staging


def _manifest(job_id: int) -> dict[str, object]:
    return {"job": {"id": job_id}, "metadata": "preserved"}


def _incoming(stager: staging.JSONLStager, job_id: int) -> Path:
    path = stager.root / f"job-{job_id}.json"
    path.write_text("{}", encoding="utf-8")
    return path


def _append(stager: staging.JSONLStager, job_id: int, content: str = "text"):
    return stager.append_manifest(
        job_id=job_id,
        file_path=f"docs/{job_id}.pdf",
        file_name=f"{job_id}.pdf",
        content=content,
        manifest=_manifest(job_id),
        incoming_path=_incoming(stager, job_id),
    )


def test_size_only_rotation_writes_complete_utf8_records(tmp_path, settings):
    settings.BITBUCKET_TEMP_ROOT = tmp_path
    settings.PDF_JSONL_STAGING_DIRECTORY = ""
    settings.PDF_JSONL_CHUNK_SIZE_BYTES = 120
    stager = staging.JSONLStager()

    result = _append(stager, 1, "café " * 30)

    assert result.sealed_chunk is not None
    assert result.sealed_chunk.status == staging.CHUNK_STATUS_SEALED
    assert result.sealed_chunk.byte_count >= settings.PDF_JSONL_CHUNK_SIZE_BYTES
    assert staging.current_jsonl_path().read_bytes() == b""
    records = list(staging.iter_chunk_records(result.sealed_chunk.path))
    assert len(records) == 1
    assert records[0][1]["file_path"] == "docs/1.pdf"
    assert records[0][1]["file_name"] == "1.pdf"
    assert records[0][1]["content"] == "café " * 30


def test_count_and_elapsed_time_do_not_rotate_current_jsonl(tmp_path, settings):
    settings.BITBUCKET_TEMP_ROOT = tmp_path
    settings.PDF_JSONL_STAGING_DIRECTORY = ""
    settings.PDF_JSONL_CHUNK_SIZE_BYTES = 1024 * 1024
    stager = staging.JSONLStager()

    first = _append(stager, 1)
    second = _append(stager, 2)

    assert first.sealed_chunk is None
    assert second.sealed_chunk is None
    assert staging.list_chunks() == ()
    sealed = stager.seal_current()
    assert sealed is not None
    assert sealed.record_count == 2


def test_current_recovery_truncates_partial_tail_and_deduplicates_fsynced_record(
    tmp_path, settings
):
    settings.BITBUCKET_TEMP_ROOT = tmp_path
    settings.PDF_JSONL_STAGING_DIRECTORY = ""
    settings.PDF_JSONL_CHUNK_SIZE_BYTES = 1024 * 1024
    stager = staging.JSONLStager()
    _append(stager, 1)
    current = staging.current_jsonl_path()
    complete_size = current.stat().st_size
    with current.open("ab") as stream:
        stream.write(b'{"file_path":"partial')

    recovered = staging.JSONLStager()

    assert recovered.current_size_bytes == complete_size
    assert current.stat().st_size == complete_size
    duplicate = _incoming(recovered, 1)
    result = recovered.append_manifest(
        job_id=1,
        file_path="docs/1.pdf",
        file_name="1.pdf",
        content="text",
        manifest=_manifest(1),
        incoming_path=duplicate,
    )
    assert result.sealed_chunk is None
    assert not duplicate.exists()
    assert len(current.read_text(encoding="utf-8").splitlines()) == 1


def test_current_recovery_preserves_and_rejects_invalid_complete_line(tmp_path, settings):
    settings.BITBUCKET_TEMP_ROOT = tmp_path
    settings.PDF_JSONL_STAGING_DIRECTORY = ""
    current = staging.current_jsonl_path()
    current.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(staging.JSONLStagingError, match="complete line"):
        staging.JSONLStager()

    assert current.read_text(encoding="utf-8") == "not-json\n"


def test_sealed_chunk_without_sidecar_is_recovered_as_queued(tmp_path, settings):
    settings.BITBUCKET_TEMP_ROOT = tmp_path
    settings.PDF_JSONL_STAGING_DIRECTORY = ""
    settings.PDF_JSONL_CHUNK_SIZE_BYTES = 1024 * 1024
    stager = staging.JSONLStager()
    _append(stager, 1)
    sealed = stager.seal_current()
    assert sealed is not None
    sealed.metadata_path.unlink()

    recovered = staging.JSONLStager()

    chunks = staging.list_chunks()
    assert recovered.current_path.exists()
    assert len(chunks) == 1
    assert chunks[0].status == staging.CHUNK_STATUS_SEALED
    assert chunks[0].metadata_path.exists()


def test_only_expired_imported_chunks_are_cleaned(tmp_path, settings):
    settings.BITBUCKET_TEMP_ROOT = tmp_path
    settings.PDF_JSONL_STAGING_DIRECTORY = ""
    settings.PDF_JSONL_CHUNK_SIZE_BYTES = 1024 * 1024
    settings.PDF_JSONL_RETENTION_DAYS = 7
    stager = staging.JSONLStager()
    _append(stager, 1)
    imported_chunk = stager.seal_current()
    assert imported_chunk is not None
    claimed = staging.claim_oldest_chunk()
    assert claimed is not None
    now = datetime(2026, 9, 4, tzinfo=UTC)
    imported = staging.mark_chunk_imported(
        claimed,
        imported_at=now - timedelta(days=8),
    )
    _append(stager, 2)
    queued = stager.seal_current()
    assert queued is not None

    removed = staging.cleanup_expired_imported_chunks(now=now)

    assert removed == (imported.path.name,)
    assert not imported.path.exists()
    assert not imported.metadata_path.exists()
    assert queued.path.exists()
    assert queued.metadata_path.exists()
    assert staging.current_jsonl_path().exists()


def test_chunk_metadata_tracks_import_and_cleanup_eligibility(tmp_path, settings):
    settings.BITBUCKET_TEMP_ROOT = tmp_path
    settings.PDF_JSONL_STAGING_DIRECTORY = ""
    settings.PDF_JSONL_CHUNK_SIZE_BYTES = 1024 * 1024
    settings.PDF_JSONL_RETENTION_DAYS = 7
    stager = staging.JSONLStager()
    _append(stager, 1)
    sealed = stager.seal_current()
    assert sealed is not None
    claimed = staging.claim_oldest_chunk()
    assert claimed is not None
    imported_at = datetime(2026, 9, 4, tzinfo=UTC)
    imported = staging.mark_chunk_imported(claimed, imported_at=imported_at)

    payload = json.loads(imported.metadata_path.read_text(encoding="utf-8"))
    snapshot = staging.staging_snapshot(now=imported_at)

    assert payload["status"] == "IMPORTED"
    assert payload["imported_at"] == imported_at.isoformat()
    assert snapshot["writerState"] == "IDLE"
    assert snapshot["retainedChunks"] == 1
    assert snapshot["oldestCleanupEligibleAt"] == (imported_at + timedelta(days=7)).isoformat()
