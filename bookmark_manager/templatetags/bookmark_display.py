"""Accessible, compact display helpers for bookmark dates and status text."""

from __future__ import annotations

from datetime import datetime

from django import template
from django.utils import timezone

register = template.Library()


@register.filter
def relative_datetime(value: datetime | None) -> str:
    """Return a concise relative label while templates retain the exact timestamp.

    This filter intentionally uses calendar dates for Today and Yesterday. The caller
    must place the original ISO value in a ``time`` element so the compact label never
    hides exact, timezone-aware information from pointer or keyboard users.
    """

    if value is None:
        return "Not available"

    current = timezone.localtime(timezone.now())
    candidate = timezone.localtime(value) if timezone.is_aware(value) else value
    day_delta = (current.date() - candidate.date()).days

    if day_delta <= 0:
        return "Today"
    if day_delta == 1:
        return "Yesterday"
    if day_delta < 30:
        return f"{day_delta} days ago"
    if day_delta < 365:
        months = max(1, day_delta // 30)
        return f"{months} month{'s' if months != 1 else ''} ago"
    if candidate.year == current.year:
        return "This year"

    years = max(1, current.year - candidate.year)
    return f"{years} year{'s' if years != 1 else ''} ago"
