from __future__ import annotations

import json
import logging
import ssl
from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.db import OperationalError
from django.http import HttpResponse
from django.test import RequestFactory

from bookmark_manager import urls, views
from bookmark_manager.services import bookmark_application, confluence_adapter, web_bookmarks
from bookmark_manager.services.bookmark_application import BookmarkActionError
from bookmark_manager.services.configuration import (
    ActiveConfluenceProfile,
    ConfigurationUnavailable,
)
from bookmark_manager.services.confluence_adapter import (
    ConfluenceAdapter,
    ConfluenceResult,
    ConfluenceResultCode,
    ResponseTooLargeError,
)
from bookmark_manager.services.confluence_adapter import (
    HttpResponse as AdapterResponse,
)
from bookmark_manager.services.confluence_validation import validate_confluence_origin

pytestmark = pytest.mark.django_db
PRIVATE = "PRIVATE_BOOKMARK_PAT_TITLE_PEOPLE_BODY_QUERY"
ORIGIN = "https://confluence.example.invalid/wiki"


@pytest.fixture
def bookmark_logs(caplog):
    logger = logging.getLogger("owl.bookmarks")
    caplog.set_level(logging.DEBUG, logger="owl.bookmarks")
    logger.addHandler(caplog.handler)
    yield caplog
    logger.removeHandler(caplog.handler)


@pytest.fixture
def origin():
    return validate_confluence_origin(ORIGIN, allow_test_targets=True)


def _events(capture, event=None, *, component=None):
    return [
        record
        for record in capture.records
        if record.name.startswith("owl.bookmarks.")
        and (component is None or record.name == f"owl.bookmarks.{component}")
        and (event is None or f"event={event} " in f"{record.getMessage()} ")
    ]


def _assert_private(capture):
    for record in _events(capture):
        assert PRIVATE not in record.getMessage()
        assert ORIGIN not in record.getMessage()
        assert "https://" not in record.getMessage()
        assert record.exc_info is None


def _adapter(origin, *outcomes):
    pending = deque(outcomes)

    def send(_request):
        outcome = pending.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    transport = SimpleNamespace(send=Mock(side_effect=send))
    return ConfluenceAdapter(origin, PRIVATE, transport=transport), transport


def _request(method="get", **data):
    request = getattr(RequestFactory(), method)("/bookmarks/", data=data, REMOTE_ADDR="127.0.0.1")
    request._dont_enforce_csrf_checks = True
    return request


def test_web_save_logs_safe_request_created_and_existing_outcomes(bookmark_logs):
    url = f"https://example.invalid/{PRIVATE}?query={PRIVATE}"
    first = web_bookmarks.save_web_bookmark(url)
    second = web_bookmarks.save_web_bookmark(url)

    assert first.created and not second.created
    completed = _events(bookmark_logs, "web_bookmark_completed")
    assert len(completed) == 2
    assert all(record.levelno == logging.INFO for record in completed)
    assert f"bookmark_id={first.bookmark.pk} status=created" in completed[0].getMessage()
    assert f"bookmark_id={first.bookmark.pk} status=existing" in completed[1].getMessage()
    _assert_private(bookmark_logs)


def test_web_validation_is_warning_and_operational_failure_is_error(bookmark_logs, monkeypatch):
    with pytest.raises(web_bookmarks.WebBookmarkError):
        web_bookmarks.save_web_bookmark(f"javascript:{PRIVATE}")
    rejected = _events(bookmark_logs, "web_bookmark_rejected")
    assert rejected[0].levelno == logging.WARNING
    assert not _events(bookmark_logs, "web_bookmark_failed")

    monkeypatch.setattr(
        web_bookmarks, "category_for_url", Mock(side_effect=OperationalError(PRIVATE))
    )
    with pytest.raises(OperationalError):
        web_bookmarks.save_web_bookmark(f"https://example.invalid/{PRIVATE}")
    assert _events(bookmark_logs, "web_bookmark_failed")[0].levelno == logging.ERROR
    _assert_private(bookmark_logs)


