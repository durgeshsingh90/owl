from __future__ import annotations

import builtins
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from bitbucket_search.services import pdf_extractor
from bitbucket_search.services.pdf_extractor import (
    PDFExtractionState,
    PDFPageState,
    extract_pdf,
    extract_pdf_request,
    normalize_pdf_text,
)


class FakePage:
    def __init__(self, value: str | None = "", *, error: Exception | None = None) -> None:
        self.value = value
        self.error = error

    def extract_text(self) -> str | None:
        if self.error is not None:
            raise self.error
        return self.value


class FakeReader:
    def __init__(self, pages=(), *, encrypted: bool = False) -> None:
        self.pages = tuple(pages)
        self.is_encrypted = encrypted


class PdfReadError(Exception):
    pass


def _source(tmp_path: Path, content: bytes = b"%PDF-1.7\nsynthetic") -> Path:
    path = tmp_path / "document.pdf"
    path.write_bytes(content)
    return path


def _extract(path: Path, reader: FakeReader):
    return extract_pdf(
        path,
        max_file_bytes=10_000,
        max_pages=100,
        max_characters=10_000,
        reader_factory=lambda _source_file: reader,
    )


def _write_real_pdf(path: Path, *, text: str = "", password: str = "") -> None:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    if text:
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        fonts = DictionaryObject({NameObject("/F1"): writer._add_object(font)})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): fonts})
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(content)
    if password:
        writer.encrypt(password)
    with path.open("wb") as target:
        writer.write(target)


def test_ready_extraction_normalizes_text_numbers_pages_and_verifies_hash_without_logging(
    tmp_path,
    caplog,
):
    path = _source(tmp_path)
    private_text = "Ａ\tprivate\n\n text\u200b"

    result = _extract(
        path,
        FakeReader((FakePage(private_text), FakePage(None))),
    )

    expected_text = "A private text"
    expected_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    assert result.state == PDFExtractionState.READY
    assert result.publishable is True
    assert result.page_count == 2
    assert result.extracted_character_count == len(expected_text)
    assert result.content_sha256_before == expected_hash
    assert result.content_sha256_after == expected_hash
    assert [(page.page_number, page.state) for page in result.pages] == [
        (1, PDFPageState.READY),
        (2, PDFPageState.NO_TEXT),
    ]
    assert result.pages[0].text == expected_text
    assert result.pages[0].character_count == len(expected_text)
    assert private_text not in repr(result)
    assert expected_text not in repr(result)
    assert private_text not in caplog.text
    assert expected_text not in caplog.text


def test_real_pypdf_reader_extracts_machine_readable_page_text(tmp_path):
    path = tmp_path / "readable.pdf"
    _write_real_pdf(path, text="Hello searchable PDF")

    result = extract_pdf(
        path,
        max_file_bytes=100_000,
        max_pages=10,
        max_characters=10_000,
    )

    assert result.state == PDFExtractionState.READY
    assert result.page_count == 1
    assert result.pages[0].text == "Hello searchable PDF"


def test_real_pypdf_reader_distinguishes_blank_encrypted_and_corrupt_documents(tmp_path):
    blank = tmp_path / "blank.pdf"
    encrypted = tmp_path / "encrypted.pdf"
    corrupt = tmp_path / "corrupt.pdf"
    _write_real_pdf(blank)
    _write_real_pdf(encrypted, password="not-a-real-secret")
    corrupt.write_bytes(b"%PDF-1.7\nnot a valid PDF")

    def state(path: Path) -> PDFExtractionState:
        return extract_pdf(
            path,
            max_file_bytes=100_000,
            max_pages=10,
            max_characters=10_000,
        ).state

    assert state(blank) == PDFExtractionState.NO_TEXT
    assert state(encrypted) == PDFExtractionState.ENCRYPTED
    assert state(corrupt) == PDFExtractionState.CORRUPT


