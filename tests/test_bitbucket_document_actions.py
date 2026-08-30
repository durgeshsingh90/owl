from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, call

import pytest

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFDocumentLifecycle,
    RepositorySyncState,
)
from bitbucket_search.services import document_actions
from bitbucket_search.services.document_actions import (
    DocumentActionError,
    open_registered_pdf,
    open_registered_pdfs,
    record_successful_open,
    reveal_registered_pdf,
    validated_pdf_path,
)
from bitbucket_search.services.filesystem_paths import display_path
from bitbucket_search.services.git_sync import managed_repository_path
from bitbucket_search.services.repository_lock import repository_checkout_lock

pytestmark = pytest.mark.django_db


@pytest.fixture
def registered_pdf(tmp_path: Path, settings) -> tuple[PDFDocument, Path]:
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "media" / "repositories"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "media" / "tmp"
    repository = BitbucketRepository.objects.create(
        display_name="Architecture Docs",
        canonical_remote_key="example.invalid/workspace/architecture-docs",
        remote_url="https://example.invalid/workspace/architecture-docs.git",
        sync_state=RepositorySyncState.READY,
    )
    checkout = managed_repository_path(repository)
    (checkout / ".git").mkdir(parents=True)
    (checkout / "docs").mkdir()
    pdf_path = checkout / "docs" / "Architecture.PDF"
    pdf_path.write_bytes(b"synthetic PDF")
    repository.local_path = str(checkout)
    repository.save(update_fields=("local_path", "updated_at"))
    document = PDFDocument.objects.create(
        repository=repository,
        filename=pdf_path.name,
        relative_path="docs/Architecture.PDF",
        file_size=pdf_path.stat().st_size,
    )
    return document, pdf_path


def test_validated_pdf_path_accepts_only_the_managed_regular_pdf(registered_pdf):
    document, pdf_path = registered_pdf

    assert validated_pdf_path(document) == pdf_path.resolve(strict=True)


@pytest.mark.parametrize(
    "relative_path",
    (
        "../outside.pdf",
        "/tmp/outside.pdf",
        "docs\\Architecture.pdf",
        "docs//Architecture.pdf",
        "docs/./Architecture.pdf",
        "C:/Architecture.pdf",
        "docs/line\nbreak.pdf",
        "docs/carriage\rreturn.pdf",
        "docs/tab\tname.pdf",
        "docs/control\x1fname.pdf",
        "docs/delete\x7fname.pdf",
        "docs/next-line\u0085name.pdf",
        "docs/line-separator\u2028name.pdf",
        "docs/paragraph-separator\u2029name.pdf",
    ),
)
def test_validated_pdf_path_rejects_unsafe_posix_paths(registered_pdf, relative_path):
    document, _pdf_path = registered_pdf
    document.relative_path = relative_path

    with pytest.raises(DocumentActionError) as captured:
        validated_pdf_path(document)

    assert captured.value.code == "invalid_document_path"


def test_validated_pdf_path_rejects_inactive_or_mismatched_records(
    registered_pdf,
    tmp_path,
):
    document, _pdf_path = registered_pdf
    document.lifecycle_state = PDFDocumentLifecycle.REMOVED

    with pytest.raises(DocumentActionError) as inactive:
        validated_pdf_path(document)
    assert inactive.value.code == "document_unavailable"

    document.lifecycle_state = PDFDocumentLifecycle.ACTIVE
    document.repository.local_path = str(tmp_path / "other-checkout")
    with pytest.raises(DocumentActionError) as mismatched:
        validated_pdf_path(document)
    assert mismatched.value.code == "invalid_repository_checkout"


@pytest.mark.parametrize("symlink_kind", ("component", "final"))
def test_validated_pdf_path_rejects_symlink_components_and_final_file(
    registered_pdf,
    tmp_path,
    symlink_kind,
):
    document, pdf_path = registered_pdf
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_pdf = outside / "outside.pdf"
    outside_pdf.write_bytes(b"outside")

    try:
        if symlink_kind == "component":
            link = pdf_path.parents[1] / "linked"
            link.symlink_to(outside, target_is_directory=True)
            document.relative_path = "linked/outside.pdf"
        else:
            link = pdf_path.with_name("linked.pdf")
            link.symlink_to(outside_pdf)
            document.relative_path = "docs/linked.pdf"
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"Symbolic links are unavailable: {exc}")

    with pytest.raises(DocumentActionError) as captured:
        validated_pdf_path(document)

    assert captured.value.code == "invalid_document_path"


