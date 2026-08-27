from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
from django.test import Client
from django.urls import reverse

from bookmark_manager import views
from bookmark_manager.models import Bookmark, ConfluenceConfiguration, ConfluencePageNode
from bookmark_manager.services.bookmark_domain import (
    ConfluenceNodeSnapshot,
    ConfluencePageSnapshot,
    upsert_bookmark,
)
from bookmark_manager.services.configuration import (
    ConfigurationActionResult,
    save_ui_configuration,
)
from bookmark_manager.services.secret_store import (
    InMemorySecretStore,
    get_secret_store,
)

pytestmark = pytest.mark.django_db

SYNTHETIC_ORIGIN = "https://confluence.example.invalid/wiki"
SYNTHETIC_PAT = "synthetic-phase2-http-pat-never-valid-001"


@pytest.fixture(autouse=True)
def isolated_phase_two_settings(settings):
    settings.CONFLUENCE_BASE_URL = ""
    settings.CONFLUENCE_PAT = ""
    settings.CONFLUENCE_AUTH_MODE = "bearer"
    settings.CONFLUENCE_ACTION_COOLDOWN_SECONDS = 0
    settings.OWL_ALLOW_NON_LOOPBACK = False
    settings.OWL_ALLOW_SYNTHETIC_CONFLUENCE_TARGETS = True


@pytest.fixture
def loopback_client(client):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    return client


@pytest.fixture
def secure_store() -> InMemorySecretStore:
    store = get_secret_store()
    assert isinstance(store, InMemorySecretStore)
    return store


def node_snapshot(
    page_id: str,
    title: str,
    *,
    parent_position: int | None = None,
) -> ConfluenceNodeSnapshot:
    return ConfluenceNodeSnapshot(
        page_id=page_id,
        title=title,
        url=f"{SYNTHETIC_ORIGIN}/spaces/ENG/pages/{page_id}",
        space_key="ENG",
        sibling_position=parent_position,
    )


def page_snapshot(
    page_id: str = "300",
    title: str = "Private DNS Architecture",
    *,
    ancestors: tuple[ConfluenceNodeSnapshot, ...] | None = None,
) -> ConfluencePageSnapshot:
    return ConfluencePageSnapshot(
        page_id=page_id,
        title=title,
        url=f"{SYNTHETIC_ORIGIN}/spaces/ENG/pages/{page_id}",
        space_name="Engineering",
        space_key="ENG",
        version=7,
        created_at=datetime(2025, 1, 2, 9, 30, tzinfo=UTC),
        updated_at=datetime(2026, 8, 24, 16, 15, tzinfo=UTC),
        created_by_name="Synthetic Creator",
        modified_by_name="Synthetic Modifier",
        author_name="Synthetic Author",
        ancestors=ancestors
        if ancestors is not None
        else (
            node_snapshot("100", "Engineering", parent_position=0),
            node_snapshot("200", "Networking", parent_position=3),
        ),
        sibling_position=4,
    )


def create_bookmark(
    page_id: str = "300",
    title: str = "Private DNS Architecture",
    *,
    ancestors: tuple[ConfluenceNodeSnapshot, ...] | None = None,
) -> Bookmark:
    return upsert_bookmark(
        page_snapshot(page_id, title, ancestors=ancestors),
    ).bookmark


def configure_profile(store: InMemorySecretStore, token: str = SYNTHETIC_PAT):
    result = save_ui_configuration(
        base_url=SYNTHETIC_ORIGIN,
        personal_access_token=token,
        secret_store=store,
    )
    assert result.success is True
    return result


def response_html(response) -> str:
    return response.content.decode("utf-8")


def password_input_tag(html: str) -> str:
    match = re.search(r'<input[^>]*type="password"[^>]*>', html)
    assert match is not None
    return match.group(0)


def assert_no_secret_in_local_surfaces(client, response, marker: str) -> None:
    assert marker not in response_html(response)
    assert marker not in repr(dict(client.session.items()))
    assert marker not in repr(list(ConfluenceConfiguration.objects.values()))
    assert marker not in repr(list(Bookmark.objects.values()))


