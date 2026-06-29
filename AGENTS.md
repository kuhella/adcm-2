# AGENTS.md -- ADCM (Arenadata Cluster Manager)

This file provides instructions for AI coding agents working on this repository.

---

## Project Overview

ADCM is a Django-based cluster management platform for deploying and managing distributed
data infrastructure. It consists of three main components:

- **Python backend** (`python/`) -- Django + DRF REST API, Ansible automation, Celery tasks
- **React frontend** (`adcm-web/app/`) -- TypeScript/React SPA with Redux Toolkit
- **Go status server** (`go/adcm/`) -- lightweight health monitoring service

**Key technologies:** Python 3.10, Django 5.2, DRF 3.14, Dishka (DI), Ansible Core 2.16.4,
React 18, Redux Toolkit, TypeScript 5.2, Vite, Biome, Go 1.23, PostgreSQL 13+, Docker.

---

## Repository Structure

```
python/                     # Python backend (Django)
    core/                   # Framework-independent services (clean architecture core)
    use_cases/              # Business logic orchestrating core services
    cm/                     # Cluster Manager -- Django models, ORM, legacy logic
    rbac/                   # RBAC -- roles, policies, permissions (django-guardian)
    audit/                  # Audit logging (CEF-compliant)
    api_v2/                 # REST API (DRF viewsets, serializers, filters)
    application/            # App bootstrapping, DI containers
    jobs/                   # Job execution logic
    infra/                  # Infrastructure utilities
    integrations/           # External system integrations
    ansible_plugin/         # Custom Ansible modules
    ansible_collections/    # Ansible collection (arenadata.adcm)
    adcm/                   # Django settings, URLs, WSGI config
    tests/                  # Cross-cutting test utilities
adcm-web/app/               # React frontend
    src/
        api/                # API client layer
        components/         # UI components (pages, uikit, common, layouts)
        hooks/              # Custom React hooks
        models/             # TypeScript type definitions
        store/              # Redux store (RTK slices)
        utils/              # Utility functions
go/adcm/                    # Go status server
    config/                 # Secrets/config loading (file system, Vault)
    status/                 # HTTP server, WebSocket, service map
conf/                       # System configuration, default ADCM bundles
dev/                        # Development tooling (custom linters, profiling)
```

---

## Code Style

See [codestyle.md](codestyle.md) for the comprehensive code style guide covering:

- Python formatting (ruff, pyright, import-linter)
- Frontend formatting (Biome, OxLint, TypeScript strict mode)
- Go formatting (gofmt, golangci-lint)
- License header requirements
- Naming conventions
- Architectural layer rules

**Quick reference:**

| Area     | Formatter         | Linter          | Line Length | Type Checker     |
|----------|-------------------|-----------------|-------------|------------------|
| Python   | `ruff format`     | `ruff check`    | 120         | `pyright` (standard) |
| Frontend | Biome             | Biome + OxLint  | 120         | `tsc` (strict)   |
| Go       | `gofmt -s`        | `golangci-lint` | standard    | Go compiler      |

---

## Architecture

The Python backend follows a **layered architecture** (status: STABILIZATION):

```
┌─────────────────────────────────────────────────────┐
│  Entry Points: api_v2, task_runner, ansible_plugin   │
├─────────────────────────────────────────────────────┤
│  Use Cases: python/use_cases/                        │
├─────────────────────────────────────────────────────┤
│  Implementations: cm, rbac, audit (Django/ORM)       │
├─────────────────────────────────────────────────────┤
│  Core: python/core/ (framework-independent)          │
└─────────────────────────────────────────────────────┘
```

**Critical import rules (enforced by import-linter, breaks CI if violated):**

1. `core/` must NOT import from: `cm`, `rbac`, `api_v2`, `audit`, `jobs`, `django`, `use_cases`, `infra`, `application`, `integrations`, `ansible_*`
2. `core.*` sub-modules must NOT import `core.bundle`
3. Core internal layers have a strict hierarchy -- lower layers cannot import from higher ones (see `pyproject.toml` `[[tool.importlinter.contracts]]`)

**Dependency injection:**
- Container: Dishka
- API views use `@inject` decorator with `FromDishka[ServiceType]` typed parameters
- Use cases receive dependencies via `@dataclass(slots=True)` constructor injection

---

## Development Setup

### Prerequisites

- Poetry (Python package manager)
- Node.js 20 + Yarn 4 (frontend)
- Go 1.23 (status server)
- Docker (for builds and test database)
- GNU Make

### Install and Run

```sh
# Install Python dependencies
poetry install --no-root

# Run Django migrations
poetry run python python/manage.py migrate

# Initialize database
poetry run python python/init_db.py

# Upgrade roles
poetry run python python/manage.py upgraderole

# Start dev server
poetry run python python/manage.py runserver --insecure
```

### Frontend

```sh
cd adcm-web/app
yarn install
yarn start          # Dev server (Vite)
yarn build          # Production build
yarn lint           # Biome + OxLint
yarn tsc            # Type check
yarn test           # Jest tests
```

### Docker Build

```sh
make build          # Full Docker image (python + frontend + go)
```

---

## Linting and Formatting

### Python

```sh
make pretty         # Auto-format and fix (ruff format + ruff check --fix + license headers)
make lint           # Full lint check (ruff + pyright + import-linter + license + migrations)
```

Lint covers files in: `python/`, `dev/linters/`, `conf/adcm/python_scripts/`

### Frontend

```sh
cd adcm-web/app
yarn lint           # Biome check + OxLint (import cycles)
yarn tsc            # TypeScript type check
```

