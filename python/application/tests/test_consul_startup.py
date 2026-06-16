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

from unittest import TestCase, mock

from integrations.consul import ConsulClientSettings

from application.startup.consul import (
    ConsulConfigurationError,
    build_service_registration,
    ensure_default_adcm_url_when_consul_configured,
)


class TestDefaultAdcmUrlMandatory(TestCase):
    def test_raises_when_consul_set_without_adcm_url(self) -> None:
        with mock.patch.dict("os.environ", {"CONSUL_URL": "https://consul.local:8501"}, clear=True):
            with self.assertRaises(ConsulConfigurationError):
                ensure_default_adcm_url_when_consul_configured()

    def test_passes_when_consul_and_adcm_url_set(self) -> None:
        env = {"CONSUL_URL": "https://consul.local:8501", "DEFAULT_ADCM_URL": "http://10.92.40.33:8000/api/v1/"}
        with mock.patch.dict("os.environ", env, clear=True):
            ensure_default_adcm_url_when_consul_configured()

    def test_passes_when_consul_not_configured(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            ensure_default_adcm_url_when_consul_configured()


class TestBuildServiceRegistration(TestCase):
    def _settings(self) -> ConsulClientSettings:
        return ConsulClientSettings(
            url="https://consul.local:8501",
            datacenter="dc1",
            health_check_interval="15s",
            health_check_timeout="3s",
            deregister_critical_service_after="10m",
        )

    def test_derives_address_port_and_check_url(self) -> None:
        with mock.patch("socket.gethostname", return_value="container-123"):
            with mock.patch.dict("os.environ", {"STATUS_SERVICE_BASE_PATH": "/status"}, clear=True):
                registration = build_service_registration(
                    settings=self._settings(),
                    adcm_url="http://10.92.40.33:8000/api/v1/",
                    adcm_uuid="uuid-42",
                )

        self.assertEqual(registration.service_id, "adcm@container-123")
        self.assertEqual(registration.name, "adcm")
        self.assertEqual(registration.datacenter, "dc1")
        self.assertEqual(registration.tags, ["adcm", "backend", "uuid-42"])
        self.assertEqual(registration.address, "10.92.40.33")
        self.assertEqual(registration.port, 8000)
        self.assertEqual(registration.health_check_url, "http://10.92.40.33:8000/api/health/ready")
        self.assertEqual(registration.check_interval, "15s")
        self.assertEqual(registration.check_timeout, "3s")
        self.assertEqual(registration.deregister_critical_service_after, "10m")
        self.assertEqual(
            registration.meta,
            {"status_service_url": "http://10.92.40.33:8000/status", "status_service_base_path": "/status"},
        )

    def test_meta_without_status_service_base_path(self) -> None:
        with mock.patch("socket.gethostname", return_value="container-123"):
            with mock.patch.dict("os.environ", {}, clear=True):
                registration = build_service_registration(
                    settings=self._settings(),
                    adcm_url="https://adcm.example.com/api/v1/",
                    adcm_uuid="uuid-42",
                )

        self.assertEqual(registration.address, "adcm.example.com")
        self.assertEqual(registration.port, 443)
        self.assertEqual(registration.meta, {"status_service_url": "https://adcm.example.com"})

    def test_invalid_url_raises(self) -> None:
        with self.assertRaises(ConsulConfigurationError):
            build_service_registration(settings=self._settings(), adcm_url="not-a-url", adcm_uuid="uuid-42")