def test_first_use_page_has_accessible_settings_gear_dialog_and_blank_pat(loopback_client):
    response = loopback_client.get(reverse("bookmark_manager:index"))
    html = response_html(response)

    assert response.status_code == 200
    assert 'aria-label="Confluence settings"' in html
    assert 'title="Confluence settings — Not configured"' in html
    assert 'data-settings-fallback="/bookmarks/settings/"' in html
    assert "<dialog" in html
    assert 'aria-labelledby="confluence-settings-heading"' in html
    assert 'data-open-on-load="false"' in html
    assert "Connect Confluence first" in html
    assert "No bookmarks saved yet" in html
    assert 'autocomplete="new-password"' in password_input_tag(html)
    assert "value=" not in password_input_tag(html)
    assert SYNTHETIC_PAT not in html


def test_invalid_settings_submission_never_refills_or_echoes_password(loopback_client):
    response = loopback_client.post(
        reverse("bookmark_manager:settings_save"),
        {
            "base_url": f"{SYNTHETIC_ORIGIN}?not-allowed=true",
            "personal_access_token": SYNTHETIC_PAT,
            "auth_mode": "bearer",
            "return_to": "settings",
        },
    )
    html = response_html(response)

    assert response.status_code == 400
    assert "Review" in html or "cannot" in html
    assert "value=" not in password_input_tag(html)
    assert SYNTHETIC_PAT not in html
    assert ConfluenceConfiguration.objects.count() == 0
    assert_no_secret_in_local_surfaces(loopback_client, response, SYNTHETIC_PAT)


def test_environment_managed_settings_are_disabled_blank_and_secret_free(
    loopback_client,
    settings,
):
    environment_pat = "synthetic-environment-managed-pat-never-valid-001"
    environment_origin = "https://managed.example.invalid/confluence"
    settings.CONFLUENCE_BASE_URL = environment_origin
    settings.CONFLUENCE_PAT = environment_pat

    response = loopback_client.get(reverse("bookmark_manager:settings"))
    html = response_html(response)
    form = response.context["settings_form"]

    assert response.status_code == 200
    assert response.context["configuration"].managed_externally is True
    assert response.context["configuration"].label == "Managed externally"
    assert form.initial["base_url"] == ""
    assert all(
        form.fields[field_name].disabled
        for field_name in ("base_url", "personal_access_token", "auth_mode")
    )
    assert "Managed outside OWL" in html
    assert environment_origin not in html
    assert environment_pat not in html
    assert "value=" not in password_input_tag(html)


def test_get_and_post_methods_are_restricted_to_their_declared_purpose(loopback_client):
    bookmark = create_bookmark()
    post_only = (
        reverse("bookmark_manager:save"),
        reverse("bookmark_manager:open", args=(bookmark.pk,)),
        reverse("bookmark_manager:settings_test"),
        reverse("bookmark_manager:settings_save"),
        reverse("bookmark_manager:settings_remove"),
    )
    get_only = (
        reverse("bookmark_manager:index"),
        reverse("bookmark_manager:settings"),
    )

    for path in post_only:
        assert loopback_client.get(path).status_code == 405
    for path in get_only:
        assert loopback_client.post(path).status_code == 405


def test_every_state_changing_route_enforces_csrf():
    bookmark = create_bookmark()
    csrf_client = Client(
        enforce_csrf_checks=True,
        HTTP_HOST="127.0.0.1",
        REMOTE_ADDR="127.0.0.1",
    )
    actions = (
        (reverse("bookmark_manager:save"), {"page": "300"}),
        (reverse("bookmark_manager:open", args=(bookmark.pk,)), {}),
        (
            reverse("bookmark_manager:settings_test"),
            {
                "base_url": SYNTHETIC_ORIGIN,
                "personal_access_token": SYNTHETIC_PAT,
                "auth_mode": "bearer",
            },
        ),
        (
            reverse("bookmark_manager:settings_save"),
            {
                "base_url": SYNTHETIC_ORIGIN,
                "personal_access_token": SYNTHETIC_PAT,
                "auth_mode": "bearer",
            },
        ),
        (reverse("bookmark_manager:settings_remove"), {"confirm": "remove"}),
    )

    for path, data in actions:
        assert csrf_client.post(path, data).status_code == 403


