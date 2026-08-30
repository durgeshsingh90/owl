"""Transactional OWL-owned manual-folder operations."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from bookmark_manager.models import Bookmark, BookmarkFolder
from bookmark_manager.services.logging_events import logged_operation


class BookmarkFolderError(ValueError):
    """Raised when a manual-folder operation has invalid local input."""


@dataclass(frozen=True, slots=True)
class BookmarkFolderMoveResult:
    """The durable outcome of moving bookmarks into or out of a manual folder."""

    folder: BookmarkFolder | None
    bookmark_ids: tuple[int, ...]
    updated_count: int


@logged_operation("create_folder", expected_errors=(ValidationError,))
@transaction.atomic
def create_bookmark_folder(*, name: str) -> BookmarkFolder:
    """Create one flat manual folder with a normalized, case-insensitive identity."""

    folder = BookmarkFolder(name=name)
    folder.save()
    return folder


@logged_operation(
    "move_to_folder",
    expected_errors=(BookmarkFolderError, Bookmark.DoesNotExist, BookmarkFolder.DoesNotExist),
)
@transaction.atomic
def move_bookmarks_to_folder(
    bookmarks_or_pks: Iterable[Bookmark | int],
    folder_or_pk: BookmarkFolder | int,
) -> BookmarkFolderMoveResult:
    """Move all requested bookmarks without altering their canonical source nodes."""

    bookmark_ids = _bookmark_ids(bookmarks_or_pks)
    folder_id = _saved_pk(folder_or_pk, model=BookmarkFolder)
    folder = BookmarkFolder.objects.select_for_update().get(pk=folder_id)
    bookmarks = _locked_bookmarks(bookmark_ids)
    changed_ids = tuple(
        bookmark.pk for bookmark in bookmarks if bookmark.manual_folder_id != folder.pk
    )
    if changed_ids:
        Bookmark.objects.filter(pk__in=changed_ids).update(manual_folder=folder)
    return BookmarkFolderMoveResult(
        folder=folder,
        bookmark_ids=bookmark_ids,
        updated_count=len(changed_ids),
    )


def move_bookmark_to_folder(
    bookmark_or_pk: Bookmark | int,
    folder_or_pk: BookmarkFolder | int,
) -> BookmarkFolderMoveResult:
    """Move one bookmark to a manual folder."""

    return move_bookmarks_to_folder((bookmark_or_pk,), folder_or_pk)


@logged_operation("unfile_bookmarks", expected_errors=(BookmarkFolderError, Bookmark.DoesNotExist))
@transaction.atomic
def unfile_bookmarks(
    bookmarks_or_pks: Iterable[Bookmark | int],
) -> BookmarkFolderMoveResult:
    """Return bookmarks to source-tree presentation by clearing manual placement."""

    bookmark_ids = _bookmark_ids(bookmarks_or_pks)
    bookmarks = _locked_bookmarks(bookmark_ids)
    changed_ids = tuple(
        bookmark.pk for bookmark in bookmarks if bookmark.manual_folder_id is not None
    )
    if changed_ids:
        Bookmark.objects.filter(pk__in=changed_ids).update(manual_folder=None)
    return BookmarkFolderMoveResult(
        folder=None,
        bookmark_ids=bookmark_ids,
        updated_count=len(changed_ids),
    )


def unfile_bookmark(bookmark_or_pk: Bookmark | int) -> BookmarkFolderMoveResult:
    """Return one bookmark to source-tree presentation."""

    return unfile_bookmarks((bookmark_or_pk,))


def _bookmark_ids(bookmarks_or_pks: Iterable[Bookmark | int]) -> tuple[int, ...]:
    unique_ids: list[int] = []
    seen: set[int] = set()
    for bookmark_or_pk in bookmarks_or_pks:
        bookmark_id = _saved_pk(bookmark_or_pk, model=Bookmark)
        if bookmark_id in seen:
            continue
        seen.add(bookmark_id)
        unique_ids.append(bookmark_id)
    if not unique_ids:
        raise BookmarkFolderError("Select at least one saved bookmark.")
    return tuple(unique_ids)


def _saved_pk(value, *, model) -> int:
    if isinstance(value, model):
        if value.pk is None:
            raise model.DoesNotExist(f"An unsaved {model._meta.verbose_name} cannot be used.")
        return value.pk
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise model.DoesNotExist(f"A saved {model._meta.verbose_name} is required.")
    return value


def _locked_bookmarks(bookmark_ids: tuple[int, ...]) -> tuple[Bookmark, ...]:
    bookmarks = tuple(
        Bookmark.objects.select_for_update().filter(pk__in=bookmark_ids).order_by("pk")
    )
    found_ids = {bookmark.pk for bookmark in bookmarks}
    missing_ids = tuple(bookmark_id for bookmark_id in bookmark_ids if bookmark_id not in found_ids)
    if missing_ids:
        missing = ", ".join(str(bookmark_id) for bookmark_id in missing_ids)
        raise Bookmark.DoesNotExist(f"Bookmark IDs do not exist: {missing}.")
    return bookmarks
