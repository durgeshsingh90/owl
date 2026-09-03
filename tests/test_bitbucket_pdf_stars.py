from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import OperationalError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from bitbucket_search import views as bitbucket_views
from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFDocumentAddedEvidence,
    PDFDocumentLifecycle,
    PDFTextRevision,
    PDFTextRevisionState,
)
from bitbucket_search.services import pdf_search, pdf_stars
from bitbucket_search.services.pdf_catalog import (
    CatalogBuild,
    CatalogPDF,
    publish_repository_pdf_catalog,
)
from bitbucket_search.services.pdf_search import search_documents
from bitbucket_search.services.pdf_search_query import (
    PDFSearchQuery,
    PDFSearchScope,
    PDFSearchSort,
)
from bitbucket_search.services.pdf_stars import (
    PDFStarConflictError,
    PDFStarNotFoundError,
    PDFStarValidationError,
    set_document_star,
)
from semantic_search.services.search import SemanticSearchMatch

pytestmark = pytest.mark.django_db


@pytest.fixture
def loopback_client(client):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    return client


@pytest.fixture
def repository() -> BitbucketRepository:
    return BitbucketRepository.objects.create(
        display_name="Architecture",
        canonical_remote_key="bitbucket.org/workspace/architecture",
        remote_url="ssh://git@bitbucket.org/workspace/architecture.git",
    )


@pytest.fixture
def document(repository) -> PDFDocument:
    return PDFDocument.objects.create(
        repository=repository,
        filename="Network Plan.pdf",
        relative_path="docs/Network Plan.pdf",
    )


def _catalog(*, path: str | None, blob: str = "a" * 40) -> CatalogBuild:
    documents = ()
    if path is not None:
        documents = (
            CatalogPDF(
                filename=path.rsplit("/", maxsplit=1)[-1],
                relative_path=path,
                file_size=128,
                git_blob_id=blob,
                added_evidence=PDFDocumentAddedEvidence.NOT_FOUND,
                added_commit=None,
                last_commit=None,
            ),
        )
    return CatalogBuild(documents=documents, history_is_shallow=False)


def _searchable_document(
    repository: BitbucketRepository,
    *,
    filename: str,
    marker: str,
    starred: bool,
) -> PDFDocument:
    revision = PDFTextRevision.objects.create(
        content_sha256=marker * 64,
        extractor_version="pdf-star-test-v1",
        source_byte_size=0,
        state=PDFTextRevisionState.READY,
        page_count=0,
        extracted_character_count=0,
    )
    return PDFDocument.objects.create(
        repository=repository,
        filename=filename,
        relative_path=f"docs/{filename}",
        indexed_revision=revision,
        index_state="ready",
        starred=starred,
    )


def test_pdf_document_star_defaults_off_and_is_indexed(document):
    assert document.starred is False
    assert PDFDocument._meta.get_field("starred").db_index is True


def test_set_document_star_is_idempotent_and_allows_removed_documents(document):
    starred = set_document_star(document.pk, starred=True)
    repeated_star = set_document_star(document.pk, starred=True)

    assert starred == repeated_star
    assert starred.document_id == document.pk
    assert starred.filename == "Network Plan.pdf"
    assert starred.starred is True
    document.refresh_from_db()
    assert document.starred is True

    PDFDocument.objects.filter(pk=document.pk).update(
        lifecycle_state=PDFDocumentLifecycle.REMOVED,
        removed_at=timezone.now(),
    )
    unstarred = set_document_star(document.pk, starred=False)
    repeated_unstar = set_document_star(document.pk, starred=False)

    assert unstarred == repeated_unstar
    assert unstarred.starred is False
    document.refresh_from_db()
    assert document.lifecycle_state == PDFDocumentLifecycle.REMOVED
    assert document.starred is False


@pytest.mark.parametrize("document_id", (None, True, 0, -1, "1"))
def test_set_document_star_rejects_invalid_or_missing_documents(document_id):
    with pytest.raises(PDFStarNotFoundError, match="not registered"):
        set_document_star(document_id, starred=True)


def test_set_document_star_requires_boolean_state_and_translates_database_conflicts(
    document,
    monkeypatch,
):
    with pytest.raises(PDFStarValidationError, match="must be true or false"):
        set_document_star(document.pk, starred="true")

    def raise_conflict(*_args, **_kwargs):
        raise OperationalError("synthetic database conflict")

    monkeypatch.setattr(pdf_stars, "_set_document_star", raise_conflict)
    with pytest.raises(PDFStarConflictError, match="PDF star is busy"):
        set_document_star(document.pk, starred=True)