@pytest.mark.parametrize("junction_location", ("parent", "root", "git", "component"))
@pytest.mark.parametrize("allow_missing", (False, True))
def test_checkout_pdf_validation_rejects_simulated_in_boundary_junctions(
    registered_pdf, monkeypatch, junction_location, allow_missing
):
    document, pdf_path = registered_pdf
    checkout = pdf_path.parents[1]
    junction_path = {
        "parent": checkout.parent,
        "root": checkout,
        "git": checkout / ".git",
        "component": pdf_path.parent,
    }[junction_location]
    # Simulate Windows junction detection on a real in-bound directory. The
    # ordinary symlink and final containment checks would otherwise accept it.
    assert not junction_path.is_symlink()
    assert junction_path.resolve(strict=True) == junction_path
    original_is_junction = Path.is_junction

    def is_junction(candidate):
        return display_path(candidate) == junction_path or original_is_junction(candidate)

    monkeypatch.setattr(Path, "is_junction", is_junction)
    if allow_missing:
        document.relative_path = "docs/Missing.pdf"

    with pytest.raises(DocumentActionError) as captured:
        document_actions.validated_checkout_pdf_path(document, allow_missing=allow_missing)

    expected_code = (
        "invalid_document_path"
        if junction_location == "component"
        else "invalid_repository_checkout"
    )
    assert captured.value.code == expected_code
    assert str(junction_path) not in captured.value.summary
    assert pdf_path.read_bytes() == b"synthetic PDF"


def test_validated_pdf_path_rejects_missing_non_pdf_and_non_regular_entries(registered_pdf):
    document, pdf_path = registered_pdf

    document.relative_path = "docs/missing.pdf"
    with pytest.raises(DocumentActionError) as missing:
        validated_pdf_path(document)
    assert missing.value.code == "document_unavailable"

    text_path = pdf_path.with_suffix(".txt")
    text_path.write_text("not a PDF", encoding="utf-8")
    document.relative_path = "docs/Architecture.txt"
    with pytest.raises(DocumentActionError) as non_pdf:
        validated_pdf_path(document)
    assert non_pdf.value.code == "unsupported_document_type"

    directory = pdf_path.parent / "directory.pdf"
    directory.mkdir()
    document.relative_path = "docs/directory.pdf"
    with pytest.raises(DocumentActionError) as non_regular:
        validated_pdf_path(document)
    assert non_regular.value.code == "document_unavailable"


def test_macos_open_and_reveal_use_bounded_argument_array_launches(monkeypatch, tmp_path):
    pdf_path = tmp_path / "report.pdf"
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(document_actions.sys, "platform", "darwin")
    monkeypatch.setattr(document_actions.subprocess, "run", run)

    document_actions.open_pdf_native(pdf_path)
    document_actions.reveal_pdf_in_folder(pdf_path)

    common = {
        "check": False,
        "shell": False,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "timeout": 10,
    }
    assert run.call_args_list == [
        call(["/usr/bin/open", str(pdf_path)], **common),
        call(["/usr/bin/open", "-R", str(pdf_path)], **common),
    ]


def test_linux_open_and_reveal_use_xdg_open_without_a_shell(monkeypatch, tmp_path):
    pdf_path = tmp_path / "folder" / "report.pdf"
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(document_actions.sys, "platform", "linux")
    monkeypatch.setattr(document_actions.shutil, "which", Mock(return_value="/usr/bin/xdg-open"))
    monkeypatch.setattr(document_actions.subprocess, "run", run)

    document_actions.open_pdf_native(pdf_path)
    document_actions.reveal_pdf_in_folder(pdf_path)

    assert run.call_args_list[0].args[0] == ["/usr/bin/xdg-open", str(pdf_path)]
    assert run.call_args_list[1].args[0] == ["/usr/bin/xdg-open", str(pdf_path.parent)]
    assert all(arguments.kwargs["shell"] is False for arguments in run.call_args_list)


