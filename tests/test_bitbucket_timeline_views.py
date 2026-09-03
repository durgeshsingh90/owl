from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from bitbucket_search import views
from bitbucket_search.models import (
    BitbucketRepository,
    GitCommit,
    PDFDocument,
    PDFDocumentAddedEvidence,
    PDFDocumentTimelineBasis,
    RepositorySyncState,
)
from bitbucket_search.services.document_actions import DocumentActionError

pytestmark = pytest.mark.django_db


def _repository(name: str = "networking") -> BitbucketRepository:
    return BitbucketRepository.objects.create(
        display_name=name,
        canonical_remote_key=f"bitbucket.org/cloud-team/{name}",
        remote_url=f"ssh://git@bitbucket.org/cloud-team/{name}.git",
        sync_state=RepositorySyncState.READY,
    )


def test_inventory_pages_contain_at_most_five_hundred_pdfs_without_auto_loading(client):
    repository = _repository()
    PDFDocument.objects.bulk_create(
        [
            PDFDocument(
                repository=repository,
                filename=f"Plan {number}.pdf",
                relative_path=f"docs/Plan {number}.pdf",
            )
            for number in range(501)
        ]
    )
    first = client.get(reverse("bitbucket_search:index"))
    first_html = first.content.decode()
    assert first_html.count("data-pdf-row") == 500
    assert "500 PDFs per page" in first_html
    assert "data-load-older" not in first_html
    assert "data-pdf-visible-end>500</b> of 501 PDFs" in first_html
    next_url = reverse("bitbucket_search:document_page") + "?page=2"
    assert f'href="{next_url}" rel="next"' in first_html
    second = client.get(next_url)
    second_html = second.content.decode()
    assert second_html.count("data-pdf-row") == 1
    assert "data-pdf-visible-start>501</b>" in second_html
    assert 'rel="prev"' in second_html
    first_ids = {document.pk for document in first.context["pdf_page"]}
    second_ids = {document.pk for document in second.context["pdf_page"]}
    assert not first_ids.intersection(second_ids)
    assert len(first_ids | second_ids) == 501


@pytest.mark.parametrize(
    ("value", "expected_key", "expected_label"),
    (
        (date(2026, 8, 30), "future", "Future Git date"),
        (date(2026, 8, 29), "today", "Today"),
        (date(2026, 8, 28), "yesterday", "Yesterday"),
        (date(2026, 8, 27), "day-before-yesterday", "Day Before Yesterday"),
        (date(2026, 8, 26), "week", "This Week"),
        (date(2026, 8, 24), "week", "This Week"),
        (date(2026, 8, 23), "last-week", "Last Week"),
        (date(2026, 8, 17), "last-week", "Last Week"),
        (date(2026, 8, 16), "month", "This Month"),
        (date(2026, 8, 1), "month", "This Month"),
        (date(2026, 7, 31), "last-month", "Last Month"),
        (date(2026, 7, 1), "last-month", "Last Month"),
        (date(2026, 6, 30), "three-months", "Last 3 Months"),
        (date(2026, 5, 29), "three-months", "Last 3 Months"),
        (date(2026, 5, 28), "six-months", "Last 6 Months"),
        (date(2026, 2, 28), "six-months", "Last 6 Months"),
        (date(2026, 2, 27), "year", "This Year"),
        (date(2026, 1, 1), "year", "This Year"),
        (date(2025, 12, 31), "last-year", "Last Year"),
        (date(2025, 1, 1), "last-year", "Last Year"),
        (date(2024, 12, 31), "last-two-years", "Last 2 Years"),
        (date(2024, 8, 29), "last-two-years", "Last 2 Years"),
        (date(2024, 8, 28), "year-2024", "2024"),
    ),
)
def test_timeline_buckets_are_exclusive_and_newest_first(value, expected_key, expected_label):
    key, label, _detail = views._timeline_bucket(value, today=date(2026, 8, 29))

    assert key == expected_key
    assert label == expected_label


@override_settings(TIME_ZONE="Europe/Dublin")
def test_timeline_groups_git_timestamp_by_owl_local_date(monkeypatch):
    repository = _repository()
    committed_at = datetime(2026, 8, 28, 23, 30, tzinfo=UTC)
    commit = GitCommit.objects.create(
        repository=repository,
        commit_hash="f" * 40,
        authored_at=committed_at,
        committed_at=committed_at,
    )
    document = PDFDocument.objects.create(
        repository=repository,
        filename="Local date.pdf",
        relative_path="Local date.pdf",
        added_evidence=PDFDocumentAddedEvidence.CONFIRMED,
        added_commit=commit,
    )
    monkeypatch.setattr(views.timezone, "localdate", lambda: date(2026, 8, 29))

    with timezone.override("Europe/Dublin"):
        page, groups = views._pdf_timeline_page(1)

    assert [item.pk for item in page] == [document.pk]
    assert [group.key for group in groups] == ["today"]


