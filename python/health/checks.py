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

"""Readiness checks used by the ``/api/health/ready`` endpoint."""

from __future__ import annotations

from dataclasses import dataclass
import os
import logging

from django.db import DatabaseError, connections
from integrations.consul import ConsulBackend

logger = logging.getLogger("adcm")

VAULT_SECRET_BACKEND = "VaultBackend"


@dataclass(slots=True, frozen=True)
class CheckResult:
    name: str
    healthy: bool
    detail: str = ""


def check_database() -> CheckResult:
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError as error:
        return CheckResult(name="database", healthy=False, detail=str(error))

    return CheckResult(name="database", healthy=True)


def check_vault() -> CheckResult | None:
    """Check Vault connectivity, only when the Vault secret backend is configured."""
    if os.getenv("SECRET_BACKEND") != VAULT_SECRET_BACKEND:
        return None

    from application.di.providers.environment import parse_vault_settings_from_env  # noqa: PLC0415
    from integrations.vault import VaultSecretsBackend  # noqa: PLC0415

    try:
        backend = VaultSecretsBackend.from_settings(parse_vault_settings_from_env().vault)
        healthy = backend.check_connection()
    except Exception as error:  # noqa: BLE001
        return CheckResult(name="vault", healthy=False, detail=str(error))

    return CheckResult(name="vault", healthy=healthy, detail="" if healthy else "Vault is not reachable")


def check_consul() -> CheckResult | None:
    """Check Consul connectivity, only when the Consul backend is configured."""
    backend = ConsulBackend.instance()
    if backend is None:
        return None

    healthy = backend.check_connection()
    return CheckResult(name="consul", healthy=healthy, detail="" if healthy else "Consul is not reachable")


def run_readiness_checks() -> list[CheckResult]:
    results = [check_database()]
    for optional in (check_vault(), check_consul()):
        if optional is not None:
            results.append(optional)

    return results