def test_module_json_boundary_runs_in_a_separate_process(tmp_path):
    path = tmp_path / "isolated.pdf"
    _write_real_pdf(path, text="Isolated searchable text")
    request = {
        "path": str(path),
        "max_file_bytes": 100_000,
        "max_pages": 10,
        "max_characters": 10_000,
    }

    completed = subprocess.run(
        [sys.executable, "-m", "bitbucket_search.services.pdf_extractor"],
        input=json.dumps(request).encode(),
        capture_output=True,
        check=True,
        timeout=5,
    )
    payload = json.loads(completed.stdout)

    assert completed.stderr == b""
    assert payload["state"] == "ready"
    assert payload["pages"][0]["text"] == "Isolated searchable text"
    assert str(path) not in completed.stderr.decode()


def test_isolated_missing_installation_is_actionable_not_a_corrupt_or_unknown_pdf(tmp_path):
    path = _source(tmp_path)
    completed = subprocess.run(
        [sys.executable, "-S", "-m", "bitbucket_search.services.pdf_extractor"],
        input=json.dumps(
            {
                "path": str(path),
                "max_file_bytes": 100_000,
                "max_pages": 10,
                "max_characters": 10_000,
            }
        ).encode(),
        capture_output=True,
        check=True,
        timeout=5,
    )
    payload = json.loads(completed.stdout)

    assert payload["state"] == "dependency_unavailable"
    assert payload["error_code"] == "pdf_dependency_unavailable"
    assert "Install the project requirements" in payload["error_summary"]
    assert payload["diagnostic"] == {
        "stage": "load_parser",
        "reason": "module_not_found",
        "errno": None,
        "winerror": None,
    }
    assert payload["publishable"] is False
    assert payload["pages"] == []
    assert completed.stderr == b""
    assert str(path) not in completed.stdout.decode()


@pytest.mark.parametrize("error", [ImportError("private dll"), OSError(126, "private dll")])
def test_parser_binary_startup_failure_has_safe_dependency_diagnostics(
    tmp_path, monkeypatch, error
):
    original_import = builtins.__import__

    def broken_import(name, *args, **kwargs):
        if name == "pypdf":
            raise error
        return original_import(name, *args, **kwargs)

    path = _source(tmp_path)
    monkeypatch.setattr(builtins, "__import__", broken_import)
    result = extract_pdf(path, max_file_bytes=10_000, max_pages=100, max_characters=10_000)

    assert result.state == PDFExtractionState.DEPENDENCY_UNAVAILABLE
    assert result.diagnostic.stage == "load_parser"
    assert result.diagnostic.reason in {"import_error", "os_error"}
    assert "private" not in str(result.to_payload())


def test_parser_startup_memory_error_keeps_resource_limit_classification(tmp_path, monkeypatch):
    original_import = builtins.__import__

    def memory_limited_import(name, *args, **kwargs):
        if name == "pypdf":
            raise MemoryError("private detail")
        return original_import(name, *args, **kwargs)

    path = _source(tmp_path)
    monkeypatch.setattr(builtins, "__import__", memory_limited_import)
    result = extract_pdf(path, max_file_bytes=10_000, max_pages=100, max_characters=10_000)
    assert result.state == PDFExtractionState.RESOURCE_LIMIT
    assert result.diagnostic.reason == "memory_error"
    assert result.diagnostic.stage == "load_parser"


def test_parser_unknown_error_preserves_only_safe_category_and_stage(tmp_path):
    private_exception = type("SensitiveDocumentName", (RuntimeError,), {})
    error = private_exception("credential-shaped private parser contents")
    path = _source(tmp_path)
    result = extract_pdf(
        path,
        max_file_bytes=10_000,
        max_pages=100,
        max_characters=10_000,
        reader_factory=lambda _source: (_ for _ in ()).throw(error),
    )
    assert result.state == PDFExtractionState.UNKNOWN_ERROR
    assert result.diagnostic.stage == "read_pdf"
    assert result.diagnostic.reason == "runtime_error"
    assert "SensitiveDocumentName" not in str(result.to_payload())
    assert "credential-shaped" not in str(result.to_payload())


def test_windows_style_file_error_preserves_only_numeric_os_codes(tmp_path, monkeypatch):
    error = OSError(22, "private Windows file name")
    error.winerror = 123
    path = _source(tmp_path)
    monkeypatch.setattr(
        pdf_extractor, "_fingerprint_file", lambda *_args, **_kwargs: (_ for _ in ()).throw(error)
    )

    result = _extract(path, FakeReader())

    assert result.diagnostic.to_payload() == {
        "stage": "fingerprint_before",
        "reason": "os_error",
        "errno": 22,
        "winerror": 123,
    }
    assert "private" not in str(result.to_payload())


