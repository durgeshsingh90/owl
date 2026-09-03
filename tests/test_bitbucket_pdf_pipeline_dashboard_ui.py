from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.urls import reverse

PROJECT_ROOT = Path(__file__).parents[1]


class _Document(HTMLParser):
    def __init__(self, html: str):
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs):
        self.elements.append((tag, dict(attrs)))

    def with_attribute(self, attribute: str):
        return [(tag, attrs) for tag, attrs in self.elements if attribute in attrs]


@pytest.fixture
def loopback_client(client):
    client.defaults.update(HTTP_HOST="127.0.0.1", REMOTE_ADDR="127.0.0.1")
    return client


@pytest.mark.django_db
def test_repository_logs_render_detailed_accessible_pipeline_dashboard(loopback_client):
    response = loopback_client.get(reverse("bitbucket_search:index_status"))

    assert response.status_code == 200
    html = response.content.decode()
    document = _Document(html)
    ((dashboard_tag, dashboard),) = document.with_attribute("data-pipeline-dashboard")
    assert dashboard_tag == "section"
    assert dashboard["aria-labelledby"] == "bb-pipeline-heading"
    assert dashboard["data-pipeline-metrics-url"] == reverse("bitbucket_search:pipeline_metrics")
    assert dashboard["data-pipeline-active-interval"] == "5000"
    assert dashboard["data-pipeline-idle-interval"] == "30000"

    overview_at = html.index("bb-log-overview")
    dashboard_at = html.index("data-pipeline-dashboard")
    repository_picker_at = html.index("bb-log-toolbar")
    assert overview_at < dashboard_at < repository_picker_at
    assert dashboard_at < html.index("Choose a repository")

    panels = [
        attrs
        for tag, attrs in document.elements
        if tag == "article" and "bb-pipeline-panel" in attrs.get("class", "").split()
    ]
    assert len(panels) == 2
    assert {panel["aria-labelledby"] for panel in panels} == {
        "bb-pipeline-capacity-heading",
        "bb-pipeline-flow-heading",
    }
    assert "Capacity and state timeline" in html
    assert "Flow balance and durable backlog" in html

    graph_hooks = (
        "data-pipeline-capacity-chart",
        "data-pipeline-flow-chart",
    )
    for hook in graph_hooks:
        ((tag, graph),) = document.with_attribute(hook)
        assert tag == "svg"
        assert graph["role"] == "img"
        labelled_by = graph["aria-labelledby"].split()
        assert len(labelled_by) == 2
        for element_id in labelled_by:
            assert any(attrs.get("id") == element_id for _, attrs in document.elements)

    legend_labels = {
        attrs["aria-label"]
        for tag, attrs in document.elements
        if tag == "ul" and attrs.get("aria-label")
    }
    assert {"Capacity timeline legend", "Flow timeline legend"} <= legend_labels
    for unit_label in (
        "Extractor outputs/min",
        "Durable publications/min",
        "Backpressure depth (jobs)",
        "Backpressure threshold (jobs)",
    ):
        assert unit_label in html

    ((fallback_tag, fallback),) = document.with_attribute("data-pipeline-sample-rows")
    assert fallback_tag == "tbody"
    assert fallback["data-pipeline-sample-rows"] is None
    assert 'aria-label="Pipeline timeline values"' in html
    assert "Equivalent numeric values for both graphs" in html

    for marker in (
        "data-pipeline-total-eta",
        "data-pipeline-activity",
        "data-pipeline-extracted-rate",
        "data-pipeline-written-rate",
        "data-pipeline-capacity-value",
        "data-pipeline-backlog-value",
    ):
        assert len(document.with_attribute(marker)) == 1

    assert len(document.with_attribute("data-pipeline-recovery-history-rows")) == 1
    assert len(document.with_attribute("data-pipeline-tuning-history-rows")) == 1
    assert "Recovery event history" in html
    assert "Performance tuning history" in html

    stylesheets = [
        attrs.get("href", "")
        for tag, attrs in document.elements
        if tag == "link" and attrs.get("rel") == "stylesheet"
    ]
    scripts = [attrs for tag, attrs in document.elements if tag == "script" and attrs.get("src")]
    assert sum("pdf_pipeline_dashboard.css" in href for href in stylesheets) == 1
    dashboard_scripts = [attrs for attrs in scripts if "pdf_pipeline_dashboard.js" in attrs["src"]]
    assert len(dashboard_scripts) == 1
    assert "defer" in dashboard_scripts[0]

    dashboard_html = html[dashboard_at:repository_picker_at]
    assert "csrfmiddlewaretoken" not in dashboard_html
    assert "password" not in dashboard_html.lower()
    assert "access_token" not in dashboard_html.lower()
    assert "client_secret" not in dashboard_html.lower()


def test_pipeline_dashboard_assets_are_local_lightweight_and_safely_render_values():
    script = (PROJECT_ROOT / "static/bitbucket_search/pdf_pipeline_dashboard.js").read_text()
    stylesheet = (PROJECT_ROOT / "static/bitbucket_search/pdf_pipeline_dashboard.css").read_text()

    assert "global.OWLPDFPipelineDashboard = api" in script
    assert "pipelineMetricsUrl" in script
    assert "owl:pipeline-metrics" in script
    assert "document.hidden" in script
    assert "textContent" in script
    assert "innerHTML" not in script
    assert "eval(" not in script
    assert "new Function" not in script
    for heavy_library in ("chart.js", "highcharts", "plotly", "d3.js", "echarts"):
        assert heavy_library not in script.lower()
        assert heavy_library not in stylesheet.lower()

    assert ".bb-pipeline-panels" in stylesheet
    assert "grid-template-columns" in stylesheet
    assert "prefers-reduced-motion" in stylesheet
