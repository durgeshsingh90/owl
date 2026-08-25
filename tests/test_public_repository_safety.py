from __future__ import annotations

import json
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from scripts import check_tracked_files as scanner


def _assignment(name: str, value: str, *, json_style: bool = False) -> str:
    if json_style:
        return json.dumps({name: value}, separators=(",", ":"))
    return f"{name}={value}"


@pytest.mark.parametrize(
    "candidate",
    [
        "secrets/credential.txt",
        "screenshots/configuration.png",
        "docs/customer-screenshot.png",
        "Screenshot 2026-08-25 at 10.20.30.png",
        "config/django-secret-key",
        "config/signing-key.p8",
        "config/keystore.jks",
        ".git-credentials",
        "fixtures/private-document.pdf",
        "fixtures/bookmark-export.json",
        "fixtures/bookmarks.json",
        "evidence/bookmark_export.zip",
        "tmp/extracted-page.txt",
        "staticfiles/admin/css/base.css",
        "database/owl.sqlite3",
    ],
)
def test_private_paths_are_rejected(candidate: str):
    assert scanner.path_problems(PurePosixPath(candidate), 10)


@pytest.mark.parametrize(
    "candidate",
    [
        "docs/schema.json",
        "docs/bookmark_schema.json",
        "static/owl/icon.svg",
    ],
)
def test_normal_public_paths_are_allowed(candidate: str):
    assert scanner.path_problems(PurePosixPath(candidate), 10) == []


def test_lowercase_quoted_minified_json_credential_is_rejected():
    text = _assignment("pat", "live-value-4f91c8d2a730", json_style=True)

    problems = scanner.content_problems(text)

    assert problems == [(1, "non-placeholder value assigned to PAT")]


@pytest.mark.parametrize("embedded_word", ["test", "sample", "fake"])
def test_real_looking_credential_is_not_excused_by_safe_sounding_substring(
    embedded_word: str,
):
    candidate_value = f"live-{embedded_word}-credential-4f91c8d2a730"
    text = _assignment("CONFLUENCE_PAT", candidate_value)

    problems = scanner.content_problems(text)

    assert problems == [(1, "non-placeholder value assigned to CONFLUENCE_PAT")]


def test_git_credential_url_userinfo_is_rejected_without_exposing_it():
    user_name = "developer"
    candidate_value = "live-private-credential-4f91c8d2a730"
    text = "https://" + user_name + ":" + candidate_value + "@git.internal.company"

    problems = scanner.content_problems(text)

    assert problems == [(1, "credential-bearing URL userinfo found")]
    assert candidate_value not in repr(problems)


def test_credential_in_url_query_parameter_is_rejected_without_exposing_it():
    candidate_value = "live-private-credential-4f91c8d2a730"
    text = "https://api.example.com/data?" + "access_token=" + candidate_value

    problems = scanner.content_problems(text)

    assert problems == [(1, "credential-bearing URL parameter found")]
    assert candidate_value not in repr(problems)


@pytest.mark.parametrize(
    ("name", "prefix"),
    [("authorization", "Bearer "), ("cookie", "session=")],
)
def test_sensitive_http_header_assignment_is_rejected(name: str, prefix: str):
    candidate_value = "live-private-credential-4f91c8d2a730"
    text = _assignment(name, prefix + candidate_value, json_style=True)

    problems = scanner.content_problems(text)

    assert problems == [(1, f"credential-bearing HTTP header assigned to {name.upper()}")]
    assert candidate_value not in repr(problems)


@pytest.mark.parametrize("value", ["Bearer <token>", "Bearer placeholder", "redacted"])
def test_unmistakable_http_header_placeholder_is_allowed(value: str):
    assert scanner.content_problems(_assignment("Authorization", value, json_style=True)) == []


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (
            "DJANGO_SECRET_KEY",
            "ci-only-synthetic-secret-key-not-for-real-use-0123456789-abcdef",
        ),
        (
            "DJANGO_SECRET_KEY",
            "local-check-only-synthetic-secret-key-not-for-real-use-0123456789",
        ),
        (
            "DJANGO_SECRET_KEY",
            "synthetic-test-secret-key-only-not-for-real-use-0123456789-abcdefghij",
        ),
        ("CONFLUENCE_PAT", "owl-test-pat-never-valid"),
        ("CONFLUENCE_PAT", "synthetic-value-never-returned"),
    ],
)
def test_current_synthetic_ci_and_test_values_are_allowed(name: str, value: str):
    assert scanner.content_problems(_assignment(name, value)) == []


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CONFLUENCE_BASE_URL", "https://wiki.intranet.company"),
        ("endpoint_url", "https://10.12.0.5/api"),
        ("server", "private-build-host"),
    ],
)
def test_literal_internal_or_private_endpoint_is_rejected(name: str, value: str):
    problems = scanner.content_problems(_assignment(name, value, json_style=True))

    assert problems == [(1, f"literal internal/private endpoint assigned to {name.upper()}")]


@pytest.mark.parametrize(
    "value",
    [
        "https://confluence.example.invalid",
        "https://api.github.com",
        "https://192.0.2.10",
    ],
)
def test_documentation_and_public_endpoints_are_allowed(value: str):
    assert scanner.content_problems(_assignment("BASE_URL", value)) == []


@pytest.mark.parametrize(
    "source",
    [
        "token: str\nurl: str\nhost: str\n",
        "url = current_url\ntoken = submitted_token\nhost = origin.host\n",
        "result = client(url=url, token=token, server=server)\n",
        "self.current_base_url = current_base_url\n",
        (
            "class ResultKind(StrEnum):\n"
            '    SERVER = "server"\n'
            '    MODERN_URL = "modern_url"\n'
            '    LEGACY_URL = "legacy_url"\n'
        ),
        ("class Request:\n    token: str\n    url: str\n    server: str\n"),
    ],
)
def test_python_annotations_enums_and_variable_expressions_are_not_literals(source: str):
    problems = scanner.content_problems(source, path=PurePosixPath("module.py"))

    assert problems == []


