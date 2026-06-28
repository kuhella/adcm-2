# ADCM Code Style Guide

> Version: 1.0.0

This document defines the code style, formatting, and architectural rules
for all components of the ADCM project: Python backend, React frontend, and Go status server.

---

## Python Backend

### Formatting and Linting

| Tool             | Purpose                              | Config                  |
|------------------|--------------------------------------|-------------------------|
| `ruff format`    | Code formatter                       | `pyproject.toml`        |
| `ruff check`     | Linter (replaces flake8/pylint)      | `pyproject.toml`        |
| `pyright`        | Static type checker (standard mode)  | `pyproject.toml`        |
| `import-linter`  | Architectural layer enforcement      | `pyproject.toml`        |
| `license_checker`| Apache 2.0 header verification       | `dev/linters/`          |
| `migrations_checker` | Migration import restrictions    | `dev/linters/`          |

**Key settings:**
- Line length: **120 characters**
- Python version: **3.10**
- Ruff rule sets: `F`, `E`, `W`, `I`, `N`, `UP`, `YTT`, `ANN`, `S`, `BLE`, `FBT`, `B`, `COM`, `A`, `C4`, `DTZ`, `ICN`, `PIE`, `Q`, `RET`, `SIM`, `ARG`, `PTH`, `PLE`, `TRY`
- Import sorting: `isort` via ruff with `force-sort-within-sections`, `length-sort-straight`, `order-by-type`

### Silencing Linters

Silencing linters is **generally disallowed**.
Do it only when absolutely necessary and always provide a comment explaining the silencing:

```python
value = some_call()  # noqa: S101 -- assertion is used in test helpers
```

### License Header

Every Python (and Go) source file must start with the Apache 2.0 license header:

```python
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
```

Run `make pretty` to auto-fix missing headers, or `make lint` to check.

### Type Annotations

- `pyright` runs in **standard** mode (`typeCheckingMode = "standard"`)
- All new code must be fully type-annotated (arguments, return types, variables where not obvious)
- `reportUnnecessaryTypeIgnoreComment` is enabled -- do not leave stale `# type: ignore` comments
- Use `TypeAlias`, `NewType`, `NamedTuple`, `dataclass` for domain types (see `core/types.py` for patterns)
- Prefer `Protocol` over abstract base classes for dependency interfaces

### Import Rules

- Imports follow ruff isort rules: stdlib, third-party, first-party, each force-sorted within sections
- `import-linter` enforces architectural boundaries (see Architecture section below)
- **Core modules must NOT import:** `cm`, `rbac`, `api_v2`, `ansible_share`, `ansible_collections`, `ansible_plugin`, `audit`, `jobs`, `django`, `use_cases`, `infra`, `application`, `integrations`
- **Core sub-modules must NOT import** `core.bundle` (prevents circular dependencies)
- Migration files are allowed relaxed rules: `ARG001`, `N806`, `N999`
- Migration files may only import from a restricted set of standard/Django modules

### Module Conventions

- Private modules use underscore prefix: `_service.py`, `_repo.py`, `_types.py`
- Public API is re-exported via `__init__.py`
- Packages expose their API through `__init__.py` with explicit `__all__` or re-exports
- `__init__.py` controls import order when it matters (e.g., `core/__init__.py`)

### Naming

- Classes: `PascalCase`
- Functions/methods: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Type aliases: `PascalCase` (e.g., `ObjectID`, `ClusterID`)
- NewTypes: `PascalCase` (e.g., `CurrentADCMVersion = NewType(...)`)
- Protocols (interfaces): suffix with `I` (e.g., `ConfigRepoI`, `FileStorageI`)
- Private attributes/methods: underscore prefix (`_comment`, `_license_lines`)

### Dataclasses and Types

- Prefer `@dataclass(slots=True)` for value objects and DTOs
- Use `NamedTuple` for lightweight immutable records
- Use `TypeAlias` for type aliases: `ObjectID: TypeAlias = int`
- Use `NewType` for distinct semantic types: `ClusterID = NewType("ClusterID", int)`
- Use `Enum` for enumerations; prefer `auto()` values when the actual value doesn't matter

### Error Handling

- Project defines `Success[T]` / `Fail[T]` result types in `core/result.py`
- Use `is_success()` / `is_fail()` type guards for result checking
- Domain errors extend `ADCMLocalizedError` or `ADCMMessageError`
- Use `localize_error()` context manager for error location tracking
- Avoid bare `except Exception` unless wrapped in a result pattern (e.g., `fail_with_call_on_error`)