def test_windows_open_and_reveal_use_startfile(monkeypatch, tmp_path):
    pdf_path = tmp_path / "folder" / "report.pdf"
    startfile = Mock()
    monkeypatch.setattr(document_actions.sys, "platform", "win32")
    monkeypatch.setattr(document_actions.os, "startfile", startfile, raising=False)

    document_actions.open_pdf_native(pdf_path)
    document_actions.reveal_pdf_in_folder(pdf_path)

    assert startfile.call_args_list == [call(str(pdf_path)), call(str(pdf_path.parent))]


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    (
        (subprocess.TimeoutExpired(["open"], 10), "native_action_timeout"),
        (OSError("/private/secret/report.pdf could not be opened"), "native_action_failed"),
    ),
)
def test_native_launcher_errors_are_sanitized(monkeypatch, tmp_path, failure, expected_code):
    pdf_path = tmp_path / "private-secret-report.pdf"
    monkeypatch.setattr(document_actions.sys, "platform", "darwin")
    monkeypatch.setattr(document_actions.subprocess, "run", Mock(side_effect=failure))

    with pytest.raises(DocumentActionError) as captured:
        document_actions.open_pdf_native(pdf_path)

    assert captured.value.code == expected_code
    assert str(pdf_path) not in captured.value.summary
    assert "private/secret" not in captured.value.summary


def test_record_successful_open_uses_atomic_count_and_preserves_first_time(registered_pdf):
    document, _pdf_path = registered_pdf
    first_time = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
    second_time = datetime(2026, 8, 29, 11, 0, tzinfo=UTC)

    record_successful_open(document.pk, at=first_time)
    updated = record_successful_open(document.pk, at=second_time)

    assert updated.open_count == 2
    assert updated.first_opened_at == first_time
    assert updated.last_opened_at == second_time


def test_open_registered_pdf_validates_dispatches_then_counts(registered_pdf, monkeypatch):
    document, pdf_path = registered_pdf
    observed_counts: list[int] = []

    def dispatch(path: Path) -> None:
        document.refresh_from_db()
        observed_counts.append(document.open_count)
        assert path == pdf_path.resolve(strict=True)

    monkeypatch.setattr(document_actions, "open_pdf_native", dispatch)

    updated = open_registered_pdf(document.pk)

    assert observed_counts == [0]
    assert updated.open_count == 1
    assert updated.first_opened_at is not None
    assert updated.last_opened_at is not None


def test_bulk_open_deduplicates_in_order_and_counts_after_each_dispatch(
    registered_pdf,
    monkeypatch,
):
    first_document, first_path = registered_pdf
    second_path = first_path.with_name("Second.pdf")
    second_path.write_bytes(b"second synthetic PDF")
    second_document = PDFDocument.objects.create(
        repository=first_document.repository,
        filename=second_path.name,
        relative_path="docs/Second.pdf",
        file_size=second_path.stat().st_size,
    )
    observed: list[tuple[Path, int, int]] = []

    def dispatch(path: Path) -> None:
        first_document.refresh_from_db()
        second_document.refresh_from_db()
        observed.append((path, first_document.open_count, second_document.open_count))

    monkeypatch.setattr(document_actions, "open_pdf_native", dispatch)

    result = open_registered_pdfs([second_document.pk, first_document.pk, second_document.pk])

    assert observed == [
        (second_path.resolve(strict=True), 0, 0),
        (first_path.resolve(strict=True), 0, 1),
    ]
    assert result.requested_count == 2
    assert [document.pk for document in result.opened_documents] == [
        second_document.pk,
        first_document.pk,
    ]
    assert result.opened_count == 2
    assert result.failed_count == 0


def test_bulk_open_validates_every_path_before_dispatching_anything(
    registered_pdf,
    monkeypatch,
):
    valid_document, _valid_path = registered_pdf
    missing_document = PDFDocument.objects.create(
        repository=valid_document.repository,
        filename="Missing.pdf",
        relative_path="docs/Missing.pdf",
    )
    dispatch = Mock()
    monkeypatch.setattr(document_actions, "open_pdf_native", dispatch)

    with pytest.raises(DocumentActionError) as captured:
        open_registered_pdfs([valid_document.pk, missing_document.pk])

    assert captured.value.code == "document_unavailable"
    dispatch.assert_not_called()
    valid_document.refresh_from_db()
    assert valid_document.open_count == 0


