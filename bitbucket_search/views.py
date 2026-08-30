"""Repository registration, background sync, and honest search-shell views."""

from __future__ import annotations

import calendar
import hashlib
import ipaddress
import logging
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.contrib import messages
from django.core.paginator import EmptyPage, Page, PageNotAnInteger, Paginator
from django.db import OperationalError
from django.db.models import Case, DateTimeField, F, Value, When
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from bitbucket_search.models import (
    BitbucketPeopleGroup,
    BitbucketRepository,
    PDFDocument,
    PDFDocumentAddedEvidence,
    PDFDocumentLifecycle,
    PDFExtractionJob,
    PDFExtractionJobStatus,
    PDFIndexState,
    RepositoryRemovalRecovery,
    RepositorySyncJob,
    RepositorySyncJobStatus,
    RepositorySyncState,
)
from bitbucket_search.services.document_actions import (
    BulkDocumentOpenResult,
    DocumentActionError,
    open_registered_pdf,
    open_registered_pdfs,
    reveal_registered_pdf,
)
from bitbucket_search.services.git_output import bounded_git_output
from bitbucket_search.services.git_sync import RepositorySyncError, managed_repository_path
from bitbucket_search.services.logging_events import get_logger, log_event
from bitbucket_search.services.path_safety import has_disallowed_path_characters
from bitbucket_search.services.pdf_indexing import (
    ExtractionStatusSnapshot,
    extraction_status_snapshot,
)
from bitbucket_search.services.pdf_local_policy import frozen_pdf_path
from bitbucket_search.services.pdf_search import (
    PDFSearchHit,
    PDFSearchPage,
    search_documents,
    search_index_available,
)
from bitbucket_search.services.pdf_search_query import (
    MAX_COMMITTER_FILTERS,
    MAX_SEARCH_PAGE_SIZE,
    InvalidPDFSearchQuery,
    PDFSearchMatchMode,
    PDFSearchQuery,
    PDFSearchScope,
    PDFSearchSort,
    parse_pdf_search_query,
)
from bitbucket_search.services.people import (
    PeopleGroupValidationError,
    create_people_group,
    git_people_summaries,
)
from bitbucket_search.services.repository_lifecycle import (
    RepositoryLifecycleError,
    remove_repository,
    set_repository_refresh_excluded,
)
from bitbucket_search.services.repository_sync import (
    RepositoryRefreshInProgress,
    launch_sync_worker,
    mark_worker_launch_failed,
    queue_all_repository_refreshes,
    queue_due_daily_repository_refreshes,
    queue_repository_refresh,
    register_and_queue_repository,
    release_repository_worker_wakeups,
    repository_status_snapshot,
    reserve_queued_repository_worker_wakeups,
    resident_repository_workers_active,
)
from bitbucket_search.services.repository_urls import RepositoryURLValidationError


@dataclass(frozen=True, slots=True)
class PDFTimelineRow:
    """Presentation-safe metadata for one active PDF timeline result."""

    document: PDFDocument
    full_path: str
    display_path: str
    path_copy_available: bool
    project_label: str
    added_by_label: str
    added_date_label: str
    added_date_detail: str
    history_label: str
    local_policy_state: str = ""
    search_hit: PDFSearchHit | None = None


@dataclass(frozen=True, slots=True)
class PDFTimelineGroup:
    """One exclusive chronological bucket in newest-first display order."""

    key: str
    label: str
    detail: str
    rows: tuple[PDFTimelineRow, ...]


@dataclass(frozen=True, slots=True)
class PeopleGroupSummary:
    """Presentation-safe state for one persisted Git-committer group."""

    id: int
    name: str
    member_names: tuple[str, ...]
    member_count: int
    pdf_count: int
    selected: bool


MAX_PEOPLE_GROUP_FILTERS = 50
MAX_SELECTED_REPOSITORIES = 100
logger = get_logger("web")


def _logged_web_action(operation: str, *, quiet: bool = False):
    """Trace action routing without serializing request bodies, paths, or queries."""

    def decorate(function):
        @wraps(function)
        def invoke(request, *args, **kwargs):
            context = {
                "operation": operation,
                "document_id": kwargs.get("document_id"),
                "repository_id": kwargs.get("repository_id"),
            }
            if not quiet:
                log_event(logger, logging.DEBUG, "web_action_requested", **context)
            try:
                response = function(request, *args, **kwargs)
            except Http404 as error:
                log_event(
                    logger,
                    logging.WARNING,
                    "web_action_rejected",
                    error=error,
                    reason="not_found",
                    **context,
                )
                raise
            except Exception as error:
                log_event(logger, logging.ERROR, "web_action_failed", error=error, **context)
                raise
            if not quiet:
                log_event(
                    logger,
                    logging.DEBUG,
                    "web_action_response",
                    status=response.status_code,
                    **context,
                )
            return response

        return invoke

    return decorate


def _is_loopback_request(request: HttpRequest) -> bool:
    candidate = request.META.get("REMOTE_ADDR", "")
    try:
        return ipaddress.ip_address(candidate.split("%", maxsplit=1)[0]).is_loopback
    except ValueError:
        return False


def _require_local_action(request: HttpRequest) -> HttpResponse | None:
    if settings.OWL_ALLOW_NON_LOOPBACK or _is_loopback_request(request):
        return None
    log_event(logger, logging.WARNING, "web_action_rejected", reason="non_loopback")
    return HttpResponseForbidden("This action is available only from the local OWL application.")


def _require_strict_loopback_action(request: HttpRequest) -> HttpResponse | None:
    """Keep native filesystem actions local even if repository APIs are proxied."""

    if _is_loopback_request(request):
        return None
    log_event(logger, logging.WARNING, "filesystem_action_rejected", reason="non_loopback")
    return HttpResponseForbidden("This filesystem action is available only on this computer.")


def _is_async_request(request: HttpRequest) -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return "0 B"


def _subtract_calendar_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _display_date(value: date) -> str:
    return date_format(value, "j M Y")


def _display_datetime(value: datetime) -> str:
    return date_format(timezone.localtime(value), "j M Y, H:i")


def _timeline_bucket(value: date, *, today: date) -> tuple[str, str, str]:
    """Assign one date to exactly one WhatsApp-style chronological group."""

    if value > today:
        return "future", "Future Git date", "Timestamp recorded in Git"
    if value == today:
        return "today", "Today", _display_date(today)

    yesterday = today - timedelta(days=1)
    if value == yesterday:
        return "yesterday", "Yesterday", _display_date(yesterday)

    week_start = today - timedelta(days=today.weekday())
    if value >= week_start:
        return (
            "week",
            "This Week",
            f"{_display_date(week_start)} – {_display_date(today)}",
        )

    month_start = today.replace(day=1)
    if value >= month_start:
        return "month", "This Month", date_format(today, "F Y")

    three_month_start = _subtract_calendar_months(today, 3)
    if value >= three_month_start:
        return "three-months", "Last 3 Months", f"Since {_display_date(three_month_start)}"

    six_month_start = _subtract_calendar_months(today, 6)
    if value >= six_month_start:
        return "six-months", "Last 6 Months", f"Since {_display_date(six_month_start)}"

    if value.year == today.year:
        return "year", "This Year", str(today.year)

    if value.year == today.year - 1:
        return "last-year", "Last Year", str(today.year - 1)

    year = str(value.year)
    return f"year-{year}", year, "Older PDFs"


def _project_label(repository: BitbucketRepository) -> str:
    """Read the project key from Bitbucket Server's HTTP clone route."""

    try:
        remote = urlsplit(repository.remote_url)
    except ValueError:
        return ""
    path_parts = remote.path.strip("/").split("/")
    if (
        remote.scheme in {"https", "http"}
        and len(path_parts) >= 3
        and path_parts[-3].casefold() == "scm"
        and path_parts[-2]
        and path_parts[-1]
    ):
        return path_parts[-2]
    # A GitHub owner or Bitbucket Cloud workspace is not a Bitbucket project.
    return ""


