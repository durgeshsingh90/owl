from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.urls import reverse

from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFIndexState,
    PDFPageExtractionState,
    PDFTextPage,
    PDFTextRevision,
    PDFTextRevisionState,
)

pytestmark = pytest.mark.django_db


def _repository(name: str) -> BitbucketRepository:
    return BitbucketRepository.objects.create(
        display_name=name,
        canonical_remote_key=f"bitbucket.org/team/{name.casefold()}",
        remote_url=f"ssh://git@bitbucket.org/team/{name.casefold()}.git",
    )


def _indexed_pdf(
    repository: BitbucketRepository,
    *,
    filename: str,
    page_text: str,
    digest: str,
) -> PDFDocument:
    revision = PDFTextRevision.objects.create(
        content_sha256=digest * 64,
        extractor_version="view-test-v1",
        source_byte_size=len(page_text.encode()),
        state=PDFTextRevisionState.READY,
        page_count=1,
        extracted_character_count=len(page_text),
    )
    PDFTextPage.objects.create(
        revision=revision,
        page_number=1,
        extracted_text=page_text,
        character_count=len(page_text),
        extraction_state=PDFPageExtractionState.READY,
    )
    return PDFDocument.objects.create(
        repository=repository,
        filename=filename,
        relative_path=f"docs/{filename}",
        indexed_revision=revision,
        indexed_git_blob_id=digest * 40,
        index_state=PDFIndexState.READY,
        page_count=1,
        extracted_character_count=len(page_text),
    )


def test_q_fallback_renders_a_chip_explanation_escaped_snippet_and_search_return(client):
    repository = _repository("Security")
    document = _indexed_pdf(
        repository,
        filename="Controls.pdf",
        page_text="before <script>alert(1)</script> private link after",
        digest="a",
    )

    response = client.get(reverse("bitbucket_search:index"), {"q": "private link"})
    html = response.content.decode()

    assert response.status_code == 200
    assert 'name="chip" value="private link"' in html
    assert "data-pdf-search-input" in html
    assert f'data-document-id="{document.pk}"' in html
    assert "PDF content" in html
    assert "Page 1" in html
    assert "<mark>private link</mark>" in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert 'name="return_to" value="/pdfs/?q=private+link"' in html
    assert "data-pdf-timeline" not in html


def test_repository_and_index_filters_are_search_mode_without_phrase_chips(client):
    selected_repository = _repository("Selected")
    other_repository = _repository("Other")
    selected = _indexed_pdf(
        selected_repository,
        filename="Selected.pdf",
        page_text="selected content",
        digest="b",
    )
    other = _indexed_pdf(
        other_repository,
        filename="Other.pdf",
        page_text="other content",
        digest="c",
    )

    response = client.get(
        reverse("bitbucket_search:index"),
        {
            "repository": selected_repository.pk,
            "index_state": PDFIndexState.READY,
            "sort": "filename_descending",
        },
    )
    html = response.content.decode()

    assert response.status_code == 200
    assert f'data-document-id="{selected.pk}"' in html
    assert f'data-document-id="{other.pk}"' not in html
    assert "Browsing PDFs with the selected repository" in html
    assert 'value="filename_descending" selected' in html
    assert "data-pdf-timeline" not in html


@pytest.mark.parametrize("legacy_page_size", [None, 200])
def test_search_html_paginates_at_one_hundred_and_preserves_canonical_query(
    client, legacy_page_size
):
    repository = _repository("Guides")
    PDFDocument.objects.bulk_create(
        [
            PDFDocument(
                repository=repository,
                filename=f"Guide {number:02d}.pdf",
                relative_path=f"docs/Guide {number:02d}.pdf",
            )
            for number in range(101)
        ]
    )

    query = {"q": "Guide"}
    if legacy_page_size is not None:
        query["page_size"] = legacy_page_size
    response = client.get(reverse("bitbucket_search:index"), query)
    html = response.content.decode()

    assert response.status_code == 200
    assert html.count("data-pdf-row") == 100
    assert "1–100 of 101 matching PDFs" in html
    assert re.search(r"\(\d+\.\d{2} seconds\)", html)
    assert "up to 100 results per page" in html
    assert "chip=Guide" in html
    assert "page=2" in html
    assert "page_size=100" in html
    assert "Copy page paths (100)" in html
    assert "Open page PDFs (100)" in html
    assert "Open 100 PDFs?" in html
    assert 'data-confirm-threshold="10"' in html

    second_page = client.get(
        reverse("bitbucket_search:index"),
        {"q": "Guide", "page": 2},
    )
    second_page_html = second_page.content.decode()

    assert second_page.status_code == 200
    assert second_page_html.count("data-pdf-row") == 1
    assert "Copy page paths (1)" in second_page_html
    assert "Open page PDFs (1)" in second_page_html
    assert "data-open-all-confirmation" not in second_page_html


