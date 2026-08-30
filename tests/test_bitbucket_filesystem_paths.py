from __future__ import annotations

import os
from pathlib import Path

import pytest

from bitbucket_search.services.filesystem_paths import (
    _extended_windows_path,
    _plain_windows_path,
    display_path,
    filesystem_path,
)


@pytest.mark.parametrize(
    ("normal", "extended"),
    [
        (r"C:\repos\Docs\Guide.pdf", r"\\?\C:\repos\Docs\Guide.pdf"),
        ("C:/repos/Docs/Guide.pdf", r"\\?\C:\repos\Docs\Guide.pdf"),
        (r"C:\repos\docs\..\Guide.pdf", r"\\?\C:\repos\Guide.pdf"),
        (r"\\server\share\repo\Guide.pdf", r"\\?\UNC\server\share\repo\Guide.pdf"),
        (r"\\?\C:\repos\Guide.pdf", r"\\?\C:\repos\Guide.pdf"),
        (r"\\?\UNC\server\share\Guide.pdf", r"\\?\UNC\server\share\Guide.pdf"),
    ],
)
def test_windows_io_spelling_supports_drive_unc_and_existing_prefix(normal, extended):
    assert _extended_windows_path(normal) == extended
    assert not _plain_windows_path(extended).startswith("\\\\?\\")


@pytest.mark.parametrize(
    "unsafe",
    [
        "relative.pdf",
        "C:relative.pdf",
        r"\rooted-without-drive.pdf",
        r"\\.\PhysicalDrive0",
        r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1",
        r"\\?\Volume{example}\Guide.pdf",
    ],
)
def test_windows_io_adapter_rejects_relative_and_device_namespaces(unsafe):
    with pytest.raises(ValueError):
        _extended_windows_path(unsafe)


def test_long_windows_paths_are_not_truncated_and_display_spelling_is_unchanged():
    normal = "C:\\repos\\" + "a-long-folder\\" * 30 + "Guide.pdf"
    assert len(normal) > 260
    assert _plain_windows_path(_extended_windows_path(normal)) == normal


@pytest.mark.skipif(os.name == "nt", reason="POSIX preserves its native path spelling")
def test_posix_paths_are_unchanged(tmp_path):
    path = tmp_path / "docs" / "Guide.pdf"
    assert filesystem_path(path) is path
    assert display_path(path) is path


@pytest.mark.django_db
@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows long-path filesystem APIs")
def test_windows_deep_pdf_can_be_scanned_catalogued_validated_and_extracted(tmp_path, settings):
    from pypdf import PdfWriter

    from bitbucket_search.models import BitbucketRepository, PDFDocument, RepositorySyncState
    from bitbucket_search.services.document_actions import validated_pdf_path
    from bitbucket_search.services.git_sync import _scan_documents, managed_repository_path
    from bitbucket_search.services.pdf_catalog import _parse_tree_pdfs
    from bitbucket_search.services.pdf_indexing import run_isolated_pdf_extractor

    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "repos"
    settings.BITBUCKET_TEMP_ROOT = tmp_path / "temporary"
    repository = BitbucketRepository.objects.create(
        display_name="Deep documents",
        canonical_remote_key="example.invalid/deep-documents",
        remote_url="https://example.invalid/deep-documents.git",
        sync_state=RepositorySyncState.READY,
    )
    root = managed_repository_path(repository)
    filesystem_path(root / ".git").mkdir(parents=True)
    relative = "/".join(["nested-folder"] * 25 + ["Guide.pdf"])
    normal_path = root / Path(relative)
    io_path = filesystem_path(normal_path)
    io_path.parent.mkdir(parents=True)
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    with io_path.open("wb") as output:
        writer.write(output)
    size = io_path.stat().st_size
    repository.local_path = str(root)
    repository.save(update_fields=("local_path",))
    document = PDFDocument.objects.create(
        repository=repository,
        filename="Guide.pdf",
        relative_path=relative,
        git_blob_id="a" * 40,
        file_size=size,
    )

    assert _scan_documents(root)[0].pdf_count == 1
    record = b"100644 " + b"a" * 40 + b" 0\t" + relative.encode()
    assert _parse_tree_pdfs(root, iter([record]))[0].file_size == size
    validated = validated_pdf_path(document)
    assert validated == normal_path
    assert not str(validated).startswith("\\\\?\\")
    result = run_isolated_pdf_extractor(filesystem_path(validated), lambda: None)
    assert result.publishable
    assert result.page_count == 1
