from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.test import override_settings

from bitbucket_search.services.git_output import (
    MAX_LINE_CHARACTERS,
    MAX_LOG_CHARACTERS,
    MAX_LOG_LINES,
    MAX_RAW_LINE,
    RepositoryGitLog,
    bounded_git_output,
    sanitize_git_output,
)


def test_ordinary_transport_progress_and_actionable_failures_are_preserved():
    output = (
        "remote: Counting objects: 100% (12/12), done.\r"
        "Receiving objects: 50% (6/12), 1.23 MiB | 4.56 MiB/s\r\n"
        "Resolving deltas: 100% (3/3), done.\n"
        "fatal: unable to access remote: SSL certificate problem\n"
        "Permission denied (publickey).\n"
        "Already up to date.\n"
    )

    assert sanitize_git_output(output).splitlines() == [
        "remote: Counting objects: 100% (12/12), done.",
        "Receiving objects: 50% (6/12), 1.23 MiB | 4.56 MiB/s",
        "Resolving deltas: 100% (3/3), done.",
        "fatal: unable to access remote: SSL certificate problem",
        "Permission denied (publickey).",
        "Already up to date.",
    ]


@pytest.mark.parametrize("scheme", ["http", "https", "ssh", "git", "ftp", "git+ssh", "file"])
def test_entire_urls_are_removed_including_userinfo_paths_query_and_fragment(scheme):
    user = "synthetic-user-never-valid"
    value = "synthetic-value-never-valid"
    output = (
        f"fatal: unable to access '{scheme}://{user}:{value}@example.invalid/"
        f"private-location?signature={value}#fragment-{value}': connection refused"
    )

    safe = sanitize_git_output(output)

    assert "[remote URL]" in safe
    assert "connection refused" in safe
    for private in (user, value, "example.invalid", "private-location", "signature", "fragment"):
        assert private not in safe


@pytest.mark.parametrize(
    "address",
    [
        "git@example.invalid:private-location/repository.git",
        "git@[2001:db8::1]:private-location/repository.git",
        "synthetic-user:synthetic-value@example.invalid:private-location/repository.git",
    ],
)
def test_scp_style_ssh_addresses_are_not_left_in_output(address):
    assert sanitize_git_output(f"fatal: '{address}' could not be read") == (
        "fatal: '[remote URL]' could not be read"
    )


@pytest.mark.parametrize(
    "label",
    [
        "Authorization: Basic",
        "AUTHORIZATION: Bearer",
        "Authorization: Negotiate",
        "Proxy-Authorization: Digest",
        "Cookie: session=",
        "Set-Cookie: session=",
        "password=",
        "passwd:",
        '"password":',
        "'api_key':",
        "http.extraHeader=Authorization:",
        "credentials:",
        "passphrase:",
        "access_token=",
        "refresh-token:",
        "client_secret=",
        "pat=",
        "TOKEN:",
        "secret=",
        "username=",
        "Bearer",
        "Basic",
    ],
)
def test_credential_marked_lines_are_hidden_including_quoted_or_spaced_values(label):
    value = "synthetic-value never-valid, still-private; not-for-use"

    safe = sanitize_git_output(f"remote: {label} '{value}'")

    assert safe == "[Credential-related Git output hidden]"
    assert "still-private" not in safe


@pytest.mark.parametrize(
    "parts",
    [
        ("ghp_", "a" * 36),
        ("gho_", "b" * 36),
        ("github_pat_", "c" * 64),
        ("glpat-", "d" * 20),
        ("ATATT", "e" * 32),
        ("AKIA", "F" * 16),
        ("ASIA", "G" * 16),
        ("xoxb-", "1234567890-abcdefgh"),
        ("eyJ", "abcdef.ghijkl.mnopqr"),
    ],
)
def test_provider_shaped_bare_values_are_redacted_without_a_label(parts):
    value = "".join(parts)

    safe = sanitize_git_output(f"remote: rejected {value}")

    assert value not in safe
    assert "[REDACTED]" in safe


@override_settings(
    CONFLUENCE_PAT="synthetic-log-pat-never-valid-privacy-check",
    SECRET_KEY="synthetic-test-secret-key-only-not-for-real-use-privacy-check",
)
def test_known_application_secrets_are_redacted_even_without_labels(settings):
    output = f"remote: {settings.CONFLUENCE_PAT}\nremote: {settings.SECRET_KEY}"

    safe = sanitize_git_output(output)

    assert settings.CONFLUENCE_PAT not in safe
    assert settings.SECRET_KEY not in safe