def test_application_fallback_save_completes_and_never_logs_url(bookmark_logs, monkeypatch):
    monkeypatch.setattr(
        bookmark_application,
        "get_active_profile",
        Mock(side_effect=ConfigurationUnavailable(PRIVATE)),
    )
    result = bookmark_application.save_bookmark_input(f"https://example.invalid/{PRIVATE}")
    assert _events(bookmark_logs, "bookmark_configuration_fallback")[0].levelno == logging.DEBUG
    assert (
        f"bookmark_id={result.bookmark.pk}"
        in _events(bookmark_logs, "bookmark_action_completed")[0].getMessage()
    )
    _assert_private(bookmark_logs)


@pytest.mark.parametrize("error", [BookmarkActionError(PRIVATE, PRIVATE), RuntimeError(PRIVATE)])
def test_application_does_not_trust_exception_codes(bookmark_logs, monkeypatch, error):
    monkeypatch.setattr(bookmark_application, "get_active_profile", Mock(side_effect=error))
    with pytest.raises(type(error)):
        bookmark_application.save_bookmark_input(PRIVATE)
    failure = _events(bookmark_logs, "bookmark_action_failed")[0]
    assert failure.levelno == logging.ERROR
    assert "error_code=action_failed" in failure.getMessage()
    _assert_private(bookmark_logs)


def test_application_adapter_error_is_error_even_when_returned(bookmark_logs, monkeypatch, origin):
    monkeypatch.setattr(
        bookmark_application,
        "get_active_profile",
        lambda **kwargs: ActiveConfluenceProfile(origin, PRIVATE, "bearer", "environment"),
    )
    client = SimpleNamespace(
        get_page=lambda _page_id: ConfluenceResult(ConfluenceResultCode.ACCESS_DENIED, PRIVATE)
    )
    with pytest.raises(BookmarkActionError):
        bookmark_application.save_bookmark_input("300", client_factory=lambda _profile: client)
    failure = _events(bookmark_logs, "bookmark_metadata_failed")[0]
    assert failure.levelno == logging.ERROR
    assert "page_id=300 error_code=access_denied" in failure.getMessage()
    _assert_private(bookmark_logs)


def test_adapter_success_has_request_timing_and_safe_outcome(bookmark_logs, origin):
    adapter, transport = _adapter(
        origin, AdapterResponse(200, body=json.dumps({"displayName": PRIVATE}).encode())
    )
    assert adapter.test_connection().ok
    assert transport.send.call_count == 1
    assert _events(bookmark_logs, "confluence_http_requested")[0].levelno == logging.DEBUG
    completed = _events(bookmark_logs, "confluence_http_completed")[0]
    assert completed.levelno == logging.DEBUG
    assert "http_status=200" in completed.getMessage()
    assert "elapsed_ms=" in completed.getMessage()
    assert _events(bookmark_logs, "confluence_operation_completed")[0].levelno == logging.INFO
    _assert_private(bookmark_logs)


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
def test_adapter_unsuccessful_status_is_error(bookmark_logs, origin, status):
    adapter, _transport = _adapter(origin, AdapterResponse(status, body=PRIVATE.encode()))
    assert not adapter.test_connection().ok
    failure = _events(bookmark_logs, "confluence_operation_failed")[0]
    assert failure.levelno == logging.ERROR
    assert f"http_status={status}" in failure.getMessage()
    _assert_private(bookmark_logs)


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(PRIVATE),
        OSError(PRIVATE),
        ssl.SSLError(PRIVATE),
        ResponseTooLargeError(PRIVATE),
    ],
)
def test_adapter_caught_transport_failures_are_error(bookmark_logs, origin, error):
    adapter, _transport = _adapter(origin, error)
    assert not adapter.test_connection().ok
    assert _events(bookmark_logs, "confluence_http_failed")[0].levelno == logging.ERROR
    assert _events(bookmark_logs, "confluence_operation_failed")[0].levelno == logging.ERROR
    _assert_private(bookmark_logs)


