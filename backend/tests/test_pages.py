from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db


@pytest.fixture
def loopback_client(client):
    client.defaults["HTTP_HOST"] = "127.0.0.1"
    client.defaults["REMOTE_ADDR"] = "127.0.0.1"
    return client


@pytest.mark.parametrize("path", ("/", "/bookmarks/", "/bitbucket/"))
def test_primary_pages_render(loopback_client, path):
    response = loopback_client.get(path)

    assert response.status_code == 200


def test_primary_navigation_contains_only_current_applications(loopback_client):
    workspace = loopback_client.get("/home/workspace/").json()

    assert workspace["urls"]["bookmarks"] == "/bookmarks/"
    assert workspace["urls"]["bitbucket"] == "/bitbucket/"
    assert "bitbucketSearch" not in workspace["urls"]


def test_shared_shell_keeps_theme_controls(loopback_client):
    html = loopback_client.get("/bitbucket/").content.decode()

    assert "owl-frontend.css" in html
    assert "owl-frontend.js" in html


def test_removed_pdf_routes_return_not_found(loopback_client):
    assert loopback_client.get("/pdfs/").status_code == 404
    assert loopback_client.get("/pdfs/status/").status_code == 404