@pytest.mark.parametrize(
    "source",
    [
        'data["request_insecure_uri"] = "Request URI redacted by OWL."\n',
        'client.defaults["HTTP_HOST"] = "127.0.0.1"\n',
        'remote = f"https://{user}:{password}@git.internal.company/repository"\n',
        (
            'page = f"https://confluence.example.invalid/wiki/pages/1?token='
            '{settings.CONFLUENCE_PAT}"\n'
        ),
        (
            'page = f"https://confluence.example.invalid/wiki/pages/1?token='
            '{settings.values["CONFLUENCE_PAT"]}"\n'
        ),
    ],
)
def test_safe_python_redactions_loopback_and_url_expressions_are_allowed(source: str):
    assert scanner.content_problems(source, path=PurePosixPath("module.py")) == []


@pytest.mark.parametrize(
    "value",
    [
        "owl-test-pat-never-valid-bookmark-application",
        "synthetic-log-pat-never-valid-91c73e2b",
    ],
)
def test_explicit_never_valid_project_credentials_are_allowed(value: str):
    source = f'@override_settings(CONFLUENCE_PAT="{value}")\ndef test_synthetic():\n    pass\n'

    assert scanner.content_problems(source, path=PurePosixPath("test_synthetic.py")) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            'CONFLUENCE_PAT: str = "live-private-credential-4f91c8d2a730"\n',
            "non-placeholder value assigned to CONFLUENCE_PAT",
        ),
        (
            'def connect(token: str = "live-private-credential-4f91c8d2a730"):\n    pass\n',
            "non-placeholder value assigned to TOKEN",
        ),
        (
            'client(token="live-private-credential-4f91c8d2a730")\n',
            "non-placeholder value assigned to TOKEN",
        ),
        (
            'settings = {"Authorization": "Bearer live-private-credential-4f91c8d2a730"}\n',
            "credential-bearing HTTP header assigned to AUTHORIZATION",
        ),
        (
            'BASE_URL = "https://wiki.intranet.company"\n',
            "literal internal/private endpoint assigned to BASE_URL",
        ),
        (
            'client(url="https://wiki.intranet.company")\n',
            "literal internal/private endpoint assigned to URL",
        ),
        (
            'config["PAT"] = "live-private-credential-4f91c8d2a730"\n',
            "non-placeholder value assigned to PAT",
        ),
        (
            'TOKEN = "live-private-" + "credential-4f91c8d2a730"\n',
            "non-placeholder value assigned to TOKEN",
        ),
        (
            'client(url="https://" + "wiki.intranet.company")\n',
            "literal internal/private endpoint assigned to URL",
        ),
    ],
)
def test_python_hard_coded_sensitive_literals_remain_rejected(source: str, expected: str):
    problems = scanner.content_problems(source, path=PurePosixPath("module.py"))

    assert problems == [(1, expected)]


def test_enum_member_names_are_ignored_but_method_literals_are_still_scanned():
    source = (
        "class ResultKind(StrEnum):\n"
        '    SERVER = "server"\n'
        "\n"
        "    def connect(self):\n"
        '        token = "live-private-credential-4f91c8d2a730"\n'
    )

    assert scanner.content_problems(source, path=PurePosixPath("module.py")) == [
        (5, "non-placeholder value assigned to TOKEN")
    ]


def test_javascript_password_ternary_is_not_mistaken_for_a_json_assignment():
    source = 'patInput.type = showing ? "password" : "text";'

    assert scanner.content_problems(source, path=PurePosixPath("controls.js")) == []


def test_javascript_object_literal_with_hard_coded_secret_remains_rejected():
    source = 'const config = {"token":"live-private-credential-4f91c8d2a730"};'

    assert scanner.content_problems(source, path=PurePosixPath("settings.js")) == [
        (1, "non-placeholder value assigned to TOKEN")
    ]


def test_scanner_reads_staged_blob_even_after_worktree_is_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    candidate_path = tmp_path / "integration.txt"
    staged_value = "live-contest-credential-4f91c8d2a730"
    candidate_path.write_text(_assignment("CONFLUENCE_PAT", staged_value), encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", candidate_path.name],
        check=True,
        capture_output=True,
    )
    candidate_path.write_text(_assignment("CONFLUENCE_PAT", "placeholder"), encoding="utf-8")
    monkeypatch.setattr(scanner, "REPOSITORY_ROOT", tmp_path)

    result = scanner.main()
    output = capsys.readouterr()
    combined_output = output.out + output.err

    assert result == 1
    assert "integration.txt:1" in combined_output
    assert staged_value not in combined_output


def test_untracked_nonignored_file_is_scanned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    candidate_value = "live-private-credential-4f91c8d2a730"
    (tmp_path / "candidate.txt").write_text(
        _assignment("TOKEN", candidate_value),
        encoding="utf-8",
    )
    monkeypatch.setattr(scanner, "REPOSITORY_ROOT", tmp_path)

    result = scanner.main()
    output = capsys.readouterr()
    combined_output = output.out + output.err

    assert result == 1
    assert "candidate.txt:1" in combined_output
    assert candidate_value not in combined_output


def test_adversarial_test_source_does_not_trigger_the_repository_scanner():
    source = Path(__file__).read_text(encoding="utf-8")

    assert scanner.content_problems(source, path=PurePosixPath(__file__)) == []