def _display_full_path(document: PDFDocument) -> str:
    value = document.relative_path
    relative = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or has_disallowed_path_characters(value)
        or relative.is_absolute()
        or PureWindowsPath(value).drive
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        log_event(
            logger,
            logging.DEBUG,
            "pdf_display_path_rejected",
            document_id=document.pk,
            reason="invalid_registered_path",
        )
        return "Unavailable: invalid registered path"
    try:
        saved_path = frozen_pdf_path(document)
        if saved_path is not None:
            return str(saved_path)
        repository_root = managed_repository_path(document.repository)
        candidate = repository_root.joinpath(*relative.parts).resolve(strict=False)
    except (
        DocumentActionError,
        OSError,
        RepositorySyncError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as error:
        log_event(
            logger,
            logging.ERROR,
            "pdf_display_path_failed",
            error=error,
            document_id=document.pk,
            repository_id=document.repository_id,
            stage="path_resolution",
        )
        return "Unavailable: invalid managed checkout"
    if not candidate.is_relative_to(repository_root):
        return "Unavailable: invalid registered path"
    return str(candidate)


def _repository_added_at(document: PDFDocument) -> datetime | None:
    """Use original Git addition evidence, never the OWL discovery timestamp."""

    if (
        document.added_evidence == PDFDocumentAddedEvidence.CONFIRMED
        and document.added_commit is not None
    ):
        return document.added_commit.committed_at
    return None


def _timeline_row(
    document: PDFDocument,
    *,
    search_hit: PDFSearchHit | None = None,
) -> PDFTimelineRow:
    added_at = _repository_added_at(document)
    if added_at is not None:
        added_by = f"{document.added_commit.author_name} · Git author"
        added_detail = f"Original Git addition · {_display_datetime(added_at)}"
    else:
        added_by = "Unavailable in available Git history"
        added_detail = "Original addition not found in available Git history."

    history_label = (
        "Available history" if document.repository.history_is_shallow else "Full reachable history"
    )
    if added_at is None:
        history_label = f"{history_label} · Git-added date unavailable"

    full_path = _display_full_path(document)
    path_copy_available = not full_path.startswith("Unavailable:")
    local_policy = getattr(document, "local_policy", None)
    return PDFTimelineRow(
        document=document,
        full_path=full_path,
        display_path=(
            f"{document.repository.display_name}/{document.relative_path}"
            if path_copy_available
            else "Unavailable"
        ),
        path_copy_available=path_copy_available,
        project_label=_project_label(document.repository),
        added_by_label=added_by,
        added_date_label=(
            _display_date(timezone.localtime(added_at).date())
            if added_at is not None
            else "Unavailable"
        ),
        added_date_detail=added_detail,
        history_label=history_label,
        local_policy_state=local_policy.state if local_policy is not None else "",
        search_hit=search_hit,
    )


def _pdf_timeline_page(page_number: object) -> tuple[Page, tuple[PDFTimelineGroup, ...]]:
    queryset = (
        PDFDocument.objects.filter(
            lifecycle_state=PDFDocumentLifecycle.ACTIVE,
            repository__enabled=True,
        )
        .select_related("repository", "added_commit", "last_commit", "local_policy")
        .annotate(
            repository_added_at=Case(
                When(
                    added_evidence=PDFDocumentAddedEvidence.CONFIRMED,
                    added_commit__isnull=False,
                    then=F("added_commit__committed_at"),
                ),
                default=Value(None),
                output_field=DateTimeField(),
            )
        )
        .order_by(F("repository_added_at").desc(nulls_last=True), "-id")
    )
    paginator = Paginator(queryset, settings.BITBUCKET_PDF_PAGE_SIZE)
    try:
        page = paginator.page(page_number)
    except (PageNotAnInteger, EmptyPage):
        if page_number in (None, "", 1, "1"):
            page = paginator.page(1)
        else:
            raise

    today = timezone.localdate()
    grouped: OrderedDict[str, tuple[str, str, list[PDFTimelineRow]]] = OrderedDict()
    for document in page.object_list:
        if document.repository_added_at is None:
            key, label, detail = "repo-date-unavailable", "Repo date unavailable", ""
        else:
            timeline_date = timezone.localtime(document.repository_added_at).date()
            key, label, detail = _timeline_bucket(timeline_date, today=today)
        if key not in grouped:
            grouped[key] = (label, detail, [])
        grouped[key][2].append(_timeline_row(document))

    groups = tuple(
        PDFTimelineGroup(key=key, label=label, detail=detail, rows=tuple(rows))
        for key, (label, detail, rows) in grouped.items()
    )
    return page, groups


def _pdf_page_url(page_number: int) -> str:
    return f"{reverse('bitbucket_search:document_page')}?page={page_number}"


def _next_pdf_page_url(page: Page) -> str:
    if not page.has_next():
        return ""
    return _pdf_page_url(page.next_page_number())


def _previous_pdf_page_url(page: Page) -> str:
    if not page.has_previous():
        return ""
    return _pdf_page_url(page.previous_page_number())


def _people_group_ids(request: HttpRequest | None) -> tuple[int, ...]:
    """Parse a bounded, duplicate-free list of persisted People-group IDs."""

    if request is None:
        return ()
    values = request.GET.getlist("people_group")
    if len(values) > MAX_PEOPLE_GROUP_FILTERS:
        raise InvalidPDFSearchQuery(
            f"Select at most {MAX_PEOPLE_GROUP_FILTERS} People groups at a time."
        )
    group_ids: list[int] = []
    seen: set[int] = set()
    for value in values:
        if not isinstance(value, str) or not value.isdecimal() or int(value) < 1:
            raise InvalidPDFSearchQuery("Every People group filter must be a positive number.")
        group_id = int(value)
        if group_id not in seen:
            seen.add(group_id)
            group_ids.append(group_id)
    return tuple(group_ids)


def _people_filter_navigation(
    request: HttpRequest | None,
) -> tuple[tuple[tuple[str, str], ...], str]:
    """Preserve non-People search state while replacing only People filters."""

    if request is None:
        return (), reverse("bitbucket_search:index")
    values = request.GET.copy()
    for key in (
        "committer",
        "people_group",
        "page",
        "people_group_error",
        "open_people_group",
    ):
        values.pop(key, None)
    hidden_fields = tuple(
        (name, value) for name, field_values in values.lists() for value in field_values
    )
    clear_query = values.urlencode()
    clear_href = reverse("bitbucket_search:index")
    if clear_query:
        clear_href = f"{clear_href}?{clear_query}"
    return hidden_fields, clear_href


def _search_query_pairs(
    query: PDFSearchQuery,
    *,
    page: int,
    committer_names: tuple[str, ...] | None = None,
    people_group_ids: tuple[int, ...] = (),
) -> list[tuple[str, str]]:
    pairs = [("chip", chip.display) for chip in query.chips]
    pairs.append(("match_mode", query.match_mode.value))
    pairs.extend(("scope", scope.value) for scope in query.scopes)
    pairs.extend(("repository", str(repository_id)) for repository_id in query.repository_ids)
    pairs.extend(("index_state", state.value) for state in query.index_states)
    selected_committers = query.committer_names if committer_names is None else committer_names
    pairs.extend(("committer", name) for name in selected_committers)
    pairs.extend(("people_group", str(group_id)) for group_id in people_group_ids)
    pairs.extend(
        (
            ("sort", query.sort.value),
            ("page", str(page)),
            ("page_size", str(query.page_size)),
        )
    )
    return pairs


def _search_page_url(
    query: PDFSearchQuery,
    *,
    page: int,
    committer_names: tuple[str, ...] | None = None,
    people_group_ids: tuple[int, ...] = (),
) -> str:
    pairs = _search_query_pairs(
        query,
        page=page,
        committer_names=committer_names,
        people_group_ids=people_group_ids,
    )
    return f"{reverse('bitbucket_search:index')}?{urlencode(pairs)}"


def _search_filter_options(
    query: PDFSearchQuery,
    repositories: tuple[BitbucketRepository, ...],
) -> dict[str, tuple[dict[str, object], ...]]:
    scope_labels = {
        PDFSearchScope.CONTENT: "PDF content",
        PDFSearchScope.FILENAME: "Filename",
        PDFSearchScope.PATH: "Path",
        PDFSearchScope.REPOSITORY: "Repository",
    }
    sort_labels = {
        PDFSearchSort.RELEVANCE: "Best match",
        PDFSearchSort.MOST_OPENED: "Most opened",
        PDFSearchSort.LEAST_OPENED: "Least opened",
        PDFSearchSort.RECENTLY_OPENED: "Recently opened",
        PDFSearchSort.LEAST_RECENTLY_OPENED: "Least recently opened",
        PDFSearchSort.FILENAME_ASCENDING: "Filename A–Z",
        PDFSearchSort.FILENAME_DESCENDING: "Filename Z–A",
        PDFSearchSort.GIT_UPDATED_NEWEST: "Git updated newest",
        PDFSearchSort.INDEXED_NEWEST: "Indexed newest",
        PDFSearchSort.REPOSITORY_ASCENDING: "Repository A–Z",
    }
    selected_repositories = set(query.repository_ids)
    selected_index_states = {state.value for state in query.index_states}
    return {
        "search_match_options": tuple(
            {
                "value": value.value,
                "label": label,
                "selected": query.match_mode is value,
            }
            for value, label in (
                (PDFSearchMatchMode.ALL, "Match every phrase"),
                (PDFSearchMatchMode.ANY, "Match any phrase"),
            )
        ),
        "search_scope_options": tuple(
            {
                "value": scope.value,
                "label": label,
                "selected": scope in query.scopes,
            }
            for scope, label in scope_labels.items()
        ),
        "search_repository_options": tuple(
            {
                "id": repository.pk,
                "label": repository.display_name,
                "selected": repository.pk in selected_repositories,
            }
            for repository in repositories
        ),
        "search_index_state_options": tuple(
            {
                "value": value,
                "label": label,
                "selected": value in selected_index_states,
            }
            for value, label in PDFIndexState.choices
        ),
        "search_sort_options": tuple(
            {
                "value": sort.value,
                "label": label,
                "selected": query.sort is sort,
            }
            for sort, label in sort_labels.items()
        ),
    }


def _extraction_payload(snapshot: ExtractionStatusSnapshot) -> dict[str, object]:
    publication_signature = ":".join(
        str(value)
        for value in (
            snapshot.pending_documents,
            snapshot.indexed_documents,
            snapshot.stale_documents,
            snapshot.failed_jobs,
            snapshot.interrupted_jobs,
        )
    )
    return {
        "queuedJobs": snapshot.queued_jobs,
        "runningJobs": snapshot.running_jobs,
        "failedJobs": snapshot.failed_jobs,
        "interruptedJobs": snapshot.interrupted_jobs,
        "pendingDocuments": snapshot.pending_documents,
        "indexedDocuments": snapshot.indexed_documents,
        "staleDocuments": snapshot.stale_documents,
        "active": bool(snapshot.queued_jobs or snapshot.running_jobs),
        "publicationSignature": publication_signature,
    }


def _automatic_value(automatic: object, name: str, default=None):
    if automatic is None:
        return default
    if isinstance(automatic, dict):
        return automatic.get(name, default)
    return getattr(automatic, name, default)


def _nonnegative_integer(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _iso_value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _display_timestamp(value: object) -> str:
    if not isinstance(value, datetime):
        return ""
    return _display_datetime(value)


def _automatic_refresh_payload(repository: BitbucketRepository) -> dict[str, object]:
    automatic = getattr(repository, "automatic_refresh", None)
    globally_enabled = bool(getattr(settings, "BITBUCKET_DAILY_REFRESH_ENABLED", True))
    repository_enabled = bool(repository.enabled and not repository.exclude_from_refresh)
    state = str(
        _automatic_value(
            automatic,
            "state",
            "disabled" if not globally_enabled or not repository_enabled else "due",
        )
    )
    label = str(
        _automatic_value(
            automatic,
            "label",
            "Daily refresh off" if state == "disabled" else "Daily refresh due",
        )
    )
    detail = str(
        _automatic_value(
            automatic,
            "detail",
            (
                "This repository is excluded from automatic refreshes."
                if state == "disabled"
                else "Waiting for the daily refresh scheduler."
            ),
        )
    )
    next_action_at = _automatic_value(automatic, "next_action_at")
    last_attempt_at = _automatic_value(automatic, "last_attempt_at")
    scheduled_day = _automatic_value(automatic, "scheduled_day")
    retry_count = _nonnegative_integer(_automatic_value(automatic, "retry_count", 0))
    max_retries = _nonnegative_integer(_automatic_value(automatic, "max_retries", 0))
    retries_remaining = _nonnegative_integer(
        _automatic_value(automatic, "retries_remaining", max_retries - retry_count)
    )
    trigger = _automatic_value(automatic, "trigger")
    return {
        "enabled": globally_enabled and repository_enabled and state != "disabled",
        "state": state,
        "label": label,
        "detail": detail,
        "nextActionAt": _iso_value(next_action_at),
        "nextActionDisplay": _display_timestamp(next_action_at),
        "retryCount": retry_count,
        "maxRetries": max_retries,
        "retriesRemaining": retries_remaining,
        "scheduledDay": _iso_value(scheduled_day),
        "trigger": str(trigger) if trigger else None,
        "lastAttemptAt": _iso_value(last_attempt_at),
        "lastAttemptDisplay": _display_timestamp(last_attempt_at),
        "active": state == "active",
        "retrying": state == "retry_wait",
        "exhausted": state == "exhausted",
    }


def _catalog_publication_signature(
    repositories: tuple[BitbucketRepository, ...],
) -> str:
    """Return a secret-free marker that changes with the published PDF catalogue."""

    parts = []
    for repository in repositories:
        published_at = (
            repository.last_sync_successful_at.isoformat()
            if repository.last_sync_successful_at
            else ""
        )
        parts.append(
            ":".join(
                (
                    str(repository.pk),
                    repository.metadata_indexed_commit or repository.last_synced_commit,
                    str(repository.pdf_count),
                    published_at,
                )
            )
        )
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _repository_catalog_is_stale(repository: BitbucketRepository) -> bool:
    return bool(
        repository.last_sync_successful_at
        and repository.last_sync_completed_at
        and repository.last_sync_completed_at > repository.last_sync_successful_at
    )


def _automation_payload(
    repositories: tuple[BitbucketRepository, ...],
) -> dict[str, object]:
    enabled = bool(getattr(settings, "BITBUCKET_DAILY_REFRESH_ENABLED", True))
    rows = tuple(_automatic_refresh_payload(repository) for repository in repositories)
    state_counts = {
        state: sum(row["state"] == state for row in rows)
        for state in ("active", "up_to_date", "retry_wait", "exhausted", "due", "disabled")
    }
    dated_rows = tuple(
        (repository, row)
        for repository, row in zip(repositories, rows, strict=True)
        if _automatic_value(getattr(repository, "automatic_refresh", None), "next_action_at")
        is not None
    )
    next_action_at = min(
        (
            _automatic_value(repository.automatic_refresh, "next_action_at")
            for repository, _row in dated_rows
        ),
        default=None,
    )
    retry_rows = tuple(row for row in rows if row["state"] == "retry_wait")
    exhausted_rows = tuple(row for row in rows if row["state"] == "exhausted")

    if not enabled:
        state = "disabled"
        label = "Automatic refresh off"
        detail = "Daily repository refreshes are disabled."
    elif not repositories:
        state = "empty"
        label = "Not configured"
        detail = "No repositories connected"
    elif state_counts["active"]:
        state = "active"
        label = "Refreshing"
        count = state_counts["active"]
        detail = f"{count} repositor{'y' if count == 1 else 'ies'} in progress"
    elif exhausted_rows:
        state = "exhausted"
        label = "Needs attention"
        count = len(exhausted_rows)
        detail = (
            f"{count} repositor{'y has' if count == 1 else 'ies have'} exhausted "
            "the current daily cycle's retries"
        )
    elif retry_rows:
        state = "retry_wait"
        label = "Retry scheduled"
        count = len(retry_rows)
        detail = f"{count} repositor{'y' if count == 1 else 'ies'} waiting for automatic retry"
    elif state_counts["due"]:
        state = "due"
        label = "Daily refresh due"
        detail = "The scheduler is preparing the next repository refresh."
    elif state_counts["up_to_date"]:
        state = "up_to_date"
        label = "Up to date"
        detail = "Daily repository refresh is scheduled"
    else:
        state = "disabled"
        label = "Automatic refresh off"
        detail = "No enabled repositories are included in the daily refresh."

    return {
        "enabled": enabled,
        "state": state,
        "label": label,
        "detail": detail,
        "active": state_counts["active"] > 0,
        "nextActionAt": _iso_value(next_action_at),
        "nextActionDisplay": _display_timestamp(next_action_at),
        "retryCount": max((int(row["retryCount"]) for row in retry_rows), default=0),
        "maxRetries": max((int(row["maxRetries"]) for row in rows), default=0),
        "retriesRemaining": min(
            (int(row["retriesRemaining"]) for row in retry_rows),
            default=max((int(row["maxRetries"]) for row in rows), default=0),
        ),
        "activeRepositories": state_counts["active"],
        "retryScheduledRepositories": state_counts["retry_wait"],
        "exhaustedRepositories": state_counts["exhausted"],
        "dueRepositories": state_counts["due"],
    }


def _sync_summary(
    repositories: tuple[BitbucketRepository, ...],
    extraction: ExtractionStatusSnapshot,
    automation: dict[str, object] | None = None,
) -> dict[str, object]:
    automation = automation or _automation_payload(repositories)
    active = tuple(repository for repository in repositories if repository.has_active_sync)
    attention = tuple(
        repository
        for repository in repositories
        if repository.sync_state
        in {
            RepositorySyncState.FAILED,
            RepositorySyncState.INTERRUPTED,
            RepositorySyncState.BLOCKED_DIRTY,
        }
        and _automatic_refresh_payload(repository)["state"] != "retry_wait"
    )
    completed = tuple(
        repository.last_sync_successful_at
        for repository in repositories
        if repository.last_sync_successful_at is not None
    )
    if active:
        return {
            "state": "active",
            "label": "Syncing",
            "detail": f"{len(active)} repositor{'y' if len(active) == 1 else 'ies'} in progress",
            "last_completed": max(completed) if completed else None,
        }
    if extraction.queued_jobs or extraction.running_jobs:
        active_extractions = extraction.queued_jobs + extraction.running_jobs
        return {
            "state": "active",
            "label": "Indexing",
            "detail": (
                f"{active_extractions} PDF job{'s' if active_extractions != 1 else ''} in progress"
            ),
            "last_completed": max(completed) if completed else None,
        }
    if automation["state"] == "exhausted":
        return {
            "state": "attention",
            "label": automation["label"],
            "detail": automation["detail"],
            "last_completed": max(completed) if completed else None,
        }
    if attention:
        return {
            "state": "attention",
            "label": "Needs attention",
            "detail": f"{len(attention)} repositor{'y' if len(attention) == 1 else 'ies'} needs review",
            "last_completed": max(completed) if completed else None,
        }
    if automation["state"] == "retry_wait":
        return {
            "state": "scheduled",
            "label": automation["label"],
            "detail": automation["detail"],
            "last_completed": max(completed) if completed else None,
        }
    if automation["state"] == "due":
        return {
            "state": "scheduled",
            "label": automation["label"],
            "detail": automation["detail"],
            "last_completed": max(completed) if completed else None,
        }
    if extraction.failed_jobs or extraction.interrupted_jobs or extraction.stale_documents:
        return {
            "state": "attention",
            "label": "Needs attention",
            "detail": "One or more PDF indexing jobs need review",
            "last_completed": max(completed) if completed else None,
        }
    if repositories:
        if extraction.pending_documents:
            return {
                "state": "attention",
                "label": "Indexing pending",
                "detail": (
                    f"{extraction.pending_documents} PDF"
                    f"{'s' if extraction.pending_documents != 1 else ''} awaiting indexing"
                ),
                "last_completed": max(completed) if completed else None,
            }
        return {
            "state": "ready",
            "label": "Up to date",
            "detail": "Repository and PDF workers are idle",
            "last_completed": max(completed) if completed else None,
        }
    return {
        "state": "empty",
        "label": "Not configured",
        "detail": "No repositories connected",
        "last_completed": None,
    }


def _index_context(
    *,
    request: HttpRequest | None = None,
    active_section: str = "search",
    open_repository_form: bool = False,
    pdf_page_number: object = 1,
    people_group_error: str = "",
    open_people_group_form: bool = False,
    people_group_form_name: str = "",
    people_group_form_members: tuple[str, ...] = (),
):
    repositories = _with_repository_work_status(repository_status_snapshot())
    automation_payload = _automation_payload(repositories)
    catalog_publication_signature = _catalog_publication_signature(repositories)
    extraction_status = extraction_status_snapshot()
    extraction_payload = _extraction_payload(extraction_status)
    pdf_page, timeline_groups = _pdf_timeline_page(pdf_page_number)
    inventory_document_count = pdf_page.paginator.count
    people = git_people_summaries()
    stored_people_groups = tuple(
        BitbucketPeopleGroup.objects.prefetch_related("members").order_by("normalized_name", "id")
    )
    people_groups_by_id = {group.pk: group for group in stored_people_groups}
    requested_search_query = PDFSearchQuery(page_size=settings.BITBUCKET_SEARCH_PAGE_SIZE)
    search_query = requested_search_query
    selected_people_group_ids: tuple[int, ...] = ()
    search_error = ""
    raw_search_input = ""
    if request is not None and active_section == "search":
        raw_search_input = request.GET.get("q", "")[:4096]
        try:
            requested_search_query = parse_pdf_search_query(request.GET)
            requested_search_query = replace(
                requested_search_query,
                page_size=min(
                    requested_search_query.page_size, settings.BITBUCKET_SEARCH_PAGE_SIZE
                ),
            )
            requested_group_ids = _people_group_ids(request)
            selected_people_group_ids = tuple(
                group_id for group_id in requested_group_ids if group_id in people_groups_by_id
            )
            if len(selected_people_group_ids) != len(requested_group_ids):
                search_error = "One or more selected People groups are no longer available."

            selected_group_member_names = tuple(
                member.person_name
                for group_id in selected_people_group_ids
                for member in people_groups_by_id[group_id].members.all()
            )
            effective_values = request.GET.copy()
            effective_values["page_size"] = str(requested_search_query.page_size)
            effective_values.setlist(
                "committer",
                [*requested_search_query.committer_names, *selected_group_member_names],
            )
            search_query = parse_pdf_search_query(effective_values)
            if requested_search_query.chips:
                raw_search_input = ""
        except InvalidPDFSearchQuery as error:
            log_event(
                logger,
                logging.WARNING,
                "pdf_search_validation_rejected",
                error=error,
                reason="invalid_search_query",
            )
            search_error = str(error)

    search_active = bool(
        search_query.has_query
        or search_query.repository_ids
        or search_query.index_states
        or search_query.committer_names
        or selected_people_group_ids
        or search_query.sort is not PDFSearchSort.RELEVANCE
    )
    search_page: PDFSearchPage | None = None
    search_return_to = ""
    if search_active:
        search_page = search_documents(search_query)
        search_rows = tuple(
            _timeline_row(hit.document, search_hit=hit) for hit in search_page.results
        )
        pdf_timeline_groups = (
            (
                PDFTimelineGroup(
                    key="search-results",
                    label="Search results",
                    detail=f"Page {search_page.page} · up to {search_page.page_size} results",
                    rows=search_rows,
                ),
            )
            if search_rows
            else ()
        )
        pdf_document_count = search_page.total
        timeline_page_number = search_page.page
        search_return_to = request.get_full_path() if request is not None else ""
        next_search_page_url = (
            _search_page_url(
                search_query,
                page=search_page.page + 1,
                committer_names=requested_search_query.committer_names,
                people_group_ids=selected_people_group_ids,
            )
            if search_page.has_next
            else ""
        )
        previous_search_page_url = (
            _search_page_url(
                search_query,
                page=search_page.page - 1,
                committer_names=requested_search_query.committer_names,
                people_group_ids=selected_people_group_ids,
            )
            if search_page.has_previous
            else ""
        )
        next_pdf_page_url = ""
        previous_pdf_page_url = ""
    else:
        pdf_timeline_groups = timeline_groups
        pdf_document_count = inventory_document_count
        timeline_page_number = pdf_page.number
        next_search_page_url = ""
        previous_search_page_url = ""
        next_pdf_page_url = _next_pdf_page_url(pdf_page)
        previous_pdf_page_url = _previous_pdf_page_url(pdf_page)

    pdf_count = sum(repository.pdf_count for repository in repositories)
    vsdx_count = sum(repository.vsdx_count for repository in repositories)
    document_bytes = sum(repository.document_bytes for repository in repositories)
    allowed_hosts = tuple(settings.BITBUCKET_ALLOWED_HOSTS)
    selected_people_keys = {name.casefold() for name in requested_search_query.committer_names}
    group_form_member_keys = {name.casefold() for name in people_group_form_members}
    git_people = tuple(
        {
            "name": person.name,
            "aliases": person.aliases,
            "commit_count": person.commit_count,
            "pdf_count": person.pdf_count,
            "repository_count": person.repository_count,
            "repository_names": person.repository_names,
            "selected": person.name.casefold() in selected_people_keys,
            "group_form_selected": person.name.casefold() in group_form_member_keys,
        }
        for person in people
    )
    people_pdf_counts = {person.name.casefold(): person.pdf_count for person in people}
    selected_group_id_set = set(selected_people_group_ids)
    people_groups = tuple(
        PeopleGroupSummary(
            id=group.pk,
            name=group.name,
            member_names=tuple(member.person_name for member in group.members.all()),
            member_count=len(group.members.all()),
            pdf_count=sum(
                people_pdf_counts.get(member.normalized_person_name, 0)
                for member in group.members.all()
            ),
            selected=group.pk in selected_group_id_set,
        )
        for group in stored_people_groups
    )
    people_filter_hidden_fields, people_filter_clear_href = _people_filter_navigation(request)
    people_group_return_to = reverse("bitbucket_search:index")
    if request is not None and request.path == reverse("bitbucket_search:index"):
        people_group_return_to = request.get_full_path()
    people_search_hidden_fields = tuple(
        ("committer", name) for name in requested_search_query.committer_names
    ) + tuple(("people_group", str(group_id)) for group_id in selected_people_group_ids)
    context = {
        "active_app": "bitbucket",
        "active_section": active_section,
        "active_nav": "pdf_search" if active_section == "search" else "repositories",
        "page_title": "Bitbucket Search",
        "status_message": (
            f"PDF indexing: {extraction_status.indexed_documents} published, "
            f"{extraction_status.pending_documents} pending"
        ),
        "repositories": repositories,
        "repository_removal_recoveries": RepositoryRemovalRecovery.objects.exclude(
            repository_id__in=(repository.pk for repository in repositories)
        ).order_by("display_name", "id"),
        "repository_count": len(repositories),
        "enabled_repository_count": sum(
            item.enabled and not item.exclude_from_refresh and not item.has_removal_pending
            for item in repositories
        ),
        "active_repository_count": sum(item.has_active_sync for item in repositories),
        "active_work_repository_count": sum(item.has_active_work for item in repositories),
        "active_enabled_repository_count": sum(
            item.enabled and item.has_active_sync for item in repositories
        ),
        "pdf_count": pdf_count,
        "inventory_document_count": inventory_document_count,
        "pdf_document_count": pdf_document_count,
        "pdf_timeline_groups": pdf_timeline_groups,
        "pdf_page": pdf_page,
        "timeline_page_number": timeline_page_number,
        "next_pdf_page_url": next_pdf_page_url,
        "previous_pdf_page_url": previous_pdf_page_url,
        "search_active": search_active,
        "search_error": search_error,
        "search_input_value": raw_search_input,
        "search_query": search_query,
        "requested_search_query": requested_search_query,
        "search_page": search_page,
        "search_return_to": search_return_to,
        "open_all_confirm_threshold": settings.OPEN_ALL_CONFIRM_THRESHOLD,
        "next_search_page_url": next_search_page_url,
        "previous_search_page_url": previous_search_page_url,
        "search_index_available": search_index_available(),
        "extraction_status": extraction_status,
        "extraction_payload": extraction_payload,
        "vsdx_count": vsdx_count,
        "document_count": pdf_count + vsdx_count,
        "document_bytes": document_bytes,
        "document_bytes_label": _format_bytes(document_bytes),
        "sync_summary": _sync_summary(repositories, extraction_status, automation_payload),
        "automation_payload": automation_payload,
        "catalog_publication_signature": catalog_publication_signature,
        "allowed_hosts": allowed_hosts,
        "allowed_hosts_label": ", ".join(allowed_hosts) if allowed_hosts else "Not configured",
        "open_repository_form": open_repository_form,
        "git_people": git_people,
        "git_people_total": len(git_people),
        "people_groups": people_groups,
        "selected_people_count": len(requested_search_query.committer_names),
        "selected_group_count": len(selected_people_group_ids),
        "selected_people_group_ids": selected_people_group_ids,
        "people_filter_hidden_fields": people_filter_hidden_fields,
        "people_filter_clear_href": people_filter_clear_href,
        "people_search_hidden_fields": people_search_hidden_fields,
        "people_group_return_to": people_group_return_to,
        "people_group_error": people_group_error,
        "people_group_form_name": people_group_form_name,
        "people_group_form_members": people_group_form_members,
        "open_people_group_form": (
            open_people_group_form
            or bool(people_group_error)
            or bool(request and request.GET.get("open_people_group") == "1")
        ),
        "people_panel_open": bool(
            requested_search_query.committer_names
            or selected_people_group_ids
            or people_group_error
        ),
    }
    context.update(_search_filter_options(search_query, repositories))
    return context


def _with_repository_work_status(
    repositories: tuple[BitbucketRepository, ...],
) -> tuple[BitbucketRepository, ...]:
    """Keep lifecycle controls locked while either Git or PDF workers own a repo."""

    repository_ids = tuple(repository.pk for repository in repositories)
    if not repository_ids:
        return repositories
    busy_ids = set(
        RepositorySyncJob.objects.filter(
            repository_id__in=repository_ids,
            status__in=(RepositorySyncJobStatus.QUEUED, RepositorySyncJobStatus.RUNNING),
        ).values_list("repository_id", flat=True)
    )
    busy_ids.update(
        PDFExtractionJob.objects.filter(
            document__repository_id__in=repository_ids,
            status__in=(PDFExtractionJobStatus.QUEUED, PDFExtractionJobStatus.RUNNING),
        ).values_list("document__repository_id", flat=True)
    )
    for repository in repositories:
        repository.has_active_work = repository.has_active_sync or repository.pk in busy_ids
    removal_ids = set(
        RepositoryRemovalRecovery.objects.filter(repository_id__in=repository_ids).values_list(
            "repository_id", flat=True
        )
    )
    for repository in repositories:
        repository.has_removal_pending = repository.pk in removal_ids
    return repositories


def _repository_payload(repository: BitbucketRepository) -> dict[str, object]:
    if not hasattr(repository, "has_active_work"):
        _with_repository_work_status((repository,))
    automatic = _automatic_refresh_payload(repository)
    last_attempt_at = automatic["lastAttemptAt"] or (
        repository.last_sync_completed_at.isoformat() if repository.last_sync_completed_at else None
    )
    catalog_is_stale = _repository_catalog_is_stale(repository)
    return {
        "id": repository.pk,
        "name": repository.display_name,
        "host": repository.canonical_remote_key.split("/", maxsplit=1)[0],
        "state": repository.sync_state,
        "stateLabel": repository.get_sync_state_display(),
        "progress": repository.sync_progress,
        "message": repository.status_message,
        "active": repository.has_active_sync,
        "workerTiming": getattr(repository, "worker_timing", None),
        "pdfCount": repository.pdf_count,
        "vsdxCount": repository.vsdx_count,
        "documentCount": repository.document_count,
        "documentBytes": repository.document_bytes,
        "documentBytesLabel": _format_bytes(repository.document_bytes),
        "branch": repository.default_branch,
        "enabled": repository.enabled,
        "refreshExcluded": repository.exclude_from_refresh,
        "hasActiveWork": repository.has_active_work,
        "hasRemovalPending": repository.has_removal_pending,
        "lastAttemptAt": last_attempt_at,
        "lastSuccessfulAt": (
            repository.last_sync_successful_at.isoformat()
            if repository.last_sync_successful_at
            else None
        ),
        "catalogPublishedAt": (
            repository.last_sync_successful_at.isoformat()
            if repository.last_sync_successful_at
            else None
        ),
        "catalogStale": catalog_is_stale,
        "automatic": automatic,
        "refreshUrl": reverse(
            "bitbucket_search:repository_refresh", kwargs={"repository_id": repository.pk}
        ),
        "exclusionUrl": reverse(
            "bitbucket_search:repository_exclude", kwargs={"repository_id": repository.pk}
        ),
        "removeUrl": reverse(
            "bitbucket_search:repository_remove", kwargs={"repository_id": repository.pk}
        ),
    }


@require_GET
@never_cache
@_logged_web_action("pdf_index", quiet=True)
def index(request: HttpRequest) -> HttpResponse:
    """Render the repository-aware Bitbucket Search workspace."""

    return render(request, "bitbucket_search/index.html", _index_context(request=request))


@require_GET
@never_cache
@_logged_web_action("repositories", quiet=True)
def repositories(request: HttpRequest) -> HttpResponse:
    """Open the same workspace with repository registration expanded."""

    return render(
        request,
        "bitbucket_search/index.html",
        _index_context(active_section="repositories", open_repository_form=True),
    )


@require_POST
@csrf_protect
@never_cache
@_logged_web_action("people_group_create", quiet=True)
def add_people_group(request: HttpRequest) -> HttpResponse:
    """Persist one local group of known Git committers without changing Git."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error

    member_names = tuple(request.POST.getlist("member"))
    try:
        if len(member_names) > MAX_COMMITTER_FILTERS:
            raise PeopleGroupValidationError(
                f"Select at most {MAX_COMMITTER_FILTERS} Git committers for one group."
            )
        group = create_people_group(request.POST.get("name", ""), member_names)
    except PeopleGroupValidationError as error:
        context = _index_context(
            people_group_error=str(error),
            open_people_group_form=True,
            people_group_form_name=request.POST.get("name", "")[:255],
            people_group_form_members=member_names[:MAX_COMMITTER_FILTERS],
        )
        context["people_group_return_to"] = _document_action_return_url(request)
        return render(request, "bitbucket_search/index.html", context, status=400)

    member_count = group.members.count()
    messages.success(
        request,
        f'People group "{group.name}" created with {member_count} '
        f"Git committer{'s' if member_count != 1 else ''}.",
    )
    return redirect(_document_action_return_url(request))


@require_GET
@never_cache
@_logged_web_action("pdf_document_page", quiet=True)
def document_page(request: HttpRequest) -> HttpResponse:
    """Return the next durable PDF timeline fragment for progressive scrolling."""

    page_number = request.GET.get("page")
    if not _is_async_request(request):
        try:
            context = _index_context(pdf_page_number=page_number)
        except (PageNotAnInteger, EmptyPage):
            return HttpResponse("That PDF timeline page is not available.", status=404)
        return render(request, "bitbucket_search/index.html", context)

    try:
        page, groups = _pdf_timeline_page(page_number)
    except (PageNotAnInteger, EmptyPage):
        return JsonResponse(
            {"detail": "That PDF timeline page is not available."},
            status=404,
        )
    html = render_to_string(
        "bitbucket_search/_pdf_timeline.html",
        {
            "pdf_timeline_groups": groups,
            "timeline_page_number": page.number,
        },
        request=request,
    )
    return JsonResponse(
        {
            "html": html,
            "nextPageUrl": _next_pdf_page_url(page),
            "page": page.number,
        }
    )


def _document_action_failure(
    request: HttpRequest,
    error: DocumentActionError,
    *,
    document_id: int | None = None,
) -> HttpResponse:
    log_event(
        logger,
        logging.WARNING
        if error.code
        in {"invalid_document_selection", "too_many_documents", "confirmation_required"}
        else logging.DEBUG,
        "pdf_action_error_response",
        document_id=document_id,
        error_code=error.code,
    )
    status = {
        "document_not_found": 404,
        "invalid_document_selection": 400,
        "too_many_documents": 400,
        "invalid_document_path": 400,
        "unsupported_document_type": 400,
        "invalid_repository_checkout": 409,
        "document_unavailable": 409,
        "repository_refresh_in_progress": 409,
        "repository_not_ready": 409,
        "repository_busy": 409,
        "pdf_extraction_busy": 409,
        "pdf_delete_confirmation_required": 409,
        "pdf_policy_cleanup_failed": 500,
        "pdf_policy_rollback_failed": 500,
        "confirmation_required": 409,
        "native_action_unavailable": 503,
        "unsupported_platform": 503,
        "native_action_timeout": 503,
        "native_action_failed": 503,
    }.get(error.code, 400)
    if _is_async_request(request):
        return JsonResponse(
            {"state": "failed", "code": error.code, "detail": error.summary},
            status=status,
        )
    messages.error(request, error.summary)
    return redirect(_document_action_return_url(request, document_id=document_id))


def _document_action_return_url(
    request: HttpRequest,
    *,
    document_id: int | None = None,
) -> str:
    navigation = request.POST if request.method == "POST" else request.GET
    raw_return_to = navigation.get("return_to", "")
    if raw_return_to and len(raw_return_to) <= 8192:
        parsed = urlsplit(raw_return_to)
        if (
            not parsed.scheme
            and not parsed.netloc
            and parsed.path == reverse("bitbucket_search:index")
        ):
            destination = parsed.path
            if parsed.query:
                destination = f"{destination}?{parsed.query}"
            if document_id is not None:
                return f"{destination}#pdf-document-{document_id}"
            return destination

    raw_page = navigation.get("return_page", "")
    try:
        page_number = int(raw_page)
    except (TypeError, ValueError, OverflowError):
        page_number = 0
    destination = (
        _pdf_page_url(page_number)
        if 2 <= page_number <= 1_000_000
        else reverse("bitbucket_search:index")
    )
    if document_id is not None:
        return f"{destination}#pdf-document-{document_id}"
    return destination


def _bulk_document_ids(request: HttpRequest) -> tuple[int, ...]:
    raw_document_ids = request.POST.getlist("document_id")
    if not raw_document_ids:
        raise DocumentActionError(
            "invalid_document_selection",
            "Select one or more PDFs from the current results page.",
        )
    if len(raw_document_ids) > MAX_SEARCH_PAGE_SIZE:
        raise DocumentActionError(
            "too_many_documents",
            f"Open All supports at most {MAX_SEARCH_PAGE_SIZE} PDFs at a time.",
        )

    document_ids: list[int] = []
    seen: set[int] = set()
    for raw_document_id in raw_document_ids:
        if not raw_document_id or not raw_document_id.isascii() or not raw_document_id.isdecimal():
            raise DocumentActionError(
                "invalid_document_selection",
                "The selected PDF list is not valid.",
            )
        document_id = int(raw_document_id)
        if document_id <= 0:
            raise DocumentActionError(
                "invalid_document_selection",
                "The selected PDF list is not valid.",
            )
        if document_id not in seen:
            seen.add(document_id)
            document_ids.append(document_id)
    return tuple(document_ids)


def _bulk_open_payload(result: BulkDocumentOpenResult) -> dict[str, object]:
    if result.failures and result.opened_count:
        state = "partially_opened"
    elif result.failures:
        state = "failed"
    elif result.usage_failures:
        state = "opened_with_warnings"
    else:
        state = "opened"
    return {
        "state": state,
        "requestedCount": result.requested_count,
        "openedCount": result.opened_count,
        "failedCount": result.failed_count,
        "usageFailureCount": result.usage_failure_count,
        "documents": [
            {
                "documentId": document.pk,
                "openCount": document.open_count,
                "lastOpenedAt": (
                    document.last_opened_at.isoformat() if document.last_opened_at else None
                ),
            }
            for document in result.opened_documents
        ],
        "failures": [
            {
                "documentId": failure.document_id,
                "code": failure.code,
                "detail": failure.summary,
            }
            for failure in result.failures
        ],
        "usageFailures": [
            {
                "documentId": failure.document_id,
                "code": failure.code,
                "detail": failure.summary,
            }
            for failure in result.usage_failures
        ],
    }


@require_POST
@csrf_protect
@never_cache
@_logged_web_action("pdf_open")
def open_document(request: HttpRequest, document_id: int) -> HttpResponse:
    """Open one registered local PDF and count only a successful dispatch."""

    local_error = _require_strict_loopback_action(request)
    if local_error:
        return local_error
    try:
        document = open_registered_pdf(document_id)
    except DocumentActionError as exc:
        return _document_action_failure(request, exc, document_id=document_id)

    if _is_async_request(request):
        return JsonResponse(
            {
                "state": "opened",
                "documentId": document.pk,
                "openCount": document.open_count,
                "lastOpenedAt": (
                    document.last_opened_at.isoformat() if document.last_opened_at else None
                ),
            }
        )
    messages.success(request, f"Opened {document.filename}.")
    return redirect(_document_action_return_url(request, document_id=document.pk))


@require_POST
@csrf_protect
@never_cache
@_logged_web_action("pdf_open_all")
def open_documents(request: HttpRequest) -> HttpResponse:
    """Open a bounded result-page selection after validating every managed path."""

    local_error = _require_strict_loopback_action(request)
    if local_error:
        return local_error
    try:
        document_ids = _bulk_document_ids(request)
        if (
            len(document_ids) > settings.OPEN_ALL_CONFIRM_THRESHOLD
            and request.POST.get("confirmed") != "1"
        ):
            raise DocumentActionError(
                "confirmation_required",
                f"Confirm before opening {len(document_ids)} PDFs from this result page.",
            )
        result = open_registered_pdfs(document_ids)
    except DocumentActionError as exc:
        return _document_action_failure(request, exc)

    if _is_async_request(request):
        return JsonResponse(
            _bulk_open_payload(result),
            status=200 if result.opened_count else 503,
        )

    pdf_label = "PDF" if result.requested_count == 1 else "PDFs"
    usage_warning = ""
    if result.usage_failures:
        usage_label = "PDF" if result.usage_failure_count == 1 else "PDFs"
        usage_warning = (
            f" Usage could not be recorded for {result.usage_failure_count} opened {usage_label}."
        )

    if not result.failures and not result.usage_failures:
        messages.success(request, f"Opened all {result.requested_count} {pdf_label}.")
    elif not result.failures:
        messages.warning(
            request,
            f"Opened all {result.requested_count} {pdf_label}.{usage_warning}",
        )
    elif result.opened_count:
        messages.warning(
            request,
            (
                f"Opened {result.opened_count} of {result.requested_count} {pdf_label}. "
                f"{result.failed_count} could not be opened.{usage_warning}"
            ),
        )
    else:
        messages.error(
            request,
            (
                f"None of the {result.requested_count} {pdf_label} could be opened. "
                f"{result.failures[0].summary}"
            ),
        )
    return redirect(_document_action_return_url(request))


@require_POST
@csrf_protect
@never_cache
@_logged_web_action("pdf_reveal")
def reveal_document(request: HttpRequest, document_id: int) -> HttpResponse:
    """Reveal one registered local PDF without changing its open count."""

    local_error = _require_strict_loopback_action(request)
    if local_error:
        return local_error
    try:
        document = reveal_registered_pdf(document_id)
    except DocumentActionError as exc:
        return _document_action_failure(request, exc, document_id=document_id)

    if _is_async_request(request):
        return JsonResponse(
            {
                "state": "revealed",
                "documentId": document.pk,
                "openCount": document.open_count,
            }
        )
    messages.success(request, f"Opened the folder containing {document.filename}.")
    return redirect(_document_action_return_url(request, document_id=document.pk))


@require_POST
@csrf_protect
@never_cache
@_logged_web_action("pdf_exclude")
def exclude_document(request: HttpRequest, document_id: int) -> HttpResponse:
    """Old per-file controls must not create new exclusions after the repo-level move."""

    return _retired_pdf_refresh_control(request, document_id)


@require_POST
@csrf_protect
@never_cache
@_logged_web_action("pdf_resume")
def resume_document(request: HttpRequest, document_id: int) -> HttpResponse:
    """Retain a safe response for forms left open before repository-level controls."""

    return _retired_pdf_refresh_control(request, document_id)


def _retired_pdf_refresh_control(request: HttpRequest, document_id: int) -> HttpResponse:

    local_error = _require_strict_loopback_action(request)
    if local_error:
        return local_error
    get_object_or_404(PDFDocument, pk=document_id)
    detail = (
        "Refresh exclusions are now managed for the whole repository. Use its menu in the sidebar."
    )
    if _is_async_request(request):
        return JsonResponse(
            {"state": "gone", "code": "repository_refresh_controls", "detail": detail},
            status=410,
        )
    return HttpResponse(detail, status=410, content_type="text/plain; charset=utf-8")


@require_http_methods(["GET", "POST"])
@csrf_protect
@never_cache
@_logged_web_action("pdf_delete")
def delete_document(request: HttpRequest, document_id: int) -> HttpResponse:
    """Reject old delete links and forms: individual repository PDFs are read-only."""

    local_error = _require_strict_loopback_action(request)
    if local_error:
        return local_error
    get_object_or_404(PDFDocument, pk=document_id)
    detail = "PDFs are read-only. Individual file deletion is unavailable."
    if _is_async_request(request):
        return JsonResponse(
            {"state": "gone", "code": "pdf_read_only", "detail": detail}, status=410
        )
    return HttpResponse(detail, status=410, content_type="text/plain; charset=utf-8")


@require_POST
@csrf_protect
@never_cache
@_logged_web_action("repository_add")
def add_repository(request: HttpRequest) -> HttpResponse:
    """Validate one URL, enqueue clone/refresh, and return before Git starts."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error
    try:
        queued = register_and_queue_repository(request.POST.get("repository_url"))
    except RepositorySyncError as error:
        return _repository_lifecycle_failure(request, error)
    except RepositoryURLValidationError as exc:
        log_event(
            logger,
            logging.WARNING,
            "repository_url_rejected",
            error=exc,
            error_code=exc.code,
        )
        if _is_async_request(request):
            return JsonResponse(
                {"state": "invalid", "code": exc.code, "detail": str(exc)},
                status=400,
            )
        messages.error(request, str(exc))
        return redirect(f"{reverse('bitbucket_search:repositories')}#bb-add-repository")

    # Reserve the queued job before launching so the redirected page's automatic
    # schedule tick cannot start a second helper for the same work.
    if queued.job.status == RepositorySyncJobStatus.QUEUED:
        _workers_started, launch_failed = _wake_queued_repository_workers(
            job_ids=(queued.job.pk,),
        )
        if launch_failed:
            mark_worker_launch_failed(queued.job.pk)
            queued.repository.refresh_from_db()
            detail = "OWL could not start the background repository worker."
            if _is_async_request(request):
                return JsonResponse(
                    {
                        "state": "worker_unavailable",
                        "detail": detail,
                        "repository": _repository_payload(queued.repository),
                    },
                    status=503,
                )
            messages.error(request, detail)
            return redirect(reverse("bitbucket_search:repositories"))

    queued.repository.refresh_from_db()
    detail = (
        "Repository added. The background worker will clone PDF and VSDX files."
        if queued.repository_created
        else "Repository already exists. Its background refresh is queued."
    )
    if not queued.job_created:
        detail = "This repository already has a background sync in progress."
    if _is_async_request(request):
        return JsonResponse(
            {
                "state": "queued" if queued.job_created else "already_running",
                "detail": detail,
                "repository": _repository_payload(queued.repository),
            },
            status=202,
        )
    messages.success(request, detail)
    return redirect(reverse("bitbucket_search:index"))


@require_POST
@csrf_protect
@never_cache
@_logged_web_action("repository_exclude")
def exclude_repository(request: HttpRequest, repository_id: int) -> HttpResponse:
    """Change future automatic/global refresh eligibility without hiding saved PDFs."""

    local_error = _require_strict_loopback_action(request)
    if local_error:
        return local_error
    get_object_or_404(BitbucketRepository, pk=repository_id)
    value = request.POST.get("excluded")
    if value not in {"yes", "no"}:
        return JsonResponse(
            {"state": "invalid", "detail": "Choose whether to exclude this repository."},
            status=400,
        )
    try:
        repository = set_repository_refresh_excluded(repository_id, excluded=value == "yes")
    except RepositoryLifecycleError as error:
        return _repository_lifecycle_failure(request, error)
    detail = (
        f"{repository.display_name} is excluded from Refresh all and scheduled refreshes. "
        "Its PDFs remain available, and you can still refresh this repository manually."
        if repository.exclude_from_refresh
        else f"{repository.display_name} is included in Refresh all and scheduled refreshes."
    )
    if _is_async_request(request):
        return JsonResponse(
            {"state": "updated", "detail": detail, "repository": _repository_payload(repository)}
        )
    messages.success(request, detail)
    return redirect(reverse("bitbucket_search:index"))


def _repository_lifecycle_failure(
    request: HttpRequest, error: RepositoryLifecycleError | RepositorySyncError
) -> HttpResponse:
    status = 404 if error.code == "repository_unavailable" else 409
    if _is_async_request(request):
        return JsonResponse(
            {"state": "blocked", "code": error.code, "detail": error.summary}, status=status
        )
    return HttpResponse(error.summary, status=status, content_type="text/plain; charset=utf-8")


def _selected_repository_ids(request: HttpRequest) -> tuple[int, ...]:
    """Validate the entire bounded selection before touching any repository."""

    values = request.POST.getlist("repository_ids")
    if not values or len(values) > MAX_SELECTED_REPOSITORIES * 2:
        raise RepositoryLifecycleError(
            "invalid_repository_selection",
            f"Select between 1 and {MAX_SELECTED_REPOSITORIES} repositories.",
        )
    ids = []
    for value in values:
        if (
            not value
            or len(value) > 19
            or not value.isascii()
            or not value.isdecimal()
            or not 0 < int(value) <= 2**63 - 1
        ):
            raise RepositoryLifecycleError(
                "invalid_repository_selection", "The selected repository list is not valid."
            )
        ids.append(int(value))
    unique_ids = tuple(dict.fromkeys(ids))
    if len(unique_ids) > MAX_SELECTED_REPOSITORIES:
        raise RepositoryLifecycleError(
            "invalid_repository_selection",
            f"Select at most {MAX_SELECTED_REPOSITORIES} repositories.",
        )
    return unique_ids


def _selected_repository_error(
    request: HttpRequest, error: RepositoryLifecycleError
) -> HttpResponse:
    status = {
        "invalid_repository_selection": 400,
        "invalid_repository_operation": 400,
        "invalid_refresh_policy": 400,
        "repository_unavailable": 404,
    }.get(error.code, 409)
    if _is_async_request(request):
        return JsonResponse(
            {"state": "blocked", "code": error.code, "detail": error.summary}, status=status
        )
    return HttpResponse(error.summary, status=status, content_type="text/plain; charset=utf-8")


def _selected_removal_context(repository_ids: tuple[int, ...]) -> dict[str, object]:
    repositories = BitbucketRepository.objects.in_bulk(repository_ids)
    _with_repository_work_status(tuple(repositories.values()))
    recoveries = {
        recovery.repository_id: recovery
        for recovery in RepositoryRemovalRecovery.objects.filter(repository_id__in=repository_ids)
    }
    rows = []
    for repository_id in repository_ids:
        repository = repositories.get(repository_id)
        recovery = recoveries.get(repository_id)
        path = ""
        if repository:
            try:
                path = str(managed_repository_path(repository))
            except RepositorySyncError:
                path = "Managed checkout path unavailable"
        rows.append(
            {
                "id": repository_id,
                "name": (
                    repository.display_name
                    if repository
                    else recovery.display_name
                    if recovery
                    else f"Repository {repository_id} (unavailable)"
                ),
                "path": path,
                "pdf_count": repository.pdf_count if repository else 0,
                "vsdx_count": repository.vsdx_count if repository else 0,
                "busy": bool(repository and repository.has_active_work),
                "removal_incomplete": recovery is not None,
            }
        )
    return {
        "active_app": "bitbucket",
        "page_title": "Remove selected repositories",
        "selected_repositories": rows,
        "repository_count": len(rows),
        "repository_busy": any(row["busy"] for row in rows),
        "cancel_url": reverse("bitbucket_search:index"),
    }


def _remove_selected_repositories(
    request: HttpRequest, repository_ids: tuple[int, ...]
) -> HttpResponse:
    context = _selected_removal_context(repository_ids)
    if request.POST.get("confirmed") != "yes":
        return render(request, "bitbucket_search/repositories_remove.html", context)
    if context["repository_busy"]:
        return _selected_repository_error(
            request,
            RepositoryLifecycleError(
                "repository_busy",
                "Wait for the selected repositories' Git and PDF workers to finish before removing them.",
            ),
        )

    removed_ids = []
    failures = []
    for repository_id in repository_ids:
        try:
            remove_repository(repository_id, confirmed=True)
        except RepositoryLifecycleError as error:
            failures.append(
                {"repositoryId": repository_id, "code": error.code, "detail": error.summary}
            )
        else:
            removed_ids.append(repository_id)
    remaining_ids = tuple(failure["repositoryId"] for failure in failures)
    detail = (
        f"Removed {len(removed_ids)} of {len(repository_ids)} selected repositories and their "
        "downloaded files and indexed data from this computer. Remote repositories were not changed."
    )
    if failures:
        detail += f" {len(failures)} removals are unfinished; retry only the remaining selection."
    if _is_async_request(request):
        return JsonResponse(
            {
                "state": "partially_removed" if failures else "removed",
                "detail": detail,
                "removedIds": removed_ids,
                "remainingIds": remaining_ids,
                "removedCount": len(removed_ids),
                "failures": failures,
            },
            status=409 if failures else 200,
        )
    if failures:
        context = _selected_removal_context(remaining_ids)
        context.update({"removal_errors": failures, "removal_result": detail})
        return render(request, "bitbucket_search/repositories_remove.html", context, status=409)
    messages.success(request, detail)
    return redirect(reverse("bitbucket_search:index"))


@require_POST
@csrf_protect
@never_cache
@_logged_web_action("repositories_selected")
def selected_repositories(request: HttpRequest) -> HttpResponse:
    """Apply a toolbar action only to an explicitly validated repository selection."""

    local_error = _require_strict_loopback_action(request)
    if local_error:
        return local_error
    operation = request.POST.get("operation")
    excluded = request.POST.get("excluded")
    try:
        if operation not in {"refresh", "exclude", "remove"}:
            raise RepositoryLifecycleError(
                "invalid_repository_operation", "Choose a valid repository action."
            )
        if operation == "exclude" and excluded not in {"yes", "no"}:
            raise RepositoryLifecycleError(
                "invalid_refresh_policy", "Choose whether to exclude the selected repositories."
            )
        repository_ids = _selected_repository_ids(request)
        repositories = BitbucketRepository.objects.in_bulk(repository_ids)
        recovery_ids = set(
            RepositoryRemovalRecovery.objects.filter(repository_id__in=repository_ids).values_list(
                "repository_id", flat=True
            )
        )
        known_ids = set(repositories) | (recovery_ids if operation == "remove" else set())
        if set(repository_ids) != known_ids:
            raise RepositoryLifecycleError(
                "repository_unavailable",
                "One or more selected repositories no longer exist. Reload and select again.",
            )
        if operation != "remove" and recovery_ids:
            raise RepositoryLifecycleError(
                "repository_removal_pending",
                "A selected repository has an incomplete removal. Use Retry removal first.",
            )
        _with_repository_work_status(tuple(repositories.values()))
        if operation == "refresh" and any(
            repository.has_active_work or not repository.enabled
            for repository in repositories.values()
        ):
            raise RepositoryLifecycleError(
                "repository_busy",
                "Select enabled repositories whose Git and PDF workers have finished before refreshing.",
            )
    except RepositoryLifecycleError as error:
        return _selected_repository_error(request, error)

    if operation == "remove":
        return _remove_selected_repositories(request, repository_ids)

    completed_ids = []
    jobs = []
    failures = []
    for repository_id in repository_ids:
        try:
            if operation == "exclude":
                set_repository_refresh_excluded(repository_id, excluded=excluded == "yes")
            else:
                queued = queue_repository_refresh(repository_id)
                if queued.job.status == RepositorySyncJobStatus.QUEUED:
                    jobs.append(queued.job.pk)
        except (RepositoryLifecycleError, RepositorySyncError) as error:
            failures.append(
                {"repositoryId": repository_id, "code": error.code, "detail": error.summary}
            )
        except BitbucketRepository.DoesNotExist:
            failures.append(
                {
                    "repositoryId": repository_id,
                    "code": "repository_unavailable",
                    "detail": "This repository no longer exists. Reload and select again.",
                }
            )
        except OperationalError as error:
            log_event(
                logger,
                logging.ERROR,
                "repository_selected_action_failed",
                error=error,
                repository_id=repository_id,
                operation=operation,
            )
            failures.append(
                {
                    "repositoryId": repository_id,
                    "code": "repository_database_busy",
                    "detail": "OWL could not save this repository action. Reload and try again.",
                }
            )
        else:
            completed_ids.append(repository_id)
    workers_started, launch_failed = (
        _wake_queued_repository_workers(job_ids=tuple(jobs)) if jobs else (0, False)
    )
    action = (
        "Queued background refresh for" if operation == "refresh" else "Updated refresh policy for"
    )
    detail = f"{action} {len(completed_ids)} of {len(repository_ids)} selected repositories."
    if failures:
        detail += f" {len(failures)} could not be updated; reload before trying them again."
    if launch_failed:
        detail += " Some helper workers could not start; durable refresh jobs remain queued."
    if _is_async_request(request):
        updated = BitbucketRepository.objects.filter(pk__in=completed_ids)
        return JsonResponse(
            {
                "state": "partial"
                if failures
                else ("queued" if operation == "refresh" else "updated"),
                "detail": detail,
                "completedIds": completed_ids,
                "completedCount": len(completed_ids),
                "workersStarted": workers_started,
                "workerWakeupFailed": launch_failed,
                "failures": failures,
                "repositories": [_repository_payload(repository) for repository in updated],
            },
            status=409 if failures else (202 if operation == "refresh" else 200),
        )
    if failures or launch_failed:
        messages.warning(request, detail)
    else:
        messages.success(request, detail)
    return redirect(reverse("bitbucket_search:index"))


@require_http_methods(["GET", "POST"])
@csrf_protect
@never_cache
@_logged_web_action("repository_remove")
def remove_repository_view(request: HttpRequest, repository_id: int) -> HttpResponse:
    """Confirm local checkout/index deletion; never mutate a remote Git repository."""

    local_error = _require_strict_loopback_action(request)
    if local_error:
        return local_error
    repository = BitbucketRepository.objects.filter(pk=repository_id).first()
    recovery = RepositoryRemovalRecovery.objects.filter(repository_id=repository_id).first()
    if repository is None and recovery is None:
        raise Http404("Repository not found.")
    if repository:
        _with_repository_work_status((repository,))
    context = {
        "active_app": "bitbucket",
        "page_title": "Remove local repository",
        "repository_id": repository_id,
        "repository_name": repository.display_name if repository else recovery.display_name,
        "repository_path": "",
        "repository_busy": bool(repository and repository.has_active_work),
        "removal_incomplete": recovery is not None,
        "removal_database_deleted": bool(recovery and recovery.database_deleted),
        "cancel_url": reverse("bitbucket_search:index"),
        "confirmation_required": request.method == "POST"
        and request.POST.get("confirmed") != "yes",
    }
    if repository:
        try:
            context["repository_path"] = str(managed_repository_path(repository))
        except RepositorySyncError:
            # The lifecycle service will reject unsafe paths before deletion.
            context["repository_path"] = "Managed checkout path unavailable"
    if request.method == "GET" or request.POST.get("confirmed") != "yes":
        return render(
            request,
            "bitbucket_search/repository_remove.html",
            context,
            status=400 if request.method == "POST" else 200,
        )
    try:
        remove_repository(repository_id, confirmed=True)
    except RepositoryLifecycleError as error:
        if _is_async_request(request):
            return _repository_lifecycle_failure(request, error)
        recovery = RepositoryRemovalRecovery.objects.filter(repository_id=repository_id).first()
        context["removal_incomplete"] = recovery is not None
        context["removal_database_deleted"] = bool(recovery and recovery.database_deleted)
        context["removal_error"] = error.summary
        return render(request, "bitbucket_search/repository_remove.html", context, status=409)
    detail = (
        f"Removed {context['repository_name']} and its downloaded files and indexed data "
        "from this computer. The remote repository was not changed."
    )
    if _is_async_request(request):
        return JsonResponse({"state": "removed", "repositoryId": repository_id, "detail": detail})
    messages.success(request, detail)
    return redirect(reverse("bitbucket_search:index"))


@require_POST
@csrf_protect
@never_cache
@_logged_web_action("repository_refresh")
def refresh_repository(request: HttpRequest, repository_id: int) -> HttpResponse:
    """Queue a safe background fetch/fast-forward for one managed repository."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error
    get_object_or_404(BitbucketRepository, pk=repository_id)
    try:
        queued = queue_repository_refresh(repository_id)
    except RepositorySyncError as error:
        return _repository_lifecycle_failure(request, error)
    # The short reservation deduplicates this explicit wakeup against the
    # automatic schedule tick that runs as soon as the redirected page opens.
    if queued.job.status == RepositorySyncJobStatus.QUEUED:
        _workers_started, launch_failed = _wake_queued_repository_workers(
            job_ids=(queued.job.pk,),
        )
        if launch_failed:
            mark_worker_launch_failed(queued.job.pk)
            queued.repository.refresh_from_db()
            if _is_async_request(request):
                return JsonResponse(
                    {
                        "state": "worker_unavailable",
                        "detail": "OWL could not start the background repository worker.",
                        "repository": _repository_payload(queued.repository),
                    },
                    status=503,
                )
            messages.error(request, "OWL could not start the background repository worker.")
            return redirect(reverse("bitbucket_search:index"))
    queued.repository.refresh_from_db()
    if _is_async_request(request):
        return JsonResponse(
            {
                "state": "queued" if queued.job_created else "already_running",
                "detail": (
                    "Background refresh queued."
                    if queued.job_created
                    else "This repository already has a background sync in progress."
                ),
                "repository": _repository_payload(queued.repository),
            },
            status=202,
        )
    messages.success(request, "Repository refresh queued in the background.")
    return redirect(reverse("bitbucket_search:index"))


@require_POST
@csrf_protect
@never_cache
@_logged_web_action("repository_refresh_all")
def refresh_all_repositories(request: HttpRequest) -> HttpResponse:
    """Queue a clone/refresh for every enabled repository without blocking the page."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error

    try:
        queued = queue_all_repository_refreshes(require_idle=True)
    except RepositoryRefreshInProgress as error:
        log_event(
            logger,
            logging.WARNING,
            "repository_refresh_all_rejected",
            error=error,
            reason="repository_busy",
        )
        detail = "Wait for repository additions and refreshes to finish before refreshing all."
        if _is_async_request(request):
            return JsonResponse(
                {"state": "busy", "detail": detail, "queued": 0, "workersStarted": 0},
                status=409,
            )
        messages.warning(request, detail)
        return redirect(reverse("bitbucket_search:index"))
    if queued.eligible_total == 0:
        log_event(
            logger,
            logging.DEBUG,
            "repository_refresh_all_skipped",
            reason="no_enabled_repositories",
        )
        detail = "No repositories are included in refresh."
        if _is_async_request(request):
            return JsonResponse(
                {
                    "state": "empty",
                    "detail": detail,
                    "eligible": 0,
                    "queued": 0,
                    "alreadyActive": 0,
                    "alreadyQueued": 0,
                    "alreadyRunning": 0,
                    "workersStarted": 0,
                },
                status=409,
            )
        messages.warning(request, detail)
        return redirect(reverse("bitbucket_search:index"))

    workers_started, launch_failed = _wake_queued_repository_workers(
        job_ids=tuple(job.pk for job in queued.fallback_worker_jobs),
    )

    newly_queued_count = queued.newly_queued_count
    already_queued_count = queued.already_queued_count
    already_running_count = queued.already_running_count
    already_active_count = queued.already_active_count
    if newly_queued_count:
        detail = (
            f"Queued {newly_queued_count} repositor"
            f"{'y' if newly_queued_count == 1 else 'ies'} for background Git refresh."
        )
        prior_details = []
        if already_queued_count:
            prior_details.append(f"{already_queued_count} already queued")
        if already_running_count:
            prior_details.append(f"{already_running_count} already running")
        if prior_details:
            detail += f" {'; '.join(prior_details).capitalize()}."
    else:
        state_details = []
        if already_queued_count:
            state_details.append(f"{already_queued_count} queued")
        if already_running_count:
            state_details.append(f"{already_running_count} running")
        detail = "All eligible repositories already have active sync jobs: "
        detail += f"{'; '.join(state_details)}."
    if launch_failed:
        detail += (
            " OWL could not start every helper worker, but the durable jobs remain queued "
            "for the resident worker pool."
        )

    if _is_async_request(request):
        return JsonResponse(
            {
                "state": (
                    "queued_worker_wakeup_failed"
                    if launch_failed
                    else ("queued" if newly_queued_count else "already_active")
                ),
                "detail": detail,
                "eligible": queued.eligible_total,
                "queued": newly_queued_count,
                "alreadyActive": already_active_count,
                "alreadyQueued": already_queued_count,
                "alreadyRunning": already_running_count,
                "workersStarted": workers_started,
            },
            status=202,
        )
    if launch_failed:
        messages.warning(request, detail)
    else:
        messages.success(request, detail)
    return redirect(reverse("bitbucket_search:index"))


def _wake_queued_repository_workers(
    *,
    job_ids: tuple[int, ...] | None = None,
) -> tuple[int, bool]:
    """Wake bounded helpers for direct runserver sessions without losing jobs."""

    if resident_repository_workers_active():
        return 0, False
    reservation = reserve_queued_repository_worker_wakeups(job_ids=job_ids)
    workers_started = 0
    for worker_number, _job_id in enumerate(reservation.job_ids):
        try:
            launch_sync_worker()
        except OSError as error:
            log_event(
                logger,
                logging.ERROR,
                "repository_worker_wakeup_failed",
                error=error,
                job_id=_job_id,
                worker_count=workers_started,
                failed_count=len(reservation.job_ids) - worker_number,
                stage="worker_launch",
            )
            release_repository_worker_wakeups(
                reservation,
                job_ids=reservation.job_ids[worker_number:],
            )
            return workers_started, True
        workers_started += 1
    if workers_started:
        log_event(
            logger,
            logging.DEBUG,
            "repository_worker_wakeup_completed",
            worker_count=workers_started,
        )
    return workers_started, False


@require_POST
@csrf_protect
@never_cache
@_logged_web_action("repository_schedule_tick", quiet=True)
def tick_repository_schedule(request: HttpRequest) -> JsonResponse:
    """Catch up the latest configured daily slot when any OWL page is open."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error
    queued = ()
    try:
        queued = queue_due_daily_repository_refreshes()
        workers_started, launch_failed = _wake_queued_repository_workers()
    except OperationalError as error:
        # Another short SQLite writer can temporarily own the database. The
        # browser retries this idempotent tick, and any jobs already committed
        # remain durable for the resident worker pool.
        log_event(
            logger,
            logging.ERROR,
            "repository_schedule_request_failed",
            error=error,
            queued_count=len(queued),
            stage="schedule_tick",
        )
        return JsonResponse(
            {
                "state": "busy",
                "queued": len(queued),
                "workersStarted": 0,
            },
            status=202,
        )
    if queued or workers_started:
        log_event(
            logger,
            logging.INFO,
            "repository_schedule_dispatched",
            queued_count=len(queued),
            worker_count=workers_started,
            status="worker_wakeup_failed" if launch_failed else "queued",
        )
    return JsonResponse(
        {
            "state": (
                "worker_wakeup_failed"
                if launch_failed
                else ("queued" if queued else ("worker_started" if workers_started else "waiting"))
            ),
            "queued": len(queued),
            "workersStarted": workers_started,
        },
        status=202 if queued or workers_started or launch_failed else 200,
    )


@require_GET
@never_cache
@_logged_web_action("repository_status", quiet=True)
def repository_status(request: HttpRequest) -> JsonResponse:
    """Expose compact, credential-free progress for the repository rail poller."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error
    repositories = _with_repository_work_status(repository_status_snapshot())
    extraction_status = extraction_status_snapshot()
    automation = _automation_payload(repositories)
    return JsonResponse(
        {
            "repositories": [_repository_payload(repository) for repository in repositories],
            "summary": _sync_summary(repositories, extraction_status, automation),
            "automation": automation,
            "catalog": {
                "publicationSignature": _catalog_publication_signature(repositories),
            },
            "extraction": _extraction_payload(extraction_status),
            "totals": {
                "repositories": len(repositories),
                "pdfs": sum(repository.pdf_count for repository in repositories),
                "vsdx": sum(repository.vsdx_count for repository in repositories),
                "documents": sum(repository.document_count for repository in repositories),
                "bytes": sum(repository.document_bytes for repository in repositories),
                "bytesLabel": _format_bytes(
                    sum(repository.document_bytes for repository in repositories)
                ),
            },
        }
    )


@require_GET
@never_cache
@_logged_web_action("repository_logs", quiet=True)
def repository_logs(request: HttpRequest, repository_id: int) -> JsonResponse:
    """Read only one repository's latest bounded, redacted Git transport log."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error
    get_object_or_404(BitbucketRepository.objects.only("id"), pk=repository_id)
    job = (
        RepositorySyncJob.objects.filter(repository_id=repository_id)
        .only(
            "id",
            "status",
            "operation",
            "phase",
            "output_log",
            "output_log_truncated",
            "output_log_updated_at",
        )
        .order_by("-requested_at", "-id")
        .first()
    )
    safe_log, clipped = bounded_git_output(job.output_log if job else "")
    return JsonResponse(
        {
            "repositoryId": repository_id,
            "jobId": job.pk if job else None,
            "status": job.status if job else "not_started",
            "operation": job.operation if job else "",
            "phase": job.phase if job else "",
            "log": safe_log,
            "truncated": bool(clipped or (job and job.output_log_truncated)),
            "updatedAt": job.output_log_updated_at.isoformat()
            if job and job.output_log_updated_at
            else None,
        }
    )


@require_GET
@never_cache
@_logged_web_action("pdf_index_status", quiet=True)
def index_status(request: HttpRequest) -> HttpResponse:
    """Show a truthful summary of durable repository background jobs."""

    repositories = repository_status_snapshot()
    for repository in repositories:
        repository.automatic_refresh_ui = _automatic_refresh_payload(repository)
        repository.catalog_is_stale = _repository_catalog_is_stale(repository)
    active_jobs = RepositorySyncJob.objects.filter(status__in=("queued", "running")).count()
    unresolved_repositories = tuple(
        repository
        for repository in repositories
        if repository.sync_state
        in {
            RepositorySyncState.FAILED,
            RepositorySyncState.INTERRUPTED,
            RepositorySyncState.BLOCKED_DIRTY,
        }
    )
    extraction_status = extraction_status_snapshot()
    automation = _automation_payload(repositories)
    return render(
        request,
        "bitbucket_search/status.html",
        {
            "active_app": "bitbucket",
            "active_section": "index_status",
            "active_nav": "index_status",
            "page_title": "Index & Refresh Status",
            "eyebrow": "Durable background repository worker",
            "heading": "Repository sync activity",
            "repositories": repositories,
            "repository_count": len(repositories),
            "enabled_repository_count": sum(repository.enabled for repository in repositories),
            "active_jobs": active_jobs,
            "unresolved_repository_count": len(unresolved_repositories),
            "automation_payload": automation,
            "sync_summary": _sync_summary(repositories, extraction_status, automation),
            "catalog_publication_signature": _catalog_publication_signature(repositories),
            "pdf_count": sum(repository.pdf_count for repository in repositories),
            "extraction_status": extraction_status,
            "repo_worker_limit": settings.BITBUCKET_MAX_REPO_WORKERS,
            "status_message": "Repository background worker status",
        },
    )
