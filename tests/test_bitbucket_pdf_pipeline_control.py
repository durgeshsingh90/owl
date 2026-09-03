from __future__ import annotations

from unittest.mock import Mock

import pytest

from bitbucket_search.models import PDFPipelineRecovery, PDFPipelineRecoveryState
from bitbucket_search.services import pdf_indexing, pdf_pipeline_control

pytestmark = pytest.mark.django_db


def _recovery(scope: str, state: str) -> PDFPipelineRecovery:
    return PDFPipelineRecovery.objects.create(scope=scope, state=state)


@pytest.mark.parametrize(
    "scope",
    ("pipeline", "supervisor", "controller", "extraction_pool", "publisher"),
)
def test_unsafe_shared_scope_blocks_new_extraction_claims(scope, settings):
    settings.PDF_PIPELINE_RECOVERY_ENABLED = True
    _recovery(scope, PDFPipelineRecoveryState.RETRY_WAIT)

    assert pdf_pipeline_control.extraction_admission_allowed() is False


def test_extraction_only_pause_still_allows_publisher_to_drain(settings):
    settings.PDF_PIPELINE_RECOVERY_ENABLED = True
    _recovery("extraction_pool", PDFPipelineRecoveryState.PAUSED)

    assert pdf_pipeline_control.extraction_admission_allowed() is False
    assert pdf_pipeline_control.publication_admission_allowed() is True


@pytest.mark.parametrize("scope", ("pipeline", "supervisor", "publisher"))
def test_active_recovery_probe_can_publish_but_retry_wait_cannot(scope, settings):
    settings.PDF_PIPELINE_RECOVERY_ENABLED = True
    recovery = _recovery(scope, PDFPipelineRecoveryState.RECOVERING)

    assert pdf_pipeline_control.publication_admission_allowed() is True

    recovery.state = PDFPipelineRecoveryState.RETRY_WAIT
    recovery.save(update_fields=("state",))
    assert pdf_pipeline_control.publication_admission_allowed() is False


def test_publisher_probe_drains_staging_without_admitting_new_extraction(settings):
    settings.PDF_PIPELINE_RECOVERY_ENABLED = True
    _recovery("publisher", PDFPipelineRecoveryState.RECOVERING)

    assert pdf_pipeline_control.publication_admission_allowed() is True
    assert pdf_pipeline_control.extraction_admission_allowed() is False


def test_individual_paused_slot_does_not_stop_healthy_slots(settings):
    settings.PDF_PIPELINE_RECOVERY_ENABLED = True
    _recovery("extraction_slot:2", PDFPipelineRecoveryState.PAUSED)

    assert pdf_pipeline_control.extraction_admission_allowed() is True
    assert pdf_pipeline_control.publication_admission_allowed() is True


def test_disabled_component_recovery_preserves_legacy_admission(settings):
    settings.PDF_PIPELINE_RECOVERY_ENABLED = False
    _recovery("pipeline", PDFPipelineRecoveryState.PAUSED)

    assert pdf_pipeline_control.extraction_admission_allowed() is True
    assert pdf_pipeline_control.publication_admission_allowed() is True


def test_extraction_claim_stops_before_lock_when_recovery_blocks(monkeypatch):
    claim_lock = Mock()
    monkeypatch.setattr(pdf_indexing, "extraction_admission_allowed", Mock(return_value=False))
    monkeypatch.setattr(pdf_indexing, "pdf_extraction_claim_lock", claim_lock)

    assert pdf_indexing.claim_next_extraction_job() is None
    claim_lock.assert_not_called()


def test_publication_claim_stops_before_queue_read_when_recovery_blocks(monkeypatch):
    gate = Mock(return_value=False)
    monkeypatch.setattr(pdf_indexing, "publication_admission_allowed", gate)
    query = Mock(side_effect=AssertionError("publication queue should not be read"))
    monkeypatch.setattr(pdf_indexing.PDFExtractionJob.objects, "filter", query)

    assert pdf_indexing.work_one_publication_job() is None
    gate.assert_called_once_with()
    query.assert_not_called()