def test_timeline_row_labels_author_without_claiming_push_or_project_evidence(
    tmp_path,
    settings,
):
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "managed-repositories"
    repository = _repository()
    repository.history_is_shallow = False
    repository.save(update_fields=("history_is_shallow", "updated_at"))
    committed_at = datetime(2026, 8, 20, 9, 15, tzinfo=UTC)
    commit = GitCommit.objects.create(
        repository=repository,
        commit_hash="a" * 40,
        author_name="A. Architect",
        committer_name="C. Committer",
        authored_at=committed_at - timedelta(minutes=5),
        committed_at=committed_at,
    )
    document = PDFDocument.objects.create(
        repository=repository,
        filename="Architecture.pdf",
        relative_path="docs/Architecture.pdf",
        added_evidence=PDFDocumentAddedEvidence.CONFIRMED,
        added_commit=commit,
        timeline_at=committed_at,
        timeline_basis=PDFDocumentTimelineBasis.GIT_ADDED,
    )

    row = views._timeline_row(document)

    assert row.added_by_label == "A. Architect"
    assert row.commit_hash == "a" * 40
    assert row.short_commit_hash == "a" * 12
    assert row.commit_copy_available is True
    assert row.commit_detail == "Original Git addition commit"
    assert row.added_date_label == "20 Aug 2026"
    assert row.added_date_source_label == "Git addition"
    assert "Original Git addition" in row.added_date_detail
    assert row.project_label == ""
    assert row.history_label == "Full reachable history"
    assert "Pushed by" not in repr(row)
    assert row.full_path.endswith(f"/{repository.pk}-networking/docs/Architecture.pdf")
    assert row.display_path == "networking/docs/Architecture.pdf"
    assert row.path_copy_available is True


@override_settings(TIME_ZONE="Europe/Dublin")
def test_unknown_git_addition_never_displays_owl_discovery_as_a_git_date(tmp_path, settings):
    settings.BITBUCKET_REPOSITORIES_ROOT = tmp_path / "managed-repositories"
    repository = _repository()
    discovered_at = datetime(2026, 8, 22, 9, 15, tzinfo=UTC)
    document = PDFDocument.objects.create(
        repository=repository,
        filename="Legacy.pdf",
        relative_path="legacy/Legacy.pdf",
        discovered_at=discovered_at,
        timeline_at=discovered_at,
        added_evidence=PDFDocumentAddedEvidence.BEFORE_AVAILABLE_HISTORY,
        timeline_basis=PDFDocumentTimelineBasis.OWL_DISCOVERED,
    )

    row = views._timeline_row(document)

    assert row.added_by_label == "Unavailable in available Git history"
    assert row.commit_hash == ""
    assert row.short_commit_hash == ""
    assert row.commit_copy_available is False
    assert row.commit_detail == ""
    assert row.added_date_label == "Unavailable"
    assert row.added_date_source_label == "Git date unavailable"
    assert "No Git commit timestamp is available" in row.added_date_detail
    assert "22 Aug 2026" not in row.added_date_detail
    assert row.history_label == "Available history · Original Git-added date unavailable"


@pytest.mark.parametrize("search", [False, True])
@override_settings(TIME_ZONE="Europe/Dublin")
def test_list_and_search_show_original_repo_addition_not_discovery_or_latest_change(client, search):
    repository = _repository()
    addition = GitCommit.objects.create(
        repository=repository,
        commit_hash="b" * 40,
        author_name="Original Author",
        committer_name="Original Committer",
        authored_at=datetime(2023, 1, 1, 12, tzinfo=UTC),
        committed_at=datetime(2024, 7, 2, 23, 30, tzinfo=UTC),
    )
    latest = GitCommit.objects.create(
        repository=repository,
        commit_hash="c" * 40,
        author_name="Later Editor",
        committer_name="Later Committer",
        authored_at=datetime(2026, 8, 29, 12, tzinfo=UTC),
        committed_at=datetime(2026, 8, 29, 12, tzinfo=UTC),
    )
    document = PDFDocument.objects.create(
        repository=repository,
        filename="Original Guide.pdf",
        relative_path="docs/Original Guide.pdf",
        added_evidence=PDFDocumentAddedEvidence.CONFIRMED,
        added_commit=addition,
        last_commit=latest,
        discovered_at=datetime(2026, 8, 30, 12, tzinfo=UTC),
        # Legacy cached timeline values must not override real Git evidence.
        timeline_at=datetime(2026, 8, 30, 12, tzinfo=UTC),
        timeline_basis=PDFDocumentTimelineBasis.OWL_DISCOVERED,
    )

    response = client.get(reverse("bitbucket_search:index"), {"q": "Original"} if search else {})
    html = response.content.decode()
    row_start = html.index(f'data-document-id="{document.pk}"')
    row_html = html[row_start : html.index("</tr>", row_start)]

    assert response.status_code == 200
    assert 'id="bb-pdf-column-added">Date added to repo</th>' in html
    assert ">Date added to repo</span>" in row_html
    assert "3 Jul 2024" in row_html  # Git commit timestamp, converted to local time.
    assert "00:30" in row_html
    assert "Git addition" in row_html
    assert "Original Author" in row_html
    assert f'data-commit-id="{"b" * 40}"' in row_html
    assert "<code>bbbbbbbbbbbb</code>" in row_html
    assert f'data-commit-id="{"c" * 40}"' not in row_html
    assert "Git author" not in row_html
    assert "2023" not in row_html
    assert "2026" not in row_html
    assert "OWL discovery" not in html
    assert ">Date added</" not in html


