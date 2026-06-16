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

import os

from django.db import connection
from drf_spectacular.utils import extend_schema
from integrations.consul import ConsulBackend
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.status import HTTP_200_OK, HTTP_503_SERVICE_UNAVAILABLE
from rest_framework.views import APIView


class HealthReadyView(APIView):
    permission_classes = (AllowAny,)
    authentication_classes = ()

    @extend_schema(
        summary="Readiness probe",
        tags=["health"],
        operation_id="getHealthCheck",
        description="Get information about adcm backend health",
        responses={200: None, 503: None},
    )
    def get(self, request: Request) -> Response:  # noqa: ARG002
        checks: dict[str, bool] = {}

        checks["db"] = self._check_db()

        if os.getenv("SECRET_BACKEND") == "VaultBackend":
            checks["vault"] = self._check_vault()

        consul_backend = ConsulBackend.get_instance()
        if consul_backend is not None:
            checks["consul"] = consul_backend.check_connectivity()

        all_healthy = all(checks.values())
        status_code = HTTP_200_OK if all_healthy else HTTP_503_SERVICE_UNAVAILABLE

        return Response(data={"status": "ok" if all_healthy else "unavailable", "checks": checks}, status=status_code)

    @staticmethod
    def _check_db() -> bool:
        try:
            connection.ensure_connection()
            return True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _check_vault() -> bool:
        try:
            from application.di.providers.environment import parse_vault_settings_from_env  # noqa: PLC0415
            from integrations.vault import VaultSecretsBackend  # noqa: PLC0415

            vault_settings = parse_vault_settings_from_env()
            backend = VaultSecretsBackend.from_settings(vault_settings.vault)
            return backend.client.is_authenticated()
        except Exception:  # noqa: BLE001
            return False
