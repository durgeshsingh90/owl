# OWL

OWL (Organised Workspace Locator) is a private, local workspace with two connected tools:

- a Confluence bookmark manager built around Page ID identity and the real page tree;
- a fast local search tool for PDFs synchronized from Git or Bitbucket repositories.

OWL runs on your own computer and listens only on `127.0.0.1`. The repository is public, but
your credentials, databases, repository checkouts, PDF contents, indexes, and logs must remain
local.

## Current implementation stage

Phase 3 implements the Bookmark Manager tree and productivity scope defined by the master
requirements and validated by work prompt `002`: secure Confluence setup, Page ID identity,
permanent OWL numbers, the real hierarchy, advanced local search/filter/sort views, notes, tags,
favorites, pins, usage tracking, saved views, and portable JSON import/export. Durable Confluence
refresh jobs, PDF repository synchronization, and indexing remain later numbered phases.

The shared UI has **Home**, **Bookmark Manager**, and **Bitbucket Search**. Home is a compact
overview; Bookmark Manager and Bitbucket Search are the two working tools. Each tool keeps its
own functions in a labelled left sidebar; shared System Status remains a footer/sidebar utility,
and unfinished Global Search is not presented as another application.

The complete product contract is in [work_prompts](work_prompts/README.md).

## What you need

- macOS, Linux, or Windows 10/11;
- Python 3.13 or 3.14;
- Git;
- an internet connection while cloning OWL and downloading the pinned Python packages.

You do not need a Confluence PAT for setup or automated tests. You need a Confluence Data Center
PAT only when you deliberately connect your own installation through OWL's local settings panel.
Never paste a real PAT into this repository, a test, a screenshot, or a support message.

## First-time setup

The project is already in the requested location on this Mac. Open Terminal and run these
commands exactly:

```bash
cd /Users/durgesh/Projects/owl
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install uv==0.12.5
uv sync --locked --all-extras
cp -n .env.example .env
python manage.py migrate
```

What those commands do:

1. open the existing OWL project;
2. create an isolated Python environment inside `.venv`;
3. install the exact locked application and development dependency versions;
4. create your ignored local settings file without replacing one that already exists;
5. create the local database under `var/`.

On another computer where OWL has not been downloaded, first run:

```bash
git clone git@github.com:durgeshsingh90/owl.git
cd owl
```

### Windows Command Prompt

On Windows with Python 3.13 installed, open Command Prompt and run:

```bat
cd C:\Users\YOUR_USERNAME\code\owl
py -3.13 -m venv .venv
.venv\Scripts\activate.bat
python --version
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if not exist .env copy .env.example .env
python manage.py migrate
python manage.py runserver
```

`python --version` must report Python 3.13.x or 3.14.x. The activated interpreter shown by
`where python` should be the `.venv\Scripts\python.exe` inside this checkout.

The first start creates a strong machine-local Django key under `var/secrets/` when
`DJANGO_SECRET_KEY` is blank. The key and the entire `var/` directory are ignored by Git.

## Run OWL

From `/Users/durgesh/Projects/owl`, run:

```bash
source .venv/bin/activate
python manage.py runserver 127.0.0.1:8000
```