@pytest.mark.parametrize("control", ["\x00", "\x0b", "\x0c", "\x85", "\u200b", "\u2028"])
def test_control_characters_cannot_split_sensitive_markers(control):
    value = "synthetic-value-never-valid"

    safe = sanitize_git_output(f"pass{control}word: {value}")

    assert safe == "[Credential-related Git output hidden]"


def test_ansi_formatting_is_removed_before_matching_sensitive_markers():
    value = "synthetic-value-never-valid"
    output = f"\x1b[31mAuth\x1b[0morization: Bearer {value}\x1b[0m"

    assert sanitize_git_output(output) == "[Credential-related Git output hidden]"
    assert sanitize_git_output("\x1b[32mReceiving objects: 50%\x1b[0m") == (
        "Receiving objects: 50%"
    )


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("\x1b]", "\x07"),
        ("\x1b]", "\x1b\\"),
        ("\x9d", "\x9c"),
        ("\x1bP", "\x1b\\"),
        ("\x1b_", "\x1b\\"),
    ],
)
def test_terminal_control_strings_and_multiline_payloads_are_omitted(start, end):
    value = "synthetic-value-never-valid"
    output = f"before{start}52;c;{value}\nmore-{value}{end}after"

    assert sanitize_git_output(output) == "beforeafter"


def test_incomplete_terminal_control_string_is_not_returned_as_visible_text():
    assert sanitize_git_output("Receiving objects: 50%\x1b]52;c;synthetic-value-never-valid") == (
        "Receiving objects: 50%"
    )
    assert sanitize_git_output("synthetic-value-never-valid\x1b[31") == (
        "[Terminal-control Git output hidden]"
    )


def test_backspace_output_is_hidden_instead_of_interpreted_as_a_terminal():
    safe = sanitize_git_output("remote: passX\bword: synthetic-value-never-valid")

    assert "synthetic-value-never-valid" not in safe
    assert "\x08" not in safe


def test_unicode_compatibility_forms_are_normalized_before_secret_matching():
    assert sanitize_git_output("ｐａｓｓｗｏｒｄ: synthetic-value-never-valid") == (
        "[Credential-related Git output hidden]"
    )


@pytest.mark.parametrize("key_type", ["PRIVATE KEY", "RSA PRIVATE KEY", "OPENSSH PRIVATE KEY"])
def test_complete_private_key_blocks_are_hidden_including_unstructured_short_body(key_type):
    output = (
        "Receiving objects: 50%\n"
        f"-----BEGIN {key_type}-----\n"
        "short-synthetic-body-never-valid\n"
        "another-synthetic-line-never-valid\n"
        f"-----END {key_type}-----\n"
        "Receiving objects: 100%"
    )

    safe = sanitize_git_output(output)

    assert "short-synthetic" not in safe
    assert "another-synthetic" not in safe
    assert "PRIVATE KEY" not in safe
    assert safe.startswith("Receiving objects: 50%\n")
    assert safe.endswith("\nReceiving objects: 100%")


def test_incomplete_private_key_block_hides_remaining_lines():
    key_type = "PRIVATE KEY"
    output = f"-----BEGIN {key_type}-----\nshort-synthetic-body-never-valid\nremaining"

    safe = sanitize_git_output(output)

    assert "short-synthetic-body-never-valid" not in safe
    assert "remaining" not in safe


@pytest.mark.parametrize("label", ["password:", '"token":', "Authorization=", "private key:"])
def test_credential_only_label_hides_its_following_line(label):
    output = f"remote: {label}\nsynthetic-value-never-valid\nReceiving objects: 100%"

    safe = sanitize_git_output(output)

    assert "synthetic-value-never-valid" not in safe
    assert safe.endswith("Receiving objects: 100%")


@pytest.mark.parametrize("prefix", ["", "remote: "])
def test_unlabeled_long_encoded_key_material_is_hidden(prefix):
    value = "Abc123+/" * 8 + "=="

    safe = sanitize_git_output(f"{prefix}{value}")

    assert value not in safe
    assert "[REDACTED]" in safe


def test_overlong_raw_line_is_omitted_whole_instead_of_exposing_a_suffix():
    value = "synthetic-value-never-valid"
    output = f"password={'x' * MAX_RAW_LINE}{value}\nReceiving objects: 100%"

    safe = sanitize_git_output(output)

    assert safe == "[Git output line omitted: too long]\nReceiving objects: 100%"
    assert value not in safe