@pytest.mark.parametrize("search", [False, True])
@pytest.mark.parametrize(
    ("evidence", "with_commit"),
    [
        (PDFDocumentAddedEvidence.NOT_FOUND, False),
        (PDFDocumentAddedEvidence.BEFORE_AVAILABLE_HISTORY, False),
        (PDFDocumentAddedEvidence.CONFIRMED, False),
        (PDFDocumentAddedEvidence.NOT_FOUND, True),
    ],
)
@override_settings(TIME_ZONE="Europe/Dublin")
def test_unknown_addition_uses_latest_available_git_commit_and_never_owl_discovery(
    client, search, evidence, with_commit
):
    repository = _repository()
    commit = None
    if with_commit:
        commit = GitCommit.objects.create(
            repository=repository,
            commit_hash="d" * 40,
            authored_at=datetime(2024, 1, 1, tzinfo=UTC),
            committed_at=datetime(2024, 1, 2, tzinfo=UTC),
        )
    document = PDFDocument.objects.create(
        repository=repository,
        filename="Legacy Guide.pdf",
        relative_path="Legacy Guide.pdf",
        added_evidence=evidence,
        last_commit=commit,
        discovered_at=datetime(2026, 8, 30, tzinfo=UTC),
        timeline_at=datetime(2026, 8, 30, tzinfo=UTC),
    )
    response = client.get(reverse("bitbucket_search:index"), {"q": "Legacy"} if search else {})
    html = response.content.decode()
    row_start = html.index(f'data-document-id="{document.pk}"')
    row_html = html[row_start : html.index("</tr>", row_start)]
    assert "30 Aug 2026" not in row_html
    assert "First seen by OWL" not in row_html
    if with_commit:
        assert "2 Jan 2024" in row_html
        assert "Git commit" in row_html
        assert f'data-commit-id="{"d" * 40}"' in row_html
        assert "<code>dddddddddddd</code>" in row_html
    else:
        assert "Git date unavailable" in row_html
        assert "Unavailable" in row_html
        assert "data-copy-commit-id" not in row_html
    if not search:
        expected_key, _label, _detail = views._timeline_bucket(
            timezone.localtime(commit.committed_at if commit else document.discovered_at).date(),
            today=timezone.localdate(),
        )
        assert f'data-timeline-group-key="{expected_key}"' in html
        assert 'data-timeline-group-key="repo-date-unavailable"' not in html


@override_settings(BITBUCKET_PDF_PAGE_SIZE=10)
def test_timeline_orders_and_groups_by_the_displayed_date_source(monkeypatch):
    repository = _repository()
    monkeypatch.setattr(views.timezone, "localdate", lambda: date(2026, 8, 30))
    dated_ids = []
    for number in range(10):
        added_at = datetime(2024, 7, number + 1, 12, tzinfo=UTC)
        commit = GitCommit.objects.create(
            repository=repository,
            commit_hash=f"{number + 1:040x}",
            authored_at=added_at,
            committed_at=added_at,
        )
        document = PDFDocument.objects.create(
            repository=repository,
            filename=f"Dated-{number}.pdf",
            relative_path=f"Dated-{number}.pdf",
            added_commit=commit,
            added_evidence=PDFDocumentAddedEvidence.CONFIRMED,
            # Deliberately opposite ordering in the obsolete cache.
            timeline_at=datetime(2026, 8, 30 - number, 12, tzinfo=UTC),
        )
        dated_ids.append(document.pk)
    unknown = PDFDocument.objects.create(
        repository=repository,
        filename="Unknown.pdf",
        relative_path="Unknown.pdf",
        discovered_at=datetime(2026, 8, 30, 12, tzinfo=UTC),
        timeline_at=datetime(2026, 8, 30, 23, tzinfo=UTC),
    )

    page, groups = views._pdf_timeline_page(1)
    assert [document.pk for document in page] == [unknown.pk, *list(reversed(dated_ids[1:]))]
    assert [group.key for group in groups] == ["today", "year-2024"]
    page, groups = views._pdf_timeline_page(2)
    assert [document.pk for document in page] == [dated_ids[0]]
    assert [group.key for group in groups] == ["year-2024"]