Then open [http://127.0.0.1:8000/](http://127.0.0.1:8000/) in your browser. Press `Control-C`
in Terminal when you want to stop OWL.

Phase 3 has no separate background-worker command. A worker command will be added and documented
when durable refresh, repository synchronization, and indexing are implemented.

## Connect Confluence and save a bookmark

1. Open **Bookmark Manager**.
2. Select the **Confluence settings** gear in the top-right corner.
3. Enter the exact HTTPS base URL for your Confluence Data Center application, including its
   context path when it has one.
4. Enter a PAT, select **Test connection**, and review the sanitized result.
5. Select **Save settings**. On macOS, the PAT is stored in Keychain; SQLite contains only the
   non-secret origin, mode, and verification state.
6. Paste a modern or legacy Confluence page URL, or its numeric Page ID, into **Save bookmark**.

Reopening settings never returns the stored PAT. Changing to a different canonical origin requires
a new PAT. Removing the connection deletes the secure credential while keeping local bookmarks.
OWL's adapter sends bounded, read-only `GET` requests using the standard Confluence Data Center
Bearer-PAT flow; it does not create, edit, or delete Confluence content.

## Organize and reuse bookmarks

The Bookmark Manager keeps its hierarchy and every productivity field locally:

- type `/` to focus search, or search by title, Page ID, URL, space, person, breadcrumb, tag, or
  note;
- combine status, tag, person, space, date, usage, favorite, and pin filters, then save the current
  filter/sort combination as a named view;
- add quick notes and comma-separated tags, and mark favorites and pins independently;
- use recent, frequent, never-viewed, favorite, pinned, or flat sorted views without changing the
  stored Confluence tree;
- use the arrow keys to move through the tree, `E` to expand/collapse, `F` for favorite, `P` for
  pin, and `Enter` to open details;
- copy a Page ID, breadcrumb, or validated source URL, and open a page through OWL to update local
  usage and viewed-version history.

**Export JSON** downloads a versioned, SHA-256-protected local backup that excludes credentials and
connection configuration. **Import a backup** accepts current OWL exports and heterogeneous legacy
JSON collections, continues after malformed records, and reports sanitized record-level failures.
Re-importing is idempotent, and existing OWL-owned notes, tags, favorites, pins, and usage win over
incoming values. Deleting a bookmark requires confirmation and affects OWL only, never Confluence.

Tree expansion, selection, scroll position, and multi-selection are retained in the browser for
the local workspace. Refresh One, Refresh Selected, Refresh All, and source-driven change
transitions are intentionally part of Phase 4 because they require durable background jobs.

## Run all local checks

After activating `.venv`, run one command:

```bash
./scripts/check.sh
```

It checks formatting and code quality, validates Django and migrations, runs the synthetic test
suite with coverage, and confirms that Git's index and untracked, non-ignored files contain no
likely secrets or runtime data. It clears Confluence and Bitbucket connection settings for the
check process, so this normal quality gate cannot use a real integration accidentally.

To run only the read-only public-repository safety scan:

```bash
python scripts/check_tracked_files.py
```

That scan changes nothing. It reads tracked content directly from Git's staged index and reads
untracked, non-ignored files from the working tree. Therefore, replacing a staged private value
with a placeholder only in the working copy cannot hide it from the check. It reports file names,
line numbers, and problem types without printing suspected values. Runtime directories, secret
key files, PDFs, screenshots, and bookmark export artifacts are also rejected from public source.
This is a strong guardrail, not proof that arbitrary content can never contain private data;
review the staged diff before every public commit as well.

## Local configuration and PAT safety

`.env.example` contains documented defaults only. Your copied `.env` is private and ignored by
Git.

For normal interactive use, the Bookmark Manager configuration gear encrypts the Confluence PAT
with OWL's machine-local secret key. OWL prefers the operating-system credential store (macOS
Keychain or Windows Credential Manager) and automatically stores the encrypted payload in its
local SQLite database when that store is unavailable. A complete `CONFLUENCE_BASE_URL` plus
`CONFLUENCE_PAT` environment profile remains available for deliberate development or headless use.
An environment-managed profile takes precedence, remains blank in the browser, and cannot be
replaced or removed through the UI.

Public CI uses only blank or clearly synthetic connection values, a temporary database, an
isolated in-memory credential store, and tests marked to exclude every live external integration.

## Dependency versions

The versions are pinned in `pyproject.toml`:

- Django 6.1;
- cryptography 50.0.0;
- python-dotenv 1.2.3;
- keyring 25.7.0;
- pytest 9.1.1;
- pytest-django 4.14.0;
- coverage 7.15.4;
- Ruff 0.16.4.

`uv.lock` also fixes all transitive package versions across supported platforms. OWL uses
uv 0.12.5 to verify and install that universal lock; `./scripts/check.sh` fails if the lock no
longer matches `pyproject.toml`.

The shared shell also bundles Bootstrap 5.3.8 CSS locally under `static/vendor/`; OWL does not
load its UI framework from a CDN. The accompanying MIT license is stored with the asset.

## Repository

- Public source: [github.com/durgeshsingh90/owl](https://github.com/durgeshsingh90/owl)
- SSH address: `git@github.com:durgeshsingh90/owl.git`

No real credentials, private repository URLs, internal document data, local databases, indexed
files, logs, backups, or exports belong in this public repository.
