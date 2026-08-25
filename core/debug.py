"""Secret-safe exception reporting for OWL's local development runtime."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from django.conf import settings
from django.views.debug import ExceptionReporter, SafeExceptionReporterFilter


class OWLExceptionReporterFilter(SafeExceptionReporterFilter):
    """Keep credential-shaped values out of reports, including with DEBUG enabled."""

    hidden_settings = re.compile(
        r"API|AUTH|TOKEN|KEY|SECRET|PASS|PAT|CREDENTIAL|SIGNATURE|HTTP_COOKIE",
        flags=re.IGNORECASE,
    )

    def is_active(self, request) -> bool:
        """Redaction is mandatory in OWL, even on a loopback debug server."""

        return True

    def get_post_parameters(self, request):
        if request is None:
            return {}

        cleansed = request.POST.copy()
        for name in request.POST:
            if self.hidden_settings.search(str(name)):
                cleansed.setlist(name, [self.cleansed_substitute])
        return super().get_cleansed_multivaluedict(request, cleansed)


def _iter_sensitive_request_values(request, pattern: re.Pattern[str]) -> Iterable[str]:
    if request is None:
        return

    for values in (request.GET, request.POST):
        for name, value_list in values.lists():
            if pattern.search(str(name)):
                for value in value_list:
                    if value:
                        yield str(value)

    for name, value in request.COOKIES.items():
        if pattern.search(str(name)) and value:
            yield str(value)

    for name, value in request.META.items():
        if pattern.search(str(name)) and value:
            yield str(value)


def _redact_text(value: str, sensitive_values: set[str], substitute: str) -> str:
    redacted = value
    for sensitive_value in sorted(sensitive_values, key=len, reverse=True):
        redacted = redacted.replace(sensitive_value, substitute)
    return redacted


def _redact_nested(value: Any, sensitive_values: set[str], substitute: str) -> Any:
    if isinstance(value, str):
        return _redact_text(value, sensitive_values, substitute)
    if isinstance(value, dict):
        return {
            key: _redact_nested(item, sensitive_values, substitute) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_nested(item, sensitive_values, substitute) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_nested(item, sensitive_values, substitute) for item in value)
    return value


class OWLExceptionReporter(ExceptionReporter):
    """Remove known request/configuration secrets from HTML and text tracebacks."""

    def get_traceback_data(self):
        data = super().get_traceback_data()
        pattern = self.filter.hidden_settings
        sensitive_values = set(_iter_sensitive_request_values(self.request, pattern))
        environment_pat = getattr(settings, "CONFLUENCE_PAT", "")
        if environment_pat:
            sensitive_values.add(str(environment_pat))

        if self.request is not None:
            data["request_GET_items"] = [
                (name, self.filter.cleanse_setting(name, value))
                for name, value in self.request.GET.items()
            ]
            data["request_insecure_uri"] = "Request URI redacted by OWL."

        if sensitive_values:
            exception_value = data.get("exception_value")
            if exception_value is not None:
                data["exception_value"] = _redact_text(
                    str(exception_value),
                    sensitive_values,
                    self.filter.cleansed_substitute,
                )
            data = _redact_nested(
                data,
                sensitive_values,
                self.filter.cleansed_substitute,
            )
        return data
