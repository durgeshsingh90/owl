"""Low-cost, fail-closed admission gates for the supervised PDF pipeline."""

from __future__ import annotations

from django.conf import settings
from django.db import DatabaseError

from bitbucket_search.models import PDFPipelineRecovery, PDFPipelineRecoveryState

_EXTRACTION_BLOCKING_SCOPES = (
    "pipeline",
    "supervisor",
    "controller",
    "extraction_pool",
)
_PUBLICATION_BLOCKING_SCOPES = (
    "pipeline",
    "supervisor",
    "publisher",
)
_ACTIVE_PROBE_STATES = (
    PDFPipelineRecoveryState.HEALTHY,
    PDFPipelineRecoveryState.RECOVERING,
    PDFPipelineRecoveryState.RECOVERING_HALF_OPEN,
)


def _recovery_allows(
    scopes: tuple[str, ...],
    *,
    allowed_states: tuple[str, ...] = _ACTIVE_PROBE_STATES,
) -> bool:
    """Block new claims whenever canonical circuit truth is unsafe or unreadable."""

    if not getattr(settings, "PDF_PIPELINE_RECOVERY_ENABLED", True):
        return True
    try:
        return (
            not PDFPipelineRecovery.objects.filter(scope__in=scopes)
            .exclude(
                state__in=allowed_states,
            )
            .exists()
        )
    except DatabaseError:
        # A claim itself needs this database. Failing closed avoids bypassing a
        # pause merely because its control row cannot currently be read.
        return False


def extraction_admission_allowed() -> bool:
    """Allow a new parser only while its upstream/downstream scopes are healthy."""

    return _recovery_allows(_EXTRACTION_BLOCKING_SCOPES) and _recovery_allows(
        ("publisher",),
        allowed_states=(PDFPipelineRecoveryState.HEALTHY,),
    )


def publication_admission_allowed() -> bool:
    """Allow writer claims while extraction-only pauses can safely drain output."""

    return _recovery_allows(_PUBLICATION_BLOCKING_SCOPES)