def test_rendered_settings_form_supplies_a_working_csrf_token(monkeypatch):
    csrf_client = Client(
        enforce_csrf_checks=True,
        HTTP_HOST="127.0.0.1",
        REMOTE_ADDR="127.0.0.1",
    )
    page = csrf_client.get(reverse("bookmark_manager:index"))
    match = re.search(
        r'name="csrfmiddlewaretoken" value="(?P<token>[^"]+)"',
        response_html(page),
    )
    assert match is not None

    monkeypatch.setattr(
        views,
        "test_candidate_connection",
        lambda **_kwargs: ConfigurationActionResult(
            success=True,
            state="connected",
            label="Connected",
            detail="Synthetic read-only connection succeeded.",
            verification_receipt="synthetic-receipt",
        ),
    )
    response = csrf_client.post(
        reverse("bookmark_manager:settings_test"),
        {
            "csrfmiddlewaretoken": match.group("token"),
            "base_url": SYNTHETIC_ORIGIN,
            "personal_access_token": SYNTHETIC_PAT,
            "auth_mode": "bearer",
        },
    )

    assert response.status_code == 200
    assert response.json()["state"] == "connected"


def test_non_loopback_clients_cannot_trigger_local_actions(loopback_client):
    bookmark = create_bookmark()
    actions = (
        (reverse("bookmark_manager:save"), {"page": "300"}),
        (reverse("bookmark_manager:open", args=(bookmark.pk,)), {}),
        (
            reverse("bookmark_manager:settings_test"),
            {
                "base_url": SYNTHETIC_ORIGIN,
                "personal_access_token": SYNTHETIC_PAT,
                "auth_mode": "bearer",
            },
        ),
        (
            reverse("bookmark_manager:settings_save"),
            {
                "base_url": SYNTHETIC_ORIGIN,
                "personal_access_token": SYNTHETIC_PAT,
                "auth_mode": "bearer",
            },
        ),
        (reverse("bookmark_manager:settings_remove"), {"confirm": "remove"}),
    )

    for path, data in actions:
        response = loopback_client.post(path, data, REMOTE_ADDR="198.51.100.27")
        assert response.status_code == 403
        assert "only from the local OWL application" in response_html(response)


def test_connection_json_has_an_explicit_allowlist_and_never_returns_pat(
    loopback_client,
    monkeypatch,
):
    submitted_digest = hashlib.sha256(SYNTHETIC_PAT.encode()).hexdigest()
    received = {}

    def fake_connection_test(**kwargs):
        received["base_url"] = kwargs["base_url"]
        received["auth_mode"] = kwargs["auth_mode"]
        received["token_digest"] = hashlib.sha256(
            kwargs["personal_access_token"].encode()
        ).hexdigest()
        return ConfigurationActionResult(
            success=True,
            state="connected",
            label="Connected",
            detail="Synthetic read-only connection succeeded.",
            verification_receipt="synthetic-receipt",
        )

    monkeypatch.setattr(views, "test_candidate_connection", fake_connection_test)
    response = loopback_client.post(
        reverse("bookmark_manager:settings_test"),
        {
            "base_url": SYNTHETIC_ORIGIN,
            "personal_access_token": SYNTHETIC_PAT,
            "auth_mode": "bearer",
        },
        HTTP_ACCEPT="application/json",
    )
    payload = response.json()

    assert response.status_code == 200
    assert set(payload) == {
        "state",
        "label",
        "detail",
        "verification_receipt",
    }
    assert payload["state"] == "connected"
    assert received == {
        "base_url": SYNTHETIC_ORIGIN,
        "auth_mode": "bearer",
        "token_digest": submitted_digest,
    }
    assert SYNTHETIC_PAT not in response_html(response)
    assert SYNTHETIC_ORIGIN not in response_html(response)
    assert ConfluenceConfiguration.objects.count() == 0
    assert_no_secret_in_local_surfaces(loopback_client, response, SYNTHETIC_PAT)


