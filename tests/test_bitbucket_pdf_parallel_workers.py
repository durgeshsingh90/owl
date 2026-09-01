from __future__ import annotations

import ctypes
import hashlib
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
from django.db import close_old_connections, connection

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFDocumentLifecycle,
    PDFExtractionJob,
    PDFExtractionJobStatus,
    PDFIndexState,
    PDFPageExtractionState,
    PDFTextRevisionState,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncOperation,
    RepositorySyncState,
)
from bitbucket_search.services import pdf_indexing, repository_lock
from bitbucket_search.services.git_sync import managed_repository_path
from bitbucket_search.services.pdf_extractor import PDF_EXTRACTOR_VERSION
from bitbucket_search.services.repository_lock import (
    RepositoryCheckoutBusy,
    repository_checkout_lock,
)


@pytest.fixture
def parallel_targets(settings, tmp_path):
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "repositories"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "temporary"
    settings.PDF_MAX_EXTRACTION_WORKERS = 3

    def create(name, count):
        repository = BitbucketRepository.objects.create(
            display_name=name,
            canonical_remote_key=f"example.invalid/team/{name}",
            remote_url=f"https://example.invalid/team/{name}.git",
            sync_state=RepositorySyncState.READY,
        )
        checkout = managed_repository_path(repository)
        (checkout / ".git").mkdir(parents=True)
        repository.local_path = str(checkout)
        repository.save(update_fields=("local_path", "updated_at"))
        documents = []
        for number in range(count):
            path = checkout / f"parallel-{number}.pdf"
            path.write_bytes(f"%PDF synthetic parallel {name} {number}".encode())
            documents.append(
                PDFDocument.objects.create(
                    repository=repository,
                    filename=path.name,
                    relative_path=path.name,
                    git_blob_id=f"{number + 1:040x}",
                    last_seen_commit="b" * 40,
                    file_size=path.stat().st_size,
                )
            )
        pdf_indexing.queue_repository_pdf_extractions(repository)
        return repository, documents

    return create


def _stage(path):
    text = f"Parallel searchable fixture {path.name}"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return pdf_indexing.StagedPDFExtraction(
        state=PDFTextRevisionState.READY,
        pages=(
            pdf_indexing.StagedPDFPage(
                page_number=1,
                text=text,
                character_count=len(text),
                state=PDFPageExtractionState.READY,
            ),
        ),
        page_count=1,
        extracted_character_count=len(text),
        source_size_bytes=path.stat().st_size,
        content_sha256_before=digest,
        content_sha256_after=digest,
        extractor_version=PDF_EXTRACTOR_VERSION,
    )