def test_repository_filtered_search_honors_configured_page_size(client, settings):
    settings.BITBUCKET_SEARCH_PAGE_SIZE = 50
    repository = _repository("Filtered")
    PDFDocument.objects.bulk_create(
        [
            PDFDocument(
                repository=repository,
                filename=f"Guide {number}.pdf",
                relative_path=f"docs/Guide {number}.pdf",
            )
            for number in range(51)
        ]
    )
    response = client.get(
        reverse("bitbucket_search:index"), {"repository": repository.pk, "page_size": 200}
    )
    html = response.content.decode()
    assert html.count("data-pdf-row") == 50
    assert "1–50 of 51 matching PDFs" in html
    assert 'name="page_size" value="50"' in html
    assert "page_size=50" in html


def test_search_bulk_actions_include_only_the_rendered_result_page(client):
    repository = _repository("Bulk actions")
    first = _indexed_pdf(
        repository,
        filename="Shared Architecture.pdf",
        page_text="shared search phrase one",
        digest="e",
    )
    second = _indexed_pdf(
        repository,
        filename="Shared Controls.pdf",
        page_text="shared search phrase two",
        digest="f",
    )

    response = client.get(reverse("bitbucket_search:index"), {"q": "shared"})
    html = response.content.decode()

    assert response.status_code == 200
    assert "Copy page paths (2)" in html
    assert "Open page PDFs (2)" in html
    assert "data-copy-search-result-paths" in html
    assert 'role="status" aria-live="polite"' in html
    assert html.count("data-search-result-row") == 2
    assert html.count("data-pdf-local-path") == 2

    form_start = html.index("data-open-search-results-form")
    form_end = html.index("</form>", form_start)
    form_html = html[form_start:form_end]
    assert f'action="{reverse("bitbucket_search:documents_open_all")}"' in html[:form_end]
    assert 'name="return_to" value="/pdfs/?q=shared"' in form_html
    assert form_html.count('name="document_id"') == 2
    assert f'name="document_id" value="{first.pk}"' in form_html
    assert f'name="document_id" value="{second.pk}"' in form_html
    assert 'name="csrfmiddlewaretoken"' in form_html
    assert 'name="confirmed" value="0"' in form_html
    assert "data-open-all-confirmation" not in html

    inventory_html = client.get(reverse("bitbucket_search:index")).content.decode()
    empty_search_html = client.get(
        reverse("bitbucket_search:index"),
        {"q": "does not match anything"},
    ).content.decode()
    for inactive_html in (inventory_html, empty_search_html):
        assert "data-copy-search-result-paths" not in inactive_html
        assert "data-open-search-results-form" not in inactive_html


def test_search_bulk_copy_uses_visible_result_paths_and_clipboard_fallback():
    javascript = (
        Path(__file__).parents[1] / "static" / "bitbucket_search" / "bitbucket_search.js"
    ).read_text(encoding="utf-8")

    assert '"[data-search-result-row]:not([hidden]) [data-pdf-local-path]"' in javascript
    assert "navigator.clipboard?.writeText" in javascript
    assert 'paths.join("\\n")' in javascript
    assert 'document.createElement("textarea")' in javascript
    assert 'document.execCommand("copy")' in javascript
    assert "PDF paths could not be copied" in javascript


def test_commit_copy_uses_full_id_and_accessible_feedback():
    javascript = (
        Path(__file__).parents[1] / "static" / "bitbucket_search" / "bitbucket_search.js"
    ).read_text(encoding="utf-8")

    assert 'event.target.closest?.("[data-copy-commit-id]")' in javascript
    assert "button.dataset.commitId" in javascript
    assert "await copyText(commitId)" in javascript
    assert 'button.title = "Full commit ID copied"' in javascript
    assert 'status.textContent = "Copied"' in javascript
    assert 'status.textContent = "Copy failed"' in javascript
    assert "pendingCommitCopies" in javascript


def test_large_search_page_requires_accessible_open_all_confirmation():
    javascript = (
        Path(__file__).parents[1] / "static" / "bitbucket_search" / "bitbucket_search.js"
    ).read_text(encoding="utf-8")

    assert 'openSearchResultsForm.addEventListener("submit"' in javascript
    assert "openAllConfirmation.showModal()" in javascript
    assert "window.confirm(" in javascript
    assert 'confirmedInput.value = "1"' in javascript


def test_invalid_search_state_is_explained_without_disabling_search(client):
    response = client.get(
        reverse("bitbucket_search:index"),
        {"q": "needle", "scope": "not-a-scope"},
    )
    html = response.content.decode()

    assert response.status_code == 200
    assert "Unknown PDF search scope" in html
    assert "data-pdf-search-input" in html
    assert "data-pdf-search-input disabled" not in html


