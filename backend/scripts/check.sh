#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIRECTORY="$(dirname "${SCRIPT_DIRECTORY}")"
cd "${PROJECT_DIRECTORY}"

# Keep routine checks synthetic even if the developer's shell contains real
# integration credentials. Test-specific monkeypatches can still exercise the
# configuration boundary without contacting an external service.
export DJANGO_SECRET_KEY="local-check-only-synthetic-secret-key-not-for-real-use-0123456789"
export DJANGO_DEBUG="false"
export CONFLUENCE_BASE_URL=""
export CONFLUENCE_PAT=""
export CONFLUENCE_SECRET_BACKEND="memory"
export OWL_ALLOW_IN_MEMORY_SECRET_STORE="true"
export OWL_ALLOW_NON_LOOPBACK="false"
export OWL_ALLOW_LIVE_EXTERNAL_TESTS="false"

uv lock --check
uv sync --locked --all-extras
npm --prefix ../frontend ci --ignore-scripts --no-audit --no-fund
npm --prefix ../frontend run check
uv run --locked python scripts/check_tracked_files.py
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked python manage.py check
uv run --locked python manage.py makemigrations --check --dry-run
uv run --locked coverage run -m pytest
uv run --locked coverage report