def test_save_reopen_and_remove_keep_pat_hidden_and_bookmarks_retained(
    loopback_client,
    secure_store,
):
    bookmark = create_bookmark()
    save_response = loopback_client.post(
        reverse("bookmark_manager:settings_save"),
        {
            "base_url": SYNTHETIC_ORIGIN,
            "personal_access_token": SYNTHETIC_PAT,
            "auth_mode": "bearer",
            "verification_receipt": "",
            "return_to": "settings",
        },
    )

    assert save_response.status_code == 302
    assert save_response.headers["Location"] == reverse("bookmark_manager:settings")
    assert SYNTHETIC_PAT in (secure_store.get() or "")
    configuration = ConfluenceConfiguration.objects.get(pk=1)
    assert configuration.base_url == SYNTHETIC_ORIGIN
    assert SYNTHETIC_PAT not in repr(configuration.__dict__)
    assert_no_secret_in_local_surfaces(loopback_client, save_response, SYNTHETIC_PAT)

    reopen_response = loopback_client.get(reverse("bookmark_manager:settings"))
    reopen_html = response_html(reopen_response)
    assert reopen_response.status_code == 200
    assert "Replace PAT" in reopen_html
    assert "Stored securely. Leave this empty" in reopen_html
    assert "value=" not in password_input_tag(reopen_html)
    assert_no_secret_in_local_surfaces(loopback_client, reopen_response, SYNTHETIC_PAT)

    remove_response = loopback_client.post(
        reverse("bookmark_manager:settings_remove"),
        {"confirm": "remove", "return_to": "settings"},
    )
    assert remove_response.status_code == 302
    assert remove_response.headers["Location"] == reverse("bookmark_manager:settings")
    assert secure_store.get() is None
    configuration.refresh_from_db()
    assert configuration.base_url == ""
    assert Bookmark.objects.filter(pk=bookmark.pk).exists()
    assert ConfluencePageNode.objects.filter(pk=bookmark.tree_node_id).exists()
    assert_no_secret_in_local_surfaces(loopback_client, remove_response, SYNTHETIC_PAT)


def test_bookmark_tree_and_selected_details_render_source_hierarchy(loopback_client):
    bookmark = create_bookmark()
    response = loopback_client.get(
        reverse("bookmark_manager:index"),
        {"selected": bookmark.pk},
    )
    html = response_html(response)

    assert response.status_code == 200
    assert 'role="tree"' in html
    assert "Engineering" in html
    assert "Networking" in html
    assert "Private DNS Architecture" in html
    assert f"OWL #{bookmark.pk}" in html
    assert "Confluence Page ID 300" in html
    assert "Synthetic Author" in html
    assert "Synthetic Modifier" in html
    assert "Opened</dt><dd>0 times" in html
    assert bookmark.url in html
    assert 'aria-current="true"' in html


@pytest.mark.parametrize(
    "query",
    (
        "Private DNS",
        "300",
        f"{SYNTHETIC_ORIGIN}/spaces/ENG/pages/300",
        "1",
    ),
    ids=("title", "page-id", "url", "owl-number"),
)
def test_search_finds_title_url_page_id_and_owl_number(loopback_client, query):
    matched = create_bookmark()
    create_bookmark("902", "Storage Operations Handbook", ancestors=())

    response = loopback_client.get(reverse("bookmark_manager:index"), {"q": query})
    html = response_html(response)

    assert response.status_code == 200
    assert "Private DNS Architecture" in html
    assert "Storage Operations Handbook" not in html
    assert "Visible results</dt>" in html
    assert "Filtered" in html
    assert f'href="?selected={matched.pk}' in html
    assert f'href="/bookmarks/{matched.pk}/link/"' in html


def test_existing_page_post_reveals_the_root_after_descendant_sync(
    loopback_client,
    monkeypatch,
):
    existing = create_bookmark()
    result = upsert_bookmark(page_snapshot("300"))
    monkeypatch.setattr(views, "save_bookmark_input", lambda _value: result)

    response = loopback_client.post(
        reverse("bookmark_manager:save"),
        {"page": f"{SYNTHETIC_ORIGIN}/spaces/ENG/pages/000300/renamed"},
    )
    parsed = urlparse(response.headers["Location"])
    query = parse_qs(parsed.query)

    assert response.status_code == 302
    assert parsed.path == reverse("bookmark_manager:index")
    assert query == {
        "selected": [str(existing.pk)],
        "located": [str(existing.pk)],
        "saved": ["existing"],
    }
    assert Bookmark.objects.count() == 1


def test_similar_title_save_remains_distinct_and_shows_non_blocking_warning(
    loopback_client,
    monkeypatch,
):
    existing = create_bookmark("300", "Private DNS Architecture")
    new_result = upsert_bookmark(
        page_snapshot("301", "Private DNS Architecture Guide", ancestors=())
    )
    monkeypatch.setattr(views, "save_bookmark_input", lambda _value: new_result)

    response = loopback_client.post(
        reverse("bookmark_manager:save"),
        {"page": "301"},
        follow=True,
    )
    html = response_html(response)

    assert response.status_code == 200
    assert Bookmark.objects.count() == 2
    assert new_result.bookmark.pk != existing.pk
    assert "Similar title found" in html
    assert "saved separately" in html
    assert f"#{existing.pk} Private DNS Architecture" in html
    assert f"OWL #{new_result.bookmark.pk}" in html


