from html import escape

import pytest
from django.urls import reverse

from bitbucket_search import views
from bitbucket_search.models import BitbucketRepository, PDFDocument

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("remote_url", "expected"),
    [
        ("https://scm.example.invalid/stash/scm/adr/architecture.git", "adr"),
        ("https://scm.example.invalid/scm/ADR/architecture.git", "ADR"),
        ("https://scm.example.invalid/company/stash/scm/reviews/architecture.git", "reviews"),
        ("https://github.com/team/architecture.git", ""),
        ("https://bitbucket.org/workspace/architecture.git", ""),
        ("ssh://git@bitbucket.org/workspace/architecture.git", ""),
        ("https://scm.example.invalid/scm/architecture.git", ""),
    ],
)
def test_project_key_comes_from_the_bitbucket_server_clone_route(remote_url, expected):
    repository = BitbucketRepository(remote_url=remote_url)

    assert views._project_label(repository) == expected


def _document(relative_path="docs/Review & Sign-off.pdf"):
    repository = BitbucketRepository.objects.create(
        display_name="engineering-sign-off",
        canonical_remote_key="scm.example.invalid/stash/scm/adr/engineering-sign-off",
        remote_url="https://scm.example.invalid/stash/scm/adr/engineering-sign-off.git",
    )
    return PDFDocument.objects.create(
        repository=repository,
        filename=relative_path.rsplit("/", 1)[-1],
        relative_path=relative_path,
    )


@pytest.mark.parametrize("search", [False, True])
def test_pdf_list_uses_short_clickable_paths_but_copies_the_absolute_path(client, search):
    document = _document()
    row = views._timeline_row(document)
    response = client.get(reverse("bitbucket_search:index"), {"q": "Review"} if search else {})
    html = response.content.decode()

    assert response.status_code == 200
    assert row.project_label == "adr"
    assert row.display_path == "engineering-sign-off/docs/Review & Sign-off.pdf"
    assert f'data-pdf-local-path="{escape(row.full_path, quote=True)}"' in html
    assert escape(row.display_path) in html
    assert "data-copy-pdf-path" in html
    assert "data-pdf-path-copy-status" in html
    assert ">Project</th>" in html
    assert "Project / workspace" not in html
    assert "Git-added date unavailable" not in html
    assert "Full reachable history" not in html
    assert "Unavailable from Git" not in html


def test_windows_full_path_is_preserved_in_copy_payload(client, monkeypatch):
    _document()
    full_path = (
        r"C:\OWL\media\bitbucket\repositories\1-engineering-sign-off\docs\Review & Sign-off.pdf"
    )
    monkeypatch.setattr(views, "_display_full_path", lambda document: full_path)

    html = client.get(reverse("bitbucket_search:index")).content.decode()

    assert f'data-pdf-local-path="{escape(full_path, quote=True)}"' in html
    assert "engineering-sign-off/docs/Review &amp; Sign-off.pdf" in html


def test_invalid_document_path_is_not_copyable(client):
    document = _document("../outside.pdf")

    row = views._timeline_row(document)
    html = client.get(reverse("bitbucket_search:index")).content.decode()

    assert row.display_path == "Unavailable"
    assert row.path_copy_available is False
    assert "data-pdf-local-path=" not in html
    assert "../outside.pdf" not in html
