"""Stable Word-style outline numbering for the local bookmark hierarchy."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from django.db import transaction

from bookmark_manager.models import ConfluencePageNode


def next_outline_position(*, parent_id: int | None) -> int:
    """Return the lowest unused durable position beneath one parent.

    Existing positions remain authoritative: this allocator only fills a gap left by
    a pruned node.  When called inside a transaction, locking the parent and current
    siblings serializes normal writers.  Database uniqueness constraints remain the
    final race-safety boundary, including the first root created in an empty tree.
    """

    connection = transaction.get_connection()
    if connection.in_atomic_block and parent_id is not None:
        # All normal child writers lock the same parent before inspecting its slots.
        ConfluencePageNode.objects.select_for_update().filter(pk=parent_id).exists()

    siblings = ConfluencePageNode.objects.filter(
        parent_id=parent_id,
        outline_position__isnull=False,
    ).order_by("outline_position")
    if connection.in_atomic_block:
        siblings = siblings.select_for_update()

    candidate = 1
    for position in siblings.values_list("outline_position", flat=True):
        if position < candidate:
            continue
        if position > candidate:
            break
        candidate += 1
    return candidate


def ensure_outline_position(
    node: ConfluencePageNode,
    *,
    parent_id: int | None,
    force: bool = False,
) -> int:
    """Assign a position once, or assign a fresh one after a genuine reparent."""

    if node.outline_position is not None and not force:
        return node.outline_position
    node.outline_position = next_outline_position(parent_id=parent_id)
    node.save(update_fields=("outline_position", "metadata_updated_at"))
    return node.outline_position


def outline_number_map(nodes: Iterable[ConfluencePageNode]) -> dict[int, str]:
    """Return stable dotted numbers for a complete set of hierarchy nodes.

    Persisted positions are authoritative. The fallback only covers synthetic or
    legacy nodes that have not yet passed through the migration/application service.
    Numbers are calculated from the complete tree so filtering never renumbers it.
    """

    node_list = list(nodes)
    children: dict[int | None, list[ConfluencePageNode]] = defaultdict(list)
    node_ids = {node.pk for node in node_list}
    for node in node_list:
        parent_id = node.parent_id if node.parent_id in node_ids else None
        children[parent_id].append(node)

    numbers: dict[int, str] = {}
    visited: set[int] = set()

    def assign(parent_id: int | None, prefix: str = "") -> None:
        siblings = sorted(
            children.get(parent_id, ()),
            key=lambda node: (
                node.outline_position is None,
                node.outline_position or 0,
                node.sibling_position is None,
                node.sibling_position or 0,
                node.title.casefold(),
                node.pk,
            ),
        )
        used_positions: set[int] = set()
        fallback_position = 1
        for node in siblings:
            position = node.outline_position
            if position is None or position in used_positions:
                while fallback_position in used_positions:
                    fallback_position += 1
                position = fallback_position
            used_positions.add(position)
            fallback_position = max(fallback_position, position + 1)
            number = f"{prefix}.{position}" if prefix else str(position)
            numbers[node.pk] = number
            visited.add(node.pk)
            assign(node.pk, number)

    assign(None)
    # A defensive fallback keeps malformed legacy cycles visible and numbered.
    for node in sorted(node_list, key=lambda candidate: candidate.pk):
        if node.pk not in visited:
            numbers[node.pk] = str(len(numbers) + 1)
    return numbers