def _run_with_file_backed_database(node_id):
    """Exercise runtime SQLite locking, not Django's shared-cache memory mode."""

    database_name = str(connection.settings_dict["NAME"])
    if database_name != ":memory:" and "mode=memory" not in database_name:
        return False
    child_script = textwrap.dedent(
        """
        import os
        import sys
        os.environ['DJANGO_SETTINGS_MODULE'] = 'owl.settings_test'
        from django.conf import settings
        test_database = settings.TEST_OWL_DATA_ROOT / 'parallel-runtime.sqlite3'
        settings.DATABASES['default'].setdefault('TEST', {})['NAME'] = str(test_database)
        import pytest
        raise SystemExit(pytest.main(['-q', sys.argv[1]]))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", child_script, node_id],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return True


@pytest.mark.django_db(transaction=True)
def test_three_controllers_parse_same_and_other_repositories_concurrently(
    parallel_targets, request
):
    # Shared-cache in-memory SQLite immediately raises SQLITE_LOCKED (262)
    # when a final SELECT overlaps a writer. OWL uses an ordinary SQLite file,
    # whose busy timeout permits those short overlaps. Run this same test body
    # in a disposable file-backed child; its database name prevents recursion.
    if _run_with_file_backed_database(request.node.nodeid):
        return
    first_repository, first_documents = parallel_targets("first", 2)
    second_repository, second_documents = parallel_targets("second", 1)
    claims = [pdf_indexing.claim_next_extraction_job() for _ in range(3)]
    assert len({job.pk for job in claims}) == 3
    # Fair claiming gives the other repository a slot before the first takes two.
    assert [job.document.repository_id for job in claims[:2]] == [
        first_repository.pk,
        second_repository.pk,
    ]
    assert pdf_indexing.claim_next_extraction_job() is None
    parsing_together = Barrier(3)

    def execute(job_id, repository_id):
        close_old_connections()
        connection.ensure_connection()
        connection_id = id(connection.connection)

        def extract(path, heartbeat):
            # All parsers must enter before any can finish. This fails when the
            # repository read gate accidentally serializes the first two PDFs.
            parsing_together.wait(timeout=10)
            with (
                pytest.raises(RepositoryCheckoutBusy),
                repository_checkout_lock(repository_id, blocking=False),
            ):
                pytest.fail("Git/delete must never mutate a checkout being read")
            heartbeat()
            return _stage(path)

        try:
            completed = pdf_indexing.execute_claimed_extraction_job(
                job_id, extraction_runner=extract
            )
            return connection_id, completed.status
        finally:
            connection.close()
            close_old_connections()

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(execute, job.pk, job.document.repository_id) for job in claims]
        results = [future.result(timeout=20) for future in futures]

    assert len({connection_id for connection_id, _status in results}) == 3
    assert {status for _connection_id, status in results} == {PDFExtractionJobStatus.SUCCEEDED}
    assert (
        PDFDocument.objects.filter(
            pk__in=[document.pk for document in first_documents + second_documents],
            index_state=PDFIndexState.READY,
        ).count()
        == 3
    )
    assert PDFExtractionJob.objects.filter(status=PDFExtractionJobStatus.RUNNING).count() == 0
    for claimed in claims:
        entries = list(claimed.operation_log_entries.order_by("id"))
        assert len({entry.pk for entry in entries}) == len(entries)
        assert entries[0].event == "indexing_claimed"
        assert entries[-1].event == "indexing_completed"
        assert sum(entry.event == "indexing_completed" for entry in entries) == 1
        assert [entry.phase for entry in entries if entry.event == "indexing_phase_changed"] == [
            "validating",
            "hashing",
            "extracting",
            "publishing",
        ]
    with repository_checkout_lock(first_repository.pk, blocking=False):
        pass


@pytest.mark.django_db(transaction=True)
def test_simultaneous_claims_never_exceed_the_global_capacity(parallel_targets, settings):
    parallel_targets("capacity", 8)
    settings.PDF_MAX_EXTRACTION_WORKERS = 2
    callers_ready = Barrier(6)

    def claim():
        close_old_connections()
        try:
            callers_ready.wait(timeout=5)
            job = pdf_indexing.claim_next_extraction_job()
            return job.pk if job is not None else None
        finally:
            connection.close()
            close_old_connections()

    with ThreadPoolExecutor(max_workers=6) as executor:
        claimed = list(executor.map(lambda _number: claim(), range(6)))

    claimed_ids = [job_id for job_id in claimed if job_id is not None]
    assert len(claimed_ids) == len(set(claimed_ids)) == 2
    assert PDFExtractionJob.objects.filter(status=PDFExtractionJobStatus.RUNNING).count() == 2


@pytest.mark.django_db(transaction=True)
def test_concurrent_unavailable_sweeps_publish_one_terminal_event(parallel_targets, request):
    if _run_with_file_backed_database(request.node.nodeid):
        return
    _repository, documents = parallel_targets("cancel-race", 1)
    document = documents[0]
    job = PDFExtractionJob.objects.get(document=document)
    PDFDocument.objects.filter(pk=document.pk).update(lifecycle_state=PDFDocumentLifecycle.REMOVED)
    sweepers_ready = Barrier(2)

    def sweep():
        close_old_connections()
        try:
            sweepers_ready.wait(timeout=5)
            return pdf_indexing.sweep_pdf_extraction_queue().cancelled_job_ids
        finally:
            connection.close()
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _number: sweep(), range(2)))

    job.refresh_from_db()
    assert job.status == PDFExtractionJobStatus.CANCELLED
    assert job.error_code == "extraction_target_unavailable"
    assert sum(job.pk in cancelled_ids for cancelled_ids in results) == 1
    entries = list(job.operation_log_entries.filter(event="indexing_cancelled"))
    assert len(entries) == 1
    assert entries[0].message == (
        "PDF indexing was cancelled because the document is no longer active in an enabled "
        "repository."
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status", [RepositorySyncJobStatus.QUEUED, RepositorySyncJobStatus.RUNNING]
)
def test_another_repository_sync_does_not_block_ready_pdf_extraction(parallel_targets, status):
    syncing, _documents = parallel_targets("syncing", 1)
    ready, _documents = parallel_targets("ready", 1)
    RepositorySyncJob.objects.create(
        repository=syncing,
        operation=RepositorySyncOperation.REFRESH,
        status=status,
    )

    claimed = pdf_indexing.claim_next_extraction_job()
    assert claimed.document.repository_id == ready.pk
    completed = pdf_indexing.execute_claimed_extraction_job(
        claimed.pk, extraction_runner=lambda path, heartbeat: (heartbeat(), _stage(path))[1]
    )

    assert completed.status == PDFExtractionJobStatus.SUCCEEDED
    assert pdf_indexing.claim_next_extraction_job() is None
    assert PDFExtractionJob.objects.get(document__repository=syncing).status == "queued"


def test_checkout_allows_multiple_readers_but_not_mutation(settings, tmp_path):
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "temporary"
    with (
        repository_checkout_lock(17, blocking=False, shared=True),
        repository_checkout_lock(17, blocking=False, shared=True),
        pytest.raises(RepositoryCheckoutBusy),
        repository_checkout_lock(17, blocking=False),
    ):
        pass
    with (
        repository_checkout_lock(17, blocking=False),
        pytest.raises(RepositoryCheckoutBusy),
        repository_checkout_lock(17, blocking=False, shared=True),
    ):
        pass


@pytest.mark.parametrize(
    ("parent_shared", "child_shared", "expected_status"),
    [(True, True, 0), (True, False, 3), (False, True, 3)],
)
def test_checkout_reader_writer_gate_applies_across_real_processes(
    settings, tmp_path, parent_shared, child_shared, expected_status
):
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "temporary"
    child_script = """
import sys
from django.conf import settings
settings.configure(BITBUCKET_TEMP_ROOT=sys.argv[1])
from bitbucket_search.services.repository_lock import RepositoryCheckoutBusy, repository_checkout_lock
try:
    with repository_checkout_lock(17, blocking=False, shared=sys.argv[2] == 'reader'):
        pass
except RepositoryCheckoutBusy:
    sys.exit(3)
"""
    with repository_checkout_lock(17, blocking=False, shared=parent_shared):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                child_script,
                str(settings.BITBUCKET_TEMP_ROOT),
                "reader" if child_shared else "writer",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    assert result.returncode == expected_status, result.stderr


def test_standalone_queue_reserves_sqlite_writer_before_reading(tmp_path):
    """A real file-backed DB reproduces the queue/publication upgrade race."""

    child_script = textwrap.dedent(
        """
        import os
        import sys
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        os.environ['DJANGO_SETTINGS_MODULE'] = 'owl.settings_test'
        from django.conf import settings
        settings.DATABASES['default']['NAME'] = sys.argv[1]
        import django
        django.setup()
        from django.core.management import call_command
        from django.db import close_old_connections, connection, transaction
        from django.db.models import F
        from bitbucket_search.models import BitbucketRepository, PDFDocument, PDFExtractionJob
        from bitbucket_search.services import pdf_indexing

        call_command('migrate', verbosity=0)
        repository = BitbucketRepository.objects.create(
            display_name='Concurrent queue',
            canonical_remote_key='example.invalid/team/concurrent-queue',
            remote_url='https://example.invalid/team/concurrent-queue.git',
            sync_state='ready',
        )
        PDFDocument.objects.create(
            repository=repository,
            filename='synthetic.pdf',
            relative_path='synthetic.pdf',
            git_blob_id='a' * 40,
            last_seen_commit='b' * 40,
            file_size=20,
        )
        read_started = Event()
        worker_attempted = Event()
        worker_reserved = Event()
        queue_finished = Event()

        def queue():
            close_old_connections()
            observed = False
            def after_first_repository_read(execute, sql, params, many, context):
                nonlocal observed
                result = execute(sql, params, many, context)
                if not observed and sql.lstrip().startswith('SELECT') and (
                    'bitbucket_search_bitbucketrepository' in sql
                ):
                    observed = True
                    read_started.set()
                    assert worker_attempted.wait(5)
                    # Without the early reservation the publisher grabs the
                    # writer lock here, forcing the queue's upgrade to fail.
                    assert not worker_reserved.wait(0.2)
                return result
            try:
                with connection.execute_wrapper(after_first_repository_read):
                    result = pdf_indexing.queue_repository_pdf_extractions(repository.pk)
                assert len(result.queued_job_ids) == 1
            finally:
                queue_finished.set()
                connection.close()

        def publisher():
            close_old_connections()
            try:
                assert read_started.wait(5)
                with transaction.atomic():
                    worker_attempted.set()
                    PDFExtractionJob.objects.filter(pk=-1).update(status=F('status'))
                    worker_reserved.set()
                    assert queue_finished.wait(5)
            finally:
                connection.close()

        # Queue reconciliation can run inside catalogue publication's existing
        # DB transaction; it must not acquire the PDF claim/publication file gate.
        def forbidden_gate():
            raise AssertionError('queue must not acquire a file gate after entering its transaction')
        pdf_indexing.pdf_extraction_claim_lock = forbidden_gate
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(queue)
            second = pool.submit(publisher)
            first.result(timeout=10)
            second.result(timeout=10)
        assert worker_reserved.is_set()
        assert PDFExtractionJob.objects.count() == 1
        connection.close()
        print('queue and publisher both completed')
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", child_script, str(tmp_path / "queue-concurrency.sqlite3")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "queue and publisher both completed" in result.stdout


@pytest.mark.parametrize(
    ("shared", "blocking", "expected_flags"),
    [(True, False, 0x01), (True, True, 0), (False, False, 0x03), (False, True, 0x02)],
)
def test_windows_uses_true_shared_locks_with_pointer_sized_handles(
    monkeypatch, shared, blocking, expected_flags
):
    operations = []

    class Operation:
        def __call__(self, *arguments):
            operations.append(arguments)
            return True

    kernel = SimpleNamespace(LockFileEx=Operation(), UnlockFileEx=Operation())
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda _fd: 2**40))
    handle = SimpleNamespace(fileno=lambda: 123)

    repository_lock._windows_lock(handle, blocking=blocking, shared=shared)
    repository_lock._windows_lock(handle, blocking=False, shared=False, release=True)

    assert operations[0][0].value == 2**40
    assert operations[0][1:5] == (expected_flags, 0, 1, 0)
    assert operations[1][1:4] == (0, 1, 0)
    overlap = operations[0][-1]._obj
    assert overlap.Offset == overlap.OffsetHigh == 0
    assert overlap.hEvent is None
