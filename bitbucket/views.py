"""HTTP interface for the independent Bitbucket document workspace."""

from __future__ import annotations

import json
from collections import OrderedDict
from datetime import date, timedelta
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import F
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from bitbucket.forms import RepositoryForm
from bitbucket.models import (
    Contributor,
    Document,
    DocumentKind,
    Repository,
    RepositoryState,
    SyncJob,
    SyncJobStatus,
    SyncOperation,
)
from bitbucket.services.desktop import DocumentActionError, open_pdf, reveal_pdf
from bitbucket.services.git_sync import repository_path, wake_sync_worker
from bitbucket.services.repository_urls import parse_repository_url
from bitbucket.services.scheduler import (
    cancel_job,
    queue_due_daily_pulls,
    retry_job,
)

PAGE_SIZE = 500
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


def _absolute_document_path(document: Document) -> str:
    relative = PurePosixPath(document.relative_path)
    return str(repository_path(document.repository) / Path(*relative.parts))


def _people(repositories: tuple[Repository, ...]) -> tuple[dict[str, object], ...]:
    repository_ids = [repository.pk for repository in repositories]
    merged: dict[str, dict[str, object]] = {}
    for contributor in Contributor.objects.filter(repository_id__in=repository_ids).iterator():
        item = merged.setdefault(
            contributor.identity_key,
            {
                "name": contributor.name,
                "email": contributor.email,
                "pdf_commit_count": 0,
                "last_pdf_commit_at": contributor.last_pdf_commit_at,
            },
        )
        item["pdf_commit_count"] = int(item["pdf_commit_count"]) + contributor.pdf_commit_count
        latest = item["last_pdf_commit_at"]
        if contributor.last_pdf_commit_at and (
            latest is None or contributor.last_pdf_commit_at > latest
        ):
            item["last_pdf_commit_at"] = contributor.last_pdf_commit_at
    return tuple(
        sorted(
            merged.values(),
            key=lambda item: (-int(item["pdf_commit_count"]), str(item["name"]).casefold()),
        )
    )


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
            "vsdxCount": repository.vsdx_count,
        },
        "authenticationUrl": parsed.authentication_url,
        "retryUrl": reverse("bitbucket:sync_retry", args=(job.pk,)),
        "cancelUrl": reverse("bitbucket:sync_cancel", args=(job.pk,)),
    }


@require_GET
def index(request: HttpRequest) -> HttpResponse:
    due_jobs = queue_due_daily_pulls()
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
    visible_repositories = (selected_repository,) if selected_repository else all_repositories
    documents = Document.objects.select_related("repository").filter(kind=DocumentKind.PDF)
    if selected_repository:
        documents = documents.filter(repository=selected_repository)
    paginator = Paginator(
        documents.order_by(F("added_at").desc(nulls_last=True), "filename", "pk"),
        PAGE_SIZE,
    )
    page = paginator.get_page(request.GET.get("page", 1))
    groups: OrderedDict[str, list[Document]] = OrderedDict((label, []) for label in TIMELINE_LABELS)
    for document in page.object_list:
        document.local_path = _absolute_document_path(document)
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
    return render(
        request,
        "bitbucket/index.html",
        {
            "form": RepositoryForm(),
            "repositories": all_repositories,
            "selected_repository": selected_repository,
            "page": page,
            "timeline": timeline,
            "people": _people(visible_repositories),
            "repository_count": len(all_repositories),
            "pdf_count": sum(repository.pdf_count for repository in visible_repositories),
            "vsdx_count": sum(repository.vsdx_count for repository in visible_repositories),
            "latest_jobs": latest_jobs,
            "initial_job_ids": [
                str(job.pk)
                for job in latest_jobs
                if job.status
                in {
                    SyncJobStatus.QUEUED,
                    SyncJobStatus.RUNNING,
                    SyncJobStatus.AUTH_REQUIRED,
                }
            ],
            "page_size": PAGE_SIZE,
            "schedule_hour": getattr(settings, "BITBUCKET_APP_DAILY_PULL_LOCAL_HOUR", 9),
        },
    )


@require_POST
def repository_add(request: HttpRequest) -> JsonResponse:
    form = RepositoryForm(request.POST)
    if not form.is_valid() or form.parsed_url is None:
        return JsonResponse({"ok": False, "errors": form.errors.get_json_data()}, status=400)
    parsed = form.parsed_url
    with transaction.atomic():
        repository, created = Repository.objects.get_or_create(
            canonical_url=parsed.url,
            defaults={
                "url": parsed.url,
                "host": parsed.host,
                "project": parsed.project,
                "slug": parsed.slug,
                "state": RepositoryState.QUEUED,
                "status_message": "First clone queued.",
            },
        )
        if not created:
            return JsonResponse(
                {"ok": False, "message": "This repository is already in Bitbucket."},
                status=409,
            )
        job = SyncJob.objects.create(repository=repository, operation=SyncOperation.CLONE)
    wake_sync_worker()
    return JsonResponse({"ok": True, "job": _job_payload(job)}, status=202)


@require_POST
def schedule_tick(request: HttpRequest) -> JsonResponse:
    jobs = queue_due_daily_pulls()
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
    try:
        open_pdf(document)
    except DocumentActionError as exc:
        return JsonResponse({"ok": False, "message": str(exc)}, status=409)
    Document.objects.filter(pk=document.pk).update(open_count=F("open_count") + 1)
    document.refresh_from_db(fields=("open_count",))
    return JsonResponse({"ok": True, "openCount": document.open_count})


@require_POST
def documents_open(request: HttpRequest) -> JsonResponse:
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "message": "Invalid request."}, status=400)
    raw_ids = body.get("documentIds", []) if isinstance(body, dict) else []
    ids = list(dict.fromkeys(item for item in raw_ids if isinstance(item, int)))[:PAGE_SIZE]
    documents = {
        document.pk: document
        for document in Document.objects.select_related("repository").filter(
            pk__in=ids, kind=DocumentKind.PDF
        )
    }
    opened: list[int] = []
    skipped: list[int] = []
    for document_id in ids:
        document = documents.get(document_id)
        if document is None:
            skipped.append(document_id)
            continue
        try:
            open_pdf(document)
        except DocumentActionError:
            skipped.append(document_id)
            continue
        opened.append(document_id)
    if opened:
        Document.objects.filter(pk__in=opened).update(open_count=F("open_count") + 1)
    return JsonResponse({"ok": True, "opened": opened, "skipped": skipped})


@require_POST
def document_reveal(request: HttpRequest, document_id: int) -> JsonResponse:
    document = get_object_or_404(Document.objects.select_related("repository"), pk=document_id)
    try:
        folder = reveal_pdf(document)
    except DocumentActionError as exc:
        return JsonResponse({"ok": False, "message": str(exc)}, status=409)
    return JsonResponse({"ok": True, "folder": str(folder)})