def test_sanitizing_precedes_display_truncation():
    output = "Receiving objects: " + " ." * MAX_LINE_CHARACTERS + " password=private-value"

    assert sanitize_git_output(output) == "[Credential-related Git output hidden]"


def test_display_lines_are_bounded_without_cutting_into_long_raw_tail():
    output = "Receiving objects: " + " ." * MAX_LINE_CHARACTERS

    safe = sanitize_git_output(output)

    assert len(safe) == MAX_LINE_CHARACTERS


def test_bounded_tail_keeps_only_latest_lines():
    output = "\n".join(f"Receiving objects: item {number}" for number in range(MAX_LOG_LINES + 3))

    safe, truncated = bounded_git_output(output)

    assert truncated
    assert len(safe.splitlines()) == MAX_LOG_LINES
    assert safe.splitlines()[0] == "Receiving objects: item 3"
    assert safe.splitlines()[-1] == f"Receiving objects: item {MAX_LOG_LINES + 2}"


def test_bounded_tail_obeys_total_character_limit_and_retains_whole_lines():
    output = "\n".join(
        f"Receiving objects: item {number:03}: " + " ." * 480 for number in range(MAX_LOG_LINES)
    )

    safe, truncated = bounded_git_output(output)

    assert truncated
    assert len(safe) <= MAX_LOG_CHARACTERS
    assert len(safe.splitlines()) < MAX_LOG_LINES
    assert safe.splitlines()[0].startswith("Receiving objects: item ")
    assert safe.splitlines()[-1].startswith(f"Receiving objects: item {MAX_LOG_LINES - 1:03}:")


@pytest.mark.parametrize(
    "output",
    [
        "remote: Receiving objects: 100%\rAlready up to date.\r\n",
        "Authorization: synthetic-value-never-valid",
        "remote: https://example.invalid/private?signature=synthetic-value-never-valid",
        "\n".join(
            (
                f"-----BEGIN {'PRIVATE KEY'}-----",
                "short-synthetic-body-never-valid",
                f"-----END {'PRIVATE KEY'}-----",
            )
        ),
        "before\x1b]52;c;synthetic-value-never-valid\x07after",
    ],
)
def test_redaction_is_idempotent_for_persisted_tail_rechecking(output):
    safe = sanitize_git_output(output)

    assert sanitize_git_output(safe) == safe
    assert bounded_git_output(safe) == (safe, False)


def test_empty_output_does_not_create_spurious_log_entries():
    assert sanitize_git_output("\r\n\t\x00\u200b  \n") == ""
    assert bounded_git_output("") == ("", False)


@pytest.fixture
def buffered_sink(monkeypatch):
    # The safety tests inspect only in-memory, already-sanitized lines. No
    # database, real worker, managed repository or user settings are involved.
    sink = RepositoryGitLog(SimpleNamespace(output_log="", output_log_truncated=False))
    monkeypatch.setattr(sink, "flush", Mock())
    return sink


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("\x1b]", "\x07"),
        ("\x1b]", "\x1b\\"),
        ("\x1b]", "\x9c"),
        ("\x9d", "\x07"),
        ("\x9d", "\x9c"),
        ("\x1bP", "\x1b\\"),
        ("\x1bP", "\x9c"),
        ("\x1b_", "\x1b\\"),
        ("\x1b^", "\x1b\\"),
        ("\x1bX", "\x1b\\"),
        ("\x90", "\x9c"),
        ("\x98", "\x9c"),
        ("\x9e", "\x9c"),
        ("\x9f", "\x9c"),
    ],
)
def test_sink_suppresses_control_string_payload_across_complete_line_appends(
    buffered_sink, start, end
):
    for part in (
        f"Receiving objects: 10%{start}synthetic-first-never-valid",
        "synthetic-middle-never-valid",
        f"synthetic-last-never-valid{end}Receiving objects: 100%",
    ):
        buffered_sink.append(part, operation="clone")
        assert "synthetic-" not in "\n".join(buffered_sink.lines)

    output = "\n".join(buffered_sink.lines)
    assert "Receiving objects: 10%" in output
    assert "Receiving objects: 100%" in output
    assert buffered_sink.control_string is None


