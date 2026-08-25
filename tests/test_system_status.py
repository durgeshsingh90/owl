import json
from io import StringIO

import pytest
from django.conf import settings
from django.core.management import call_command
from django.test import override_settings

from core.services.system_status import get_system_status

pytestmark = pytest.mark.django_db


def test_system_status_reports_foundation_components_and_local_data_root_without_secrets():
    status = get_system_status()
    serialized = json.dumps(status, default=str)

    assert status["overall_state"] in {"ready", "attention"}
    assert {item["key"] for item in status["components"]} >= {
        "database",
        "fts5",
        "data_root",
        "credential_store",
        "confluence",
        "worker",
    }
    assert "owl-test-pat-never-valid" not in serialized
    assert str(settings.OWL_DATA_ROOT) in serialized


@override_settings(
    CONFLUENCE_BASE_URL="https://confluence.example.invalid",
    CONFLUENCE_PAT="owl-test-pat-never-valid",
)
def test_diagnostic_command_never_prints_environment_pat():
    output = StringIO()

    call_command("owl_status", "--json", stdout=output)

    payload = output.getvalue()
    assert "owl-test-pat-never-valid" not in payload
    assert str(settings.OWL_DATA_ROOT) not in payload
    parsed = json.loads(payload)
    assert parsed["overall_state"] in {"ready", "attention"}
    assert parsed["components"]
