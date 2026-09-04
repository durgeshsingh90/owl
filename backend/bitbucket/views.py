"""HTTP interface for the independent Bitbucket document workspace."""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, timedelta
from pathlib import PurePosixPath
from urllib.parse import urlencode

from django.conf import settings
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import F
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from bitbucket.forms import RepositoryForm
from bitbucket.models import (
    Document,
    DocumentKind,
    Repository,
    RepositoryState,
    SyncJob,
    SyncJobStatus,
    SyncOperation,
)
from bitbucket.services.credentials import (
    CredentialError,
    credential_summaries,
    has_credential,
    save_credential,
)
from bitbucket.services.document_search import search_documents
from bitbucket.services.remote_sync import wake_sync_worker
from bitbucket.services.repository_urls import parse_repository_url
from bitbucket.services.scheduler import (
    cancel_job,
    queue_due_daily_refreshes,
    retry_job,
)

TIMELINE_LABELS = (
    "Today",
    "Yesterday",
    "Day before yesterday",
    "This week",
    "This month",
    "Last month",
    "Last 3 months",
    "Last 6 months",
    "This year",
    "Last year",
    "Last 2 years",
    "Last 3 years",
    "Older",
    "Date unavailable",
)


def _month_start(value: date, months_back: int = 0) -> date:
    month_index = value.year * 12 + value.month - 1 - months_back
    return date(month_index // 12, month_index % 12 + 1, 1)


def timeline_label(added_on: date | None, *, today: date | None = None) -> str:
    if added_on is None:
        return "Date unavailable"
    current = today or timezone.localdate()
    if added_on == current:
        return "Today"
    if added_on == current - timedelta(days=1):
        return "Yesterday"
    if added_on == current - timedelta(days=2):
        return "Day before yesterday"
    week_start = current - timedelta(days=current.weekday())
    if added_on >= week_start:
        return "This week"
    current_month = _month_start(current)
    if added_on >= current_month:
        return "This month"
    if added_on >= _month_start(current, 1):
        return "Last month"
    if added_on >= _month_start(current, 3):
        return "Last 3 months"
    if added_on >= _month_start(current, 6):
        return "Last 6 months"
    if added_on.year == current.year:
        return "This year"
    if added_on.year == current.year - 1:
        return "Last year"
    if added_on.year == current.year - 2:
        return "Last 2 years"
    if added_on.year == current.year - 3:
        return "Last 3 years"
    return "Older"


def _job_payload(job: SyncJob) -> dict[str, object]:
    repository = job.repository
    parsed = parse_repository_url(repository.url)
    return {
        "id": str(job.pk),
        "status": job.status,
        "operation": job.operation,
        "errorCode": job.error_code,
        "errorMessage": job.error_message,
        "repository": {
            "id": repository.pk,
            "project": repository.project,
            "name": repository.slug,
            "state": repository.state,
            "statusMessage": repository.status_message,
            "pdfCount": repository.pdf_count,
            "indexedPdfCount": repository.indexed_pdf_count,
            "failedPdfCount": repository.failed_pdf_count,
            "vsdxCount": repository.vsdx_count,
            "url": repository.url,
        },
        "authenticationUrl": parsed.authentication_url,
        "retryUrl": reverse("bitbucket:sync_retry", args=(job.pk,)),
        "cancelUrl": reverse("bitbucket:sync_cancel", args=(job.pk,)),
    }


def _repository_payload(repository: Repository) -> dict[str, object]:
    return {
        "id": repository.pk,
        "repositoryHost": repository.host,
        "url": repository.url,
        "project": repository.project,
        "name": repository.slug,
        "state": repository.state,
        "stateLabel": repository.get_state_display(),
        "statusMessage": repository.status_message,
        "pdfCount": repository.pdf_count,
        "indexedPdfCount": repository.indexed_pdf_count,
        "failedPdfCount": repository.failed_pdf_count,
        "vsdxCount": repository.vsdx_count,
        "lastSuccessfulSyncAt": (
            repository.last_successful_sync_at.isoformat()
            if repository.last_successful_sync_at
            else None
        ),
        "selectUrl": f"?{urlencode({'repository': repository.pk})}",
    }


def _file_size_label(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / (1024 * 1024):.1f} MB"


def _text_preview(value: str, query: str) -> str | None:
    if not query or not value:
        return None
    normalized = " ".join(value.split())
    lowered = normalized.casefold()
    positions = [lowered.find(term.casefold()) for term in query.split() if term]
    positions = [position for position in positions if position >= 0]
    start = max(0, (min(positions) if positions else 0) - 80)
    end = min(len(normalized), start + 280)
    prefix = "…" if start else ""
    suffix = "…" if end < len(normalized) else ""
    return f"{prefix}{normalized[start:end]}{suffix}"


def _document_payload(document: Document, *, query: str = "") -> dict[str, object]:
    added_at = timezone.localtime(document.added_at) if document.added_at else None
    latest_at = timezone.localtime(document.latest_commit_at) if document.latest_commit_at else None
    parsed = parse_repository_url(document.repository.url)
    parent = str(PurePosixPath(document.relative_path).parent)
    displayed_commit_id = document.latest_commit_id or document.commit_id
    return {
        "id": document.pk,
        "filename": document.filename,
        "relativePath": document.relative_path,
        "project": document.repository.project,
        "repository": document.repository.slug,
        "addedAt": added_at.isoformat() if added_at else None,
        "addedDate": added_at.strftime("%d %b %Y") if added_at else None,
        "addedBy": document.added_by or None,
        "addedByEmail": document.added_by_email or None,
        "additionCommitId": document.commit_id or None,
        "commitId": displayed_commit_id or None,
        "commitShort": displayed_commit_id[:8] if displayed_commit_id else None,
        "commitMessage": document.latest_commit_message or None,
        "commitAuthor": document.latest_commit_author or None,
        "commitAt": latest_at.isoformat() if latest_at else None,
        "fileSize": document.file_size,
        "fileSizeLabel": _file_size_label(document.file_size),
        "pageCount": document.page_count,
        "contentSha256": document.content_sha256 or None,
        "textTruncated": document.text_truncated,
        "indexState": document.index_state,
        "indexStateLabel": document.get_index_state_display(),
        "indexError": document.index_error or None,
        "lastScannedAt": (
            document.last_scanned_at.isoformat() if document.last_scanned_at else None
        ),
        "textPreview": _text_preview(document.extracted_text, query),
        "openCount": document.open_count,
        "openUrl": reverse("bitbucket:document_open", args=(document.pk,)),
        "browserUrl": parsed.browse_url(document.relative_path),
        "folderUrl": parsed.browse_url("" if parent == "." else parent),
    }


def _page_url(
    *,
    selected_repository: Repository | None,
    page_number: int,
    query: str,
) -> str:
    values: dict[str, int | str] = {"page": page_number}
    if selected_repository is not None:
        values["repository"] = selected_repository.pk
    if query:
        values["q"] = query
    return f"?{urlencode(values)}"


@require_GET
@ensure_csrf_cookie
def index(request: HttpRequest) -> HttpResponse:
    return render(request, "bitbucket/index.html")


@require_GET
def workspace(request: HttpRequest) -> JsonResponse:
    due_jobs = queue_due_daily_refreshes()
    if due_jobs:
        wake_sync_worker()
    all_repositories = tuple(Repository.objects.all())
    selected_id = request.GET.get("repository", "").strip()
    selected_repository = None
    if selected_id.isdigit():
        selected_repository = next(
            (repository for repository in all_repositories if repository.pk == int(selected_id)),
            None,
        )
    documents = Document.objects.select_related("repository").filter(kind=DocumentKind.PDF)
    if selected_repository:
        documents = documents.filter(repository=selected_repository)
    max_query_characters = int(getattr(settings, "BITBUCKET_APP_SEARCH_QUERY_MAX_CHARACTERS", 200))
    query = request.GET.get("q", "").strip()[:max_query_characters]
    documents = search_documents(documents, query)
    page_size = int(getattr(settings, "BITBUCKET_APP_PDF_PAGE_SIZE", 500))
    paginator = Paginator(
        documents.order_by(F("added_at").desc(nulls_last=True), "filename", "pk"),
        page_size,
    )
    page = paginator.get_page(request.GET.get("page", 1))
    groups: OrderedDict[str, list[Document]] = OrderedDict((label, []) for label in TIMELINE_LABELS)
    for document in page.object_list:
        label = timeline_label(
            timezone.localtime(document.added_at).date() if document.added_at else None
        )
        groups[label].append(document)
    timeline = tuple((label, items) for label, items in groups.items() if items)
    latest_jobs = tuple(
        SyncJob.objects.select_related("repository")
        .exclude(status=SyncJobStatus.SUCCEEDED)
        .order_by("-created_at")[:20]
    )
    return JsonResponse(
        {
            "ok": True,
            "csrfToken": get_token(request),
            "homeUrl": reverse("core:dashboard"),
            "addRepositoryUrl": reverse("bitbucket:repository_add"),
            "settingsSaveUrl": reverse("bitbucket:settings_save"),
            "statusUrl": reverse("bitbucket:sync_status"),
            "scheduleUrl": reverse("bitbucket:schedule_tick"),
            "repositories": [_repository_payload(repository) for repository in all_repositories],
            "repositoryCount": len(all_repositories),
            "totalPdfCount": sum(repository.pdf_count for repository in all_repositories),
            "totalIndexedPdfCount": sum(
                repository.indexed_pdf_count for repository in all_repositories
            ),
            "totalFailedPdfCount": sum(
                repository.failed_pdf_count for repository in all_repositories
            ),
            "totalVsdxCount": sum(repository.vsdx_count for repository in all_repositories),
            "selectedRepository": (
                _repository_payload(selected_repository) if selected_repository else None
            ),
            "documentCount": page.paginator.count,
            "pageSize": page_size,
            "workerCount": int(getattr(settings, "BITBUCKET_APP_MAX_WORKERS", 1)),
            "search": {
                "query": query,
                "active": bool(query),
                "resultCount": page.paginator.count,
            },
            "timeline": [
                {
                    "label": label,
                    "documents": [
                        _document_payload(document, query=query) for document in documents
                    ],
                }
                for label, documents in timeline
            ],
            "pagination": {
                "current": page.number,
                "total": page.paginator.num_pages,
                "previousUrl": (
                    _page_url(
                        selected_repository=selected_repository,
                        page_number=page.previous_page_number(),
                        query=query,
                    )
                    if page.has_previous()
                    else None
                ),
                "nextUrl": (
                    _page_url(
                        selected_repository=selected_repository,
                        page_number=page.next_page_number(),
                        query=query,
                    )
                    if page.has_next()
                    else None
                ),
            },
            "credentials": credential_summaries(),
            "jobs": [_job_payload(job) for job in latest_jobs],
            "scheduleHour": getattr(settings, "BITBUCKET_APP_DAILY_REFRESH_LOCAL_HOUR", 9),
        }
    )


@require_POST
def settings_save(request: HttpRequest) -> JsonResponse:
    form = RepositoryForm(request.POST)
    if not form.is_valid() or form.parsed_url is None:
        return JsonResponse({"ok": False, "errors": form.errors.get_json_data()}, status=400)
    parsed = form.parsed_url
    token = form.cleaned_data.get("access_token", "")
    username = form.cleaned_data.get("username", "")
    if not token and not has_credential(parsed.origin):
        return JsonResponse(
            {"ok": False, "message": "Enter an HTTP access token for this Bitbucket server."},
            status=400,
        )
    try:
        with transaction.atomic():
            if token:
                save_credential(parsed, token, username=username)
            repository, created = Repository.objects.get_or_create(
                canonical_url=parsed.url,
                defaults={
                    "url": parsed.url,
                    "host": parsed.host,
                    "project": parsed.project,
                    "slug": parsed.slug,
                    "state": RepositoryState.QUEUED,
                    "status_message": "Initial API metadata fetch queued.",
                },
            )
            if not created:
                Repository.objects.filter(pk=repository.pk).update(
                    url=parsed.url,
                    host=parsed.host,
                    project=parsed.project,
                    slug=parsed.slug,
                    state=RepositoryState.QUEUED,
                    status_message="API metadata refresh queued.",
                    error_message="",
                )
                SyncJob.objects.filter(
                    repository=repository,
                    status=SyncJobStatus.AUTH_REQUIRED,
                ).update(status=SyncJobStatus.CANCELLED, finished_at=timezone.now())
            job = SyncJob.objects.create(
                repository=repository,
                operation=SyncOperation.INITIAL if created else SyncOperation.REFRESH,
            )
    except CredentialError as exc:
        return JsonResponse({"ok": False, "message": str(exc)}, status=400)
    wake_sync_worker()
    return JsonResponse({"ok": True, "job": _job_payload(job)}, status=202)


repository_add = settings_save


@require_POST
def schedule_tick(request: HttpRequest) -> JsonResponse:
    jobs = queue_due_daily_refreshes()
    if jobs:
        wake_sync_worker()
    return JsonResponse({"ok": True, "queued": len(jobs)})


@require_GET
def sync_status(request: HttpRequest) -> JsonResponse:
    raw_job_ids = request.GET.getlist("job")
    jobs = SyncJob.objects.select_related("repository")
    if raw_job_ids:
        jobs = jobs.filter(pk__in=raw_job_ids)
    else:
        jobs = jobs.filter(
            status__in=(
                SyncJobStatus.QUEUED,
                SyncJobStatus.RUNNING,
                SyncJobStatus.AUTH_REQUIRED,
            )
        ).order_by("-created_at")[:20]
    return JsonResponse({"ok": True, "jobs": [_job_payload(job) for job in jobs]})


@require_POST
def sync_retry(request: HttpRequest, job_id: str) -> JsonResponse:
    job = get_object_or_404(SyncJob.objects.select_related("repository"), pk=job_id)
    retry_job(job)
    wake_sync_worker()
    job.refresh_from_db()
    return JsonResponse({"ok": True, "job": _job_payload(job)}, status=202)


@require_POST
def sync_cancel(request: HttpRequest, job_id: str) -> JsonResponse:
    job = get_object_or_404(SyncJob.objects.select_related("repository"), pk=job_id)
    cancel_job(job)
    job.refresh_from_db()
    return JsonResponse({"ok": True, "job": _job_payload(job)})


@require_POST
def document_open(request: HttpRequest, document_id: int) -> JsonResponse:
    document = get_object_or_404(Document.objects.select_related("repository"), pk=document_id)
    Document.objects.filter(pk=document.pk).update(open_count=F("open_count") + 1)
    document.refresh_from_db(fields=("open_count",))
    return JsonResponse({"ok": True, "openCount": document.open_count})
