"""Safe local-only bookmark deletion.

Deletion is intentionally separated from every Confluence integration.  Callers must
provide an explicit confirmation flag, and the service removes only the selected OWL
bookmark plus hierarchy-only leaf nodes that are no longer shared.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from django.db import transaction

from bookmark_manager.models import Bookmark, ConfluencePageNode
from bookmark_manager.services.logging_events import logged_operation


class BookmarkDeleteError(ValueError):
    """Base error for a rejected local bookmark deletion."""


class DeleteConfirmationRequired(BookmarkDeleteError):
    """Raised when a caller has not supplied explicit user confirmation."""


@dataclass(frozen=True, slots=True)
class BookmarkDeleteResult:
    """Non-sensitive summary of one completed local deletion."""

    owl_number: int
    page_id: str
    title: str
    pruned_node_count: int


@dataclass(frozen=True, slots=True)
class BulkBookmarkDeleteResult:
    """Non-sensitive summary of one atomic multi-bookmark deletion."""

    deleted_count: int
    pruned_node_count: int


@logged_operation("delete_bookmark", expected_errors=(BookmarkDeleteError, Bookmark.DoesNotExist))
@transaction.atomic
def delete_local_bookmark(
    bookmark_or_pk: Bookmark | int,
    *,
    confirmed: bool = False,
) -> BookmarkDeleteResult:
    """Delete one local OWL bookmark and prune only its now-empty leaf branch.

    This function never receives a Confluence client and therefore cannot delete or
    mutate the source page.  Requiring ``confirmed=True`` keeps the destructive UI
    contract enforceable even if another caller is added later.
    """

    if confirmed is not True:
        raise DeleteConfirmationRequired(
            "Confirm that the local OWL bookmark and its personal data may be removed."
        )

    bookmark_pk = bookmark_or_pk.pk if isinstance(bookmark_or_pk, Bookmark) else bookmark_or_pk
    if bookmark_pk is None or isinstance(bookmark_pk, bool):
        raise Bookmark.DoesNotExist("The selected OWL bookmark does not exist.")

    bookmark = Bookmark.objects.select_for_update().select_related("tree_node").get(pk=bookmark_pk)
    result = BookmarkDeleteResult(
        owl_number=bookmark.pk,
        page_id=bookmark.page_id,
        title=bookmark.title,
        pruned_node_count=0,
    )
    node_id = bookmark.tree_node_id
    bookmark.delete()

    pruned_count = _prune_orphaned_leaf_branch(node_id)
    return BookmarkDeleteResult(
        owl_number=result.owl_number,
        page_id=result.page_id,
        title=result.title,
        pruned_node_count=pruned_count,
    )


@logged_operation("delete_bookmarks", expected_errors=(BookmarkDeleteError, Bookmark.DoesNotExist))
@transaction.atomic
def delete_local_bookmarks(
    bookmark_pks: Iterable[int],
    *,
    confirmed: bool = False,
) -> BulkBookmarkDeleteResult:
    """Atomically delete selected OWL bookmarks without contacting Confluence."""

    if confirmed is not True:
        raise DeleteConfirmationRequired(
            "Confirm that the selected local OWL bookmarks and personal data may be removed."
        )

    normalized: list[int] = []
    seen: set[int] = set()
    for value in bookmark_pks:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise Bookmark.DoesNotExist("A selected OWL bookmark does not exist.")
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    if not normalized:
        raise Bookmark.DoesNotExist("Select at least one OWL bookmark to delete.")

    bookmarks = {
        bookmark.pk: bookmark
        for bookmark in Bookmark.objects.select_for_update()
        .select_related("tree_node")
        .filter(pk__in=normalized)
    }
    if len(bookmarks) != len(normalized):
        raise Bookmark.DoesNotExist("A selected OWL bookmark does not exist.")

    pruned_node_count = 0
    for bookmark_pk in normalized:
        result = delete_local_bookmark(bookmarks[bookmark_pk], confirmed=True)
        pruned_node_count += result.pruned_node_count
    return BulkBookmarkDeleteResult(
        deleted_count=len(normalized),
        pruned_node_count=pruned_node_count,
    )


def _prune_orphaned_leaf_branch(node_id: int | None) -> int:
    """Prune upward while each node has neither a bookmark nor a child."""

    pruned_count = 0
    current_id = node_id
    while current_id is not None:
        node = ConfluencePageNode.objects.select_for_update().filter(pk=current_id).first()
        if node is None:
            break
        if node.children.exists() or Bookmark.objects.filter(tree_node_id=node.pk).exists():
            break
        parent_id = node.parent_id
        node.delete()
        pruned_count += 1
        current_id = parent_id
    return pruned_count