def test_normalize_pdf_text_applies_nfkc_and_collapses_whitespace_and_controls():
    assert normalize_pdf_text("  ℌｅｌｌｏ\r\n\tworld\x00\ud800  ") == "Hello world"


def test_no_text_is_publishable_and_keeps_one_based_page_metadata(tmp_path):
    path = _source(tmp_path)

    result = _extract(path, FakeReader((FakePage(" \n "), FakePage(None))))

    assert result.state == PDFExtractionState.NO_TEXT
    assert result.error_code == "no_text"
    assert result.publishable is True
    assert result.page_count == 2
    assert result.extracted_character_count == 0
    assert [page.page_number for page in result.pages] == [1, 2]
    assert all(page.state == PDFPageState.NO_TEXT for page in result.pages)


def test_one_failed_page_produces_publishable_partial_staging_rows(tmp_path):
    path = _source(tmp_path)

    result = _extract(
        path,
        FakeReader(
            (
                FakePage("first page"),
                FakePage(error=RuntimeError("private parser detail")),
                FakePage("third page"),
            )
        ),
    )

    assert result.state == PDFExtractionState.PARTIAL
    assert result.error_code == "partial_extraction"
    assert result.publishable is True
    assert result.page_count == 3
    assert [page.page_number for page in result.pages] == [1, 2, 3]
    assert result.pages[1].state == PDFPageState.FAILED
    assert result.pages[1].error_code == "page_extraction_failed"
    assert "private parser detail" not in result.error_summary
    assert result.diagnostic.stage == "extract_page"
    assert result.diagnostic.reason == "runtime_error"


def test_encrypted_reader_is_distinct_and_returns_no_staged_text(tmp_path):
    result = _extract(_source(tmp_path), FakeReader(encrypted=True))

    assert result.state == PDFExtractionState.ENCRYPTED
    assert result.error_code == "encrypted_pdf"
    assert result.publishable is False
    assert result.pages == ()
    assert result.content_sha256_before == result.content_sha256_after


@pytest.mark.parametrize(
    ("error", "expected_state", "expected_code"),
    [
        (PdfReadError("private corrupt detail"), PDFExtractionState.CORRUPT, "corrupt_pdf"),
        (
            RuntimeError("private unknown detail"),
            PDFExtractionState.UNKNOWN_ERROR,
            "pdf_unknown_error",
        ),
        (MemoryError(), PDFExtractionState.RESOURCE_LIMIT, "pdf_resource_limit"),
    ],
)
def test_reader_failures_have_distinct_content_free_codes(
    tmp_path,
    error,
    expected_state,
    expected_code,
):
    path = _source(tmp_path)

    result = extract_pdf(
        path,
        max_file_bytes=10_000,
        max_pages=100,
        max_characters=10_000,
        reader_factory=lambda _source_file: (_ for _ in ()).throw(error),
    )

    assert result.state == expected_state
    assert result.error_code == expected_code
    assert "private" not in result.error_summary
    assert result.pages == ()
    assert result.content_sha256_before == result.content_sha256_after


def test_git_lfs_pointer_is_rejected_before_reader_creation(tmp_path):
    pointer = (
        b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"a" * 64 + b"\nsize 123456\n"
    )
    path = _source(tmp_path, pointer)
    called = False

    def reader_factory(_source_file):
        nonlocal called
        called = True
        return FakeReader()

    result = extract_pdf(
        path,
        max_file_bytes=10_000,
        max_pages=100,
        max_characters=10_000,
        reader_factory=reader_factory,
    )

    assert result.state == PDFExtractionState.GIT_LFS_POINTER
    assert result.error_code == "git_lfs_pointer"
    assert result.content_sha256_before == result.content_sha256_after
    assert called is False


