# Arenadata Cluster Manager (ADCM)

## Project Overview

ADCM (Arenadata Cluster Manager) is a Django-based cluster management application for Arenadata's data platform products. It provides a REST API, web UI, and Ansible-based automation for deploying and managing distributed data clusters.

**Key technologies:**
- **Backend:** Python 3.10, Django 5.2, Django REST Framework 3.14
- **Frontend:** React 18, Redux Toolkit, React Router 6, Vite 5, SCSS, Storybook 7, Monaco Editor. Package manager: Yarn. Source located in `adcm-web/app/`.- **Status Server:** Go 1.23
- **Database:** PostgreSQL 13+
- **Task Queue & Service Discovery:** Scheduler modes: Local executor (default), Celery (optional).
- **Consul** is used for backend service discovery and as a KV control transport for Celery workers.
- **Automation:** Ansible Core 2.16.4 + custom Ansible plugins/collections
- **DI Container:** Dishka
- **Package Manager:** Poetry
- **Container:** Docker (multi-stage build, Alpine-based)

## Architecture

The codebase follows a layered architecture currently in stabilization (see `python/CODESTYLE.md`):

1. **Core** (`python/core/`) — Framework-independent services, generic utilities, and infrastructure interfaces.
2. **Repository / Implementations** (`python/cm/`, `python/rbac/`, `python/audit/`) — Framework/infrastructure-dependent code (Django models, DB queries).
3. **Use Cases** (`python/use_cases/`) — Business logic that orchestrates Core services and Repositories.
4. **Controllers / Entry Points** (`python/api_v2/`, `python/task_runner.py`, `python/ansible_plugin/`) — Orchestrators that validate inputs and call Use Cases via DI.

Other key packages:
- `python/adcm/` — Django settings, URLs, WSG config, API schema
- `python/infra/` — Infrastructure utilities
- `python/integrations/` — External system integrations
- `python/jobs/` — Job execution logic
- `python/application/` — Application bootstrapping and scripts
- `python/application/di` — Application DI containers
- `python/ansible_collections/` — Custom Ansible collection (`arenadata.adcm`)
- `python/health/` — Application health check logic
- `adcm-web/app/` — Frontend source (React/Redux/Vite)
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
- Access to `hub.arenadata.io` and `hub.docker.com` registries

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
# Starts PostgreSQL 14 container, runs tests
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

**With external PostgreSQL:**
```sh
docker run -d --restart=always -p 8000:8000 -v /opt/adcm:/adcm/data \
  -e DB_HOST="hostname" -e DB_PORT="5432" \
  -e DB_USER="username" -e DB_NAME="dbname" -e DB_PASS="password" \
  --name adcm hub.arenadata.io/adcm/adcm:latest
```

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

### Dependency Injection (Dishka)
- **Rule:** Entry points (API views, management commands) must NEVER instantiate services directly. They must only validate inputs and retrieve services from the DI container.
- **Registration:** When adding a new core service or repository, you must register it in the Dishka providers located within `python/application/` (specifically the DI configuration modules).- **Protocols:** Core services should depend on interfaces (Protocols) for infrastructure dependencies, not concrete Django models.

### Testing Conventions
- Unit tests live in `python/*/tests/` directories
- **No `conftest.py`:** Do not create `conftest.py` files. 
- **Base Class:** All Django/Integration tests must inherit from `python.tests.base.BaseTestCase` (or `WithPreparedFSAndInitADCM` for pure unit tests).
- **Helpers:** Use the built-in helper methods from `BaseTestCase` (e.g., `self.upload_and_load_bundle()`, `self.create_cluster()`, `self.create_policy()`) instead of writing custom setup logic.
- **DI in Tests:** The `BaseTestCase` automatically initializes the Dishka container and exposes it via `self.uc` (UseCases).
- PostgreSQL 13+ required for test database


### Routine Commands
- **Add Python dependency:** `poetry add <package>` (Remember to commit `poetry.lock`)
- **Create DB migration:** `poetry run python python/manage.py makemigrations <app_name>`
- **Update API Schema:** `poetry run python python/manage.py spectacular --file python/adcm/api_schema.yaml`
- **Run Frontend Dev Server:** `cd adcm-web/app && yarn dev`

## Notable Configuration

- **Django settings:** `python/adcm/settings.py` and `python/adcm/settings_setups/` (test, build, etc.)
- **API:** DRF with OpenAPI schema via `drf-spectacular`, camel-case naming via `djangorestframework-camel-case`
- **Authentication:** LDAP (`django-auth-ldap`), OAuth (`social-auth-app-django`), RBAC (`django-guardian`)
- **Security:** CSP headers (`django-csp`), CORS (`django-cors-headers`)
- **Secrets:** Python-GnuPG for bundle signature verification, Vault integration (`hvac`)
- **Config:** `ruyaml`, `pyyaml`, `pydantic`, `pydantic-settings` for settings management
- **Service Discovery:** HashiCorp Consul integration for backend registration and Celery worker coordination.

## Commits and PRs

- Write commit messages focused on user impact, not implementation details. The commit message **body** should describe **why** the change is made — the motivation and context — and **never what** the change is. The diff already shows what changed; restating it in prose adds noise.
- Keep dependencies in sync: If you add or update dependencies, remember to update the appropriate lockfile (`poetry.lock` or `yarn.lock`).
- Keep API schema in sync: If you change the API contract, generate the new schema via `poetry run python python/manage.py spectacular --file python/adcm/api_schema.yaml`.
- Always run `make pretty` and `make unittests` before committing any code.

## Never
- Leave `print()` statements in the code. Always use the standard Python `logging` module.
- Edit generated files by hand when a generation workflow exists.
- Commit secrets, credentials, or tokens.
- Use destructive git operations unless explicitly requested.
- Push directly to `develop` and `master` branches
- Remove `develop` and `master` branches
- Change CODEOWNERS
- Change AGENTS.md "Never" section

## Ask first
- Destructive data or migration changes.

## Before writing code
Search for
- similar Use Cases 
- similar API 
- similar tests 
- similar repository implementation 
- before creating new abstractions.