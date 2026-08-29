from datetime import timedelta

from django.db import migrations
from django.utils import timezone

TERMINAL_STATUSES = (
    "succeeded",
    "succeeded_with_errors",
    "failed",
    "interrupted",
)
RETRYABLE_ERROR_CODES = (
    "access_denied",
    "invalid_credential",
    "rate_limited",
    "refresh_error",
    "unreachable",
    "unsupported_response",
)


def _needs_retry(run) -> bool:
    if run.status in {"failed", "interrupted"}:
        return True
    return (
        run.status == "succeeded_with_errors"
        and run.failures.filter(error_code__in=RETRYABLE_ERROR_CODES).exists()
    )


def backfill_refresh_schedule_history(apps, schema_editor):
    RefreshRun = apps.get_model("bookmark_manager", "BookmarkRefreshRun")
    RefreshSchedule = apps.get_model("bookmark_manager", "BookmarkRefreshSchedule")
    latest = (
        RefreshRun.objects.filter(
            status__in=TERMINAL_STATUSES,
            completed_at__isnull=False,
        )
        .order_by("-completed_at", "-id")
        .first()
    )
    if latest is None:
        return

    latest_success = None
    for candidate in RefreshRun.objects.filter(
        status__in=("succeeded", "succeeded_with_errors"),
        completed_at__isnull=False,
    ).order_by("-completed_at", "-id"):
        if not _needs_retry(candidate):
            latest_success = candidate
            break

    retry_required = _needs_retry(latest)
    observed_at = timezone.now()
    delay = timedelta(hours=2) if retry_required else timedelta(days=7)
    schedule, _created = RefreshSchedule.objects.get_or_create(pk=1)
    schedule.last_run = latest
    schedule.last_attempt_at = latest.completed_at
    schedule.last_success_at = latest_success.completed_at if latest_success else None
    schedule.consecutive_failures = 1 if retry_required else 0
    schedule.next_run_at = max(observed_at, latest.completed_at + delay)
    schedule.save(
        update_fields=(
            "last_run",
            "last_attempt_at",
            "last_success_at",
            "consecutive_failures",
            "next_run_at",
            "updated_at",
        )
    )


class Migration(migrations.Migration):
    dependencies = [
        ("bookmark_manager", "0015_bookmarkrefreshrun_trigger_bookmarkrefreshschedule"),
    ]

    operations = [
        migrations.RunPython(backfill_refresh_schedule_history, migrations.RunPython.noop),
    ]