### Go

```sh
cd go
make pretty         # gofmt
make lint           # golangci-lint
```

### Pre-commit Hooks

- **Root** (`.pre-commit-config.yaml`): runs `make lint` on Python file changes
- **Frontend** (`.husky/pre-commit`): runs `yarn lint-staged` + `yarn tsc`

Install with:
```sh
pip install pre-commit
pre-commit install
```

---

## Testing

### Python Unit Tests

```sh
make unittests
# Requires: Docker (starts PostgreSQL 14 container automatically)
# Settings: DJANGO_SETTINGS_MODULE=adcm.settings_setups.test
```

Or manually:
```sh
docker run -d --rm -e POSTGRES_PASSWORD=postgres --name adcm-pg -p 5500:5432 postgres:14
DJANGO_SETTINGS_MODULE=adcm.settings_setups.test DB_HOST=localhost DB_USER=postgres \
  DB_PORT=5500 DB_NAME=postgres DB_PASS=postgres PYTHONPATH=python \
  poetry run python python/manage.py test python --parallel --keepdb
```

- Tests live in `python/*/tests/` directories
- Use Django's test framework (`manage.py test`)
- Tests run in parallel with `--keepdb`
- Test base classes in `python/api_v2/tests/base.py` (provides `APIV2Mixin`, bundle helpers)
- Use `# ruff: noqa: S101` at the top of test files to allow `assert` statements

### Frontend Unit Tests

```sh
cd adcm-web/app
yarn test
```

- Framework: Jest with jsdom
- Test files: `*.test.ts`, `*.utils.test.ts`, co-located with source code

---

## Making Changes

### Before You Start

1. Read the relevant section of [codestyle.md](codestyle.md) for the component you are modifying
2. Understand the architectural layer of the code you are changing
3. Check if `import-linter` contracts apply to your changes

### Python Changes

- All new code must be type-annotated and pass `pyright` in standard mode
- Follow the existing service/repository pattern in `core/`:
  - `_service.py` for the service class (public API)
  - `_repo.py` for the repository protocol (infra interface)
  - `_types.py` for feature-specific types
  - `__init__.py` for public re-exports
- Use `@dataclass(slots=True)` for DTOs, value objects, and use cases
- Use `Protocol` for dependency interfaces (not ABCs)
- Use `Success[T]` / `Fail[T]` from `core.result` for operations that can fail
- Add Apache 2.0 license header to all new files
- Do not silence linter rules without a justifying comment

### Frontend Changes

- Use `import type` for type-only imports (enforced by Biome)
- Follow component co-location pattern: `Component/Component.tsx` + `.module.scss` + `.stories.tsx`
- Use path aliases (`@api`, `@uikit`, `@store`, etc.) instead of relative paths
- API calls go through static class methods in `src/api/`
- State management via Redux Toolkit slices in `src/store/`
- No circular imports (enforced by OxLint)

### Go Changes

- Follow standard Go conventions (`gofmt -s`)
- Add Apache 2.0 license header (comment style: `//`)
- Static binary build (`CGO_ENABLED=0`)

### Before Submitting

1. Run `make pretty` to auto-format Python code
2. Run `make lint` to verify all Python checks pass
3. Run `cd adcm-web/app && yarn lint && yarn tsc` for frontend changes
4. Run `cd go && make lint` for Go changes
5. Run tests relevant to your changes

---

## Key Patterns and Conventions

### DI Pattern (Dishka)

API views inject dependencies via `FromDishka`:

```python
from dishka import FromDishka
from api_v2.utils.di import inject

class ClusterViewSet(ADCMGenericViewSet):
    @inject
    def create(self, request, create_cluster: FromDishka[CreateCluster], **_):
        ...
```

### Use Case Pattern

Use cases are dataclasses with a `do()` method:

```python
@dataclass(slots=True)
class CreateSomething:
    some_service: SomeService
    repo: SomeRepoI

    def do(self, input_data: InputType) -> OutputType:
        ...
```

### Core Service Pattern

Services have dependencies via constructor, public methods as API:

```python
class ConfigService:
    def __init__(self, repo: ConfigRepoI, file_storage: FileStorageI):
        self._repo = repo
        self._storage = file_storage

    def get_configuration(self, owner: ConfigOwner) -> Configuration:
        ...
```

### Result Types

Use `Success`/`Fail` for operations that can fail without exceptions:

```python
from core.result import Success, Fail, is_success, is_fail

result = some_operation()
if is_success(result):
    use(result.value)
elif is_fail(result):
    handle_error(result.value)
```

### Error Localization

Use `localize_error()` to add context to domain errors:

```python
from core.errors import localize_error

with localize_error("Bundle from archive.tar"):
    service.process(data)
```

---

## Notable Configuration

- **Django settings:** `python/adcm/settings.py`, `python/adcm/settings_setups/` (test, build variants)
- **API schema:** DRF + `drf-spectacular` (OpenAPI), `djangorestframework-camel-case` (JSON naming)
- **Auth:** LDAP (`django-auth-ldap`), OAuth (`social-auth-app-django`), RBAC (`django-guardian`)
- **Security:** CSP (`django-csp`), CORS (`django-cors-headers`)
- **Secrets:** GnuPG (bundle signatures), HashiCorp Vault (`hvac`)
- **Config parsing:** `ruyaml`, `pyyaml`, `pydantic`, `pydantic-settings`
- **DRF version:** pinned at 3.14.0 (3.15 introduced unwanted serializer behavior changes)
