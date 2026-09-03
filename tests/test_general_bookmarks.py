import pytest
from django.urls import reverse

from bookmark_manager.models import Bookmark, BookmarkCategory, BookmarkSource
from bookmark_manager.services import bookmark_application
from bookmark_manager.services.bookmark_application import save_bookmark_input, validated_open_url
from bookmark_manager.services.bookmark_outline import outline_number_map
from bookmark_manager.services.bookmark_query import BookmarkQuery, query_bookmarks
from bookmark_manager.services.configuration import ConfigurationUnavailable
from bookmark_manager.services.web_bookmarks import rename_bookmark_category, save_web_bookmark

pytestmark = pytest.mark.django_db


def test_any_http_url_is_saved_once_and_grouped_by_domain_without_fetching(monkeypatch):
    monkeypatch.setattr(
        bookmark_application,
        "get_active_profile",
        lambda **_kwargs: (_ for _ in ()).throw(ConfigurationUnavailable("Not configured")),
    )

    created = save_bookmark_input("https://www.Example.com:443/guides/start#overview")
    duplicate = save_bookmark_input("https://www.example.com/guides/start#another-section")

    bookmark = created.bookmark
    assert created.created is True
    assert duplicate.created is False
    assert duplicate.bookmark.pk == bookmark.pk
    assert Bookmark.objects.count() == 1
    assert bookmark.source_type == BookmarkSource.WEB
    assert bookmark.canonical_url == "https://www.example.com/guides/start"
    assert bookmark.page_text == ""
    assert bookmark.category.domain == "www.example.com"
    assert bookmark.category.name == "example.com"
    assert bookmark.tree_node.parent.title == "example.com"
    assert bookmark.tree_node.parent.outline_position == 1
    assert bookmark.tree_node.outline_position == 1
    assert validated_open_url(bookmark) == bookmark.canonical_url


def test_web_bookmarks_receive_stable_word_style_tree_numbers():
    first = save_web_bookmark("https://docs.example.org/first").bookmark
    second = save_web_bookmark("https://docs.example.org/second").bookmark
    another_domain = save_web_bookmark("https://support.example.net/start").bookmark
    duplicate = save_web_bookmark("https://docs.example.org/first").bookmark

    nodes = list(first.tree_node.__class__.objects.order_by("id"))
    numbers = outline_number_map(nodes)

    assert numbers[first.tree_node.parent_id] == "1"
    assert numbers[first.tree_node_id] == "1.1"
    assert numbers[second.tree_node_id] == "1.2"
    assert numbers[another_domain.tree_node.parent_id] == "2"
    assert numbers[another_domain.tree_node_id] == "2.1"
    assert duplicate.pk == first.pk
    assert numbers[duplicate.tree_node_id] == "1.1"


def test_domain_category_can_be_renamed_without_changing_domain_identity():
    bookmark = save_web_bookmark("https://docs.example.org/reference").bookmark
    category = bookmark.category

    rename_bookmark_category(
        category,
        "Engineering docs",
        description="Architecture standards and runbooks",
    )

    category.refresh_from_db()
    bookmark.tree_node.parent.refresh_from_db()
    assert category.name == "Engineering docs"
    assert category.description == "Architecture standards and runbooks"
    assert category.domain == "docs.example.org"
    assert bookmark.tree_node.parent.title == "Engineering docs"


def test_category_filter_and_confluence_page_text_are_local_search_inputs():
    first = save_web_bookmark("https://one.example.net/alpha").bookmark
    second = save_web_bookmark("https://two.example.net/beta").bookmark
    first.source_type = BookmarkSource.CONFLUENCE
    first.page_text = "Zero trust routing architecture"
    first.save(update_fields=("source_type", "page_text"))

    category_result = query_bookmarks(BookmarkQuery(category_ids=(second.category_id,)))
    text_result = query_bookmarks(BookmarkQuery(search="zero trust routing"))

    assert category_result.bookmarks == (second,)
    assert text_result.bookmarks == (first,)
    assert BookmarkCategory.objects.count() == 2


def test_web_bookmark_canonical_identity_must_match_before_opening():
    bookmark = save_web_bookmark("https://example.com/original").bookmark
    bookmark.url = "https://example.com/changed"

    with pytest.raises(bookmark_application.BookmarkActionError) as captured:
        validated_open_url(bookmark)

    assert captured.value.code == "unsafe_bookmark_url"


def test_http_save_lists_domain_category_and_supports_rename(client, monkeypatch):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    monkeypatch.setattr(
        bookmark_application,
        "get_active_profile",
        lambda **_kwargs: (_ for _ in ()).throw(ConfigurationUnavailable("Not configured")),
    )

    saved = client.post(
        reverse("bookmark_manager:save"),
        {"q": "https://developer.example.com/platform/guide"},
    )
    category = BookmarkCategory.objects.get()
    page = client.get(saved["Location"])

    assert saved.status_code == 302
    assert page.status_code == 200
    assert "developer.example.com" in page.content.decode()
    assert f"?category={category.pk}&amp;sort=added_newest" in page.content.decode()

    renamed = client.post(
        reverse("bookmark_manager:category_rename", args=(category.pk,)),
        {
            "name": "Developer portal",
            "description": "SDK documentation and platform runbooks",
        },
    )

    category.refresh_from_db()
    assert renamed.status_code == 302
    assert category.name == "Developer portal"
    assert category.description == "SDK documentation and platform runbooks"
    assert category.domain == "developer.example.com"

    updated_page = client.get(reverse("bookmark_manager:index"))
    updated_html = updated_page.content.decode()
    assert "Developer portal" in updated_html
    assert "SDK documentation and platform runbooks" in updated_html
    assert 'name="description"' in updated_html
    assert "Save domain" in updated_html

    settings_page = client.get(reverse("bookmark_manager:settings"))
    settings_html = settings_page.content.decode()
    assert settings_page.status_code == 200
    assert "Developer portal" not in settings_html
    assert "SDK documentation and platform runbooks" not in settings_html
    assert f"?category={category.pk}&amp;sort=added_newest" not in settings_html
    assert 'aria-label="Domain categories"' not in settings_html


def test_domain_description_is_escaped_and_can_be_cleared(client):
    bookmark = save_web_bookmark("https://docs.example.org/reference").bookmark
    category = bookmark.category
    rename_bookmark_category(
        category,
        "Engineering docs",
        description='<script>alert("domain")</script> Internal references',
    )

    page = client.get(reverse("bookmark_manager:index"))
    html = page.content.decode()

    assert "&lt;script&gt;alert(&quot;domain&quot;)&lt;/script&gt; Internal references" in html
    assert '<script>alert("domain")</script>' not in html

    cleared = client.post(
        reverse("bookmark_manager:category_rename", args=(category.pk,)),
        {"name": "Engineering docs", "description": ""},
    )
    category.refresh_from_db()

    assert cleared.status_code == 302
    assert category.description == ""