def test_sink_handles_control_string_open_and_close_escapes_split_between_calls(buffered_sink):
    for part in (
        "Receiving objects: 10%\x1b",
        "]synthetic-first-never-valid",
        "synthetic-middle-never-valid\x1b",
        "\\Receiving objects: 100%",
    ):
        buffered_sink.append(part)
        assert "synthetic-" not in "\n".join(buffered_sink.lines)

    output = "\n".join(buffered_sink.lines)
    assert "Receiving objects: 10%" in output
    assert "Receiving objects: 100%" in output
    assert not buffered_sink.pending_escape
    assert not buffered_sink.control_string_escape
    assert buffered_sink.control_string is None


def test_sink_does_not_end_dcs_at_bell_which_only_terminates_osc(buffered_sink):
    buffered_sink.append("\x1bPsynthetic-first-never-valid")
    buffered_sink.append("\x07synthetic-middle-never-valid")
    assert not buffered_sink.lines
    buffered_sink.append("\x1b\\Receiving objects: 100%")

    assert "synthetic-" not in "\n".join(buffered_sink.lines)
    assert "Receiving objects: 100%" in "\n".join(buffered_sink.lines)


def test_sink_preserves_progress_line_breaks_around_control_strings(buffered_sink):
    buffered_sink.append(
        "Receiving objects: 10%\rReceiving objects: 20%\r\n"
        "\x1b]synthetic-first-never-valid\r\nsynthetic-middle-never-valid\x07"
        "Receiving objects: 30%\nReceiving objects: 40%"
    )

    assert [line.partition("] ")[2] for line in buffered_sink.lines] == [
        "Receiving objects: 10%",
        "Receiving objects: 20%",
        "Receiving objects: 30%",
        "Receiving objects: 40%",
    ]


def test_sink_removes_control_string_before_matching_credential_marker(buffered_sink):
    buffered_sink.append(
        "pass\x1b]synthetic-control-never-valid\x07word: synthetic-value-never-valid"
    )

    assert "synthetic-" not in "\n".join(buffered_sink.lines)
    assert "[Credential-related Git output hidden]" in "\n".join(buffered_sink.lines)


def test_sink_keeps_normal_ansi_formatted_git_progress_visible(buffered_sink):
    buffered_sink.append("\x1b[32mReceiving objects: 50%\x1b[0m")
    buffered_sink.append("\x9b32mReceiving objects: 100%\x9b0m")

    assert [line.partition("] ")[2] for line in buffered_sink.lines] == [
        "Receiving objects: 50%",
        "Receiving objects: 100%",
    ]


def test_sink_unterminated_control_payload_is_never_buffered_or_released(buffered_sink):
    buffered_sink.append("Receiving objects: 10%\x1b]")
    original_lines = tuple(buffered_sink.lines)
    for _ in range(3):
        buffered_sink.append("synthetic-unlabelled-never-valid " * MAX_RAW_LINE)

    assert tuple(buffered_sink.lines) == original_lines
    assert buffered_sink.control_string == "osc"
    assert "synthetic-" not in repr(vars(buffered_sink))


def test_control_state_is_scoped_to_one_sink(buffered_sink, monkeypatch):
    buffered_sink.append("\x1b]synthetic-value-never-valid")
    other = RepositoryGitLog(SimpleNamespace(output_log="", output_log_truncated=False))
    monkeypatch.setattr(other, "flush", Mock())

    other.append("Receiving objects: 100%")

    assert not buffered_sink.lines
    assert "Receiving objects: 100%" in "\n".join(other.lines)
    assert other.control_string is None


def test_empty_lines_and_hidden_control_strings_do_not_consume_pending_credential_value(
    buffered_sink,
):
    for part in (
        "password:\r\n",
        "\x1b]synthetic-control-never-valid",
        "synthetic-control-continuation-never-valid\x07\r\n",
        "\t\u200b",
        "Authorization:",
        "synthetic-unlabelled-never-valid",
        "Receiving objects: 100%",
    ):
        buffered_sink.append(part)

    output = "\n".join(buffered_sink.lines)
    assert "synthetic-" not in output
    assert "Receiving objects: 100%" in output
    assert not buffered_sink.hide_next_line


def test_pure_sanitizer_keeps_pending_credential_value_across_blank_lines():
    output = "password:\r\n\r\n\t\u200b\nsynthetic-unlabelled-never-valid\nReceiving objects: 100%"

    safe = sanitize_git_output(output)

    assert "synthetic-" not in safe
    assert "Receiving objects: 100%" in safe
