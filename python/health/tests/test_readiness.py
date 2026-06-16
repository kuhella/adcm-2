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

from unittest import mock

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory

from health.checks import CheckResult, check_consul, check_vault, run_readiness_checks
from health.views import ReadinessView


class TestReadinessChecks(SimpleTestCase):
    def test_vault_check_skipped_when_not_configured(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(check_vault())

    def test_vault_check_runs_when_configured(self) -> None:
        with mock.patch.dict("os.environ", {"SECRET_BACKEND": "VaultBackend"}, clear=True):
            with mock.patch("integrations.vault.VaultSecretsBackend.from_settings") as from_settings:
                from_settings.return_value.check_connection.return_value = True
                with mock.patch("application.di.providers.environment.parse_vault_settings_from_env") as parse_settings:
                    parse_settings.return_value.vault = object()
                    result = check_vault()

        self.assertIsNotNone(result)
        self.assertTrue(result.healthy)

    def test_consul_check_skipped_when_not_initialized(self) -> None:
        with mock.patch("health.checks.ConsulBackend.instance", return_value=None):
            self.assertIsNone(check_consul())

    def test_consul_check_runs_when_initialized(self) -> None:
        backend = mock.MagicMock()
        backend.check_connection.return_value = False
        with mock.patch("health.checks.ConsulBackend.instance", return_value=backend):
            result = check_consul()

        self.assertIsNotNone(result)
        self.assertFalse(result.healthy)

    def test_run_readiness_checks_includes_only_configured(self) -> None:
        with mock.patch("health.checks.check_database", return_value=CheckResult("database", healthy=True)):
            with mock.patch("health.checks.check_vault", return_value=None):
                with mock.patch("health.checks.check_consul", return_value=None):
                    results = run_readiness_checks()

        self.assertEqual([result.name for result in results], ["database"])


class TestReadinessView(SimpleTestCase):
    def setUp(self) -> None:
        self.view = ReadinessView.as_view()
        self.request = APIRequestFactory().get("/api/health/ready")

    def test_returns_200_when_all_healthy(self) -> None:
        with mock.patch(
            "health.views.run_readiness_checks",
            return_value=[CheckResult("database", healthy=True)],
        ):
            response = self.view(self.request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "ok")

    def test_returns_503_when_a_check_fails(self) -> None:
        with mock.patch(
            "health.views.run_readiness_checks",
            return_value=[
                CheckResult("database", healthy=True),
                CheckResult("consul", healthy=False, detail="Consul is not reachable"),
            ],
        ):
            response = self.view(self.request)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["status"], "unavailable")
        self.assertFalse(response.data["checks"]["consul"]["healthy"])