@override_settings(BITBUCKET_PDF_PAGE_SIZE=10)
def test_document_page_returns_json_and_html_with_stable_discovery_date_pagination(client):
    repository = _repository()
    observed_at = timezone.now()
    for index in range(11):
        PDFDocument.objects.create(
            repository=repository,
            filename=f"Document-{index:02d}.pdf",
            relative_path=f"docs/Document-{index:02d}.pdf",
            timeline_at=observed_at - timedelta(minutes=index),
        )

    url = f"{reverse('bitbucket_search:document_page')}?page=2"
    response = client.get(
        url,
        REMOTE_ADDR="127.0.0.1",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["page"] == 2
    assert payload["nextPageUrl"] == ""
    assert payload["html"].count("data-pdf-row") == 1
    assert "Document-00.pdf" in payload["html"]
    assert "Git date unavailable" in payload["html"]
    assert 'data-timeline-group-key="repo-date-unavailable"' not in payload["html"]
    assert 'name="return_page" value="2"' in payload["html"]

    fallback = client.get(url, REMOTE_ADDR="127.0.0.1")
    assert fallback.status_code == 200
    assert "Document-00.pdf" in fallback.content.decode()


@override_settings(OWL_ALLOW_NON_LOOPBACK=True)
def test_native_document_endpoints_remain_strictly_loopback(client):
    open_response = client.post(
        reverse("bitbucket_search:document_open", kwargs={"document_id": 1}),
        REMOTE_ADDR="198.51.100.12",
    )
    reveal_response = client.post(
        reverse("bitbucket_search:document_reveal", kwargs={"document_id": 1}),
        REMOTE_ADDR="198.51.100.12",
    )

    assert open_response.status_code == 403
    assert reveal_response.status_code == 403


def test_native_document_endpoints_return_safe_async_results(client, monkeypatch):
    repository = _repository()
    document = PDFDocument.objects.create(
        repository=repository,
        filename="Guide.pdf",
        relative_path="docs/Guide.pdf",
        open_count=3,
        last_opened_at=timezone.now(),
    )
    opened = Mock(return_value=document)
    revealed = Mock(return_value=document)
    monkeypatch.setattr(views, "open_registered_pdf", opened)
    monkeypatch.setattr(views, "reveal_registered_pdf", revealed)

    common = {
        "REMOTE_ADDR": "127.0.0.1",
        "HTTP_X_REQUESTED_WITH": "XMLHttpRequest",
    }
    open_response = client.post(
        reverse("bitbucket_search:document_open", kwargs={"document_id": document.pk}),
        **common,
    )
    reveal_response = client.post(
        reverse("bitbucket_search:document_reveal", kwargs={"document_id": document.pk}),
        **common,
    )

    assert open_response.status_code == 200
    assert open_response.json()["openCount"] == 3
    assert reveal_response.status_code == 200
    assert reveal_response.json()["openCount"] == 3
    opened.assert_called_once_with(document.pk)
    revealed.assert_called_once_with(document.pk)

    fallback = client.post(
        reverse("bitbucket_search:document_open", kwargs={"document_id": document.pk}),
        {"return_page": "2"},
        REMOTE_ADDR="127.0.0.1",
    )
    assert fallback.status_code == 302
    assert fallback.url == (
        f"{reverse('bitbucket_search:document_page')}?page=2#pdf-document-{document.pk}"
    )


def test_document_action_failure_never_echoes_a_local_path(client, monkeypatch):
    monkeypatch.setattr(
        views,
        "open_registered_pdf",
        Mock(
            side_effect=DocumentActionError(
                "document_unavailable",
                "This PDF is missing from the managed repository.",
            )
        ),
    )

    response = client.post(
        reverse("bitbucket_search:document_open", kwargs={"document_id": 404}),
        REMOTE_ADDR="127.0.0.1",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 409
    assert response.json() == {
        "state": "failed",
        "code": "document_unavailable",
        "detail": "This PDF is missing from the managed repository.",
    }


def test_document_routes_never_accept_a_browser_supplied_filesystem_path(client):
    response = client.post(
        "/pdfs/documents/%2Ftmp%2Fsecret.pdf/open/",
        REMOTE_ADDR="127.0.0.1",
    )

    assert response.status_code == 404
