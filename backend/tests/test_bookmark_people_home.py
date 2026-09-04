"""Home integration for Bookmark Manager's source-attributed people rankings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse

from bookmark_manager.services.bookmark_domain import ConfluencePageSnapshot, upsert_bookmark

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)


@pytest.fixture
def loopback_client(client):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    return client


def _save(page_id: int, *, timestamp=NOW, editor="Casey Editor", version=3):
    return upsert_bookmark(
        ConfluencePageSnapshot(
            page_id=str(page_id),
            title=f"Synthetic design {page_id}",
            url=f"https://confluence.example.invalid/pages/{page_id}",
            author_name="Alex Writer",
            created_by_name="Alex Writer",
            modified_by_name=editor,
            created_at=timestamp - timedelta(hours=1),
            updated_at=timestamp,
            version=version,
        ),
        observed_at=NOW,
    ).bookmark


@pytest.mark.parametrize(
    ("period", "count"), (("today", 1), ("week", 2), ("month", 3), ("year", 4))
)
def test_bookmark_people_period_updates_both_rankings(loopback_client, period, count):
    for number, age in enumerate((0, 2, 20, 200), start=930001):
        _save(number, timestamp=NOW - timedelta(days=age))

    with patch("core.views.timezone.now", return_value=NOW), patch("subprocess.Popen") as spawn:
        response = loopback_client.get(reverse("core:dashboard"), {"people_period": period})

    assert response.status_code == 200
    spawn.assert_not_called()
    people = response.context["bookmark_people_dashboard"]
    assert people.period == period
    assert people.written_pages == count
    assert people.updated_pages == count
    assert people.active_people == 2
    assert people.writers[0].name == "Alex Writer"
    assert people.writers[0].page_count == count
    assert people.updaters[0].name == "Casey Editor"
    assert people.updaters[0].page_count == count
    assert people.started_at <= NOW <= people.ended_at


def test_all_dashboard_navigation_keeps_other_periods_and_bookmark_calendar(loopback_client):
    response = loopback_client.get(
        reverse("core:dashboard"),
        {
            "year": "2024",
            "activity": "opened",
            "git_period": "six_months",
            "people_period": "today",
        },
    )

    assert response.status_code == 200
    people_filters = response.context["bookmark_people_activity_filters"]
    assert len(people_filters) == 4
    for item in people_filters:
        parts = urlsplit(item["href"])
        query = parse_qs(parts.query)
        assert parts.fragment == "bookmark-people-activity"
        assert "git_period" not in query
        assert query["year"] == ["2024"]
        assert query["activity"] == ["opened"]
        assert item["selected"] == (query["people_period"] == ["today"])
    activity = response.context["dashboard"].activity
    for item in (*activity.filters, *activity.years):
        query = parse_qs(urlsplit(item.href).query)
        assert query["people_period"] == ["today"]
        assert "git_period" not in query


def test_invalid_bookmark_people_period_uses_week_and_is_not_echoed(loopback_client):
    response = loopback_client.get(
        reverse("core:dashboard"), {"people_period": '<script>alert("x")</script>'}
    )

    assert response.status_code == 200
    assert response.context["bookmark_people_dashboard"].period == "week"
    assert '<script>alert("x")</script>' not in response.content.decode()


def test_normal_source_refresh_replaces_latest_editor_instead_of_accumulating_edits(
    loopback_client,
):
    _save(940001, editor="Previous Editor", version=2)
    with patch("core.views.timezone.now", return_value=NOW):
        before = loopback_client.get(reverse("core:dashboard"))
    assert before.context["bookmark_people_dashboard"].updaters[0].name == "Previous Editor"

    _save(940001, editor="Latest Editor", version=3)
    with patch("core.views.timezone.now", return_value=NOW):
        after = loopback_client.get(reverse("core:dashboard"))
    people = after.context["bookmark_people_dashboard"]
    assert people.updated_pages == 1
    assert [(person.name, person.page_count) for person in people.updaters] == [
        ("Latest Editor", 1)
    ]
    assert people.written_pages == 1
