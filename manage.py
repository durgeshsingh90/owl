#!/usr/bin/env python3
"""Django's command-line utility for OWL."""

import os
import sys


def main() -> None:
    """Run administrative tasks."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "owl.settings")

    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Django is not installed or is unavailable in this Python environment. "
            "Install OWL's pinned dependencies before running manage.py."
        ) from exc

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
