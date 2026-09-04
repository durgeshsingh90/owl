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

from bitbucket_search.models import (
    BitbucketRepository,
    BitbucketStarredPerson,
    GitCommit,
)


class Tags(HTMLParser):
    def __init__(self, html: str):
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def with_attribute(self, attribute: str):
        return [attrs for _tag, attrs in self.tags if attribute in attrs]


def test_people_star_behavior_in_isolated_dom():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the isolated People star tests.")
    project_root = Path(__file__).parents[1]
    result = subprocess.run(
        [node, "--test", str(project_root / "tests/js/bitbucket_people_stars.test.js")],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.django_db
def test_people_star_controls_are_accessible_external_form_buttons_in_both_panels(client):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    repository = BitbucketRepository.objects.create(
        display_name="architecture",
        canonical_remote_key="bitbucket.org/workspace/architecture",
        remote_url="ssh://git@bitbucket.org/workspace/architecture.git",
    )
    committed_at = timezone.now() - timedelta(hours=1)
    for marker, person in (("a", "Alice Smith"), ("b", "Bob Jones")):
        GitCommit.objects.create(
            repository=repository,
            commit_hash=marker * 40,
            author_name=f"Author {marker}",
            committer_name=person,
            authored_at=committed_at - timedelta(minutes=5),
            committed_at=committed_at,
        )
    BitbucketStarredPerson.objects.create(person_name="Alice Smith")

    response = client.get(reverse("bitbucket_search:index"), {"committer": "Alice Smith"})

    assert response.status_code == 200
    html = response.content.decode()
    parsed = Tags(html)
    star_forms = parsed.with_attribute("data-people-star-form")
    assert star_forms == [
        {
            "id": "bb-people-star-form",
            "method": "post",
            "action": reverse("bitbucket_search:people_star_toggle"),
            "data-people-star-form": None,
            "hidden": None,
        }
    ]
    assert (
        f'name="return_to" value="{reverse("bitbucket_search:index")}?committer=Alice+Smith"'
        in html
    )

    buttons = parsed.with_attribute("data-people-star-toggle")
    assert len(buttons) == 4
    alice_buttons = [
        button for button in buttons if button["data-people-identity-key"] == "alice smith"
    ]
    bob_buttons = [
        button for button in buttons if button["data-people-identity-key"] == "bob jones"
    ]
    assert len(alice_buttons) == len(bob_buttons) == 2
    for button in alice_buttons:
        assert button["type"] == "submit"
        assert button["name"] == "person"
        assert button["value"] == "alice smith"
        assert button["form"] == "bb-people-star-form"
        assert button["formaction"] == (
            f"{reverse('bitbucket_search:people_star_toggle')}?starred=false"
        )
        assert button["aria-label"] == "Star Alice Smith"
        assert button["aria-pressed"] == "true"
        assert button["data-people-star-name"] == "Alice Smith"
        assert "data-people-name" not in button
        assert button["title"] == "Remove star from Alice Smith locally in OWL"
    for button in bob_buttons:
        assert button["formaction"] == (
            f"{reverse('bitbucket_search:people_star_toggle')}?starred=true"
        )
        assert button["aria-label"] == "Star Bob Jones"
        assert button["aria-pressed"] == "false"
        assert button["title"] == "Star Bob Jones locally in OWL"
    assert html.count(">★</span>") == 2
    assert html.count(">☆</span>") == 2
    assert (
        html.count("stars stay local to OWL and use Git committer names, not Bitbucket accounts")
        == 2
    )

    # A star is a separate POST action, never a nested form or button inside the
    # label that selects a committer for the surrounding GET filter.
    people_filter_forms = re.findall(
        r'<form class="bb-people-filter-form".*?</form>',
        html,
        flags=re.DOTALL,
    )
    assert len(people_filter_forms) == 2
    assert all(form.count("<form") == 1 for form in people_filter_forms)
    assert all('form="bb-people-star-form"' in form for form in people_filter_forms)

    scripts = [
        attrs
        for tag, attrs in parsed.tags
        if tag == "script" and "bitbucket_search/people_stars.js" in (attrs.get("src") or "")
    ]
    assert len(scripts) == 1
    assert "defer" in scripts[0]
    stylesheets = [
        attrs
        for tag, attrs in parsed.tags
        if tag == "link" and "bitbucket_search/bitbucket_search.css" in (attrs.get("href") or "")
    ]
    assert len(stylesheets) == 1
    assert stylesheets[0]["href"].endswith(
        "bitbucket_search/bitbucket_search.css?v=pdf-selection-actions-v1"
    )


def test_people_star_styles_show_state_focus_and_mobile_touch_target():
    stylesheet = (
        Path(__file__).parents[1] / "static" / "bitbucket_search" / "bitbucket_search.css"
    ).read_text(encoding="utf-8")

    assert '.bb-people-star[aria-pressed="true"]' in stylesheet
    assert ".bb-people-star:focus-visible" in stylesheet
    assert "grid-template-columns: minmax(0, 1fr) 32px;" in stylesheet
    mobile_start = stylesheet.index("@media (max-width: 620px)")
    mobile_end = stylesheet.index("@media (max-width: 390px)", mobile_start)
    mobile = stylesheet[mobile_start:mobile_end]
    assert "grid-template-columns: minmax(0, 1fr) 44px;" in mobile
    assert re.search(r"\.bb-people-star\s*\{[^}]*width: 44px;[^}]*height: 44px;", mobile)