def test_pdf_star_survives_refresh_removal_and_same_path_reappearance(repository):
    path = "docs/Durable.pdf"
    first_seen = timezone.now() - timedelta(hours=3)
    publish_repository_pdf_catalog(
        repository,
        _catalog(path=path),
        result_commit="1" * 40,
        observed_at=first_seen,
    )
    document = PDFDocument.objects.get(repository=repository, relative_path=path)
    set_document_star(document.pk, starred=True)

    publish_repository_pdf_catalog(
        repository,
        _catalog(path=path, blob="b" * 40),
        result_commit="2" * 40,
        observed_at=first_seen + timedelta(hours=1),
    )
    document.refresh_from_db()
    assert document.lifecycle_state == PDFDocumentLifecycle.ACTIVE
    assert document.git_blob_id == "b" * 40
    assert document.starred is True

    publish_repository_pdf_catalog(
        repository,
        _catalog(path=None),
        result_commit="3" * 40,
        observed_at=first_seen + timedelta(hours=2),
    )
    document.refresh_from_db()
    assert document.lifecycle_state == PDFDocumentLifecycle.REMOVED
    assert document.starred is True

    publish_repository_pdf_catalog(
        repository,
        _catalog(path=path, blob="c" * 40),
        result_commit="4" * 40,
        observed_at=first_seen + timedelta(hours=3),
    )
    restored = PDFDocument.objects.get(repository=repository, relative_path=path)
    assert restored.pk == document.pk
    assert restored.lifecycle_state == PDFDocumentLifecycle.ACTIVE
    assert restored.removed_at is None
    assert restored.starred is True


def test_repository_deletion_cascades_its_document_star(repository, document):
    set_document_star(document.pk, starred=True)

    repository.delete()

    assert not PDFDocument.objects.filter(pk=document.pk).exists()


def test_starred_first_orders_filter_only_and_exact_fts_results(repository):
    alpha = _searchable_document(
        repository,
        filename="Alpha Shared Plan.pdf",
        marker="a",
        starred=False,
    )
    zulu = _searchable_document(
        repository,
        filename="Zulu Shared Plan.pdf",
        marker="z",
        starred=True,
    )
    bravo = _searchable_document(
        repository,
        filename="Bravo Shared Plan.pdf",
        marker="b",
        starred=True,
    )

    filter_only = search_documents(PDFSearchQuery(sort=PDFSearchSort.STARRED_FIRST))
    exact = search_documents(
        PDFSearchQuery(
            chips=("Shared Plan",),
            scopes=(PDFSearchScope.FILENAME,),
            sort=PDFSearchSort.STARRED_FIRST,
        )
    )

    expected = [bravo.pk, zulu.pk, alpha.pk]
    assert [hit.document.pk for hit in filter_only.results] == expected
    assert [hit.document.pk for hit in exact.results] == expected


def test_starred_first_orders_semantic_fallback_results(repository, monkeypatch):
    unstarred = _searchable_document(
        repository,
        filename="Alpha unrelated.pdf",
        marker="c",
        starred=False,
    )
    higher_ranked_starred = _searchable_document(
        repository,
        filename="Zulu unrelated.pdf",
        marker="d",
        starred=True,
    )
    lower_ranked_starred = _searchable_document(
        repository,
        filename="Bravo unrelated.pdf",
        marker="e",
        starred=True,
    )

    monkeypatch.setattr(
        pdf_search,
        "semantic_search",
        lambda *_args, **_kwargs: (
            SemanticSearchMatch(
                source_id=unstarred.indexed_revision_id,
                score=0.95,
                page_number=None,
                snippet="Unstarred semantic result",
            ),
            SemanticSearchMatch(
                source_id=higher_ranked_starred.indexed_revision_id,
                score=0.8,
                page_number=None,
                snippet="Higher-ranked starred semantic result",
            ),
            SemanticSearchMatch(
                source_id=lower_ranked_starred.indexed_revision_id,
                score=0.7,
                page_number=None,
                snippet="Lower-ranked starred semantic result",
            ),
        ),
    )

    result = search_documents(
        PDFSearchQuery(
            chips=("semantic-only phrase",),
            scopes=(PDFSearchScope.CONTENT,),
            sort=PDFSearchSort.STARRED_FIRST,
        )
    )

    assert result.semantic_fallback_used is True
    assert [hit.document.pk for hit in result.results] == [
        higher_ranked_starred.pk,
        lower_ranked_starred.pk,
        unstarred.pk,
    ]