def test_search_input_is_full_width_enter_first_and_submits_scope_sentinel(client):
    response = client.get(reverse("bitbucket_search:index"), {"q": "needle"})
    html = response.content.decode()

    assert '<div class="bb-main-search">' in html
    assert '<label class="bb-main-search">' not in html
    assert 'for="bb-pdf-search-input"' in html
    assert 'id="bb-pdf-search-input"' in html
    assert 'name="scope_present" value="1"' in html
    assert 'name="page_size" value="100"' in html
    assert 'placeholder="Search extracted text, filenames or repo paths…"' in html
    for scope in ("content", "filename", "path"):
        assert f'name="scope" value="{scope}" checked' in html
    assert "bb-search-submit" not in html

    stylesheet = (
        Path(__file__).parents[1] / "static" / "bitbucket_search" / "bitbucket_search.css"
    ).read_text(encoding="utf-8")
    assert "grid-template-columns: minmax(0, 1fr) auto;" in stylesheet
    assert "minmax(0, 670px)" not in stylesheet
    assert ".bb-search-submit" not in stylesheet


def test_wide_workspace_uses_available_space_for_long_names():
    stylesheet = (
        Path(__file__).parents[1] / "static" / "bitbucket_search" / "bitbucket_search.css"
    ).read_text(encoding="utf-8")

    content_rule = re.search(r"\.bb-content\s*\{(?P<body>[^}]*)\}", stylesheet)
    assert content_rule is not None
    assert "width: 100%;" in content_rule.group("body")
    assert "margin: 0;" in content_rule.group("body")
    assert "1600px" not in content_rule.group("body")

    desktop_rule_start = stylesheet.index("@media (min-width: 1800px)")
    desktop_rule_end = stylesheet.index("@media (min-width: 2200px)", desktop_rule_start)
    desktop_rule = stylesheet[desktop_rule_start:desktop_rule_end]
    assert ".bb-results-table .bb-column-name { width: 23%; }" in desktop_rule
    assert ".bb-results-table .bb-column-path { width: 11%; }" in desktop_rule
    assert ".bb-results-table .bb-column-actions { width: 10%; }" in desktop_rule
    assert ".bb-document-name__icon {\n        display: none;" in desktop_rule

    ultra_wide_rule_start = desktop_rule_end
    ultra_wide_rule_end = stylesheet.index("@media (max-width: 1280px)", ultra_wide_rule_start)
    ultra_wide_rule = stylesheet[ultra_wide_rule_start:ultra_wide_rule_end]
    assert "clamp(314px, 14vw, 420px)" in ultra_wide_rule
    assert ".bb-results-table .bb-column-name { width: 20%; }" in ultra_wide_rule


@pytest.mark.parametrize(
    "query",
    [
        {"q": "aurora"},
        {"chip": ["aurora"], "scope_present": "1", "scope": ["content", "filename", "path"]},
    ],
)
def test_search_form_finds_saved_text_filename_and_repo_path_together(client, query):
    repository = _repository("Search sources")
    content_match = _indexed_pdf(
        repository, filename="Overview.pdf", page_text="Aurora deployment notes", digest="a"
    )
    filename_match = _indexed_pdf(
        repository, filename="Aurora Guide.pdf", page_text="Unrelated body", digest="b"
    )
    path_match = _indexed_pdf(
        repository, filename="Design.pdf", page_text="Unrelated description", digest="c"
    )
    path_match.relative_path = "services/aurora/Design.pdf"
    path_match.save(update_fields=("relative_path",))
    other = _indexed_pdf(repository, filename="Other.pdf", page_text="No matching term", digest="d")

    response = client.get(reverse("bitbucket_search:index"), query)
    html = response.content.decode()

    assert response.status_code == 200
    assert response.context["search_page"].total == 3
    for document in (content_match, filename_match, path_match):
        assert f'data-document-id="{document.pk}"' in html
    assert f'data-document-id="{other.pk}"' not in html


def test_empty_input_backspace_removes_the_last_chip_and_submits():
    javascript = (
        Path(__file__).parents[1] / "static" / "bitbucket_search" / "bitbucket_search.js"
    ).read_text(encoding="utf-8")

    assert 'event.key === "Backspace"' in javascript
    assert 'event.key !== "Enter"' in javascript
    assert '"[data-pdf-search-chip]:last-of-type"' in javascript
    assert "lastChip.remove();" in javascript
    assert "searchForm.submit();" in javascript


def test_repository_status_includes_extraction_counts(client):
    repository = _repository("Index status")
    _indexed_pdf(
        repository,
        filename="Indexed.pdf",
        page_text="published",
        digest="d",
    )

    response = client.get(
        reverse("bitbucket_search:repository_status"),
        REMOTE_ADDR="127.0.0.1",
    )

    assert response.status_code == 200
    assert response.json()["extraction"] == {
        "queuedJobs": 0,
        "runningJobs": 0,
        "failedJobs": 0,
        "interruptedJobs": 0,
        "pendingDocuments": 0,
        "indexedDocuments": 1,
        "staleDocuments": 0,
        "active": False,
        "workerLimit": 4,
        "publicationSignature": "0:1:0:0:0",
    }
