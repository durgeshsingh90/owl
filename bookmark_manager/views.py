"""Server-rendered Bookmark Manager and secure Confluence settings views."""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from functools import wraps
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, ProgrammingError
from django.db.models import Count, Q
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseRedirect,
    JsonResponse,
    QueryDict,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_POST

from bookmark_manager.forms import (
    BookmarkCategoryRenameForm,
    BookmarkFilterForm,
    BookmarkFolderForm,
    BookmarkImportForm,
    BookmarkInputForm,
    BookmarkOrganisationForm,
    ConfluenceSettingsForm,
    SavedBookmarkViewForm,
)
from bookmark_manager.models import (
    Bookmark,
    BookmarkAvailability,
    BookmarkCategory,
    BookmarkFolder,
    BookmarkImportRun,
    BookmarkRefreshRun,
    BookmarkSource,
    ConfluenceConfiguration,
    ConfluencePageNode,
    Notification,
    NotificationKind,
    NotificationState,
    SavedBookmarkView,
    Tag,
)
from bookmark_manager.services.bookmark_application import (
    BookmarkActionError,
    save_bookmark_input,
    validated_open_url,
)
from bookmark_manager.services.bookmark_domain import (
    find_similar_title_bookmarks,
    record_successful_open,
)
from bookmark_manager.services.bookmark_folders import (
    BookmarkFolderError,
    create_bookmark_folder,
    move_bookmarks_to_folder,
    unfile_bookmarks,
)
from bookmark_manager.services.bookmark_outline import outline_number_map
from bookmark_manager.services.bookmark_productivity import (
    BookmarkProductivityError,
    save_bookmark_view,
    toggle_bookmark_flag,
    update_bookmark_organisation,
)
from bookmark_manager.services.bookmark_query import (
    BookmarkDateFilter,
    BookmarkQuery,
    BookmarkQueryResult,
    BookmarkSort,
    InvalidBookmarkQuery,
    matching_bookmarks_for_url,
    query_bookmarks,
)
from bookmark_manager.services.bookmark_refresh import (
    create_or_get_refresh_run,
    launch_refresh_worker,
    mark_refresh_launch_failed,
    publish_refresh_notification,
    queue_due_scheduled_refresh,
    refresh_schedule_snapshot,
    refresh_status_snapshot,
)
from bookmark_manager.services.configuration import (
    ConfigurationSummary,
    ConfigurationUnavailable,
    get_active_profile,
    get_configuration_summary,
    remove_ui_configuration,
    save_ui_configuration,
    test_candidate_connection,
)
from bookmark_manager.services.deletion import delete_local_bookmark, delete_local_bookmarks
from bookmark_manager.services.import_export import (
    BookmarkImportError,
    export_bookmarks_json,
    import_bookmarks_document,
    import_bookmarks_text,
)
from bookmark_manager.services.logging_events import get_logger, log_event, logging_context
from bookmark_manager.services.notifications import (
    list_notification_payloads,
    mark_all_notifications_read,
    mark_notification_read,
    publish_notification,
    unread_notification_count,
)
from bookmark_manager.services.web_bookmarks import (
    WebBookmarkError,
    canonicalize_web_url,
    rename_bookmark_category,
)

logger = get_logger("web")


def _logged_view(function):
    """Record safe action routing and every unexpected view failure, not poll traffic."""

    @wraps(function)
    def invoke(request, *args, **kwargs):
        operation = function.__name__
        quiet = request.method == "GET" or operation == "tick_refresh_schedule"
        context = {"operation": operation}
        if operation in {
            "bookmark_link",
            "open_bookmark",
            "update_organisation",
            "toggle_favorite",
            "toggle_pin",
            "delete_bookmark",
            "open_parent",
        }:
            bookmark_id = kwargs.get("pk", args[0] if args else None)
            if isinstance(bookmark_id, int) and not isinstance(bookmark_id, bool):
                context["bookmark_id"] = bookmark_id
        if not quiet:
            log_event(logger, logging.DEBUG, "bookmark_web_requested", **context)
        try:
            with logging_context(**context):
                response = function(request, *args, **kwargs)
        except Http404 as error:
            log_event(
                logger,
                logging.WARNING,
                "bookmark_web_rejected",
                error=error,
                reason="not_found",
                **context,
            )
            raise
        except Exception as error:
            log_event(logger, logging.ERROR, "bookmark_web_failed", error=error, **context)
            raise
        if response.status_code >= 500:
            log_event(
                logger,
                logging.ERROR,
                "bookmark_web_error_response",
                http_status=response.status_code,
                **context,
            )
        elif not quiet:
            log_event(
                logger,
                logging.WARNING if response.status_code >= 400 else logging.DEBUG,
                "bookmark_web_response",
                http_status=response.status_code,
                **context,
            )
        return response

    return invoke


@dataclass(slots=True)
class BookmarkTreeItem:
    """One template-safe node in the reconstructed local hierarchy."""

    node: ConfluencePageNode
    bookmark: Bookmark | None
    children: list[BookmarkTreeItem] = field(default_factory=list)
    selected: bool = False
    located: bool = False
    matches: bool = True
    depth: int = 1
    outline_number: str = ""
    subtree_open_count: int = 0


@dataclass(frozen=True, slots=True)
class BreadcrumbItem:
    title: str
    url: str
    is_leaf: bool = False


@dataclass(frozen=True, slots=True)
class FlatBookmarkItem:
    bookmark: Bookmark
    breadcrumb: tuple[BreadcrumbItem, ...]


@dataclass(frozen=True, slots=True)
class ManualFolderTreeItem:
    """One OWL-owned folder and its currently visible bookmark rows."""

    folder: BookmarkFolder
    items: tuple[BookmarkTreeItem, ...]
    bookmark_count: int
    open_count: int


@dataclass(frozen=True, slots=True)
class BookmarkSortControl:
    """One compact two-state sort link rendered above the bookmark tree."""

    key: str
    label: str
    href: str
    active: bool
    direction: str
    aria_label: str


_BOOKMARK_SORT_CONTROL_DEFINITIONS = (
    (
        "open_count",
        "Open count",
        BookmarkSort.MOST_OPENED,
        BookmarkSort.LEAST_OPENED,
        "highest first",
        "lowest first",
    ),
    (
        "updated",
        "Confluence updated date",
        BookmarkSort.UPDATED_NEWEST,
        BookmarkSort.UPDATED_OLDEST,
        "newest first",
        "oldest first",
    ),
    (
        "created",
        "Confluence written date",
        BookmarkSort.CREATED_NEWEST,
        BookmarkSort.CREATED_OLDEST,
        "newest first",
        "oldest first",
    ),
    (
        "saved",
        "OWL saved date",
        BookmarkSort.ADDED_NEWEST,
        BookmarkSort.ADDED_OLDEST,
        "newest first",
        "oldest first",
    ),
)


@dataclass(slots=True)
class ConfluenceContactSummary:
    """One real Confluence contributor with page-level role counts."""

    name: str
    written_page_ids: set[int] = field(default_factory=set)
    updated_page_ids: set[int] = field(default_factory=set)
    selected: bool = False

    @property
    def page_count(self) -> int:
        return len(self.written_page_ids | self.updated_page_ids)

    @property
    def written_count(self) -> int:
        return len(self.written_page_ids)

    @property
    def updated_count(self) -> int:
        return len(self.updated_page_ids)


@dataclass(frozen=True, slots=True)
class BookmarkTimelineGroup:
    """One saved-date heading and its bookmark links."""

    key: str
    label: str
    bookmarks: tuple[Bookmark, ...]


@dataclass(frozen=True, slots=True)
class BookmarkTimelinePage:
    """One bounded page of the chronological bookmark navigation."""

    groups: tuple[BookmarkTimelineGroup, ...]
    number: int
    page_count: int
    total_count: int
    first_item_number: int
    last_item_number: int

    @property
    def has_previous(self) -> bool:
        return self.number > 1

    @property
    def has_next(self) -> bool:
        return self.number < self.page_count


def _bookmark_timeline_groups(
    bookmarks: Iterable[Bookmark],
    *,
    at=None,
) -> tuple[BookmarkTimelineGroup, ...]:
    """Group this year's bookmarks by month and older bookmarks by year."""

    current_year = timezone.localtime(at or timezone.now()).year
    grouped: dict[str, tuple[str, list[Bookmark]]] = {}
    ordered_bookmarks = sorted(
        bookmarks,
        key=lambda bookmark: (bookmark.saved_at, bookmark.pk),
        reverse=True,
    )
    for bookmark in ordered_bookmarks:
        saved_at = timezone.localtime(bookmark.saved_at)
        if saved_at.year == current_year:
            key = f"month-{saved_at.year}-{saved_at.month:02d}"
            label = saved_at.strftime("%B")
        else:
            key = f"year-{saved_at.year}"
            label = str(saved_at.year)
        if key not in grouped:
            grouped[key] = (label, [])
        grouped[key][1].append(bookmark)
    return tuple(
        BookmarkTimelineGroup(key=key, label=label, bookmarks=tuple(items))
        for key, (label, items) in grouped.items()
    )