def test_unified_bookmark_input_can_save_a_page_from_its_search_field(
    loopback_client,
    monkeypatch,
):
    new_result = upsert_bookmark(page_snapshot("301", "Unified input page", ancestors=()))
    monkeypatch.setattr(views, "save_bookmark_input", lambda _value: new_result)

    response = loopback_client.post(
        reverse("bookmark_manager:save"),
        {"q": "301"},
    )

    assert response.status_code == 302
    assert "selected=" in response.headers["Location"]


def test_open_is_post_only_tracks_success_and_uses_safe_redirect_headers(
    loopback_client,
    secure_store,
):
    configure_profile(secure_store)
    bookmark = create_bookmark()
    open_path = reverse("bookmark_manager:open", args=(bookmark.pk,))

    assert loopback_client.get(open_path).status_code == 405
    response = loopback_client.post(open_path)

    assert response.status_code == 302
    assert response.headers["Location"] == bookmark.url
    assert "no-store" in response.headers["Cache-Control"]
    assert response.headers["Referrer-Policy"] == "no-referrer"
    bookmark.refresh_from_db()
    assert bookmark.open_count == 1
    assert bookmark.first_opened_at is not None
    assert bookmark.last_viewed_at is not None
    assert bookmark.last_viewed_version == bookmark.version


def test_unsafe_open_stays_local_and_does_not_increment_usage(
    loopback_client,
    secure_store,
):
    configure_profile(secure_store)
    bookmark = create_bookmark()
    bookmark.url = "https://other.example.invalid/wiki/spaces/ENG/pages/300"
    bookmark.save(update_fields=["url"])

    response = loopback_client.post(
        reverse("bookmark_manager:open", args=(bookmark.pk,)),
        follow=True,
    )
    html = response_html(response)

    assert response.status_code == 200
    assert response.redirect_chain[0][0].startswith("/bookmarks/?")
    assert "open_error=1" in response.redirect_chain[0][0]
    assert "was not opened because" in html
    bookmark.refresh_from_db()
    assert bookmark.open_count == 0
    assert bookmark.last_viewed_at is None


def test_untrusted_bookmark_and_search_content_is_html_escaped(loopback_client):
    bookmark = create_bookmark(
        "777",
        '<script>alert("bookmark-title")</script>',
        ancestors=(node_snapshot("700", '<img src=x onerror="ancestor()">'),),
    )
    response = loopback_client.get(
        reverse("bookmark_manager:index"),
        {"selected": bookmark.pk, "q": '<svg onload="search()">'},
    )
    html = response_html(response)

    assert response.status_code == 200
    assert '<script>alert("bookmark-title")</script>' not in html
    assert '<img src=x onerror="ancestor()">' not in html
    assert '<svg onload="search()">' not in html
    assert "&lt;svg onload=&quot;" in html


@pytest.mark.parametrize(
    "path_name",
    ("bookmark_manager:index", "bookmark_manager:settings"),
)
def test_sensitive_pages_are_no_store_and_apply_strict_referrer_policy(
    loopback_client,
    path_name,
):
    response = loopback_client.get(reverse(path_name))
    html = response_html(response)

    assert response.status_code == 200
    assert "no-store" in response.headers["Cache-Control"]
    assert response.headers["Referrer-Policy"] == "same-origin"
    assert '<meta name="referrer" content="no-referrer">' in html


def test_failed_sensitive_post_is_no_store_and_secret_free(loopback_client):
    response = loopback_client.post(
        reverse("bookmark_manager:settings_save"),
        {
            "base_url": f"{SYNTHETIC_ORIGIN}?not-allowed=true",
            "personal_access_token": SYNTHETIC_PAT,
            "auth_mode": "bearer",
            "return_to": "settings",
        },
    )

    assert response.status_code == 400
    assert "no-store" in response.headers["Cache-Control"]
    assert response.headers["Referrer-Policy"] == "same-origin"
    assert_no_secret_in_local_surfaces(loopback_client, response, SYNTHETIC_PAT)