def test_bulk_open_continues_after_native_failure_and_counts_only_successes(
    registered_pdf,
    monkeypatch,
):
    first_document, first_path = registered_pdf
    second_path = first_path.with_name("Second.pdf")
    second_path.write_bytes(b"second synthetic PDF")
    second_document = PDFDocument.objects.create(
        repository=first_document.repository,
        filename=second_path.name,
        relative_path="docs/Second.pdf",
        file_size=second_path.stat().st_size,
    )

    def dispatch(path: Path) -> None:
        if path == first_path.resolve(strict=True):
            raise DocumentActionError("native_action_failed", "Could not open this PDF.")

    monkeypatch.setattr(document_actions, "open_pdf_native", dispatch)

    result = open_registered_pdfs([first_document.pk, second_document.pk])

    first_document.refresh_from_db()
    second_document.refresh_from_db()
    assert first_document.open_count == 0
    assert second_document.open_count == 1
    assert [document.pk for document in result.opened_documents] == [second_document.pk]
    assert result.opened_count == 1
    assert result.failures == (
        document_actions.BulkDocumentOpenFailure(
            document_id=first_document.pk,
            code="native_action_failed",
            summary="Could not open this PDF.",
        ),
    )


def test_bulk_open_continues_when_usage_count_fails_after_native_dispatch(
    registered_pdf,
    monkeypatch,
):
    first_document, first_path = registered_pdf
    second_path = first_path.with_name("Second.pdf")
    second_path.write_bytes(b"second synthetic PDF")
    second_document = PDFDocument.objects.create(
        repository=first_document.repository,
        filename=second_path.name,
        relative_path="docs/Second.pdf",
        file_size=second_path.stat().st_size,
    )
    dispatch = Mock()
    original_record = document_actions.record_successful_open

    def record(document_id: int):
        if document_id == first_document.pk:
            raise DocumentActionError(
                "document_unavailable",
                "This PDF opened, but its usage could not be recorded.",
            )
        return original_record(document_id)

    monkeypatch.setattr(document_actions, "open_pdf_native", dispatch)
    monkeypatch.setattr(document_actions, "record_successful_open", record)

    result = open_registered_pdfs([first_document.pk, second_document.pk])

    assert dispatch.call_count == 2
    assert result.opened_count == 2
    assert result.failed_count == 0
    assert result.usage_failure_count == 1
    assert result.usage_failures[0].document_id == first_document.pk
    second_document.refresh_from_db()
    assert second_document.open_count == 1


def test_native_open_fails_fast_while_repository_checkout_is_refreshing(
    registered_pdf,
    monkeypatch,
):
    document, _path = registered_pdf
    dispatch = Mock()
    monkeypatch.setattr(document_actions, "open_pdf_native", dispatch)

    with (
        repository_checkout_lock(document.repository_id, blocking=True),
        pytest.raises(DocumentActionError) as captured,
    ):
        open_registered_pdf(document.pk)

    assert captured.value.code == "repository_refresh_in_progress"
    dispatch.assert_not_called()


def test_bulk_open_rejects_empty_invalid_missing_and_oversized_selections(registered_pdf):
    document, _path = registered_pdf

    for selection, expected_code in (
        ([], "invalid_document_selection"),
        ([0], "invalid_document_selection"),
        ([document.pk, document.pk + 1], "document_not_found"),
        ([document.pk] * 201, "too_many_documents"),
    ):
        with pytest.raises(DocumentActionError) as captured:
            open_registered_pdfs(selection)
        assert captured.value.code == expected_code


def test_failed_open_does_not_count_and_reveal_never_counts(registered_pdf, monkeypatch):
    document, pdf_path = registered_pdf
    monkeypatch.setattr(
        document_actions,
        "open_pdf_native",
        Mock(side_effect=DocumentActionError("native_action_failed", "Could not open PDF.")),
    )

    with pytest.raises(DocumentActionError):
        open_registered_pdf(document.pk)
    document.refresh_from_db()
    assert document.open_count == 0

    reveal = Mock()
    monkeypatch.setattr(document_actions, "reveal_pdf_in_folder", reveal)
    revealed = reveal_registered_pdf(document.pk)
    document.refresh_from_db()

    reveal.assert_called_once_with(pdf_path.resolve(strict=True))
    assert revealed.pk == document.pk
    assert document.open_count == 0


def test_registered_actions_do_not_accept_unregistered_identifiers():
    with pytest.raises(DocumentActionError) as captured:
        open_registered_pdf("/tmp/browser-supplied.pdf")  # type: ignore[arg-type]

    assert captured.value.code == "document_not_found"
    assert "/tmp/browser-supplied.pdf" not in captured.value.summary
