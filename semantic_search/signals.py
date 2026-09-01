"""Fast-path semantic queue hooks; the periodic sweeper remains authoritative."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from django.db import transaction
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_delete
from django.dispatch import receiver

from bitbucket_search.models import PDFDocument, PDFDocumentLifecycle
from bookmark_manager.models import Bookmark, Tag
from semantic_search.models import SemanticIndex, SemanticSourceType
from semantic_search.services.jobs import (
    bump_semantic_corpus_generation,
    queue_semantic_source,
)
from semantic_search.services.logging_events import get_logger, log_event
from semantic_search.services.sources import SourceUnavailable

logger = get_logger("signals")
_BOOKMARK_SEMANTIC_FIELDS = frozenset(("title", "page_text", "notes"))
_TAG_SEMANTIC_FIELDS = frozenset(("name", "normalized_name"))
_CORPUS_INVALIDATION_MARKER = "_owl_semantic_corpus_invalidation"
_TAG_PRECLEAR_BOOKMARK_IDS = "_owl_semantic_preclear_bookmark_ids"


def _pending_corpus_invalidation(connection, source_type: str) -> bool:
    """Return whether this transaction already has the same commit hook.

    Django owns and removes ``run_on_commit`` entries on savepoint rollback and
    outer-transaction completion. Inspecting the registered callbacks lets a
    repository purge delete thousands of orphan revisions while advancing the
    cross-process cache generation only once.
    """

    for entry in connection.run_on_commit:
        callback = entry[1]
        if getattr(callback, _CORPUS_INVALIDATION_MARKER, None) == source_type:
            return True
    return False


def _invalidate_corpus_after_delete(source_type: str, *, using: str) -> None:
    """Clear this process now and invalidate every process after commit."""

    connection = transaction.get_connection(using)
    if _pending_corpus_invalidation(connection, source_type):
        return

    from semantic_search.services.search import clear_semantic_search_cache

    clear_semantic_search_cache()

    def invalidate_committed_corpus() -> None:
        try:
            bump_semantic_corpus_generation(source_type)
        finally:
            # A request could repopulate this process's cache while a large
            # repository deletion transaction is still completing.
            clear_semantic_search_cache()

    setattr(
        invalidate_committed_corpus,
        _CORPUS_INVALIDATION_MARKER,
        source_type,
    )
    transaction.on_commit(invalidate_committed_corpus, using=using)


@receiver(post_delete, sender=SemanticIndex, dispatch_uid="semantic_index_cache_invalidation")
def invalidate_deleted_semantic_index(
    sender,
    instance: SemanticIndex,
    using: str,
    **kwargs,
) -> None:
    """Invalidate local and cross-process caches after a source cascade deletes vectors."""

    del sender, kwargs
    _invalidate_corpus_after_delete(instance.source_type, using=using)


def _queue_safely(source_type: SemanticSourceType, source_id: int) -> None:
    try:
        queue_semantic_source(source_type, source_id)
    except SourceUnavailable:
        return
    except Exception as exc:
        log_event(
            logger,
            logging.ERROR,
            "semantic_signal_queue_failed",
            error=exc,
            source_type=source_type,
            source_id=source_id,
        )


def _bookmark_ids(values: Iterable[object]) -> tuple[int, ...]:
    """Return stable, unique persisted bookmark identifiers."""

    return tuple(
        sorted(
            {
                int(value)
                for value in values
                if value is not None and not isinstance(value, bool) and int(value) > 0
            }
        )
    )


def _queue_bookmarks_after_commit(bookmark_ids: Iterable[object], *, using: str) -> None:
    """Queue all affected bookmarks from their committed tag relationship state."""

    selected_ids = _bookmark_ids(bookmark_ids)
    if not selected_ids:
        return

    def queue_affected_bookmarks() -> None:
        for bookmark_id in selected_ids:
            _queue_safely(SemanticSourceType.BOOKMARK, bookmark_id)

    transaction.on_commit(queue_affected_bookmarks, using=using)


@receiver(post_save, sender=PDFDocument, dispatch_uid="semantic_pdf_revision_queue")
def queue_published_pdf_revision(
    sender,
    instance: PDFDocument,
    raw: bool,
    update_fields,
    **kwargs,
) -> None:
    """Queue only after a PDF document atomically points at published text."""

    del sender, kwargs
    if (
        raw
        or instance.indexed_revision_id is None
        or instance.lifecycle_state != PDFDocumentLifecycle.ACTIVE
    ):
        return
    if update_fields is not None and "indexed_revision" not in update_fields:
        return
    revision_id = instance.indexed_revision_id
    transaction.on_commit(lambda: _queue_safely(SemanticSourceType.PDF_REVISION, revision_id))


@receiver(post_save, sender=Bookmark, dispatch_uid="semantic_bookmark_queue")
def queue_saved_bookmark(
    sender,
    instance: Bookmark,
    created: bool,
    raw: bool,
    update_fields,
    **kwargs,
) -> None:
    """Queue when stored bookmark text changes; never fetch its URL."""

    del sender, kwargs
    if raw or instance.pk is None:
        return
    if (
        not created
        and update_fields is not None
        and not (_BOOKMARK_SEMANTIC_FIELDS & set(update_fields))
    ):
        return
    bookmark_id = instance.pk
    transaction.on_commit(lambda: _queue_safely(SemanticSourceType.BOOKMARK, bookmark_id))


@receiver(
    m2m_changed,
    sender=Bookmark.tags.through,
    dispatch_uid="semantic_bookmark_tags_queue",
)
def queue_bookmark_tag_change(
    sender,
    instance,
    action: str,
    reverse: bool,
    pk_set,
    using: str,
    **kwargs,
) -> None:
    """Re-embed labels changed from either side of the tag relationship."""

    del sender, kwargs
    if instance.pk is None:
        return

    if reverse and action == "pre_clear":
        setattr(
            instance,
            _TAG_PRECLEAR_BOOKMARK_IDS,
            tuple(instance.bookmarks.values_list("pk", flat=True)),
        )
        return
    if action not in {"post_add", "post_remove", "post_clear"}:
        return

    if not reverse:
        affected_ids = (instance.pk,)
    elif action == "post_clear":
        affected_ids = getattr(instance, _TAG_PRECLEAR_BOOKMARK_IDS, ())
        if hasattr(instance, _TAG_PRECLEAR_BOOKMARK_IDS):
            delattr(instance, _TAG_PRECLEAR_BOOKMARK_IDS)
    else:
        affected_ids = pk_set or ()
    _queue_bookmarks_after_commit(affected_ids, using=using)


@receiver(post_save, sender=Tag, dispatch_uid="semantic_bookmark_tag_name_queue")
def queue_renamed_tag_bookmarks(
    sender,
    instance: Tag,
    created: bool,
    raw: bool,
    update_fields,
    using: str,
    **kwargs,
) -> None:
    """A tag rename changes the semantic text of every attached bookmark."""

    del sender, kwargs
    if raw or created or instance.pk is None:
        return
    if update_fields is not None and not (_TAG_SEMANTIC_FIELDS & set(update_fields)):
        return
    _queue_bookmarks_after_commit(
        instance.bookmarks.values_list("pk", flat=True),
        using=using,
    )


@receiver(pre_delete, sender=Tag, dispatch_uid="semantic_bookmark_tag_delete_queue")
def queue_deleted_tag_bookmarks(
    sender,
    instance: Tag,
    using: str,
    **kwargs,
) -> None:
    """Capture affected bookmarks before Django removes tag through rows."""

    del sender, kwargs
    if instance.pk is None:
        return
    _queue_bookmarks_after_commit(
        instance.bookmarks.values_list("pk", flat=True),
        using=using,
    )
