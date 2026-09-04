from __future__ import annotations

import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from bitbucket.services.git_sync import work_one_job
from bitbucket.services.scheduler import queue_due_daily_pulls


class Command(BaseCommand):
    help = "Process the independent Bitbucket document desk's clone and pull jobs."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--poll-interval", type=float, default=1.0)
        parser.add_argument("--idle-timeout", type=float, default=0.0)

    def handle(self, *args, **options) -> None:
        once = bool(options["once"])
        poll_interval = max(0.1, float(options["poll_interval"]))
        idle_timeout = max(0.0, float(options["idle_timeout"]))
        idle_since = time.monotonic()
        while True:
            close_old_connections()
            queue_due_daily_pulls()
            job = work_one_job()
            close_old_connections()
            if job is not None:
                idle_since = time.monotonic()
                self.stdout.write(f"{job.operation} {job.repository_id}: {job.status}")
            elif once or (idle_timeout and time.monotonic() - idle_since >= idle_timeout):
                return
            if once:
                return
            time.sleep(poll_interval)
