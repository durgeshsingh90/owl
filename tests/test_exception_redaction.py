import sys

import pytest
from django.conf import settings
from django.test import RequestFactory, override_settings
from django.views.debug import (
    get_default_exception_reporter_filter,
    technical_500_response,
)

pytestmark = pytest.mark.django_db


@override_settings(DEBUG=True)
def test_debug_exception_response_redacts_pat_from_every_report_surface():
    marker = "synthetic-debug-pat-never-valid-4d2b7c9a"
    request = RequestFactory().post(
        f"/bookmarks/settings/?pat={marker}",
        {"pat": marker},
        HTTP_AUTHORIZATION=f"Bearer {marker}",
    )

    with override_settings(CONFLUENCE_PAT=marker):
        get_default_exception_reporter_filter.cache_clear()
        try:
            raise RuntimeError(f"Synthetic connection failure contained {marker}")
        except RuntimeError:
            response = technical_500_response(request, *sys.exc_info())
        finally:
            get_default_exception_reporter_filter.cache_clear()

    rendered = response.content.decode()
    assert marker not in rendered
    assert settings.DEFAULT_EXCEPTION_REPORTER == "core.debug.OWLExceptionReporter"
    assert settings.DEFAULT_EXCEPTION_REPORTER_FILTER == "core.debug.OWLExceptionReporterFilter"
    assert "Request URI redacted by OWL." in rendered
    assert "********************" in rendered