def test_adapter_malformed_response_is_error_without_body(bookmark_logs, origin):
    adapter, _transport = _adapter(origin, AdapterResponse(200, body=PRIVATE.encode()))
    assert not adapter.test_connection().ok
    assert _events(bookmark_logs, "confluence_json_failed")[0].levelno == logging.ERROR
    assert _events(bookmark_logs, "confluence_operation_failed")[0].levelno == logging.ERROR
    _assert_private(bookmark_logs)


def test_adapter_invalid_page_input_warns_without_request(bookmark_logs, origin):
    adapter, transport = _adapter(origin)
    assert not adapter.get_page(PRIVATE).ok
    assert transport.send.call_count == 0
    assert _events(bookmark_logs, "confluence_operation_failed")[0].levelno == logging.WARNING
    assert not _events(bookmark_logs, "confluence_http_requested")
    _assert_private(bookmark_logs)


def test_adapter_safe_redirect_logs_counts_without_location(bookmark_logs, origin):
    adapter, transport = _adapter(
        origin,
        AdapterResponse(302, headers={"Location": "/wiki/rest/api/user/current?expand=profile"}),
        AdapterResponse(200, body=json.dumps({"displayName": PRIVATE}).encode()),
    )
    assert adapter.test_connection().ok
    assert transport.send.call_count == 2
    assert (
        "redirect_count=1" in _events(bookmark_logs, "confluence_redirect_followed")[0].getMessage()
    )
    assert "attempt=2" in _events(bookmark_logs, "confluence_http_requested")[1].getMessage()
    _assert_private(bookmark_logs)


def test_address_retry_records_failure_without_address_or_target(
    bookmark_logs, origin, monkeypatch
):
    response = SimpleNamespace(
        status=200,
        getheader=lambda _name: None,
        read=lambda _limit: json.dumps({"displayName": PRIVATE}).encode(),
        getheaders=lambda: (),
    )
    connections = [
        SimpleNamespace(request=Mock(side_effect=OSError(PRIVATE)), close=Mock()),
        SimpleNamespace(request=Mock(), getresponse=lambda: response, close=Mock()),
    ]
    monkeypatch.setattr(confluence_adapter, "_PinnedHTTPSConnection", Mock(side_effect=connections))
    transport = confluence_adapter.StdlibHttpTransport()
    monkeypatch.setattr(transport, "_addresses", lambda _origin: ("192.0.2.1", "192.0.2.2"))
    adapter = ConfluenceAdapter(origin, PRIVATE, transport=transport)
    assert adapter.test_connection().ok
    failure = _events(bookmark_logs, "confluence_address_failed")[0]
    assert failure.levelno == logging.ERROR
    assert "attempt=1" in failure.getMessage()
    assert "attempt=2" in _events(bookmark_logs, "confluence_address_retry")[0].getMessage()
    assert "192.0.2" not in bookmark_logs.text
    _assert_private(bookmark_logs)


@pytest.mark.parametrize(
    ("view_name", "dependency", "method"),
    [
        ("index", "_index_context", "get"),
        ("global_refresh_status", "refresh_status_snapshot", "get"),
        ("notifications_status", "list_notification_payloads", "get"),
        ("settings_page", "_settings_page_context", "get"),
        ("tick_refresh_schedule", "queue_due_scheduled_refresh", "post"),
    ],
)
def test_read_and_poll_failures_are_error(
    bookmark_logs, monkeypatch, view_name, dependency, method
):
    monkeypatch.setattr(views, dependency, Mock(side_effect=OperationalError(PRIVATE)))
    with pytest.raises(OperationalError):
        getattr(views, view_name)(_request(method))
    failure = _events(bookmark_logs, "bookmark_web_failed")[0]
    assert failure.levelno == logging.ERROR
    assert f"operation={view_name}" in failure.getMessage()
    assert not _events(bookmark_logs, "bookmark_web_requested")
    _assert_private(bookmark_logs)