def test_search_options_expose_starred_first_label(repository):
    options = bitbucket_views._search_filter_options(PDFSearchQuery(), (repository,))

    labels = {item["value"]: item["label"] for item in options["search_sort_options"]}
    assert labels[PDFSearchSort.STARRED_FIRST.value] == "Starred first"


def test_pdf_star_endpoint_returns_json_and_is_idempotent(loopback_client, document):
    path = reverse("bitbucket_search:document_star", args=(document.pk,))

    starred = loopback_client.post(
        path,
        {"starred": "true"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert starred.status_code == 200
    assert starred.json() == {
        "state": "success",
        "label": "PDF updated",
        "detail": "Starred Network Plan.pdf",
        "documentId": document.pk,
        "filename": "Network Plan.pdf",
        "starred": True,
    }
    repeated = loopback_client.post(
        path,
        {"starred": "TRUE"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert repeated.status_code == 200
    assert repeated.json() == starred.json()
    document.refresh_from_db()
    assert document.starred is True


def test_pdf_star_endpoint_supports_query_fallback_and_safe_redirect(loopback_client, document):
    route = reverse("bitbucket_search:document_star", args=(document.pk,))
    return_to = f"{reverse('bitbucket_search:index')}?sort=starred_first"

    response = loopback_client.post(
        f"{route}?starred=true",
        {"return_to": return_to},
    )

    assert response.status_code == 302
    assert response.url == f"{return_to}#pdf-document-{document.pk}"
    document.refresh_from_db()
    assert document.starred is True

    post_state_wins = loopback_client.post(
        f"{route}?starred=true",
        {"starred": "false"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert post_state_wins.status_code == 200
    assert post_state_wins.json()["starred"] is False
    document.refresh_from_db()
    assert document.starred is False


@pytest.mark.parametrize("starred", (None, "", "toggle", "yes", "1"))
def test_pdf_star_endpoint_rejects_missing_or_malformed_state(
    loopback_client,
    document,
    starred,
):
    payload = {}
    if starred is not None:
        payload["starred"] = starred

    response = loopback_client.post(
        reverse("bitbucket_search:document_star", args=(document.pk,)),
        payload,
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 400
    assert response.json() == {
        "state": "invalid",
        "label": "Star not changed",
        "detail": "Starred state must be true or false.",
    }
    document.refresh_from_db()
    assert document.starred is False


def test_pdf_star_endpoint_reports_missing_document_and_database_conflict(
    loopback_client,
    document,
    monkeypatch,
):
    missing = loopback_client.post(
        reverse("bitbucket_search:document_star", args=(document.pk + 1000,)),
        {"starred": "true"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert missing.status_code == 404
    assert missing.json() == {
        "state": "not_found",
        "label": "Star not changed",
        "detail": "This PDF is not registered in OWL.",
    }

    def raise_conflict(*_args, **_kwargs):
        raise PDFStarConflictError("The PDF star is busy. Refresh and try again.")

    monkeypatch.setattr(bitbucket_views, "set_document_star", raise_conflict)
    conflict = loopback_client.post(
        reverse("bitbucket_search:document_star", args=(document.pk,)),
        {"starred": "true"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert conflict.status_code == 409
    assert conflict.json() == {
        "state": "conflict",
        "label": "Star not changed",
        "detail": "The PDF star is busy. Refresh and try again.",
    }


def test_pdf_star_endpoint_is_post_only_csrf_protected_and_local_only(document):
    path = reverse("bitbucket_search:document_star", args=(document.pk,))
    loopback = Client(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    assert loopback.get(path).status_code == 405

    csrf_client = Client(
        enforce_csrf_checks=True,
        HTTP_HOST="127.0.0.1",
        REMOTE_ADDR="127.0.0.1",
    )
    assert csrf_client.post(path, {"starred": "true"}).status_code == 403

    remote_client = Client(HTTP_HOST="127.0.0.1", REMOTE_ADDR="192.0.2.40")
    assert remote_client.post(path, {"starred": "true"}).status_code == 403
    document.refresh_from_db()
    assert document.starred is False


def test_pdf_star_endpoint_accepts_loopback_opaque_origin_with_valid_csrf(document):
    client = Client(
        enforce_csrf_checks=True,
        HTTP_HOST="localhost",
        REMOTE_ADDR="127.0.0.1",
    )
    page = client.get(reverse("bitbucket_search:index"))

    response = client.post(
        reverse("bitbucket_search:document_star", args=(document.pk,)),
        {
            "csrfmiddlewaretoken": page.cookies["csrftoken"].value,
            "starred": "true",
        },
        HTTP_ORIGIN="null",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 200
    assert response.json()["starred"] is True
    document.refresh_from_db()
    assert document.starred is True