def test_disappeared_file_after_parsing_has_a_distinct_retryable_code(tmp_path):
    path = _source(tmp_path)

    def reader_factory(_source_file):
        path.unlink()
        return FakeReader((FakePage("staged text must be discarded"),))

    result = extract_pdf(
        path,
        max_file_bytes=10_000,
        max_pages=100,
        max_characters=10_000,
        reader_factory=reader_factory,
    )

    assert result.state == PDFExtractionState.DISAPPEARED
    assert result.error_code == "pdf_disappeared"
    assert result.publishable is False
    assert result.pages == ()
    assert result.content_sha256_before
    assert result.content_sha256_after == ""


def test_permission_error_is_sanitized(monkeypatch, tmp_path):
    path = _source(tmp_path)

    def denied(*_args, **_kwargs):
        raise PermissionError("/private/repository/secret.pdf")

    monkeypatch.setattr(pdf_extractor, "_fingerprint_file", denied)
    result = _extract(path, FakeReader())

    assert result.state == PDFExtractionState.PERMISSION_DENIED
    assert result.error_code == "pdf_permission_denied"
    assert "/private" not in result.error_summary


@pytest.mark.parametrize("limit_kind", ["file", "pages", "characters"])
def test_file_page_and_character_limits_are_enforced_as_resource_errors(tmp_path, limit_kind):
    path = _source(tmp_path)
    file_limit = 10_000
    page_limit = 100
    character_limit = 10_000
    reader = FakeReader((FakePage("five!"), FakePage("second")))
    if limit_kind == "file":
        file_limit = 1
    elif limit_kind == "pages":
        page_limit = 1
    else:
        character_limit = 5

    result = extract_pdf(
        path,
        max_file_bytes=file_limit,
        max_pages=page_limit,
        max_characters=character_limit,
        reader_factory=lambda _source_file: reader,
    )

    assert result.state == PDFExtractionState.RESOURCE_LIMIT
    assert result.error_code == "pdf_resource_limit"
    assert result.publishable is False
    assert result.pages == ()


def test_changed_file_after_parsing_discards_staging_rows(tmp_path):
    path = _source(tmp_path)

    def reader_factory(_source_file):
        path.write_bytes(b"%PDF-1.7\nchanged")
        return FakeReader((FakePage("text from an obsolete revision"),))

    result = extract_pdf(
        path,
        max_file_bytes=10_000,
        max_pages=100,
        max_characters=10_000,
        reader_factory=reader_factory,
    )

    assert result.state == PDFExtractionState.CHANGED
    assert result.error_code == "pdf_changed_during_extraction"
    assert result.publishable is False
    assert result.pages == ()
    assert result.content_sha256_before != result.content_sha256_after


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("max_file_bytes", 0),
        ("max_pages", -1),
        ("max_characters", True),
    ],
)
def test_invalid_caller_limits_are_rejected_before_work(tmp_path, name, value):
    limits = {
        "max_file_bytes": 10_000,
        "max_pages": 100,
        "max_characters": 10_000,
    }
    limits[name] = value

    with pytest.raises(ValueError, match=f"{name} must be a positive integer"):
        extract_pdf(
            _source(tmp_path),
            **limits,
            reader_factory=lambda _source_file: FakeReader(),
        )


def test_json_request_boundary_returns_staging_payload_without_echoing_the_path(tmp_path):
    path = _source(tmp_path)
    payload = extract_pdf_request(
        {
            "path": str(path),
            "max_file_bytes": 10_000,
            "max_pages": 100,
            "max_characters": 10_000,
        },
        reader_factory=lambda _source_file: FakeReader((FakePage("searchable text"),)),
    )

    assert payload["state"] == "ready"
    assert payload["publishable"] is True
    assert payload["pages"][0]["page_number"] == 1
    assert payload["pages"][0]["text"] == "searchable text"
    assert str(path) not in str(payload)


def test_json_request_boundary_rejects_missing_or_extra_control_fields(tmp_path):
    path = _source(tmp_path)
    base = {
        "path": str(path),
        "max_file_bytes": 10_000,
        "max_pages": 100,
        "max_characters": 10_000,
    }

    with pytest.raises(ValueError, match="unsupported or missing"):
        extract_pdf_request({key: value for key, value in base.items() if key != "max_pages"})
    with pytest.raises(ValueError, match="unsupported or missing"):
        extract_pdf_request({**base, "private_option": "not allowed"})
