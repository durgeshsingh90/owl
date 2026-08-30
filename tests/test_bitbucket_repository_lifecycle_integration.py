"""Repository controls keep every catalogue consumer consistent."""

from __future__ import annotations

import pytest
from django.urls import reverse
from django.utils import timezone

from bitbucket_search.models import (
    BitbucketRepository,
    GitCommit,
    GitCommitFolder,
    PDFDocument,
    PDFTextPage,
    PDFTextRevision,
)
from bitbucket_search.services.git_sync import managed_repository_path
from bitbucket_search.services.pdf_search import search_documents
from bitbucket_search.services.pdf_search_query import PDFSearchQuery
from bitbucket_search.services.people import git_people_summaries
from bitbucket_search.services.repository_lifecycle import (
    remove_repository,
    set_repository_refresh_excluded,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def lifecycle_catalogue(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.BITBUCKET_REPOSITORIES_ROOT = settings.MEDIA_ROOT / "bitbucket" / "repositories"
    settings.BITBUCKET_TEMP_ROOT = settings.MEDIA_ROOT / "bitbucket" / "tmp"
    settings.BITBUCKET_DAILY_REFRESH_ENABLED = False
    revision = PDFTextRevision.objects.create(
        content_sha256="a" * 64,
        extractor_version="synthetic-lifecycle",
        state="ready",
        page_count=1,
        extracted_character_count=24,
    )
    PDFTextPage.objects.create(
        revision=revision,
        page_number=1,
        extracted_text="Sharedlifecycle text",
        character_count=20,
        extraction_state="ready",
    )
    repositories = []
    for number, name in enumerate(("Selected repository", "Unrelated repository"), 1):
        now = timezone.now()
        repository = BitbucketRepository.objects.create(
            display_name=name,
            canonical_remote_key=f"example.invalid/adr/{number}",
            remote_url=f"https://example.invalid/adr/{number}.git",
            sync_state="ready",
            pdf_count=1,
            activity_indexed_commit=str(number) * 40,
            activity_indexed_at=now,
            last_synced_commit=str(number) * 40,
            history_is_shallow=False,
        )
        checkout = managed_repository_path(repository)
        (checkout / ".git").mkdir(parents=True)
        (checkout / "Guide.pdf").write_bytes(b"%PDF synthetic lifecycle fixture")
        repository.local_path = str(checkout)
        repository.save(update_fields=("local_path",))
        commit = GitCommit.objects.create(
            repository=repository,
            commit_hash=str(number) * 40,
            author_name=f"Author {number}",
            committer_name=f"Committer {number}",
            authored_at=now,
            committed_at=now,
            in_activity_history=True,
        )
        GitCommitFolder.objects.create(commit=commit, folder_path="docs")
        PDFDocument.objects.create(
            repository=repository,
            filename="Guide.pdf",
            relative_path="Guide.pdf",
            added_commit=commit,
            added_evidence="confirmed",
            last_commit=commit,
            indexed_revision=revision,
            index_state="ready",
        )
        repositories.append(repository)
    return repositories, revision


def test_exclusion_preserves_search_people_and_dashboard(lifecycle_catalogue, client):
    (selected, _other), revision = lifecycle_catalogue
    set_repository_refresh_excluded(selected.pk, excluded=True)

    assert search_documents(PDFSearchQuery(chips=("Sharedlifecycle",))).total == 2
    assert {person.name for person in git_people_summaries()} == {"Committer 1", "Committer 2"}
    response = client.get(reverse("bitbucket_search:index"))
    assert response.status_code == 200
    assert response.context["pdf_page"].paginator.count == 2
    home = client.get(reverse("core:dashboard"))
    assert home.context["bitbucket_dashboard"].total_commits == 2
    assert PDFTextRevision.objects.filter(pk=revision.pk).exists()
    assert managed_repository_path(selected).is_dir()


def test_removal_updates_search_people_dashboard_and_keeps_shared_text(lifecycle_catalogue, client):
    (selected, other), revision = lifecycle_catalogue
    remove_repository(selected.pk, confirmed=True)

    matches = search_documents(PDFSearchQuery(chips=("Sharedlifecycle",)))
    assert matches.total == 1
    assert matches.results[0].document.repository_id == other.pk
    assert [person.name for person in git_people_summaries()] == ["Committer 2"]
    response = client.get(reverse("bitbucket_search:index"))
    assert response.status_code == 200
    assert response.context["pdf_page"].paginator.count == 1
    home = client.get(reverse("core:dashboard"))
    dashboard = home.context["bitbucket_dashboard"]
    assert dashboard.total_commits == 1
    assert [person.name for person in dashboard.people] == ["Committer 2"]
    assert PDFTextRevision.objects.filter(pk=revision.pk).exists()
    assert not managed_repository_path(selected).exists()
    assert managed_repository_path(other).is_dir()

    remove_repository(other.pk, confirmed=True)
    assert search_documents(PDFSearchQuery(chips=("Sharedlifecycle",))).total == 0
    assert not PDFTextRevision.objects.filter(pk=revision.pk).exists()
    assert not PDFTextPage.objects.exists()
    assert git_people_summaries() == ()