def _bookmark_timeline_page(
    bookmarks: Iterable[Bookmark],
    *,
    page_number: int,
    at=None,
    page_size: int = 100,
) -> BookmarkTimelinePage:
    """Return a newest-first bounded timeline page for a large bookmark library."""

    if page_size < 1:
        raise ValueError("page_size must be positive")
    ordered = sorted(
        bookmarks,
        key=lambda bookmark: (bookmark.saved_at, bookmark.pk),
        reverse=True,
    )
    total_count = len(ordered)
    page_count = max(1, (total_count + page_size - 1) // page_size)
    safe_number = min(max(page_number, 1), page_count)
    start = (safe_number - 1) * page_size
    end = min(start + page_size, total_count)
    return BookmarkTimelinePage(
        groups=_bookmark_timeline_groups(ordered[start:end], at=at),
        number=safe_number,
        page_count=page_count,
        total_count=total_count,
        first_item_number=start + 1 if total_count else 0,
        last_item_number=end,
    )


def _is_loopback_request(request: HttpRequest) -> bool:
    candidate = request.META.get("REMOTE_ADDR", "")
    try:
        return ipaddress.ip_address(candidate.split("%", maxsplit=1)[0]).is_loopback
    except ValueError:
        return False


def _require_local_action(request: HttpRequest) -> HttpResponse | None:
    if settings.OWL_ALLOW_NON_LOOPBACK or _is_loopback_request(request):
        return None
    log_event(logger, logging.WARNING, "bookmark_web_rejected", reason="non_loopback")
    return HttpResponseForbidden("This action is available only from the local OWL application.")


def _action_is_rate_limited(request: HttpRequest, action: str) -> bool:
    cooldown = settings.CONFLUENCE_ACTION_COOLDOWN_SECONDS
    if cooldown <= 0:
        return False
    remote_address = request.META.get("REMOTE_ADDR", "unknown")
    key = f"owl:local-action:{action}:{remote_address}"
    limited = not cache.add(key, True, timeout=cooldown)
    if limited:
        log_event(logger, logging.WARNING, "bookmark_web_rejected", reason="rate_limited")
    return limited


def _is_async_form(request: HttpRequest) -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _action_json(
    *,
    state: str,
    label: str,
    detail: str,
    status: int,
) -> JsonResponse:
    return JsonResponse(
        {"state": state, "label": label, "detail": detail},
        status=status,
    )


def _configuration_record() -> ConfluenceConfiguration | None:
    try:
        return ConfluenceConfiguration.objects.filter(pk=1).first()
    except (OperationalError, ProgrammingError) as error:
        log_event(
            logger,
            logging.ERROR,
            "bookmark_configuration_read_failed",
            error=error,
            stage="configuration_record",
        )
        return None


def _new_settings_form(
    summary: ConfigurationSummary,
    *,
    data=None,
) -> ConfluenceSettingsForm:
    record = None if summary.managed_externally else _configuration_record()
    current_base_url = record.base_url if record else ""
    initial = {
        "base_url": current_base_url,
        "auth_mode": record.auth_mode if record else "bearer",
    }
    return ConfluenceSettingsForm(
        data=data,
        initial=initial,
        current_base_url=current_base_url,
        has_stored_credential=(summary.has_stored_credential and not summary.managed_externally),
        managed_externally=summary.managed_externally,
    )


def _tree_items(
    query_result: BookmarkQueryResult,
    *,
    selected_pk: int | None,
    located_pk: int | None,
) -> tuple[list[BookmarkTreeItem], dict[int, str]]:
    manually_filed_node_ids = {
        bookmark.tree_node_id
        for bookmark in query_result.bookmarks
        if bookmark.manual_folder_id is not None
    }
    canonical_matched_node_ids = query_result.matched_node_ids - manually_filed_node_ids
    all_nodes = list(
        ConfluencePageNode.objects.select_related(
            "bookmark",
            "parent",
        ).order_by("parent_id", "outline_position", "sibling_position", "title", "id")
    )
    parent_ids = {node.pk: node.parent_id for node in all_nodes}
    canonical_visible_node_ids: set[int] = set()
    for matched_node_id in canonical_matched_node_ids:
        current_id: int | None = matched_node_id
        branch_seen: set[int] = set()
        while current_id is not None and current_id in parent_ids and current_id not in branch_seen:
            branch_seen.add(current_id)
            canonical_visible_node_ids.add(current_id)
            current_id = parent_ids[current_id]
    outline_numbers = outline_number_map(all_nodes)
    subtree_open_counts = _subtree_open_counts(all_nodes)
    visible_nodes = [node for node in all_nodes if node.pk in canonical_visible_node_ids]

    items_by_id: dict[int, BookmarkTreeItem] = {}
    for node in visible_nodes:
        try:
            node_bookmark = node.bookmark
        except Bookmark.DoesNotExist:
            node_bookmark = None
        # Ancestors remain visible so a filtered result keeps its full breadcrumb,
        # but an ancestor that did not match is folder context rather than a result.
        bookmark = (
            node_bookmark
            if node.pk in canonical_matched_node_ids and node_bookmark is not None
            else None
        )
        items_by_id[node.pk] = BookmarkTreeItem(
            node=node,
            bookmark=bookmark,
            selected=bool(bookmark and bookmark.pk == selected_pk),
            located=bool(bookmark and bookmark.pk == located_pk),
            matches=node.pk in canonical_matched_node_ids,
            outline_number=outline_numbers.get(node.pk, ""),
            subtree_open_count=subtree_open_counts.get(node.pk, 0),
        )

    roots: list[BookmarkTreeItem] = []
    for node in visible_nodes:
        item = items_by_id[node.pk]
        parent_item = items_by_id.get(node.parent_id)
        if parent_item is None:
            roots.append(item)
        else:
            item.depth = parent_item.depth + 1
            parent_item.children.append(item)

    match_rank = {
        bookmark.tree_node_id: rank
        for rank, bookmark in enumerate(query_result.bookmarks)
        if bookmark.manual_folder_id is None
    }
    fallback_rank = len(match_rank)

    def sort_branch(item: BookmarkTreeItem) -> int:
        child_ranks = [(sort_branch(child), child) for child in item.children]
        child_ranks.sort(key=lambda ranked: (ranked[0], ranked[1].node.pk))
        item.children[:] = [child for _rank, child in child_ranks]
        ranks = [rank for rank, _child in child_ranks]
        if item.node.pk in match_rank:
            ranks.append(match_rank[item.node.pk])
        rank = min(ranks, default=fallback_rank)
        return rank

    root_ranks = [(sort_branch(item), item) for item in roots]
    root_ranks.sort(key=lambda ranked: (ranked[0], ranked[1].node.pk))
    roots[:] = [item for _rank, item in root_ranks]
    return roots, outline_numbers


def _manual_folder_items(
    query_result: BookmarkQueryResult,
    *,
    outline_numbers: dict[int, str],
    selected_pk: int | None,
    located_pk: int | None,
) -> tuple[ManualFolderTreeItem, ...]:
    """Compose personal folders without changing the canonical Confluence tree."""

    bookmarks_by_folder: dict[int, list[BookmarkTreeItem]] = {}
    for bookmark in query_result.bookmarks:
        if bookmark.manual_folder_id is None:
            continue
        bookmarks_by_folder.setdefault(bookmark.manual_folder_id, []).append(
            BookmarkTreeItem(
                node=bookmark.tree_node,
                bookmark=bookmark,
                selected=bookmark.pk == selected_pk,
                located=bookmark.pk == located_pk,
                depth=2,
                outline_number=outline_numbers.get(bookmark.tree_node_id, str(bookmark.pk)),
                subtree_open_count=bookmark.open_count,
            )
        )

    show_empty_folders = query_result.active_filter_count == 0
    folders: list[ManualFolderTreeItem] = []
    for folder in BookmarkFolder.objects.order_by("normalized_name", "id"):
        items = tuple(bookmarks_by_folder.get(folder.pk, ()))
        if not items and not show_empty_folders:
            continue
        folders.append(
            ManualFolderTreeItem(
                folder=folder,
                items=items,
                bookmark_count=len(items),
                open_count=sum(item.bookmark.open_count for item in items if item.bookmark),
            )
        )
    return tuple(folders)


def _subtree_open_counts(nodes: Iterable[ConfluencePageNode]) -> dict[int, int]:
    """Roll every saved page's open count into its complete ancestor path.

    ``_tree_items`` supplies nodes loaded with ``select_related("bookmark")``.
    This bottom-up pass therefore adds no database queries, and it deliberately
    uses the complete local tree so a collapsed or filtered folder still reports
    the total clicks from every bookmarked page beneath it.
    """

    node_list = list(nodes)
    node_ids = {node.pk for node in node_list}
    parent_ids: dict[int, int | None] = {}
    remaining_children = {node.pk: 0 for node in node_list}
    totals: dict[int, int] = {}

    for node in node_list:
        parent_id = node.parent_id if node.parent_id in node_ids else None
        parent_ids[node.pk] = parent_id
        if parent_id is not None:
            remaining_children[parent_id] += 1
        try:
            bookmark = node.bookmark
        except Bookmark.DoesNotExist:
            totals[node.pk] = 0
        else:
            totals[node.pk] = bookmark.open_count

    pending = [node_id for node_id, child_count in remaining_children.items() if child_count == 0]
    while pending:
        node_id = pending.pop()
        parent_id = parent_ids[node_id]
        if parent_id is None:
            continue
        totals[parent_id] += totals[node_id]
        remaining_children[parent_id] -= 1
        if remaining_children[parent_id] == 0:
            pending.append(parent_id)

    return totals


def _attach_outline_numbers(
    bookmarks: Iterable[Bookmark],
    outline_numbers: dict[int, str],
) -> None:
    """Expose one consistent display number on every template bookmark instance."""

    for bookmark in bookmarks:
        bookmark.outline_number = outline_numbers.get(bookmark.tree_node_id, str(bookmark.pk))


def _confluence_contacts(
    selected_people: Iterable[str] = (),
) -> tuple[ConfluenceContactSummary, ...]:
    """Summarize saved Confluence creators/authors/modifiers without double-counting pages."""

    contacts: dict[str, ConfluenceContactSummary] = {}

    def record(name: str, bookmark_id: int, *, role: str) -> None:
        display_name = name.strip()
        if not display_name:
            return
        key = display_name.casefold()
        contact = contacts.setdefault(key, ConfluenceContactSummary(name=display_name))
        if role == "written":
            contact.written_page_ids.add(bookmark_id)
        else:
            contact.updated_page_ids.add(bookmark_id)

    bookmarks = Bookmark.objects.filter(source_type=BookmarkSource.CONFLUENCE).only(
        "id",
        "author_name",
        "created_by_name",
        "modified_by_name",
    )
    for bookmark in bookmarks:
        seen_writers: set[str] = set()
        for writer_name in (bookmark.author_name, bookmark.created_by_name):
            writer_key = writer_name.strip().casefold()
            if not writer_key or writer_key in seen_writers:
                continue
            seen_writers.add(writer_key)
            record(writer_name, bookmark.pk, role="written")
        record(bookmark.modified_by_name, bookmark.pk, role="updated")

    selected_keys = {name.strip().casefold() for name in selected_people if name.strip()}
    for key, contact in contacts.items():
        contact.selected = key in selected_keys
    return tuple(
        sorted(
            contacts.values(),
            key=lambda contact: (-contact.page_count, contact.name.casefold()),
        )
    )


def _positive_query_integer(request: HttpRequest, name: str) -> int | None:
    value = request.GET.get(name, "")
    if not value.isdecimal():
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def _query_from_filter_form(form: BookmarkFilterForm) -> BookmarkQuery:
    if not form.is_valid():
        raise InvalidBookmarkQuery("Review the highlighted bookmark filters.")
    values = form.cleaned_data
    date_filters = ()
    if values.get("date_field") and values.get("date_preset") != "any_time":
        date_filters = (
            BookmarkDateFilter(
                field=values["date_field"],
                preset=values["date_preset"],
                start=values.get("date_from"),
                end=values.get("date_to"),
            ),
        )
    return BookmarkQuery(
        search=values.get("q", ""),
        favorite=True if values.get("favorite") else None,
        pinned=True if values.get("pinned") else None,
        tags=tuple(values.get("tags", ())),
        people=tuple(values.get("person", ())),
        spaces=(values["space"],) if values.get("space") else (),
        category_ids=(values["category"],) if values.get("category") else (),
        availability=tuple(values.get("availability", ())),
        recency=tuple(values.get("recency", ())),
        changed_since_viewed=True if values.get("changed") else None,
        dates=date_filters,
        open_count_min=values.get("min_open"),
        open_count_max=values.get("max_open"),
        broken=True if values.get("broken") else None,
        sort=values.get("sort") or BookmarkSort.ADDED_NEWEST,
    )


def _filter_form_initial(query: BookmarkQuery) -> dict[str, object]:
    initial: dict[str, object] = {
        "q": query.search,
        "favorite": query.favorite is True,
        "pinned": query.pinned is True,
        "tags": [Tag.normalize_name(value) for value in query.tags],
        "person": list(query.people),
        "space": query.spaces[0] if query.spaces else "",
        "category": query.category_ids[0] if query.category_ids else None,
        "availability": list(query.availability),
        "recency": [value.value for value in query.recency],
        "changed": query.changed_since_viewed is True,
        "min_open": query.open_count_min,
        "max_open": query.open_count_max,
        "broken": query.broken is True,
        "sort": query.sort.value,
        "date_preset": "any_time",
    }
    if query.dates:
        date_filter = query.dates[0]
        initial.update(
            {
                "date_field": date_filter.field.value,
                "date_preset": date_filter.preset.value,
                "date_from": date_filter.start,
                "date_to": date_filter.end,
            }
        )
    return initial


def _resolve_bookmark_query(
    request: HttpRequest,
) -> tuple[BookmarkQuery, BookmarkFilterForm, SavedBookmarkView | None, str]:
    saved_view = None
    view_pk = _positive_query_integer(request, "saved_view")
    if view_pk:
        saved_view = SavedBookmarkView.objects.filter(pk=view_pk).first()
    try:
        if saved_view is not None:
            query = BookmarkQuery.from_saved_view(saved_view)
            if request.GET.get("people_filter") == "1":
                try:
                    requested_people = BookmarkFilterForm.base_fields["person"].clean(
                        request.GET.getlist("person")
                    )
                except ValidationError as exc:
                    raise InvalidBookmarkQuery(exc.messages[0]) from exc
                query = replace(query, people=requested_people)
            explicit_sort = request.GET.get("sort", "").strip()
            if explicit_sort:
                query = replace(query, sort=explicit_sort)
            form = BookmarkFilterForm(initial=_filter_form_initial(query))
        elif not request.GET:
            query = BookmarkQuery()
            form = BookmarkFilterForm(initial=_filter_form_initial(query))
        else:
            form = BookmarkFilterForm(request.GET or None)
            query = _query_from_filter_form(form)
        if request.GET.get("timeline") == "1":
            query = replace(query, sort=BookmarkSort.ADDED_NEWEST)
            form = BookmarkFilterForm(initial=_filter_form_initial(query))
        return query, form, saved_view, ""
    except InvalidBookmarkQuery as exc:
        fallback = BookmarkQuery()
        return fallback, BookmarkFilterForm(initial=_filter_form_initial(fallback)), None, str(exc)


def _clean_current_query_string(request: HttpRequest) -> str:
    values = request.GET.copy()
    for key in (
        "selected",
        "located",
        "saved",
        "similar",
        "opened",
        "open_error",
        "action",
        "import_run",
    ):
        values.pop(key, None)
    return values.urlencode()


def _people_filter_navigation(
    request: HttpRequest,
    query: BookmarkQuery,
) -> tuple[tuple[tuple[str, str], ...], str]:
    """Preserve active filters while replacing only the selected people."""

    values = request.GET.copy()
    for key in (
        "person",
        "people_filter",
        "selected",
        "located",
        "saved",
        "similar",
        "opened",
        "open_error",
        "open_parent_error",
        "action",
        "count",
        "import_run",
        "timeline",
        "timeline_page",
    ):
        values.pop(key, None)
    values["people_filter"] = "1"
    if not values.get("sort"):
        values["sort"] = (
            query.sort.value if request.GET.get("saved_view") else BookmarkSort.UPDATED_NEWEST.value
        )
    hidden_fields = tuple(
        (name, value) for name, field_values in values.lists() for value in field_values
    )
    clear_query = values.urlencode()
    clear_href = reverse("bookmark_manager:index")
    if clear_query:
        clear_href = f"{clear_href}?{clear_query}"
    return hidden_fields, clear_href


def _bookmark_sort_href(request: HttpRequest, sort: BookmarkSort) -> str:
    """Return an index link that changes only sorting, not filters or selection."""

    values = request.GET.copy()
    for key in (
        "saved",
        "similar",
        "opened",
        "open_error",
        "open_parent_error",
        "action",
        "count",
        "import_run",
        "timeline",
        "timeline_page",
    ):
        values.pop(key, None)
    values["sort"] = sort.value
    return f"{reverse('bookmark_manager:index')}?{values.urlencode()}"


def _bookmark_sort_controls(
    request: HttpRequest,
    query: BookmarkQuery,
) -> tuple[BookmarkSortControl, ...]:
    """Build the four icon toggles, defaulting inactive controls to descending."""

    controls: list[BookmarkSortControl] = []
    for (
        key,
        label,
        descending,
        ascending,
        descending_label,
        ascending_label,
    ) in _BOOKMARK_SORT_CONTROL_DEFINITIONS:
        if query.sort == descending:
            active = True
            direction = "descending"
            next_sort = ascending
            next_direction_label = ascending_label
        elif query.sort == ascending:
            active = True
            direction = "ascending"
            next_sort = descending
            next_direction_label = descending_label
        else:
            active = False
            direction = "inactive"
            next_sort = descending
            next_direction_label = descending_label
        controls.append(
            BookmarkSortControl(
                key=key,
                label=label,
                href=_bookmark_sort_href(request, next_sort),
                active=active,
                direction=direction,
                aria_label=f"Sort by {label}, {next_direction_label}",
            )
        )
    return tuple(controls)


def _timeline_query_string(request: HttpRequest) -> str:
    """Keep active constraints while switching a timeline selection back to the tree."""

    values = request.GET.copy()
    for key in (
        "selected",
        "located",
        "saved",
        "similar",
        "opened",
        "open_error",
        "action",
        "import_run",
        "sort",
        "timeline",
    ):
        values.pop(key, None)
    return values.urlencode()


def _timeline_paging_query_string(request: HttpRequest) -> str:
    """Keep the current result state while changing only the timeline page."""

    values = request.GET.copy()
    for key in (
        "selected",
        "located",
        "saved",
        "similar",
        "opened",
        "open_error",
        "action",
        "import_run",
        "timeline_page",
    ):
        values.pop(key, None)
    return values.urlencode()


def _bookmark_breadcrumb(bookmark: Bookmark | None) -> tuple[BreadcrumbItem, ...]:
    if bookmark is None:
        return ()
    items = [BreadcrumbItem(bookmark.title, bookmark.url, is_leaf=True)]
    parent_id = bookmark.tree_node.parent_id
    visited_node_ids = {bookmark.tree_node_id}
    while parent_id is not None and parent_id not in visited_node_ids:
        visited_node_ids.add(parent_id)
        node = ConfluencePageNode.objects.only("id", "parent_id", "title", "url").get(pk=parent_id)
        items.append(BreadcrumbItem(node.title, node.url))
        parent_id = node.parent_id
    items.reverse()
    return tuple(items)


def _search_reveal_candidate(
    query: BookmarkQuery,
    result: BookmarkQueryResult,
) -> Bookmark | None:
    if not query.search:
        return None
    needle = query.search.casefold()
    for bookmark in result.bookmarks:
        if needle in {
            bookmark.title.casefold(),
            bookmark.url.casefold(),
            bookmark.page_id.casefold(),
            str(bookmark.pk),
        }:
            return bookmark
    return result.bookmarks[0] if len(result.bookmarks) == 1 else None


def _active_bookmark_section(request: HttpRequest, *, open_settings: bool) -> str:
    """Return the exact Bookmark Manager sidebar destination for this request."""

    if open_settings:
        return "settings"
    category_pk = _positive_query_integer(request, "category")
    if category_pk:
        return f"category-{category_pk}"
    if request.GET.get("favorite") == "on":
        return "favorites"
    if request.GET.get("pinned") == "on":
        return "pinned"
    if request.GET.getlist("availability") == [BookmarkAvailability.NOT_FOUND]:
        return "deleted"
    if request.GET.get("max_open") == "0":
        return "never"
    if request.GET.get("sort") == BookmarkSort.RECENTLY_OPENED:
        return "recent"
    if request.GET.get("sort") == BookmarkSort.MOST_OPENED:
        return "frequent"
    return "all"


def _global_bookmark_counts() -> dict[str, int]:
    """Return stable sidebar counts that do not change with the active shortcut."""

    return Bookmark.objects.aggregate(
        all_bookmarks=Count("pk"),
        favorites=Count("pk", filter=Q(favorite=True)),
        pinned=Count("pk", filter=Q(pinned=True)),
        viewed=Count("pk", filter=Q(open_count__gte=1)),
        never_viewed=Count("pk", filter=Q(open_count=0)),
        deleted_pages=Count(
            "pk",
            filter=Q(availability_status=BookmarkAvailability.NOT_FOUND),
        ),
    )


def _bookmark_categories_with_totals():
    """Return every domain with its total saved bookmarks, independent of filters."""

    return BookmarkCategory.objects.annotate(total_bookmarks=Count("bookmarks"))


def _refresh_run_payload(
    run: BookmarkRefreshRun | None,
    completed: BookmarkRefreshRun | None,
) -> dict[str, object]:
    active = bool(run and run.is_active)
    processed = run.processed_bookmarks if run else 0
    total = run.total_bookmarks if run else 0
    progress = round((processed / total) * 100) if total else (0 if active else 100)
    failures = (
        tuple(
            {
                "bookmark_id": failure.bookmark_id,
                "page_id": failure.page_id,
                "url": failure.url,
                "reason": failure.reason,
                "error_code": failure.error_code,
                "attempts": failure.attempt_count,
            }
            for failure in run.failures.all()
        )
        if run is not None
        else ()
    )
    return {
        "run_id": run.pk if run else None,
        "status": run.status if run else "idle",
        "status_label": run.get_status_display() if run else "Ready",
        "active": active,
        "total": total,
        "processed": processed,
        "succeeded": run.succeeded_bookmarks if run else 0,
        "failed": run.failed_bookmarks if run else 0,
        "failures": failures,
        "progress": progress,
        "requested_at": run.requested_at.isoformat() if run else None,
        "started_at": run.started_at.isoformat() if run and run.started_at else None,
        "heartbeat_at": run.heartbeat_at.isoformat() if run and run.heartbeat_at else None,
        "completed_at": run.completed_at.isoformat() if run and run.completed_at else None,
        "completed_display": (
            timezone.localtime(run.completed_at).strftime("%d %b %Y, %H:%M:%S %Z")
            if run is not None and run.completed_at is not None
            else ""
        ),
        "last_completed_at": (
            completed.completed_at.isoformat()
            if completed is not None and completed.completed_at is not None
            else None
        ),
        "last_completed_display": (
            timezone.localtime(completed.completed_at).strftime("%d %b %Y, %H:%M:%S %Z")
            if completed is not None and completed.completed_at is not None
            else ""
        ),
        "last_completed_status": completed.status if completed else "",
        "trigger": run.trigger if run else "",
        "detail": run.last_error_message if run else "",
    }


def _publish_import_completion(run: BookmarkImportRun) -> None:
    has_failures = bool(run.failed_records)
    publish_notification(
        event_key=f"bookmark-import:{run.pk}",
        kind=NotificationKind.BOOKMARK_IMPORT,
        state=NotificationState.WARNING if has_failures else NotificationState.SUCCESS,
        title=(
            "Bookmark import completed with issues" if has_failures else "Bookmark import completed"
        ),
        message=run.outcome,
        target_path=(f"{reverse('bookmark_manager:index')}?{urlencode({'import_run': run.pk})}"),
        occurred_at=run.completed_at or timezone.now(),
    )


def _index_context(
    request: HttpRequest,
    *,
    bookmark_form: BookmarkInputForm | None = None,
    import_form: BookmarkImportForm | None = None,
    settings_form: ConfluenceSettingsForm | None = None,
    open_settings: bool = False,
    inline_error: str = "",
) -> dict[str, object]:
    summary = get_configuration_summary()
    query, filter_form, active_saved_view, filter_error = _resolve_bookmark_query(request)
    people_filter_hidden_fields, people_filter_clear_href = _people_filter_navigation(
        request,
        query,
    )
    query_result = query_bookmarks(query)
    search_term = query.search
    url_search_active = False
    if search_term:
        try:
            canonicalize_web_url(search_term)
        except WebBookmarkError:
            pass
        else:
            url_search_active = True
    url_identity_matches = matching_bookmarks_for_url(search_term) if url_search_active else ()
    first_url_identity_match = url_identity_matches[0] if url_identity_matches else None
    first_url_identity_match_href = (
        f"{reverse('bookmark_manager:index')}?"
        f"{urlencode({'selected': first_url_identity_match.pk, 'located': first_url_identity_match.pk})}"
        if first_url_identity_match is not None
        else ""
    )
    selected_pk = _positive_query_integer(request, "selected")
    located_pk = _positive_query_integer(request, "located")
    selected = (
        Bookmark.objects.select_related("tree_node", "category", "manual_folder")
        .prefetch_related("tags")
        .filter(pk=selected_pk)
        .first()
        if selected_pk
        else None
    )
    reveal_candidate = _search_reveal_candidate(query, query_result) if selected is None else None
    if reveal_candidate is not None:
        selected = reveal_candidate
        selected_pk = reveal_candidate.pk
        located_pk = reveal_candidate.pk
    if selected is None:
        selected_pk = None
        located_pk = None

    tree_items, outline_numbers = _tree_items(
        query_result,
        selected_pk=selected_pk,
        located_pk=located_pk,
    )
    manual_folder_items = _manual_folder_items(
        query_result,
        outline_numbers=outline_numbers,
        selected_pk=selected_pk,
        located_pk=located_pk,
    )
    similar_bookmarks = (
        find_similar_title_bookmarks(selected.title, exclude_page_id=selected.page_id)
        if selected and request.GET.get("similar") == "1"
        else ()
    )
    numbered_bookmarks = list(query_result.bookmarks)
    if selected is not None:
        numbered_bookmarks.append(selected)
    numbered_bookmarks.extend(similar_bookmarks)
    _attach_outline_numbers(numbered_bookmarks, outline_numbers)
    timeline_page = _bookmark_timeline_page(
        query_result.bookmarks,
        page_number=_positive_query_integer(request, "timeline_page") or 1,
    )

    last_import_run = None
    import_run_pk = _positive_query_integer(request, "import_run")
    if import_run_pk:
        last_import_run = (
            BookmarkImportRun.objects.prefetch_related("failures").filter(pk=import_run_pk).first()
        )

    effective_error = inline_error or filter_error
    status_message = (
        f"Ready · {query_result.matching_count} of {query_result.counts.all_bookmarks} bookmarks"
    )
    if request.GET.get("saved") == "new" and selected:
        category_name = selected.category.name if selected.category else "Uncategorised"
        category_domain = selected.category.domain if selected.category else "No domain"
        status_message = (
            f"Added “{selected.title}”. "
            f"Saved under {category_domain} as bookmark {selected.outline_number}. "
            f"Category: {category_name}."
        )
    elif request.GET.get("saved") == "existing" and selected:
        category_name = selected.category.name if selected.category else "Uncategorised"
        category_domain = selected.category.domain if selected.category else "No domain"
        status_message = (
            f"“{selected.title}” is already saved. "
            f"Saved under {category_domain} as bookmark {selected.outline_number}. "
            f"Category: {category_name}."
        )
    elif request.GET.get("opened") == "1" and selected:
        status_message = f"Opened bookmark {selected.outline_number}"
    elif last_import_run:
        status_message = last_import_run.outcome
    elif request.GET.get("action") == "deleted":
        try:
            deleted_count = max(1, int(request.GET.get("count", "1")))
        except (TypeError, ValueError):
            deleted_count = 1
        status_message = (
            f"{deleted_count} bookmark{'s' if deleted_count != 1 else ''} deleted from OWL"
            " · Confluence was not changed"
        )
    elif request.GET.get("action") == "view_saved":
        status_message = "Saved bookmark view updated"
    elif request.GET.get("action") == "view_deleted":
        status_message = "Saved bookmark view deleted · Bookmarks were not changed"
    elif request.GET.get("action") == "organised" and selected:
        status_message = f"Notes and tags saved for bookmark {selected.outline_number}"
    elif request.GET.get("action") == "favorite" and selected:
        status_message = "Added to favorites" if selected.favorite else "Removed from favorites"
    elif request.GET.get("action") == "pinned" and selected:
        status_message = (
            "Added to pinned bookmarks" if selected.pinned else "Removed from pinned bookmarks"
        )
    elif request.GET.get("action") == "folder_created":
        folder = BookmarkFolder.objects.filter(
            pk=_positive_query_integer(request, "folder")
        ).first()
        status_message = (
            f"Created personal folder “{folder.name}”"
            if folder is not None
            else "Personal folder created"
        )
    elif request.GET.get("action") == "folder_moved":
        folder = BookmarkFolder.objects.filter(
            pk=_positive_query_integer(request, "folder")
        ).first()
        status_message = (
            f"Moved bookmarks to personal folder “{folder.name}”"
            if folder is not None
            else "Returned bookmarks to their Confluence hierarchy"
        )
    elif effective_error:
        status_message = effective_error

    selected_breadcrumb = _bookmark_breadcrumb(selected)
    tag_text = ", ".join(tag.name for tag in selected.tags.all()) if selected else ""

    refresh_run, completed_refresh = refresh_status_snapshot()
    requested_refresh_run = None
    requested_refresh_run_pk = _positive_query_integer(request, "refresh_run")
    if requested_refresh_run_pk:
        requested_refresh_run = (
            BookmarkRefreshRun.objects.prefetch_related("failures")
            .filter(pk=requested_refresh_run_pk)
            .first()
        )
    refresh_result_run = requested_refresh_run or refresh_run
    return {
        "active_nav": "bookmarks",
        "active_app": "bookmarks",
        "active_section": _active_bookmark_section(request, open_settings=open_settings),
        "page_title": "Bookmark Manager",
        "configuration": summary,
        "settings_form": settings_form or _new_settings_form(summary),
        "bookmark_form": bookmark_form or BookmarkInputForm(),
        "filter_form": filter_form,
        "saved_view_form": SavedBookmarkViewForm(),
        "bookmark_folder_form": BookmarkFolderForm(),
        "import_form": import_form or BookmarkImportForm(),
        "organisation_form": BookmarkOrganisationForm(
            initial={"notes": selected.notes, "tags": tag_text}
        )
        if selected
        else None,
        "tag_suggestions": tuple(
            Tag.objects.order_by("normalized_name").values_list("name", flat=True)
        )
        if selected
        else (),
        "search_term": search_term,
        "url_search_active": url_search_active,
        "url_identity_matches": url_identity_matches,
        "url_identity_match_count": len(url_identity_matches),
        "first_url_identity_match_href": first_url_identity_match_href,
        "bookmark_query": query,
        "sort_controls": _bookmark_sort_controls(request, query),
        "query_result": query_result,
        "bookmark_counts": _global_bookmark_counts(),
        "refresh_status": _refresh_run_payload(refresh_run, completed_refresh),
        "refresh_result_status": _refresh_run_payload(
            refresh_result_run,
            completed_refresh,
        ),
        "refresh_result_is_historical": bool(
            requested_refresh_run is not None
            and (refresh_run is None or requested_refresh_run.pk != refresh_run.pk)
        ),
        "active_filters": query_result.active_filters,
        "current_query_string": _clean_current_query_string(request),
        "timeline_query_string": _timeline_query_string(request),
        "timeline_paging_query_string": _timeline_paging_query_string(request),
        "active_saved_view": active_saved_view,
        "saved_views": SavedBookmarkView.objects.all(),
        "tree_items": tree_items,
        "manual_folder_items": manual_folder_items,
        "tree_has_items": bool(tree_items or manual_folder_items),
        "bookmark_folders": BookmarkFolder.objects.order_by("normalized_name", "id"),
        "flat_items": tuple(
            FlatBookmarkItem(bookmark, _bookmark_breadcrumb(bookmark))
            for bookmark in query_result.bookmarks
        ),
        "result_count": query_result.matching_count,
        "total_bookmarks": query_result.counts.all_bookmarks,
        "bookmark_categories": _bookmark_categories_with_totals(),
        "confluence_contacts": _confluence_contacts(query.people),
        "active_contact": query.people[0] if query.people else "",
        "active_contacts": query.people,
        "people_filter_hidden_fields": people_filter_hidden_fields,
        "people_filter_clear_href": people_filter_clear_href,
        "selected_category": BookmarkCategory.objects.filter(pk=query.category_ids[0]).first()
        if query.category_ids
        else None,
        "selected_bookmark": selected,
        "selected_outline_number": selected.outline_number if selected else "",
        "selected_breadcrumb": selected_breadcrumb,
        "selected_parent": selected_breadcrumb[-2] if len(selected_breadcrumb) > 1 else None,
        "similar_bookmarks": similar_bookmarks,
        "bookmark_timeline_groups": timeline_page.groups,
        "bookmark_timeline_page": timeline_page,
        "last_import_run": last_import_run,
        "open_settings": open_settings,
        "inline_error": effective_error,
        "status_message": status_message,
        "bookmark_save_status": (
            status_message if request.GET.get("saved") in {"new", "existing"} else ""
        ),
    }


@require_GET
@never_cache
@_logged_view
def index(request: HttpRequest) -> HttpResponse:
    """Render the searchable local bookmark tree and details pane."""

    return render(request, "bookmark_manager/index.html", _index_context(request))


@require_POST
@csrf_protect
@never_cache
@_logged_view
def start_global_refresh(request: HttpRequest) -> HttpResponse:
    """Queue one global refresh and return before the worker contacts Confluence."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error
    summary = get_configuration_summary()
    if not summary.complete:
        return JsonResponse(
            {
                "state": "configuration_required",
                "label": "Confluence connection required",
                "detail": "Save a complete Confluence connection before refreshing bookmarks.",
            },
            status=400,
        )
    run, created = create_or_get_refresh_run()
    if created:
        publish_refresh_notification(run)
        try:
            launch_refresh_worker(run.pk)
        except OSError as error:
            log_event(
                logger,
                logging.ERROR,
                "bookmark_refresh_wakeup_failed",
                error=error,
                run_id=run.pk,
                stage="worker_launch",
            )
            mark_refresh_launch_failed(run.pk)
            run.refresh_from_db()
            latest, completed = refresh_status_snapshot()
            return JsonResponse(
                {
                    "state": "worker_unavailable",
                    "label": "Refresh could not start",
                    "detail": run.last_error_message,
                    "refresh": _refresh_run_payload(latest, completed),
                },
                status=503,
            )
    latest, completed = refresh_status_snapshot()
    payload = _refresh_run_payload(latest, completed)
    response = JsonResponse(
        {
            "state": "queued" if created else "already_running",
            "label": "Refresh queued" if created else "Refresh already running",
            "detail": (
                "A background worker is retrieving current Confluence page details and text."
                if created
                else "The existing background refresh will continue."
            ),
            "refresh": payload,
        },
        status=202,
    )
    return response


@require_GET
@never_cache
@_logged_view
def global_refresh_status(request: HttpRequest) -> JsonResponse:
    """Expose compact, secret-free progress for the top-bar tracker."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error
    latest, completed = refresh_status_snapshot()
    return JsonResponse({"refresh": _refresh_run_payload(latest, completed)})


@require_GET
@never_cache
@_logged_view
def notifications_status(request: HttpRequest) -> JsonResponse:
    """Return read-only notification, refresh, and scheduler status for every app shell."""

    from bitbucket_search.services.repository_notifications import repository_notification_statuses

    local_error = _require_local_action(request)
    if local_error:
        return local_error
    latest = BookmarkRefreshRun.objects.prefetch_related("failures").first()
    completed = (
        BookmarkRefreshRun.objects.filter(
            status__in=("succeeded", "succeeded_with_errors"),
            completed_at__isnull=False,
        )
        .order_by("-completed_at", "-id")
        .first()
    )
    return JsonResponse(
        {
            "notifications": list_notification_payloads(),
            "unread_count": unread_notification_count(),
            "refresh": _refresh_run_payload(latest, completed),
            "schedule": refresh_schedule_snapshot(initialize=False),
            "repositoryStatuses": repository_notification_statuses(),
        }
    )


@require_POST
@csrf_protect
@never_cache
@_logged_view
def read_notification(request: HttpRequest) -> JsonResponse:
    local_error = _require_local_action(request)
    if local_error:
        return local_error
    raw_notification_id = request.POST.get("notification_id", "")
    if not raw_notification_id.isascii() or not raw_notification_id.isdigit():
        return JsonResponse({"detail": "Choose a valid notification."}, status=400)
    notification_id = int(raw_notification_id)
    if not Notification.objects.filter(pk=notification_id).exists():
        return JsonResponse({"detail": "That notification no longer exists."}, status=404)
    mark_notification_read(notification_id)
    return JsonResponse({"state": "read", "unread_count": unread_notification_count()})


@require_POST
@csrf_protect
@never_cache
@_logged_view
def read_all_notifications(request: HttpRequest) -> JsonResponse:
    local_error = _require_local_action(request)
    if local_error:
        return local_error
    marked = mark_all_notifications_read()
    return JsonResponse({"state": "read", "marked": marked, "unread_count": 0})


@require_POST
@csrf_protect
@never_cache
@_logged_view
def tick_refresh_schedule(request: HttpRequest) -> JsonResponse:
    """Catch up a due schedule while the resident scheduler or browser is running."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error
    run, queued = queue_due_scheduled_refresh()
    latest, completed = refresh_status_snapshot()
    return JsonResponse(
        {
            "state": "queued" if queued else "waiting",
            "run_id": run.pk if run is not None else None,
            "refresh": _refresh_run_payload(latest, completed),
            "schedule": refresh_schedule_snapshot(),
        },
        status=202 if queued else 200,
    )


@require_GET
@never_cache
@_logged_view
def settings_page(request: HttpRequest) -> HttpResponse:
    """Render a non-JavaScript fallback for the secure settings panel."""

    return render(request, "bookmark_manager/settings.html", _settings_page_context(request))


def _settings_page_context(
    request: HttpRequest,
    *,
    settings_form: ConfluenceSettingsForm | None = None,
    import_form: BookmarkImportForm | None = None,
    inline_error: str = "",
    status_message: str = "",
) -> dict[str, object]:
    """Build the full settings page, including the shared bookmark navigation."""

    summary = get_configuration_summary()
    bookmark_counts = _global_bookmark_counts()
    import_run = None
    import_run_pk = _positive_query_integer(request, "import_run")
    if import_run_pk:
        import_run = (
            BookmarkImportRun.objects.prefetch_related("failures").filter(pk=import_run_pk).first()
        )
    refresh_run, completed_refresh = refresh_status_snapshot()
    return {
        "active_nav": "bookmarks",
        "active_app": "bookmarks",
        "active_section": "settings",
        "page_title": "Confluence Settings",
        "configuration": summary,
        "settings_form": (
            settings_form if settings_form is not None else _new_settings_form(summary)
        ),
        "import_form": import_form if import_form is not None else BookmarkImportForm(),
        "last_import_run": import_run,
        "inline_error": inline_error,
        "bookmark_counts": bookmark_counts,
        "refresh_status": _refresh_run_payload(refresh_run, completed_refresh),
        "total_bookmarks": bookmark_counts["all_bookmarks"],
        "bookmark_categories": _bookmark_categories_with_totals(),
        "selected_category": None,
        "status_message": status_message or f"Confluence settings · {summary.label}",
    }


def _first_form_error(form: ConfluenceSettingsForm) -> str:
    for field_errors in form.errors.values():
        if field_errors:
            return str(field_errors[0])
    return "Review the highlighted settings and try again."


@require_POST
@csrf_protect
@sensitive_post_parameters("personal_access_token")
@never_cache
@_logged_view
def test_connection(request: HttpRequest) -> JsonResponse | HttpResponse:
    """Test one candidate profile without saving its URL or PAT."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error
    if _action_is_rate_limited(request, "test-connection"):
        return JsonResponse(
            {
                "state": "rate_limited",
                "label": "Please wait",
                "detail": "A connection test just ran. Wait briefly before trying again.",
                "verification_receipt": "",
            },
            status=429,
        )

    summary = get_configuration_summary()
    if summary.managed_externally:
        return JsonResponse(
            {
                "state": summary.state,
                "label": summary.label,
                "detail": summary.detail,
                "verification_receipt": "",
            },
            status=400,
        )
    form = _new_settings_form(summary, data=request.POST)
    if not form.is_valid():
        return JsonResponse(
            {
                "state": "configuration_error",
                "label": "Configuration error",
                "detail": _first_form_error(form),
                "verification_receipt": "",
            },
            status=400,
        )

    result = test_candidate_connection(
        base_url=form.cleaned_data["base_url"],
        personal_access_token=form.cleaned_data["personal_access_token"],
        auth_mode=form.cleaned_data["auth_mode"],
    )
    return JsonResponse(
        {
            "state": result.state,
            "label": result.label,
            "detail": result.detail,
            "verification_receipt": result.verification_receipt,
        },
        status=200 if result.success else 400,
    )


def _render_settings_failure(
    request: HttpRequest,
    form: ConfluenceSettingsForm,
    detail: str,
) -> HttpResponse:
    if _is_async_form(request):
        return _action_json(
            state="configuration_error",
            label="Not saved",
            detail=detail,
            status=400,
        )
    form.add_error(None, detail)
    if request.POST.get("return_to") == "settings":
        return render(
            request,
            "bookmark_manager/settings.html",
            _settings_page_context(
                request,
                settings_form=form,
                status_message=detail,
            ),
            status=400,
        )
    return render(
        request,
        "bookmark_manager/index.html",
        _index_context(
            request,
            settings_form=form,
            open_settings=True,
            inline_error=detail,
        ),
        status=400,
    )


@require_POST
@csrf_protect
@sensitive_post_parameters("personal_access_token")
@never_cache
@_logged_view
def save_settings(request: HttpRequest) -> HttpResponse:
    """Persist one UI-managed profile without ever placing its PAT in Django storage."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error
    if _action_is_rate_limited(request, "save-settings"):
        if _is_async_form(request):
            return _action_json(
                state="rate_limited",
                label="Please wait",
                detail="Wait briefly before saving settings again.",
                status=429,
            )
        return HttpResponse("Please wait briefly before saving settings again.", status=429)

    summary = get_configuration_summary()
    form = _new_settings_form(summary, data=request.POST)
    if not form.is_valid():
        return _render_settings_failure(request, form, _first_form_error(form))
    result = save_ui_configuration(
        base_url=form.cleaned_data["base_url"],
        personal_access_token=form.cleaned_data["personal_access_token"],
        auth_mode=form.cleaned_data["auth_mode"],
        verification_receipt=form.cleaned_data["verification_receipt"],
    )
    if not result.success:
        return _render_settings_failure(request, form, result.detail)

    target = (
        "bookmark_manager:settings"
        if request.POST.get("return_to") == "settings"
        else "bookmark_manager:index"
    )
    return redirect(target)


@require_POST
@csrf_protect
@sensitive_post_parameters("personal_access_token")
@never_cache
@_logged_view
def remove_settings(request: HttpRequest) -> HttpResponse:
    """Remove only the secure integration profile, retaining all local bookmarks."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error
    if request.POST.get("confirm") != "remove":
        return HttpResponseBadRequest("Confirmation is required before removing the credential.")
    if _action_is_rate_limited(request, "remove-settings"):
        if _is_async_form(request):
            return _action_json(
                state="rate_limited",
                label="Please wait",
                detail="Wait briefly before removing settings again.",
                status=429,
            )
        return HttpResponse("Please wait briefly before removing settings again.", status=429)

    result = remove_ui_configuration()
    if not result.success:
        if _is_async_form(request):
            return _action_json(
                state=result.state,
                label=result.label,
                detail=result.detail,
                status=400,
            )
        return HttpResponse(result.detail, status=400)
    target = (
        "bookmark_manager:settings"
        if request.POST.get("return_to") == "settings"
        else "bookmark_manager:index"
    )
    return redirect(target)


@require_POST
@csrf_protect
@sensitive_post_parameters("personal_access_token")
@never_cache
@_logged_view
def save_bookmark(request: HttpRequest) -> HttpResponse:
    """Save one unique web URL or Confluence Page ID and reveal its outline number."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error
    form = BookmarkInputForm({"page": request.POST.get("q", request.POST.get("page", ""))})
    if not form.is_valid():
        if _is_async_form(request):
            return _action_json(
                state="configuration_error",
                label="Not saved",
                detail="Enter a valid page URL or ID.",
                status=400,
            )
        return render(
            request,
            "bookmark_manager/index.html",
            _index_context(
                request, bookmark_form=form, inline_error="Enter a valid page URL or ID."
            ),
            status=400,
        )
    try:
        result = save_bookmark_input(form.cleaned_data["page"])
    except BookmarkActionError as exc:
        if _is_async_form(request):
            return _action_json(
                state=exc.configuration_state or exc.code,
                label="Not saved",
                detail=exc.message,
                status=400,
            )
        form.add_error("page", exc.message)
        return render(
            request,
            "bookmark_manager/index.html",
            _index_context(request, bookmark_form=form, inline_error=exc.message),
            status=400,
        )

    query = {
        "selected": result.bookmark.pk,
        "located": result.bookmark.pk,
        "saved": "new" if result.created else "existing",
    }
    if result.similar_bookmarks:
        query["similar"] = "1"
    target = f"{reverse('bookmark_manager:index')}?{urlencode(query)}"
    if _is_async_form(request):
        is_confluence = result.bookmark.source_type == BookmarkSource.CONFLUENCE
        outline_number = outline_number_map(ConfluencePageNode.objects.all()).get(
            result.bookmark.tree_node_id,
            str(result.bookmark.pk),
        )
        category_name = (
            result.bookmark.category.name if result.bookmark.category else "Uncategorised"
        )
        category_domain = (
            result.bookmark.category.domain if result.bookmark.category else "No domain"
        )
        bookmark_state_detail = (
            f"Added “{result.bookmark.title}”."
            if result.created
            else f"“{result.bookmark.title}” is already saved."
        )
        location_detail = (
            f"{bookmark_state_detail} "
            f"Saved under {category_domain} as bookmark {outline_number}. "
            f"Category: {category_name}."
        )
        return JsonResponse(
            {
                "state": "success",
                "label": "Page added" if result.created else "Bookmark already saved",
                "detail": (
                    f"{location_detail} Confluence page, hierarchy, and searchable text retrieved."
                    if is_confluence and result.created
                    else f"{location_detail} The existing bookmark is ready."
                    if not result.created
                    else f"{location_detail} Web bookmark added."
                ),
                "created": result.created,
                "bookmark_id": result.bookmark.pk,
                "title": result.bookmark.title,
                "outline_number": outline_number,
                "category": category_name,
                "domain": category_domain,
                "redirect": target,
            }
        )
    return HttpResponseRedirect(target)


def _folder_action_target(
    request: HttpRequest,
    *,
    action: str,
    folder: BookmarkFolder | None,
) -> str:
    """Keep only a local Bookmark Manager destination after a folder change."""

    index_path = reverse("bookmark_manager:index")
    candidate = str(request.POST.get("return_to", "")).strip()
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc or parsed.path != index_path:
        values = QueryDict("", mutable=True)
    else:
        values = QueryDict(parsed.query, mutable=True)
    for key in ("saved", "similar", "opened", "open_error", "action", "folder"):
        values.pop(key, None)
    values["action"] = action
    if folder is not None:
        values["folder"] = str(folder.pk)
    query_string = values.urlencode()
    return f"{index_path}?{query_string}" if query_string else index_path


def _folder_action_error(request: HttpRequest, detail: str) -> HttpResponse:
    if _is_async_form(request):
        return _action_json(
            state="invalid_folder",
            label="Folder not updated",
            detail=detail,
            status=400,
        )
    return HttpResponseBadRequest(detail)


@require_POST
@csrf_protect
@never_cache
@_logged_view
def create_folder(request: HttpRequest) -> HttpResponse:
    """Create a personal folder without adding a node to Confluence hierarchy."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error
    form = BookmarkFolderForm(request.POST)
    if not form.is_valid():
        return _folder_action_error(request, "Enter a folder name up to 120 characters.")
    try:
        folder = create_bookmark_folder(name=form.cleaned_data["name"])
    except ValidationError as exc:
        detail = exc.messages[0] if exc.messages else "Enter a valid folder name."
        return _folder_action_error(request, detail)
    except IntegrityError:
        return _folder_action_error(request, "A personal folder with this name already exists.")

    target = _folder_action_target(request, action="folder_created", folder=folder)
    detail = f"Created personal folder “{folder.name}”. Drag bookmarks here or use Move."
    if _is_async_form(request):
        return JsonResponse(
            {
                "state": "success",
                "label": "Folder created",
                "detail": detail,
                "folder_id": folder.pk,
                "redirect": target,
            }
        )
    return redirect(target)


@require_POST
@csrf_protect
@never_cache
@_logged_view
def move_bookmarks_to_manual_folder(request: HttpRequest) -> HttpResponse:
    """Move selected bookmarks to a personal folder, or back to source hierarchy."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error
    raw_ids = request.POST.getlist("bookmark_ids")
    if not raw_ids or any(not value.isascii() or not value.isdigit() for value in raw_ids):
        return _folder_action_error(request, "Select at least one valid bookmark to move.")
    bookmark_ids = [int(value) for value in raw_ids]
    raw_folder_id = request.POST.get("folder_id", "").strip()
    try:
        if raw_folder_id:
            if not raw_folder_id.isascii() or not raw_folder_id.isdigit():
                raise BookmarkFolder.DoesNotExist
            result = move_bookmarks_to_folder(bookmark_ids, int(raw_folder_id))
        else:
            result = unfile_bookmarks(bookmark_ids)
    except (Bookmark.DoesNotExist, BookmarkFolder.DoesNotExist, BookmarkFolderError):
        return _folder_action_error(
            request,
            "One or more selected bookmarks or the destination folder no longer exists.",
        )

    target = _folder_action_target(request, action="folder_moved", folder=result.folder)
    count = len(result.bookmark_ids)
    destination = (
        f"personal folder “{result.folder.name}”"
        if result.folder is not None
        else "the Confluence hierarchy"
    )
    detail = f"Moved {count} bookmark{'s' if count != 1 else ''} to {destination}."
    if _is_async_form(request):
        return JsonResponse(
            {
                "state": "success",
                "label": "Bookmarks moved",
                "detail": detail,
                "updated_count": result.updated_count,
                "redirect": target,
            }
        )
    return redirect(target)


@require_POST
@csrf_protect
@never_cache
@_logged_view
def rename_category(request: HttpRequest, pk: int) -> HttpResponse:
    """Rename a domain category without changing its stable domain identity."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error
    category = get_object_or_404(BookmarkCategory, pk=pk)
    form = BookmarkCategoryRenameForm(request.POST)
    if not form.is_valid():
        detail = "Enter a category name up to 253 characters."
        if _is_async_form(request):
            return _action_json(
                state="invalid_category_name",
                label="Not renamed",
                detail=detail,
                status=400,
            )
        return redirect(f"{reverse('bookmark_manager:index')}?category={category.pk}")
    try:
        rename_bookmark_category(
            category,
            form.cleaned_data["name"],
            description=form.cleaned_data["description"],
        )
    except WebBookmarkError as exc:
        if _is_async_form(request):
            return _action_json(
                state="invalid_category_name",
                label="Not renamed",
                detail=str(exc),
                status=400,
            )
        return redirect(f"{reverse('bookmark_manager:index')}?category={category.pk}")
    if _is_async_form(request):
        return _action_json(
            state="success",
            label="Domain updated",
            detail=f"Updated {category.domain}: {category.name}",
            status=200,
        )
    return redirect(f"{reverse('bookmark_manager:index')}?category={category.pk}")


@require_GET
@never_cache
@_logged_view
def bookmark_link(request: HttpRequest, pk: int) -> HttpResponse:
    """Resolve one local search result to its verified Confluence URL."""

    bookmark = get_object_or_404(Bookmark, pk=pk)
    try:
        target_url = validated_open_url(bookmark)
    except BookmarkActionError:
        return redirect(
            f"{reverse('bookmark_manager:index')}?{urlencode({'selected': bookmark.pk, 'open_error': 1})}"
        )
    response = HttpResponseRedirect(target_url)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@require_POST
@csrf_protect
@never_cache
@_logged_view
def open_bookmark(request: HttpRequest, pk: int) -> HttpResponse:
    """Validate, initiate, then record one external Confluence page open."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error
    bookmark = get_object_or_404(Bookmark, pk=pk)
    try:
        target_url = validated_open_url(bookmark)
    except BookmarkActionError as exc:
        if _is_async_form(request):
            return _action_json(
                state=exc.code,
                label="Not opened",
                detail=exc.message,
                status=400,
            )
        query = urlencode({"selected": bookmark.pk, "open_error": "1"})
        return redirect(f"{reverse('bookmark_manager:index')}?{query}")

    record_successful_open(bookmark)
    if _is_async_form(request):
        response = JsonResponse(
            {
                "state": "success",
                "label": "Opened",
                "detail": f"Opened bookmark #{bookmark.pk} in Confluence",
                "url": target_url,
            }
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
    response = HttpResponseRedirect(target_url)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _bookmark_action_response(
    request: HttpRequest,
    bookmark: Bookmark,
    *,
    label: str,
    detail: str,
    action: str,
    extra: dict[str, object] | None = None,
) -> HttpResponse:
    if _is_async_form(request):
        payload: dict[str, object] = {
            "state": "success",
            "label": label,
            "detail": detail,
            "bookmark_id": bookmark.pk,
            "favorite": bookmark.favorite,
            "pinned": bookmark.pinned,
        }
        if extra:
            payload.update(extra)
        return JsonResponse(payload)
    query = urlencode({"selected": bookmark.pk, "action": action})
    return redirect(f"{reverse('bookmark_manager:index')}?{query}")


@require_POST
@csrf_protect
@never_cache
@_logged_view
def update_organisation(request: HttpRequest, pk: int) -> HttpResponse:
    """Save OWL-only plain-text notes and tags without contacting Confluence."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error
    bookmark = get_object_or_404(Bookmark, pk=pk)
    form = BookmarkOrganisationForm(request.POST)
    if not form.is_valid():
        detail = "Review the note and tags, then try again."
        if _is_async_form(request):
            return _action_json(
                state="invalid",
                label="Not saved",
                detail=detail,
                status=400,
            )
        return render(
            request,
            "bookmark_manager/index.html",
            _index_context(request, inline_error=detail),
            status=400,
        )
    try:
        result = update_bookmark_organisation(
            bookmark,
            notes=form.cleaned_data["notes"],
            raw_tags=form.cleaned_data["tags"],
        )
    except BookmarkProductivityError as exc:
        if _is_async_form(request):
            return _action_json(
                state="invalid",
                label="Not saved",
                detail=str(exc),
                status=400,
            )
        return render(
            request,
            "bookmark_manager/index.html",
            _index_context(request, inline_error=str(exc)),
            status=400,
        )
    return _bookmark_action_response(
        request,
        result.bookmark,
        label="Personal details saved",
        detail=f"Notes and tags saved for bookmark #{result.bookmark.pk}",
        action="organised",
        extra={"notes": result.bookmark.notes, "tags": list(result.tags)},
    )


def _toggle_flag(request: HttpRequest, pk: int, field_name: str) -> HttpResponse:
    local_error = _require_local_action(request)
    if local_error:
        return local_error
    bookmark = get_object_or_404(Bookmark, pk=pk)
    bookmark = toggle_bookmark_flag(bookmark, field_name)
    enabled = getattr(bookmark, field_name)
    noun = "favorites" if field_name == "favorite" else "pinned bookmarks"
    verb = "Added to" if enabled else "Removed from"
    return _bookmark_action_response(
        request,
        bookmark,
        label="Bookmark updated",
        detail=f"{verb} {noun}",
        action=field_name,
    )


@require_POST
@csrf_protect
@never_cache
@_logged_view
def toggle_favorite(request: HttpRequest, pk: int) -> HttpResponse:
    return _toggle_flag(request, pk, "favorite")


@require_POST
@csrf_protect
@never_cache
@_logged_view
def toggle_pin(request: HttpRequest, pk: int) -> HttpResponse:
    return _toggle_flag(request, pk, "pinned")


def _query_from_encoded_state(raw_query: str) -> BookmarkQuery:
    if len(raw_query) > 8_000:
        raise InvalidBookmarkQuery("The saved-view query is too large.")
    values = QueryDict(raw_query, mutable=False)
    saved_view_value = values.get("saved_view", "")
    if saved_view_value.isdecimal():
        saved_view = SavedBookmarkView.objects.filter(pk=int(saved_view_value)).first()
        if saved_view is not None:
            return BookmarkQuery.from_saved_view(saved_view)
    return _query_from_filter_form(BookmarkFilterForm(values))


@require_POST
@csrf_protect
@never_cache
@_logged_view
def save_view(request: HttpRequest) -> HttpResponse:
    """Persist validated search/filter/sort state without transient tree state."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error
    form = SavedBookmarkViewForm(request.POST)
    try:
        if not form.is_valid():
            raise BookmarkProductivityError("Enter a name for this view.")
        query = _query_from_encoded_state(request.POST.get("query_string", ""))
        saved_view, created = save_bookmark_view(
            name=form.cleaned_data["name"],
            search_text=query.search,
            filters=query.to_filter_dict(),
            sort=query.sort.value,
            visible_columns=[],
        )
    except (BookmarkProductivityError, InvalidBookmarkQuery) as exc:
        if _is_async_form(request):
            return _action_json(
                state="invalid",
                label="View not saved",
                detail=str(exc),
                status=400,
            )
        return render(
            request,
            "bookmark_manager/index.html",
            _index_context(request, inline_error=str(exc)),
            status=400,
        )
    action = "created" if created else "updated"
    query_string = urlencode({"saved_view": saved_view.pk, "action": "view_saved"})
    target = f"{reverse('bookmark_manager:index')}?{query_string}"
    if _is_async_form(request):
        return JsonResponse(
            {
                "state": "success",
                "label": "View saved",
                "detail": f"{saved_view.name} was {action}",
                "redirect": target,
            }
        )
    return redirect(target)


@require_POST
@csrf_protect
@never_cache
@_logged_view
def delete_view(request: HttpRequest, pk: int) -> HttpResponse:
    local_error = _require_local_action(request)
    if local_error:
        return local_error
    if request.POST.get("confirm") != "delete":
        return HttpResponseBadRequest("Confirmation is required before deleting a saved view.")
    get_object_or_404(SavedBookmarkView, pk=pk).delete()
    return redirect(f"{reverse('bookmark_manager:index')}?action=view_deleted")


@require_POST
@csrf_protect
@never_cache
@_logged_view
def export_bookmarks(request: HttpRequest) -> HttpResponse:
    """Download an explicit, sensitive local JSON backup with no credentials."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error
    exported_at = timezone.now()
    try:
        payload = export_bookmarks_json(generated_at=exported_at)
    except Exception as error:
        log_event(
            logger, logging.ERROR, "bookmark_export_response_failed", error=error, stage="export"
        )
        publish_notification(
            event_key=f"bookmark-export-error:{exported_at.strftime('%Y%m%d%H%M%S%f')}",
            kind=NotificationKind.BOOKMARK_EXPORT,
            state=NotificationState.ERROR,
            title="Bookmark export failed",
            message="OWL could not prepare the credential-free JSON backup.",
            target_path=reverse("bookmark_manager:settings"),
            occurred_at=exported_at,
        )
        return HttpResponse("OWL could not prepare the bookmark export.", status=500)
    publish_notification(
        event_key=f"bookmark-export:{exported_at.strftime('%Y%m%d%H%M%S%f')}",
        kind=NotificationKind.BOOKMARK_EXPORT,
        state=NotificationState.SUCCESS,
        title="Bookmark export ready",
        message="Your credential-free JSON bookmark backup was prepared for download.",
        target_path=reverse("bookmark_manager:settings"),
        occurred_at=exported_at,
    )
    filename = f"owl-bookmarks-{timezone.localdate(exported_at).isoformat()}.json"
    response = HttpResponse(payload, content_type="application/json; charset=utf-8")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@require_POST
@csrf_protect
@never_cache
@_logged_view
def import_bookmarks(request: HttpRequest) -> HttpResponse:
    """Merge an uploaded JSON collection or URLs extracted from UTF-8 text."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error
    form = BookmarkImportForm(request.POST, request.FILES)
    if not form.is_valid():
        detail = "Choose a valid UTF-8 .json or .txt file within the configured size limit."
        failed_at = timezone.now()
        publish_notification(
            event_key=f"bookmark-import-error:{failed_at.strftime('%Y%m%d%H%M%S%f')}",
            kind=NotificationKind.BOOKMARK_IMPORT,
            state=NotificationState.ERROR,
            title="Bookmark import not started",
            message=detail,
            target_path=reverse("bookmark_manager:settings"),
            occurred_at=failed_at,
        )
        if _is_async_form(request):
            return _action_json(
                state="invalid",
                label="Import not started",
                detail=detail,
                status=400,
            )
        if request.POST.get("return_to") == "settings":
            return render(
                request,
                "bookmark_manager/settings.html",
                _settings_page_context(request, import_form=form, inline_error=detail),
                status=400,
            )
        return render(
            request,
            "bookmark_manager/index.html",
            _index_context(
                request,
                import_form=form,
                open_settings=True,
                inline_error=detail,
            ),
            status=400,
        )
    uploaded = form.cleaned_data["import_file"]
    try:
        if uploaded.name.casefold().endswith(".txt"):
            result = import_bookmarks_text(uploaded.read(), filename=uploaded.name)
        else:
            result = import_bookmarks_document(uploaded.read(), filename=uploaded.name)
    except BookmarkImportError as exc:
        failed_at = timezone.now()
        publish_notification(
            event_key=f"bookmark-import-error:{failed_at.strftime('%Y%m%d%H%M%S%f')}",
            kind=NotificationKind.BOOKMARK_IMPORT,
            state=NotificationState.ERROR,
            title="Bookmark import failed",
            message=str(exc),
            target_path=reverse("bookmark_manager:settings"),
            occurred_at=failed_at,
        )
        if _is_async_form(request):
            return _action_json(
                state="invalid",
                label="Import could not be completed",
                detail=str(exc),
                status=400,
            )
        if request.POST.get("return_to") == "settings":
            return render(
                request,
                "bookmark_manager/settings.html",
                _settings_page_context(request, import_form=form, inline_error=str(exc)),
                status=400,
            )
        return render(
            request,
            "bookmark_manager/index.html",
            _index_context(
                request,
                import_form=form,
                open_settings=True,
                inline_error=str(exc),
            ),
            status=400,
        )
    _publish_import_completion(result.run)
    destination = (
        reverse("bookmark_manager:settings")
        if request.POST.get("return_to") == "settings"
        else reverse("bookmark_manager:index")
    )
    return redirect(f"{destination}?{urlencode({'import_run': result.run.pk})}")


@require_POST
@csrf_protect
@never_cache
@_logged_view
def delete_bookmark(request: HttpRequest, pk: int) -> HttpResponse:
    """Delete only local OWL data after explicit confirmation."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error
    if request.POST.get("confirm") != "delete":
        return HttpResponseBadRequest("Confirmation is required before deleting this bookmark.")
    delete_local_bookmark(pk, confirmed=True)
    target = f"{reverse('bookmark_manager:index')}?action=deleted"
    if _is_async_form(request):
        return JsonResponse(
            {
                "state": "success",
                "label": "Bookmark deleted",
                "detail": "Removed from OWL. Confluence was not changed.",
                "redirect": target,
            }
        )
    return redirect(target)


@require_POST
@csrf_protect
@never_cache
@_logged_view
def delete_selected_bookmarks(request: HttpRequest) -> HttpResponse:
    """Delete selected local bookmarks atomically after explicit confirmation."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error
    if request.POST.get("confirm") != "delete-selected":
        return HttpResponseBadRequest("Confirmation is required before deleting bookmarks.")

    raw_ids = request.POST.getlist("bookmark_ids")
    if not raw_ids or any(not value.isascii() or not value.isdigit() for value in raw_ids):
        return HttpResponseBadRequest("Select at least one valid bookmark to delete.")
    bookmark_ids = [int(value) for value in raw_ids]
    try:
        result = delete_local_bookmarks(bookmark_ids, confirmed=True)
    except Bookmark.DoesNotExist:
        return HttpResponseBadRequest("One or more selected bookmarks no longer exist.")

    target = (
        f"{reverse('bookmark_manager:index')}?"
        f"{urlencode({'action': 'deleted', 'count': result.deleted_count})}"
    )
    detail = (
        f"Deleted {result.deleted_count} bookmark"
        f"{'s' if result.deleted_count != 1 else ''} from OWL. "
        "Confluence was not changed."
    )
    if _is_async_form(request):
        return JsonResponse(
            {
                "state": "success",
                "label": "Bookmarks deleted",
                "detail": detail,
                "deleted_count": result.deleted_count,
                "redirect": target,
            }
        )
    return redirect(target)


@require_POST
@csrf_protect
@never_cache
@_logged_view
def open_parent(request: HttpRequest, pk: int) -> HttpResponse:
    """Open a stored parent only after revalidating the configured origin."""

    local_error = _require_local_action(request)
    if local_error:
        return local_error
    bookmark = get_object_or_404(Bookmark.objects.select_related("tree_node__parent"), pk=pk)
    parent = bookmark.tree_node.parent
    try:
        profile = get_active_profile()
    except ConfigurationUnavailable:
        profile = None
    if parent is None or profile is None or not profile.origin.contains_application_url(parent.url):
        if _is_async_form(request):
            return _action_json(
                state="unsafe_parent_url",
                label="Not opened",
                detail="The parent is unavailable or outside the active Confluence origin.",
                status=400,
            )
        query = urlencode({"selected": bookmark.pk, "open_parent_error": "1"})
        return redirect(f"{reverse('bookmark_manager:index')}?{query}")
    if _is_async_form(request):
        response = JsonResponse(
            {
                "state": "success",
                "label": "Opened",
                "detail": "Opened the parent page in Confluence",
                "url": parent.url,
            }
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
    response = HttpResponseRedirect(parent.url)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
