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

from typing import Any
from unittest import mock

from core.scenarios.adcm import ADCMUUID, DefaultURL
from django.test import SimpleTestCase, override_settings
from integrations.consul import ClientSettings

from application.startup.checks import ensure_default_adcm_url_when_consul_configured
from application.startup.consul import ConsulConfigurationError, build_service_registration, register_adcm_in_consul


class FakeContainer:
    def __init__(self, mapping: dict[Any, Any]) -> None:
        self._mapping = mapping

    def get(self, dependency: Any) -> Any:
        return self._mapping[dependency]


CONSUL_SETTINGS_OVERRIDES = {
    "STATUS_SERVICE_BASE_PATH": "/status/api/v1",
    "CONSUL_HEALTH_CHECK_INTERVAL": "15s",
    "CONSUL_HEALTH_CHECK_TIMEOUT": "3s",
    "CONSUL_DEREGISTER_CRITICAL_SERVICE_AFTER": "10m",
}


@override_settings(**CONSUL_SETTINGS_OVERRIDES)
class TestBuildServiceRegistration(SimpleTestCase):
    def _connection(self) -> ClientSettings:
        return ClientSettings(url="https://consul.local:8501", datacenter="dc1")

    def test_derives_address_port_and_check_url(self) -> None:
        with mock.patch("socket.gethostname", return_value="container-123"):
            registration = build_service_registration(
                connection=self._connection(),
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
        self.assertEqual(registration.meta, {"status_service_url": "http://10.92.40.33:8000/status/api/v1"})

    def test_https_default_port_and_missing_uuid(self) -> None:
        with mock.patch("socket.gethostname", return_value="container-123"):
            registration = build_service_registration(
                connection=self._connection(),
                adcm_url="https://adcm.example.com/api/v1/",
                adcm_uuid=None,
            )

        self.assertEqual(registration.address, "adcm.example.com")
        self.assertEqual(registration.port, 443)
        self.assertEqual(registration.tags, ["adcm", "backend"])

    def test_invalid_url_raises(self) -> None:
        with self.assertRaises(ConsulConfigurationError):
            build_service_registration(connection=self._connection(), adcm_url="not-a-url", adcm_uuid=None)


@override_settings(**CONSUL_SETTINGS_OVERRIDES)
class TestRegisterAdcmInConsul(SimpleTestCase):
    def test_no_op_when_consul_not_configured(self) -> None:
        container = FakeContainer({ClientSettings | None: None})
        with mock.patch("application.startup.consul.ConsulBackend.initialize") as initialize:
            register_adcm_in_consul(container=container)

        initialize.assert_not_called()

    def test_registers_and_installs_handlers(self) -> None:
        container = FakeContainer(
            {
                ClientSettings | None: ClientSettings(url="https://consul.local:8501", datacenter="dc1"),
                DefaultURL | None: DefaultURL("http://10.92.40.33:8000/api/v1/"),
                ADCMUUID | None: ADCMUUID("uuid-42"),
            }
        )
        backend = mock.MagicMock()
        with mock.patch("application.startup.consul.ConsulBackend.initialize", return_value=backend):
            with mock.patch("socket.gethostname", return_value="container-123"):
                with mock.patch("application.startup.consul._install_deregistration_handlers") as install_handlers:
                    register_adcm_in_consul(container=container)

        backend.register.assert_called_once()
        registration = backend.register.call_args[0][0]
        self.assertEqual(registration.service_id, "adcm@container-123")
        install_handlers.assert_called_once_with(service_id="adcm@container-123")

    def test_skips_when_adcm_url_missing(self) -> None:
        container = FakeContainer(
            {
                ClientSettings | None: ClientSettings(url="https://consul.local:8501"),
                DefaultURL | None: None,
            }
        )
        backend = mock.MagicMock()
        with mock.patch("application.startup.consul.ConsulBackend.initialize", return_value=backend):
            register_adcm_in_consul(container=container)

        backend.register.assert_not_called()


class TestDefaultAdcmUrlMandatory(SimpleTestCase):
    def test_raises_when_consul_set_without_adcm_url(self) -> None:
        container = FakeContainer(
            {
                ClientSettings | None: ClientSettings(url="https://consul.local:8501"),
                DefaultURL | None: None,
            }
        )
        with self.assertRaises(ConsulConfigurationError):
            ensure_default_adcm_url_when_consul_configured(container=container, failure_exc=ConsulConfigurationError)

    def test_passes_when_consul_and_adcm_url_set(self) -> None:
        container = FakeContainer(
            {
                ClientSettings | None: ClientSettings(url="https://consul.local:8501"),
                DefaultURL | None: DefaultURL("http://10.92.40.33:8000/api/v1/"),
            }
        )
        ensure_default_adcm_url_when_consul_configured(container=container, failure_exc=ConsulConfigurationError)

    def test_passes_when_consul_not_configured(self) -> None:
        container = FakeContainer({ClientSettings | None: None})
        ensure_default_adcm_url_when_consul_configured(container=container, failure_exc=ConsulConfigurationError)