---

## Architecture

> Status: STABILIZATION

The backend follows a 4-layer architecture:

### 1. Core (`python/core/`)

Framework-independent services with minimal inter-dependencies.

- **Generic modules:** `types`, `errors`, `result`, `settings` -- shared across all core services
- **Universal services:** `templates`, `secrets` -- generic mechanisms used by multiple features
- **Feature services:** `config`, `mapping`, `cluster`, `provider`, `upgrade`, `logs` -- per-aspect
- **Multi-aspect services:** `bundle`, `action` -- combine multiple feature services
- **Scenarios:** `core.scenarios` -- stateless orchestrations of multiple services
- **Legacy:** `core.legacy` -- code being migrated; top of the layer hierarchy

**Service structure pattern:**
```
core/<feature>/
    __init__.py          # Public API re-exports
    _service.py          # Service class (top-level API with DI dependencies)
    _repo.py             # Repository protocol (infra interface)
    _types.py            # Feature-specific types
    _operations.py       # Low-level rules/functions (no external interfaces)
```

**Layer hierarchy** (enforced by `import-linter`):
```
core.legacy
core.scenarios
core.bundle
core.action
core.config | core.mapping | core.cluster | core.provider | core.upgrade | core.logs
core.templates | core.secrets
core.errors | core.settings
core.types
core.result
```

Lower layers must not import from higher layers.

### 2. Use Cases (`python/use_cases/`)

Business logic that orchestrates core services. Implemented as `@dataclass(slots=True)` classes
with dependencies injected via constructor and a `do()` method:

```python
@dataclass(slots=True)
class ParseBundleFromRequest:
    directories: Directories
    bundle_service: core.bundle.BundleService
    rbac_scenarios: RBACScenarios

    def do(self, archive: Path) -> BundleID:
        ...
```

### 3. Implementations (`python/cm/`, `python/rbac/`, `python/audit/`)

Framework/infrastructure-dependent code: Django models, ORM queries, signal handlers,
management commands, migrations.

### 4. Entry Points (Controllers)

| Entry Point       | Package              | Purpose                          |
|-------------------|----------------------|----------------------------------|
| REST API          | `python/api_v2/`     | DRF viewsets and serializers     |
| Job Runner        | `python/task_runner.py` | Background task execution     |
| Ansible Plugins   | `python/ansible_plugin/` | Custom Ansible modules       |
| Django Commands   | `cm/`, `audit/`, `rbac/` management commands | CLI tools  |

### Dependency Injection

- DI container: **Dishka**
- Providers defined in `python/application/di/providers/`
- Container assembled in `python/application/di/containers.py`
- API views use `@inject` decorator with `FromDishka[ServiceType]` parameters
- Entry points configure DI; services receive dependencies via constructor

---

## Frontend (React)

Located in `adcm-web/app/`.

### Tooling

| Tool        | Purpose                            | Config                  |
|-------------|------------------------------------|-------------------------|
| Biome       | Linter and formatter               | `biome.jsonc`           |
| OxLint      | Additional linting (import cycles) | `.oxlintrc-precommit.json` |
| TypeScript  | Type checking (`tsc --noEmit`)     | `tsconfig.json`         |
| Vite        | Build tool and dev server          | `vite.config.ts`        |
| Jest        | Unit testing                       | `jest.config.json`      |
| Yarn 4      | Package manager                    | `package.json`          |
| Husky       | Git hooks (pre-commit)             | `.husky/`               |
| lint-staged | Staged file linting                | via `package.json`      |

### Formatting Rules (Biome)

- Indent: **2 spaces**
- Line width: **120 characters**
- Line ending: `lf`
- Semicolons: **always**
- Quote style: **single quotes** (JSX: double quotes)
- Trailing commas: **all**
- Arrow parentheses: **always**
- Bracket spacing: **true**

### TypeScript Configuration

- Target: `ESNext`
- Module: `ESNext` with bundler resolution
- **Strict mode enabled** (`strict: true`, `strictNullChecks: true`)
- JSX: `react-jsx`
- `noEmit: true` (type checking only; Vite handles compilation)
- Path aliases configured in `tsconfig.paths.json`

### Path Aliases

