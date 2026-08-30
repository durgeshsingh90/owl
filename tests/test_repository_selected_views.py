from __future__ import annotations

from unittest.mock import Mock, call

import pytest
from django.db import OperationalError
from django.test import Client
from django.urls import reverse

from bitbucket_search import views
from bitbucket_search.models import (
    BitbucketRepository,
    PDFDocument,
    PDFExtractionJob,
    PDFExtractionJobStatus,
    RepositoryRemovalRecovery,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncState,
)
from bitbucket_search.services.git_sync import managed_repository_path
from bitbucket_search.services.repository_lifecycle import RepositoryLifecycleError

pytestmark = pytest.mark.django_db


@pytest.fixture
def selected_repositories(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path.resolve() / "media"
    settings.BITBUCKET_REPOSITORIES_ROOT = settings.MEDIA_ROOT / "bitbucket" / "repositories"
    settings.BITBUCKET_TEMP_ROOT = settings.MEDIA_ROOT / "bitbucket" / "tmp"
    return tuple(
        BitbucketRepository.objects.create(
            display_name=name,
            canonical_remote_key=f"example.invalid/team/{name}",
            remote_url=f"https://example.invalid/team/{name}.git",
            sync_state=RepositorySyncState.READY,
            pdf_count=number,
            vsdx_count=1,
        )
        for number, name in enumerate(("first", "second", "unselected"), 1)
    )


@pytest.fixture
def local_client(client):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    return client


@pytest.fixture
def selected_url():
    return reverse("bitbucket_search:repositories_selected")


def _data(repositories, operation, **extra):
    return {
        "repository_ids": [repository.pk for repository in repositories],
        "operation": operation,
        **extra,
    }


def test_selected_actions_are_post_only(local_client, selected_url):
    assert local_client.get(selected_url).status_code == 405


@pytest.mark.parametrize("operation", ["refresh", "exclude", "remove"])
@pytest.mark.parametrize(
    "ids",
    [
        [],
        [""],
        ["0"],
        ["-1"],
        ["1", "bad"],
        ["1.0"],
        ["１２"],
        ["9" * 20],
        ["1"] * 201,
        [str(number) for number in range(1, 102)],
    ],
)
def test_invalid_whole_selection_cannot_mutate(
    local_client, selected_url, selected_repositories, monkeypatch, operation, ids
):
    actions = [Mock() for _ in range(3)]
    for name, action in zip(
        ("queue_repository_refresh", "set_repository_refresh_excluded", "remove_repository"),
        actions,
        strict=True,
    ):
        monkeypatch.setattr(views, name, action)
    response = local_client.post(
        selected_url,
        {"repository_ids": ids, "operation": operation, "excluded": "yes", "confirmed": "yes"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_repository_selection"
    for action in actions:
        action.assert_not_called()
    assert BitbucketRepository.objects.count() == 3


@pytest.mark.parametrize("operation", ["refresh", "exclude", "remove"])
def test_unknown_id_blocks_entire_valid_selection(
    local_client, selected_url, selected_repositories, monkeypatch, operation
):
    first = selected_repositories[0]
    mutation = Mock()
    for name in (
        "queue_repository_refresh",
        "set_repository_refresh_excluded",
        "remove_repository",
    ):
        monkeypatch.setattr(views, name, mutation)
    response = local_client.post(
        selected_url,
        {
            "repository_ids": [first.pk, 7654321],
            "operation": operation,
            "excluded": "yes",
            "confirmed": "yes",
        },
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 404
    mutation.assert_not_called()


@pytest.mark.parametrize(
    "operation,extra", [("invalid", {}), ("exclude", {}), ("exclude", {"excluded": "true"})]
)
def test_ambiguous_operation_or_policy_rejected(
    local_client, selected_url, selected_repositories, operation, extra
):
    response = local_client.post(selected_url, _data(selected_repositories[:2], operation, **extra))
    assert response.status_code == 400
    assert not BitbucketRepository.objects.filter(exclude_from_refresh=True).exists()


@pytest.mark.parametrize("operation", ["refresh", "exclude", "remove"])
@pytest.mark.parametrize(
    "origin,token,remote,allowed",
    [
        ("null", True, "127.0.0.1", True),
        ("null", False, "127.0.0.1", False),
        ("https://foreign.example.invalid", True, "127.0.0.1", False),
        ("null", True, "192.0.2.25", False),
    ],
)
def test_selected_actions_keep_csrf_and_strict_loopback(
    selected_url,
    selected_repositories,
    monkeypatch,
    settings,
    operation,
    origin,
    token,
    remote,
    allowed,
):
    settings.OWL_ALLOW_NON_LOOPBACK = True
    mutation = Mock(side_effect=RepositoryLifecycleError("repository_busy", "Synthetic busy repo"))
    for name in (
        "queue_repository_refresh",
        "set_repository_refresh_excluded",
        "remove_repository",
    ):
        monkeypatch.setattr(views, name, mutation)
    client = Client(enforce_csrf_checks=True, HTTP_HOST="localhost", REMOTE_ADDR=remote)
    page = client.get(reverse("bitbucket_search:index"))
    data = _data(selected_repositories[:1], operation, excluded="yes", confirmed="yes")
    if token:
        data["csrfmiddlewaretoken"] = page.cookies["csrftoken"].value
    response = client.post(
        selected_url, data, HTTP_ORIGIN=origin, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
    )
    assert response.status_code == (409 if allowed else 403)
    assert mutation.call_count == int(allowed)


@pytest.mark.parametrize("excluded", ["yes", "no"])
def test_selected_exclusion_is_deduplicated_and_does_not_touch_unselected(
    local_client, selected_url, selected_repositories, excluded
):
    first, second, other = selected_repositories
    BitbucketRepository.objects.update(exclude_from_refresh=excluded == "no")
    response = local_client.post(
        selected_url,
        _data((first, first, second), "exclude", excluded=excluded),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 200
    assert response.json()["completedCount"] == 2
    assert set(response.json()["completedIds"]) == {first.pk, second.pk}
    for repository in (first, second, other):
        repository.refresh_from_db()
        assert repository.exclude_from_refresh == (
            excluded == "yes" if repository != other else excluded == "no"
        )


def test_selected_exclusion_allowed_during_work_but_not_pending_removal(
    local_client, selected_url, selected_repositories
):
    first, second, _ = selected_repositories
    RepositorySyncJob.objects.create(repository=first, status=RepositorySyncJobStatus.QUEUED)
    response = local_client.post(selected_url, _data((first,), "exclude", excluded="yes"))
    assert response.status_code == 302
    RepositoryRemovalRecovery.objects.create(
        repository_id=second.pk, display_name=second.display_name
    )
    response = local_client.post(
        selected_url,
        _data((first, second), "exclude", excluded="no"),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 409
    first.refresh_from_db()
    assert first.exclude_from_refresh


def test_selected_refresh_queues_only_selection_and_wakes_workers_once(
    local_client, selected_url, selected_repositories, monkeypatch
):
    first, second, other = selected_repositories
    second.exclude_from_refresh = True
    second.save()
    wake = Mock(return_value=(2, False))
    monkeypatch.setattr(views, "_wake_queued_repository_workers", wake)
    response = local_client.post(
        selected_url,
        _data((first, first, second), "refresh"),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 202
    assert response.json()["completedCount"] == 2
    assert response.json()["workersStarted"] == 2
    assert set(RepositorySyncJob.objects.values_list("repository_id", flat=True)) == {
        first.pk,
        second.pk,
    }
    wake.assert_called_once_with(
        job_ids=tuple(RepositorySyncJob.objects.order_by("pk").values_list("pk", flat=True))
    )
    assert not other.sync_jobs.exists()


def test_mirrored_selection_cap_counts_unique_repositories(
    local_client, selected_url, selected_repositories
):
    response = local_client.post(
        selected_url,
        _data((selected_repositories[0],) * 200, "exclude", excluded="yes"),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 200
    assert response.json()["completedCount"] == 1


def test_queue_database_failure_still_wakes_successfully_queued_selected_work(
    local_client, selected_url, selected_repositories, monkeypatch
):
    first, second, _ = selected_repositories
    queue = views.queue_repository_refresh

    def queue_with_failure(repository_id):
        if repository_id == second.pk:
            raise OperationalError("Synthetic database lock")
        return queue(repository_id)

    monkeypatch.setattr(views, "queue_repository_refresh", queue_with_failure)
    wake = Mock(return_value=(1, False))
    monkeypatch.setattr(views, "_wake_queued_repository_workers", wake)
    response = local_client.post(
        selected_url,
        _data((first, second), "refresh"),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 409
    assert response.json()["state"] == "partial"
    assert response.json()["completedIds"] == [first.pk]
    assert response.json()["failures"][0]["repositoryId"] == second.pk
    assert response.json()["failures"][0]["code"] == "repository_database_busy"
    wake.assert_called_once_with(job_ids=(RepositorySyncJob.objects.get().pk,))


def test_worker_wakeup_failure_keeps_selected_jobs_queued_and_reports_warning(
    local_client, selected_url, selected_repositories, monkeypatch
):
    monkeypatch.setattr(views, "_wake_queued_repository_workers", Mock(return_value=(1, True)))
    response = local_client.post(
        selected_url,
        _data(selected_repositories[:2], "refresh"),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 202
    assert response.json()["workerWakeupFailed"]
    assert "durable refresh jobs remain queued" in response.json()["detail"]
    assert RepositorySyncJob.objects.filter(status=RepositorySyncJobStatus.QUEUED).count() == 2


@pytest.mark.parametrize(
    "operation,phase",
    [
        (operation, phase)
        for operation in ("refresh", "remove")
        for phase in ("sync_queued", "sync_running", "pdf_queued", "pdf_running")
    ]
    + [("refresh", "disabled"), ("refresh", "removal")],
)
def test_selected_actions_preflight_busy_repositories_before_any_mutation(
    local_client, selected_url, selected_repositories, monkeypatch, phase, operation
):
    first, second, _ = selected_repositories
    if phase.startswith("sync"):
        RepositorySyncJob.objects.create(
            repository=second,
            status=RepositorySyncJobStatus.QUEUED
            if phase.endswith("queued")
            else RepositorySyncJobStatus.RUNNING,
        )
    elif phase.startswith("pdf"):
        document = PDFDocument.objects.create(
            repository=second, filename="Guide.pdf", relative_path="Guide.pdf"
        )
        PDFExtractionJob.objects.create(
            document=document,
            status=PDFExtractionJobStatus.QUEUED
            if phase.endswith("queued")
            else PDFExtractionJobStatus.RUNNING,
            target_git_blob_id="a" * 40,
            target_source_commit="b" * 40,
            target_relative_path=document.relative_path,
            target_extractor_version="test",
        )
    elif phase == "disabled":
        second.enabled = False
        second.save()
    else:
        RepositoryRemovalRecovery.objects.create(
            repository_id=second.pk, display_name=second.display_name
        )
    action = Mock()
    monkeypatch.setattr(
        views, "queue_repository_refresh" if operation == "refresh" else "remove_repository", action
    )
    response = local_client.post(
        selected_url,
        _data((first, second), operation, confirmed="yes"),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 409
    action.assert_not_called()


def test_selected_removal_first_post_is_read_only_and_has_explicit_confirmation(
    local_client, selected_url, selected_repositories, monkeypatch
):
    first, second, other = selected_repositories
    remove = Mock()
    monkeypatch.setattr(views, "remove_repository", remove)
    response = local_client.post(
        selected_url, _data((first, second), "remove", return_to="https://foreign.example.invalid")
    )
    html = response.content.decode()
    assert response.status_code == 200
    assert "Remove 2 selected repositories" in html
    assert first.display_name in html and second.display_name in html
    assert f'name="repository_ids" value="{other.pk}"' not in html
    assert f'name="repository_ids" value="{first.pk}"' in html
    assert 'name="confirmed" value="yes"' in html
    assert "permanently delete" in html and "including local changes and saved PDF copies" in html
    assert "Remote repositories are not changed" in html
    assert response.context["cancel_url"] == reverse("bitbucket_search:index")
    remove.assert_not_called()


def test_confirmed_selected_removal_deletes_only_synthetic_selected_local_data(
    local_client, selected_url, selected_repositories, monkeypatch
):
    first, second, other = selected_repositories
    paths = {}
    for repository in selected_repositories:
        checkout = managed_repository_path(repository)
        checkout.mkdir(parents=True)
        (checkout / ".git").mkdir()
        (checkout / "Guide.pdf").write_bytes(b"synthetic test PDF")
        repository.local_path = str(checkout)
        repository.save(update_fields=("local_path",))
        PDFDocument.objects.create(
            repository=repository, filename="Guide.pdf", relative_path="Guide.pdf"
        )
        paths[repository.pk] = checkout
    no_remote_operation = Mock(
        side_effect=AssertionError("Removal must not run Git or contact a remote")
    )
    monkeypatch.setattr("subprocess.Popen", no_remote_operation)
    response = local_client.post(
        selected_url,
        _data((first, first, second), "remove", confirmed="yes"),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 200
    assert response.json()["removedIds"] == [first.pk, second.pk]
    assert response.json()["remainingIds"] == []
    assert set(BitbucketRepository.objects.values_list("pk", flat=True)) == {other.pk}
    assert PDFDocument.objects.count() == 1
    assert not paths[first.pk].exists() and not paths[second.pk].exists()
    assert (paths[other.pk] / "Guide.pdf").read_bytes() == b"synthetic test PDF"
    no_remote_operation.assert_not_called()


@pytest.mark.parametrize("async_request", [False, True])
def test_partial_removal_reports_successes_and_retries_only_remaining(
    local_client, selected_url, selected_repositories, monkeypatch, async_request
):
    first, second, _ = selected_repositories

    def remove(repository_id, *, confirmed):
        assert confirmed
        BitbucketRepository.objects.filter(pk=repository_id).delete()
        if repository_id == second.pk:
            RepositoryRemovalRecovery.objects.create(
                repository_id=repository_id, display_name=second.display_name, database_deleted=True
            )
            raise RepositoryLifecycleError(
                "repository_removal_failed", "Synthetic local cleanup pending."
            )

    action = Mock(side_effect=remove)
    monkeypatch.setattr(views, "remove_repository", action)
    response = local_client.post(
        selected_url,
        _data((first, second), "remove", confirmed="yes"),
        **({"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"} if async_request else {}),
    )
    assert response.status_code == 409
    action.assert_has_calls([call(first.pk, confirmed=True), call(second.pk, confirmed=True)])
    if async_request:
        assert response.json()["removedIds"] == [first.pk]
        assert response.json()["remainingIds"] == [second.pk]
    else:
        html = response.content.decode()
        assert "Removed 1 of 2" in html
        assert f'name="repository_ids" value="{first.pk}"' not in html
        assert f'name="repository_ids" value="{second.pk}"' in html
        assert "Synthetic local cleanup pending" in html
    retry = Mock()
    monkeypatch.setattr(views, "remove_repository", retry)
    response = local_client.post(selected_url, _data((second,), "remove", confirmed="yes"))
    assert response.status_code == 302
    retry.assert_called_once_with(second.pk, confirmed=True)


def test_initial_active_work_count_includes_pdf_workers_without_changing_sync_count(
    local_client, selected_repositories
):
    document = PDFDocument.objects.create(
        repository=selected_repositories[0], filename="Guide.pdf", relative_path="Guide.pdf"
    )
    PDFExtractionJob.objects.create(
        document=document,
        status=PDFExtractionJobStatus.QUEUED,
        target_git_blob_id="a" * 40,
        target_source_commit="b" * 40,
        target_relative_path="Guide.pdf",
        target_extractor_version="test",
    )
    page = local_client.get(reverse("bitbucket_search:index"))
    assert page.context["active_work_repository_count"] == 1
    assert page.context["active_repository_count"] == 0
