# Arenadata Cluster Manager (ADCM)

## Project Overview

ADCM (Arenadata Cluster Manager), internally codenamed "Chapelnik", is a Django-based cluster management application for Arenadata's data platform products. It provides a REST API, web UI, and Ansible-based automation for deploying and managing distributed data clusters.

**Key technologies:**
- **Backend:** Python 3.10, Django 5.2, Django REST Framework 3.14
- **Frontend:** Node.js 20 (Alpine-based build)
- **Status Server:** Go 1.23
- **Database:** PostgreSQL 13+
- **Task Queue:** Celery (optional, with Kombu/SQLAlchemy for DB transport)
- **Automation:** Ansible Core 2.16.4 + custom Ansible plugins/collections
- **DI Container:** Dishka
- **Package Manager:** Poetry
- **Container:** Docker (multi-stage build, Alpine-based)

## Architecture

The codebase follows a layered architecture currently in stabilization (see `python/CODESTYLE.md`):

1. **Core** (`python/core/`) — Framework-independent services with minimal inter-dependencies. Includes generic utilities, types, error handling, and feature-specific services (config, mapping, cluster, provider, bundle, action, templates, secrets, logs, upgrade).
2. **Use Cases** (`python/use_cases/`) — Business logic orchestrating core services.
3. **Implementations** (`python/cm/`, `python/rbac/`, `python/audit/`) — Framework/infrastructure-dependent code (Django models, views, migrations).
4. **Entry Points** — REST API (`python/api_v2/`), job runner (`python/task_runner.py`), Ansible plugins (`python/ansible_plugin/`), Django management commands.

Other key packages:
- `python/adcm/` — Django settings, URLs, WSG config, API schema
- `python/infra/` — Infrastructure utilities
- `python/integrations/` — External system integrations
- `python/jobs/` — Job execution logic
- `python/application/` — Application bootstrapping and scripts
- `python/application/di` — Application DI containers
- `python/ansible_collections/` — Custom Ansible collection (`arenadata.adcm`)
- `adcm-web/` — Frontend source (JS/HTML/CSS)
- `go/` — Go status server
- `conf/` — Configuration files and scripts
- `dev/` — Development tooling (linters, scripts)

## Building and Running

### Prerequisites
- GNU Make
- Docker daemon
- Poetry (for Python dependency management)
- Node.js 20 (for frontend builds)
- Go 1.23 (for status server)
- Access to `hub.adsw.io` and `ci.arenadata.io` registries

### Quick Start (Development)

```sh
# Install Python dependencies
poetry install --no-root

# Run Django migrations
poetry run python python/manage.py migrate

# Initialize database
poetry run python python/init_db.py

# Upgrade roles
poetry run python python/manage.py upgraderole

# Run dev server
poetry run python python/manage.py runserver --insecure
```

### Build Docker Image

```sh
make build
# Produces: hub.adsw.io/adcm/adcm:<branch_name>
```

### Build Sub-components

```sh
make buildjs    # Build frontend (JS/HTML/CSS)
make buildss    # Build status server (Go)
make build2js   # Build frontend for new design + API v2
make build2     # Build full Docker image with new design + API v2
```

### Run Unit Tests

```sh
make unittests
# Starts PostgreSQL 14 container, runs tests with pytest, stops container
```

### Linting and Formatting

```sh
make pretty   # Format code with ruff, fix lint issues, check licenses
make lint     # Run ruff check, ruff format --check, pyright, import-linter, license/migration checkers
```

### Pre-commit Hook

```sh
pip install pre-commit
pre-commit install
# Runs lint on every commit
```

### Running in Docker

PostgreSQL 13 or newer is required.

```sh
docker run -d --restart=always -p 8000:8000 -v /opt/adcm:/adcm/data \
  -e DB_HOST="hostname" -e DB_PORT="5432" \
  -e DB_USER="username" -e DB_NAME="dbname" -e DB_PASS="password" \
  --name adcm hub.arenadata.io/adcm/adcm:latest
```

`DB_PORT` is optional and defaults to `5432`.

**Set log level:** Add `-e LOG_LEVEL="DEBUG|INFO|WARNING|ERROR|CRITICAL"` (defaults to `ERROR`).

## Development Conventions

### Code Style
- **Formatter:** `ruff format` (line length: 120)
- **Linter:** `ruff check` with selected rules (F, E, W, I, N, UP, etc.)
- **Type checking:** `pyright` in standard mode (Python 3.10)
- **Import ordering:** `isort` via ruff, force-sorted within sections
- **Import architecture:** `import-linter` enforces layer contracts (see `pyproject.toml`)

### Key Rules
- Silencing linters is discouraged; requires explanatory comments when necessary
- Core modules (`python/core/`) must NOT import from other top-level packages (`cm`, `rbac`, `api_v2`, `audit`, `jobs`, etc.)
- Core modules must not import `core.bundle` (enforced by import-linter)
- Migration files have relaxed linting rules (ARG001, N806, N999 ignored)

### Testing
- Unit tests live in `python/*/tests/` directories
- Tests run with Django's test framework (`manage.py test`)
- PostgreSQL 14 required for test database
- Tests run in parallel with `--keepdb` flag

### Git Workflow
- CI/CD via GitLab (`.gitlab-ci.yml` references shared pipeline from `arenadata/infrastructure/code/ci/gitlab_ci_files`)
- Branch naming convention affects Docker image tags

## Notable Configuration

- **Django settings:** `python/adcm/settings.py` and `python/adcm/settings_setups/` (test, build, etc.)
- **API:** DRF with OpenAPI schema via `drf-spectacular`, camel-case naming via `djangorestframework-camel-case`
- **Authentication:** LDAP (`django-auth-ldap`), OAuth (`social-auth-app-django`), RBAC (`django-guardian`)
- **Security:** CSP headers (`django-csp`), CORS (`django-cors-headers`)
- **Secrets:** Python-GnuPG for bundle signature verification, HashiCorp Vault integration (`hvac`)
- **Config:** `ruyaml`, `pyyaml`, `pydantic`, `pydantic-settings` for settings management
