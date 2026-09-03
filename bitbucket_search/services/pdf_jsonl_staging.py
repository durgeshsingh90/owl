"""Durable JSONL chunk lifecycle between PDF extraction and SQLite publication."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from django.conf import settings

from bitbucket_search.services.repository_lock import _file_lock

CURRENT_FILENAME: Final = "current.jsonl"
CHUNK_SCHEMA_VERSION: Final = 1
CHUNK_STATUS_SEALED: Final = "SEALED"
CHUNK_STATUS_IMPORTING: Final = "IMPORTING"
CHUNK_STATUS_IMPORTED: Final = "IMPORTED"
CHUNK_STATUS_FAILED: Final = "FAILED"
_CHUNK_PATTERN = re.compile(r"^chunk_([0-9]{6,})\.jsonl$")
_META_PATTERN = re.compile(r"^chunk_([0-9]{6,})\.meta\.json$")
_VALID_STATUSES = {
    CHUNK_STATUS_SEALED,
    CHUNK_STATUS_IMPORTING,
    CHUNK_STATUS_IMPORTED,
    CHUNK_STATUS_FAILED,
}


class JSONLStagingError(RuntimeError):
    """A staging artifact could not be trusted without risking lost work."""


@dataclass(frozen=True, slots=True)
class JSONLChunk:
    path: Path
    metadata_path: Path
    sequence: int
    status: str
    record_count: int
    byte_count: int
    created_at: str
    imported_at: str | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class JSONLAppendResult:
    job_id: int
    current_size_bytes: int
    sealed_chunk: JSONLChunk | None


def staging_directory() -> Path:
    configured = str(getattr(settings, "PDF_JSONL_STAGING_DIRECTORY", "") or "").strip()
    root = (
        Path(configured).expanduser().resolve()
        if configured
        else Path(settings.BITBUCKET_TEMP_ROOT).resolve() / "pdf-publication"
    )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        if os.name != "nt":
            raise
    return root


def incoming_manifest_path(job_id: int) -> Path:
    return staging_directory() / f"job-{int(job_id)}.json"


def current_jsonl_path() -> Path:
    return staging_directory() / CURRENT_FILENAME


def chunk_metadata_path(chunk_path: Path) -> Path:
    match = _CHUNK_PATTERN.fullmatch(chunk_path.name)
    if match is None:
        raise ValueError("chunk_path must be a canonical JSONL chunk")
    return chunk_path.with_name(f"chunk_{match.group(1)}.meta.json")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat()


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json_write(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _record_job_id(record: object) -> int:
    if not isinstance(record, dict):
        raise JSONLStagingError("A JSONL record is not an object.")
    if set(record) != {"file_path", "file_name", "content", "manifest"}:
        raise JSONLStagingError("A JSONL record has an invalid envelope.")
    if not all(isinstance(record[key], str) for key in ("file_path", "file_name", "content")):
        raise JSONLStagingError("A JSONL record has invalid file fields.")
    manifest = record["manifest"]
    job = manifest.get("job") if isinstance(manifest, dict) else None
    job_id = job.get("id") if isinstance(job, dict) else None
    if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id <= 0:
        raise JSONLStagingError("A JSONL record has an invalid job identity.")
    return job_id


def iter_chunk_records(path: Path) -> Iterator[tuple[int, dict[str, object]]]:
    """Read one complete UTF-8 JSON object at a time without loading a chunk."""

    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise JSONLStagingError("The JSONL chunk is empty or is not a regular file.")
        with path.open("r", encoding="utf-8", newline="") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise JSONLStagingError("The JSONL chunk changed while it was opened.")
            for line_number, line in enumerate(stream, start=1):
                if not line.endswith("\n"):
                    raise JSONLStagingError("The JSONL chunk has an incomplete final record.")
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise JSONLStagingError(
                        f"The JSONL chunk contains invalid JSON on line {line_number}."
                    ) from exc
                _record_job_id(record)
                yield line_number, record
            after = os.fstat(stream.fileno())
            if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
                raise JSONLStagingError("The JSONL chunk changed while it was read.")
    except JSONLStagingError:
        raise
    except (OSError, UnicodeError) as exc:
        raise JSONLStagingError("The JSONL chunk could not be read safely.") from exc


def _scan_current(path: Path) -> tuple[set[int], int]:
    """Validate current.jsonl and truncate only a provably incomplete crash tail."""

    if not path.exists():
        with path.open("xb") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(path.parent)
        return set(), 0
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise JSONLStagingError("current.jsonl is not a regular file.")
        data_end = before.st_size
        with path.open("r+b") as stream:
            job_ids: set[int] = set()
            last_complete_offset = 0
            line_number = 0
            while True:
                raw_line = stream.readline()
                if not raw_line:
                    break
                line_number += 1
                if not raw_line.endswith(b"\n"):
                    stream.truncate(last_complete_offset)
                    stream.flush()
                    os.fsync(stream.fileno())
                    data_end = last_complete_offset
                    break
                try:
                    record = json.loads(raw_line.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise JSONLStagingError(
                        f"current.jsonl contains invalid JSON on complete line {line_number}."
                    ) from exc
                job_id = _record_job_id(record)
                if job_id in job_ids:
                    raise JSONLStagingError("current.jsonl contains a duplicate PDF job.")
                job_ids.add(job_id)
                last_complete_offset = stream.tell()
            return job_ids, data_end
    except JSONLStagingError:
        raise
    except OSError as exc:
        raise JSONLStagingError("current.jsonl could not be recovered safely.") from exc


def _metadata_payload(
    *,
    chunk_path: Path,
    sequence: int,
    status: str,
    record_count: int,
    byte_count: int,
    created_at: str,
    imported_at: str | None = None,
    error_code: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": CHUNK_SCHEMA_VERSION,
        "chunk_name": chunk_path.name,
        "sequence": sequence,
        "status": status,
        "record_count": record_count,
        "byte_count": byte_count,
        "created_at": created_at,
        "imported_at": imported_at,
        "error_code": error_code,
    }


def _validated_chunk(path: Path, payload: object) -> JSONLChunk:
    match = _CHUNK_PATTERN.fullmatch(path.name)
    expected_keys = {
        "schema_version",
        "chunk_name",
        "sequence",
        "status",
        "record_count",
        "byte_count",
        "created_at",
        "imported_at",
        "error_code",
    }
    if match is None or not isinstance(payload, dict) or set(payload) != expected_keys:
        raise JSONLStagingError("A JSONL chunk metadata file is invalid.")
    sequence = int(match.group(1))
    status = payload["status"]
    if (
        payload["schema_version"] != CHUNK_SCHEMA_VERSION
        or payload["chunk_name"] != path.name
        or payload["sequence"] != sequence
        or status not in _VALID_STATUSES
        or isinstance(payload["record_count"], bool)
        or not isinstance(payload["record_count"], int)
        or payload["record_count"] < 0
        or isinstance(payload["byte_count"], bool)
        or not isinstance(payload["byte_count"], int)
        or payload["byte_count"] < 0
        or not isinstance(payload["created_at"], str)
        or (payload["imported_at"] is not None and not isinstance(payload["imported_at"], str))
        or (payload["error_code"] is not None and not isinstance(payload["error_code"], str))
    ):
        raise JSONLStagingError("A JSONL chunk metadata file failed validation.")
    if status != CHUNK_STATUS_FAILED and (
        payload["record_count"] <= 0 or payload["byte_count"] <= 0
    ):
        raise JSONLStagingError("A usable JSONL chunk cannot be empty.")
    try:
        chunk_stat = path.lstat()
    except OSError as exc:
        raise JSONLStagingError("A JSONL chunk disappeared while reading metadata.") from exc
    if not stat.S_ISREG(chunk_stat.st_mode):
        raise JSONLStagingError("A sealed JSONL chunk is not a regular file.")
    actual_bytes = chunk_stat.st_size
    if actual_bytes != payload["byte_count"]:
        raise JSONLStagingError("A sealed JSONL chunk changed after rotation.")
    return JSONLChunk(
        path=path,
        metadata_path=chunk_metadata_path(path),
        sequence=sequence,
        status=str(status),
        record_count=payload["record_count"],
        byte_count=payload["byte_count"],
        created_at=payload["created_at"],
        imported_at=payload["imported_at"],
        error_code=payload["error_code"],
    )


def _read_chunk(path: Path) -> JSONLChunk:
    metadata_path = chunk_metadata_path(path)
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JSONLStagingError("A JSONL chunk metadata file could not be read.") from exc
    return _validated_chunk(path, payload)


def _write_chunk_status(
    chunk: JSONLChunk,
    *,
    status: str,
    imported_at: str | None = None,
    error_code: str | None = None,
) -> JSONLChunk:
    payload = _metadata_payload(
        chunk_path=chunk.path,
        sequence=chunk.sequence,
        status=status,
        record_count=chunk.record_count,
        byte_count=chunk.byte_count,
        created_at=chunk.created_at,
        imported_at=imported_at,
        error_code=error_code,
    )
    _atomic_json_write(chunk.metadata_path, payload)
    return _validated_chunk(chunk.path, payload)


def _recover_chunk_without_metadata(path: Path) -> JSONLChunk:
    match = _CHUNK_PATTERN.fullmatch(path.name)
    if match is None:
        raise JSONLStagingError("The sealed JSONL chunk name is invalid.")
    record_count = 0
    try:
        for _line_number, _record in iter_chunk_records(path):
            record_count += 1
    except JSONLStagingError:
        payload = _metadata_payload(
            chunk_path=path,
            sequence=int(match.group(1)),
            status=CHUNK_STATUS_FAILED,
            record_count=record_count,
            byte_count=path.stat().st_size,
            created_at=_timestamp(datetime.fromtimestamp(path.stat().st_mtime, UTC)),
            error_code="invalid_jsonl_chunk",
        )
        _atomic_json_write(chunk_metadata_path(path), payload)
        return _validated_chunk(path, payload)
    payload = _metadata_payload(
        chunk_path=path,
        sequence=int(match.group(1)),
        status=CHUNK_STATUS_SEALED,
        record_count=record_count,
        byte_count=path.stat().st_size,
        created_at=_timestamp(datetime.fromtimestamp(path.stat().st_mtime, UTC)),
    )
    _atomic_json_write(chunk_metadata_path(path), payload)
    return _validated_chunk(path, payload)


def list_chunks(*, recover: bool = True) -> tuple[JSONLChunk, ...]:
    root = staging_directory()
    chunks: list[JSONLChunk] = []
    for path in sorted(root.glob("chunk_*.jsonl")):
        if _CHUNK_PATTERN.fullmatch(path.name) is None:
            continue
        try:
            chunk = _read_chunk(path)
        except JSONLStagingError:
            if not recover:
                continue
            metadata_path = chunk_metadata_path(path)
            if metadata_path.exists():
                match = _CHUNK_PATTERN.fullmatch(path.name)
                if match is None:
                    continue
                try:
                    chunk_stat = path.stat()
                    payload = _metadata_payload(
                        chunk_path=path,
                        sequence=int(match.group(1)),
                        status=CHUNK_STATUS_FAILED,
                        record_count=0,
                        byte_count=chunk_stat.st_size,
                        created_at=_timestamp(datetime.fromtimestamp(chunk_stat.st_mtime, UTC)),
                        error_code="invalid_chunk_metadata",
                    )
                    _atomic_json_write(metadata_path, payload)
                    chunk = _validated_chunk(path, payload)
                except (OSError, JSONLStagingError):
                    continue
            else:
                chunk = _recover_chunk_without_metadata(path)
        chunks.append(chunk)
    return tuple(sorted(chunks, key=lambda item: item.sequence))


def claim_oldest_chunk() -> JSONLChunk | None:
    for chunk in list_chunks():
        if chunk.status in {CHUNK_STATUS_SEALED, CHUNK_STATUS_IMPORTING}:
            return _write_chunk_status(chunk, status=CHUNK_STATUS_IMPORTING)
    return None


def mark_chunk_imported(chunk: JSONLChunk, *, imported_at: datetime | None = None) -> JSONLChunk:
    return _write_chunk_status(
        _read_chunk(chunk.path),
        status=CHUNK_STATUS_IMPORTED,
        imported_at=_timestamp(imported_at),
    )


def mark_chunk_failed(chunk: JSONLChunk, *, error_code: str) -> JSONLChunk:
    return _write_chunk_status(
        _read_chunk(chunk.path),
        status=CHUNK_STATUS_FAILED,
        error_code=str(error_code or "jsonl_import_failed")[:64],
    )


def cleanup_expired_imported_chunks(*, now: datetime | None = None) -> tuple[str, ...]:
    cutoff = (now or _utc_now()) - timedelta(
        days=max(0, int(getattr(settings, "PDF_JSONL_RETENTION_DAYS", 7)))
    )
    removed: list[str] = []
    for chunk in list_chunks():
        if chunk.status != CHUNK_STATUS_IMPORTED or not chunk.imported_at:
            continue
        try:
            imported_at = datetime.fromisoformat(chunk.imported_at)
        except ValueError:
            continue
        if imported_at.tzinfo is None or imported_at > cutoff:
            continue
        try:
            chunk.path.unlink()
            _fsync_directory(chunk.path.parent)
            chunk.metadata_path.unlink(missing_ok=True)
            _fsync_directory(chunk.path.parent)
        except OSError:
            continue
        removed.append(chunk.path.name)
    return tuple(removed)


def staging_snapshot(*, now: datetime | None = None) -> dict[str, object]:
    observed_at = now or _utc_now()
    chunks = list_chunks(recover=False)
    current = current_jsonl_path()
    try:
        current_bytes = current.stat().st_size
    except OSError:
        current_bytes = 0
    incoming_count = 0
    incoming_bytes = 0
    for path in staging_directory().glob("job-*.json"):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        incoming_count += 1
        incoming_bytes += max(0, size)
    waiting = tuple(chunk for chunk in chunks if chunk.status == CHUNK_STATUS_SEALED)
    importing = tuple(chunk for chunk in chunks if chunk.status == CHUNK_STATUS_IMPORTING)
    queued = (*waiting, *importing)
    retained = tuple(chunk for chunk in chunks if chunk.status == CHUNK_STATUS_IMPORTED)
    eligibility: list[datetime] = []
    retention_days = max(0, int(getattr(settings, "PDF_JSONL_RETENTION_DAYS", 7)))
    for chunk in retained:
        if not chunk.imported_at:
            continue
        try:
            imported_at = datetime.fromisoformat(chunk.imported_at)
        except ValueError:
            continue
        if imported_at.tzinfo is not None:
            eligibility.append(imported_at + timedelta(days=retention_days))
    oldest_eligibility = min(eligibility) if eligibility else None
    future_eligibility = tuple(value for value in eligibility if value > observed_at)
    return {
        "currentSizeBytes": max(0, current_bytes),
        "incomingRecords": incoming_count,
        "incomingBytes": incoming_bytes,
        "sealedChunksWaiting": len(waiting),
        "queuedChunks": len(queued),
        "queuedBytes": sum(chunk.byte_count for chunk in queued),
        "writerState": "WRITING" if importing else "IDLE",
        "currentChunk": importing[0].path.name if importing else None,
        "retainedChunks": len(retained),
        "retainedBytes": sum(chunk.byte_count for chunk in retained),
        "failedChunks": sum(chunk.status == CHUNK_STATUS_FAILED for chunk in chunks),
        "oldestCleanupEligibleAt": (oldest_eligibility.isoformat() if oldest_eligibility else None),
        "nextCleanupEligibleAt": (
            min(future_eligibility).isoformat() if future_eligibility else None
        ),
        "expiredImportedChunks": sum(value <= observed_at for value in eligibility),
        "retentionDays": retention_days,
        "chunkTargetBytes": int(getattr(settings, "PDF_JSONL_CHUNK_SIZE_BYTES", 50 * 1024**2)),
    }


@contextmanager
def jsonl_stager_lock(*, blocking: bool = False) -> Iterator[None]:
    with _file_lock(staging_directory() / ".jsonl-stager.lock", blocking=blocking):
        yield


@contextmanager
def sqlite_chunk_writer_lock(*, blocking: bool = False) -> Iterator[None]:
    with _file_lock(staging_directory() / ".sqlite-chunk-writer.lock", blocking=blocking):
        yield


class JSONLStager:
    """The single process-local owner of current.jsonl and size-only rotation."""

    def __init__(self) -> None:
        self.root = staging_directory()
        self.current_path = current_jsonl_path()
        self.current_job_ids, self.current_size_bytes = _scan_current(self.current_path)
        self.current_record_count = len(self.current_job_ids)
        self._recover_sealed_chunks()

    def _recover_sealed_chunks(self) -> None:
        list_chunks(recover=True)

    def _next_sequence(self) -> int:
        sequences: list[int] = []
        for path in self.root.iterdir():
            match = _CHUNK_PATTERN.fullmatch(path.name) or _META_PATTERN.fullmatch(path.name)
            if match is not None:
                sequences.append(int(match.group(1)))
        return max(sequences, default=0) + 1

    def append_manifest(
        self,
        *,
        job_id: int,
        file_path: str,
        file_name: str,
        content: str,
        manifest: Mapping[str, object],
        incoming_path: Path,
    ) -> JSONLAppendResult:
        if job_id in self.current_job_ids:
            incoming_path.unlink(missing_ok=True)
            _fsync_directory(self.root)
            return JSONLAppendResult(job_id, self.current_size_bytes, None)
        record = {
            "file_path": str(file_path),
            "file_name": str(file_name),
            "content": str(content),
            "manifest": dict(manifest),
        }
        if _record_job_id(record) != job_id:
            raise JSONLStagingError("The JSONL record does not match its PDF job.")
        encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        try:
            with self.current_path.open("ab") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
                self.current_size_bytes = os.fstat(stream.fileno()).st_size
            self.current_job_ids.add(job_id)
            self.current_record_count += 1
            incoming_path.unlink()
            _fsync_directory(self.root)
        except OSError as exc:
            raise JSONLStagingError("The extracted PDF could not be staged durably.") from exc
        sealed = None
        appended_size = self.current_size_bytes
        target_bytes = max(
            1,
            int(getattr(settings, "PDF_JSONL_CHUNK_SIZE_BYTES", 50 * 1024**2)),
        )
        if self.current_size_bytes >= target_bytes:
            sealed = self.seal_current()
        return JSONLAppendResult(job_id, appended_size, sealed)

    def seal_current(self) -> JSONLChunk | None:
        if self.current_record_count <= 0 or self.current_size_bytes <= 0:
            return None
        sequence = self._next_sequence()
        chunk_path = self.root / f"chunk_{sequence:06d}.jsonl"
        try:
            with self.current_path.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(self.current_path, chunk_path)
            _fsync_directory(self.root)
            with self.current_path.open("xb") as stream:
                stream.flush()
                os.fsync(stream.fileno())
            _fsync_directory(self.root)
        except OSError as exc:
            raise JSONLStagingError("current.jsonl could not be sealed atomically.") from exc
        payload = _metadata_payload(
            chunk_path=chunk_path,
            sequence=sequence,
            status=CHUNK_STATUS_SEALED,
            record_count=self.current_record_count,
            byte_count=self.current_size_bytes,
            created_at=_timestamp(),
        )
        _atomic_json_write(chunk_metadata_path(chunk_path), payload)
        chunk = _validated_chunk(chunk_path, payload)
        self.current_job_ids = set()
        self.current_record_count = 0
        self.current_size_bytes = 0
        return chunk