| Alias                | Maps to                          |
|----------------------|----------------------------------|
| `@api` / `@api/*`   | `src/api/`                       |
| `@pages/*`           | `src/components/pages/*`         |
| `@uikit` / `@uikit/*` | `src/components/uikit/*`       |
| `@layouts/*`         | `src/components/layouts/*`       |
| `@commonComponents/*`| `src/components/common/*`        |
| `@models/*`          | `src/models/*`                   |
| `@hooks` / `@hooks/*`| `src/hooks/*`                    |
| `@routes/*`          | `src/routes/*`                   |
| `@store` / `@store/*`| `src/store/*`                    |
| `@utils/*`           | `src/utils/*`                    |
| `@constants`         | `src/constants`                  |

### Project Structure

```
src/
    api/              # API client layer (static class methods per resource)
    components/
        common/       # Shared business components
        layouts/      # Layout components (sidebars, navigation)
        pages/        # Page-level components (route targets)
        uikit/        # Reusable UI primitives (Button, Dialog, etc.)
    hooks/            # Custom React hooks (barrel-exported via index.ts)
    models/           # TypeScript type/interface definitions
    routes/           # Route configuration
    store/            # Redux store (RTK slices, middleware)
    utils/            # Utility functions
    scss/             # Global SCSS styles
```

### Component Conventions

- One component per file, co-located with its styles and stories:
  ```
  Button/
      Button.tsx
      Button.module.scss
      Button.stories.tsx
  ```
- Use `React.forwardRef` for components that need ref forwarding
- Functional components only (no class components)
- Use `import type` for type-only imports (enforced by Biome)

### State Management

- **Redux Toolkit (RTK)**: central state store with slices pattern
- Slices organized by feature domain under `store/adcm/`
- Naming: `<feature>Slice.ts`, `<feature>TableSlice.ts`, `<feature>ActionsSlice.ts`
- API calls via `@api` layer classes (Axios-based `httpClient`)

### Testing (Frontend)

- Framework: **Jest** with `jsdom` environment
- Test files: `*.test.ts` / `*.utils.test.ts`, co-located with source
- Babel transforms for TSX support

### Pre-commit Hook (Frontend)

Husky runs on commit:
1. `yarn lint-staged` (Biome check + OxLint on staged files)
2. `yarn tsc` (full TypeScript type check)

### Import Restrictions

- OxLint enforces **no circular imports** (`import/no-cycle: error`)
- Biome enforces **no unused imports/variables** (`error` level)

---

## Go Status Server

Located in `go/adcm/`.

### Tooling

| Tool           | Purpose       | Config          |
|----------------|---------------|-----------------|
| `gofmt`        | Formatter     | `go/Makefile`   |
| `golangci-lint`| Linter        | `go/Makefile`   |

### Conventions

- Go version: **1.23**
- Module name: `adcm`
- Formatter: `gofmt -w -l -s` (standard Go formatting with simplification)
- Build: `CGO_ENABLED=0` (static binary)
- All `.go` files must include the Apache 2.0 license header (comment style: `//`)
- Packages: `config`, `status` under the `adcm` module
- Standard Go naming conventions (exported = `PascalCase`, unexported = `camelCase`)
- Structs with JSON tags use `snake_case` field names in JSON
- Error handling: explicit `error` returns, no panics except in `main()` initialization

---

## Pre-commit and CI

### Root Pre-commit Hook

`.pre-commit-config.yaml` runs `make lint` on all Python file changes.

### `make lint` (full lint pipeline)

```
ruff check                   # Lint Python
ruff format --check          # Verify formatting
pyright                      # Type checking
lint-imports                 # Architectural layer enforcement
license_checker.py           # License header verification
migrations_checker.py        # Migration import restrictions
```

### `make pretty` (auto-fix)

```
ruff format                  # Format Python
ruff check --fix             # Auto-fix lint issues
ruff format                  # Re-format after fixes
license_checker.py --fix     # Add missing license headers
```

### Files Covered by Linting

Python files in: `python/`, `dev/linters/`, `conf/adcm/python_scripts/`

---

## General Rules

1. **No secrets in code** -- never commit credentials, tokens, or API keys
2. **License headers required** on all Python and Go source files
3. **Type safety first** -- all new Python code must pass `pyright` in standard mode; all frontend code must pass `tsc --noEmit` with strict mode
4. **No linter silencing without justification** -- if you must suppress a rule, explain why in a comment
5. **Architectural boundaries are enforced** -- `import-linter` contracts are not suggestions; violating them breaks CI
6. **Prefer existing patterns** -- follow the conventions already in the codebase for the module you are modifying