def test_successful_poll_is_quiet(bookmark_logs, monkeypatch):
    monkeypatch.setattr(views, "refresh_status_snapshot", lambda: (None, None))
    monkeypatch.setattr(views, "_refresh_run_payload", lambda *_args: {})
    assert views.global_refresh_status(_request()).status_code == 200
    assert not _events(bookmark_logs, component="web")


def test_rejected_poll_warns_without_remote_address(bookmark_logs):
    request = _request()
    request.META["REMOTE_ADDR"] = "192.0.2.123"
    assert views.global_refresh_status(request).status_code == 403
    failure = _events(bookmark_logs, "bookmark_web_rejected")[0]
    assert failure.levelno == logging.WARNING
    assert "reason=non_loopback" in failure.getMessage()
    assert "192.0.2.123" not in bookmark_logs.text


def test_web_wrapper_never_logs_arbitrary_id_text(bookmark_logs, monkeypatch):
    monkeypatch.setattr(views, "_toggle_flag", Mock(side_effect=OperationalError(PRIVATE)))
    with pytest.raises(OperationalError):
        views.toggle_pin(_request("post"), pk=PRIVATE)
    _assert_private(bookmark_logs)


def test_mutation_failure_retains_bookmark_id_and_safe_dispatch(bookmark_logs, monkeypatch):
    monkeypatch.setattr(views, "_toggle_flag", Mock(side_effect=OperationalError(PRIVATE)))
    with pytest.raises(OperationalError):
        views.toggle_pin(_request("post", private=PRIVATE), pk=123)
    assert _events(bookmark_logs, "bookmark_web_requested")[0].levelno == logging.DEBUG
    failure = _events(bookmark_logs, "bookmark_web_failed")[0]
    assert failure.levelno == logging.ERROR
    assert "bookmark_id=123" in failure.getMessage()
    _assert_private(bookmark_logs)


def test_caught_export_failure_is_error(bookmark_logs, monkeypatch):
    monkeypatch.setattr(views, "export_bookmarks_json", Mock(side_effect=OSError(PRIVATE)))
    monkeypatch.setattr(views, "publish_notification", Mock())
    assert views.export_bookmarks(_request("post")).status_code == 500
    failure = _events(bookmark_logs, "bookmark_export_response_failed")[0]
    assert failure.levelno == logging.ERROR
    assert "stage=export" in failure.getMessage()
    _assert_private(bookmark_logs)


def test_caught_worker_wakeup_failure_is_error(bookmark_logs, monkeypatch):
    run = SimpleNamespace(pk=77, last_error_message=PRIVATE, refresh_from_db=Mock())
    monkeypatch.setattr(views, "get_configuration_summary", lambda: SimpleNamespace(complete=True))
    monkeypatch.setattr(views, "create_or_get_refresh_run", lambda: (run, True))
    monkeypatch.setattr(views, "publish_refresh_notification", Mock())
    monkeypatch.setattr(views, "launch_refresh_worker", Mock(side_effect=OSError(PRIVATE)))
    monkeypatch.setattr(views, "mark_refresh_launch_failed", Mock())
    monkeypatch.setattr(views, "refresh_status_snapshot", lambda: (None, None))
    monkeypatch.setattr(views, "_refresh_run_payload", lambda *_args: {})
    assert views.start_global_refresh(_request("post")).status_code == 503
    failure = _events(bookmark_logs, "bookmark_refresh_wakeup_failed")[0]
    assert failure.levelno == logging.ERROR
    assert "run_id=77" in failure.getMessage()
    _assert_private(bookmark_logs)


def test_all_routed_views_have_failure_wrapper():
    for pattern in urls.urlpatterns:
        callback = pattern.callback
        while callback is not None:
            if callback.__code__ is views._logged_view(lambda _request: HttpResponse()).__code__:
                break
            callback = getattr(callback, "__wrapped__", None)
        else:
            pytest.fail(f"Missing safe failure wrapper for {pattern.name}")
