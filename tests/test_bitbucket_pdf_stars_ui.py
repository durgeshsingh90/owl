from __future__ import annotations

import re
import shutil
import subprocess
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.urls import reverse
from django.utils import timezone

from bitbucket_search.models import BitbucketRepository, PDFDocument


class Tags(HTMLParser):
    def __init__(self, html: str):
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def with_attribute(self, attribute: str):
        return [attrs for _tag, attrs in self.tags if attribute in attrs]


def test_pdf_star_behavior_in_isolated_dom():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the isolated PDF star tests.")
    project_root = Path(__file__).parents[1]
    result = subprocess.run(
        [node, "--test", str(project_root / "tests/js/bitbucket_pdf_stars.test.js")],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def pdfs(db):
    repository = BitbucketRepository.objects.create(
        display_name="architecture",
        canonical_remote_key="bitbucket.org/workspace/architecture",
        remote_url="ssh://git@bitbucket.org/workspace/architecture.git",
    )
    observed_at = timezone.now()
    starred = PDFDocument.objects.create(
        repository=repository,
        filename="Starred Plan.pdf",
        relative_path="docs/Starred Plan.pdf",
        timeline_at=observed_at,
        starred=True,
    )
    unstarred = PDFDocument.objects.create(
        repository=repository,
        filename="Other Plan.pdf",
        relative_path="docs/Other Plan.pdf",
        timeline_at=observed_at - timedelta(minutes=1),
    )
    return repository, starred, unstarred


def test_pdf_star_controls_use_one_external_form_and_preserve_inventory_return(client, pdfs):
    _repository, starred, unstarred = pdfs
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")

    response = client.get(reverse("bitbucket_search:index"))

    assert response.status_code == 200
    html = response.content.decode()
    parsed = Tags(html)
    assert parsed.with_attribute("data-pdf-star-form") == [
        {
            "id": "bb-pdf-star-form",
            "method": "post",
            "data-pdf-star-form": None,
            "hidden": None,
        }
    ]
    external_form = re.search(r'<form id="bb-pdf-star-form".*?</form>', html, re.DOTALL)
    assert external_form is not None
    assert external_form.group().count('name="csrfmiddlewaretoken"') == 1
    assert 'name="return_page" value="1"' in external_form.group()
    assert 'name="return_to"' not in external_form.group()

    buttons = parsed.with_attribute("data-pdf-star-toggle")
    assert len(buttons) == 2
    by_id = {button["data-document-id"]: button for button in buttons}
    starred_button = by_id[str(starred.pk)]
    assert starred_button["type"] == "submit"
    assert starred_button["form"] == "bb-pdf-star-form"
    assert starred_button["formaction"] == (
        f"{reverse('bitbucket_search:document_star', args=(starred.pk,))}?starred=false"
    )
    assert starred_button["aria-label"] == "Star PDF: Starred Plan.pdf"
    assert starred_button["aria-pressed"] == "true"
    assert starred_button["title"] == "Remove star from Starred Plan.pdf in OWL"
    assert starred_button["data-tooltip"] == "Remove star"
    assert starred_button["data-pdf-star-name"] == "Starred Plan.pdf"

    unstarred_button = by_id[str(unstarred.pk)]
    assert unstarred_button["formaction"] == (
        f"{reverse('bitbucket_search:document_star', args=(unstarred.pk,))}?starred=true"
    )
    assert unstarred_button["aria-label"] == "Star PDF: Other Plan.pdf"
    assert unstarred_button["aria-pressed"] == "false"
    assert unstarred_button["title"] == "Star Other Plan.pdf in OWL"
    assert unstarred_button["data-tooltip"] == "Star PDF"

    starred_row_start = html.index(f'data-document-id="{starred.pk}"')
    starred_row = html[starred_row_start : html.index("</tr>", starred_row_start)]
    assert 'data-pdf-starred="true"' in starred_row
    assert starred_row.count('name="csrfmiddlewaretoken"') == 3
    assert starred_row.count('name="return_page" value="1"') == 2
    actions = re.search(r'<div class="bb-document-actions">.*?</div>', starred_row, re.DOTALL)
    assert actions is not None
    assert actions.group().count("<form") == 2
    assert actions.group().index("data-pdf-star-toggle") < actions.group().index("Open file")

    assert html.count(">★</span>") >= 1
    assert html.count(">☆</span>") >= 1
    assert 'role="status" aria-live="polite" aria-atomic="true" data-pdf-star-status' in html
    assert "bitbucket_search/pdf_stars.js?v=pdf-stars-v1" in html
    assert "bitbucket_search/bitbucket_search.css?v=local-stars-v1" in html


def test_pdf_star_form_preserves_search_return_and_fragment_buttons(client, pdfs):
    repository, starred, _unstarred = pdfs
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    query = {"repository": repository.pk, "sort": "starred_first"}

    search_response = client.get(reverse("bitbucket_search:index"), query)

    assert search_response.status_code == 200
    search_html = search_response.content.decode()
    external_form = re.search(r'<form id="bb-pdf-star-form".*?</form>', search_html, re.DOTALL)
    assert external_form is not None
    assert (
        f'name="return_to" value="{reverse("bitbucket_search:index")}'
        f'?repository={repository.pk}&amp;sort=starred_first"'
    ) in external_form.group()
    assert 'name="return_page"' not in external_form.group()
    assert 'value="starred_first" selected' in search_html

    fragment_response = client.get(
        reverse("bitbucket_search:document_page"),
        {"page": 1},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert fragment_response.status_code == 200
    fragment = fragment_response.json()["html"]
    assert fragment.count("data-pdf-star-toggle") == 2
    assert 'form="bb-pdf-star-form"' in fragment
    assert f'data-document-id="{starred.pk}" data-pdf-starred="true"' in fragment
    assert not re.search(
        rf'<form[^>]+action="{reverse("bitbucket_search:document_star", args=(starred.pk,))}"',
        fragment,
    )
    assert fragment.count('name="csrfmiddlewaretoken"') == 6
    assert fragment.count('name="return_page" value="1"') == 4


def test_pdf_star_styles_cover_pressed_focus_three_actions_and_mobile_targets():
    stylesheet = (
        Path(__file__).parents[1] / "static" / "bitbucket_search" / "bitbucket_search.css"
    ).read_text(encoding="utf-8")

    assert "grid-template-columns: repeat(3, max-content);" in stylesheet
    assert '.bb-document-actions .bb-document-star[aria-pressed="true"]' in stylesheet
    assert ".bb-document-actions button:focus-visible" in stylesheet
    wide_start = stylesheet.index("@media (min-width: 1800px)")
    wide_end = stylesheet.index("@media (min-width: 2200px)", wide_start)
    assert ".bb-results-table .bb-column-actions { width: 10%; }" in stylesheet[wide_start:wide_end]
    responsive_start = stylesheet.index("@media (max-width: 940px)")
    responsive_end = stylesheet.index("@media (max-width: 900px)", responsive_start)
    responsive = stylesheet[responsive_start:responsive_end]
    assert re.search(
        r"\.bb-document-actions button\s*\{[^}]*width: 44px;[^}]*height: 44px;",
        responsive,
    )
