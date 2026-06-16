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
from unittest.mock import MagicMock

from requests import RequestException

from integrations.consul import (
    ConsulBackend,
    ConsulClientSettings,
    ConsulError,
    ServiceRegistration,
)


class TestConsulClientSettings(TestCase):
    def test_from_env_disabled_without_url(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(ConsulClientSettings.from_env())

    def test_from_env_parses_all_fields(self) -> None:
        env = {
            "CONSUL_URL": "https://consul.local:8501",
            "CONSUL_DATACENTER": "dc1",
            "CONSUL_ACL_TOKEN": "secret-token",
            "CONSUL_CACERT_FILE": "/certs/ca.pem",
            "CONSUL_CLIENT_CERT_FILE": "/certs/client.pem",
            "CONSUL_CLIENT_KEY_FILE": "/certs/client.key",
            "CONSUL_HEALTH_CHECK_INTERVAL": "15s",
            "CONSUL_HEALTH_CHECK_TIMEOUT": "3s",
            "CONSUL_DEREGISTER_CRITICAL_SERVICE_AFTER": "10m",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            settings = ConsulClientSettings.from_env()

        self.assertIsNotNone(settings)
        self.assertEqual(settings.url, "https://consul.local:8501")
        self.assertEqual(settings.datacenter, "dc1")
        self.assertEqual(settings.acl_token, "secret-token")
        self.assertEqual(settings.cacert_file, "/certs/ca.pem")
        self.assertEqual(settings.client_cert_file, "/certs/client.pem")
        self.assertEqual(settings.client_key_file, "/certs/client.key")
        self.assertEqual(settings.health_check_interval, "15s")
        self.assertEqual(settings.health_check_timeout, "3s")
        self.assertEqual(settings.deregister_critical_service_after, "10m")

    def test_half_mtls_configuration_is_rejected(self) -> None:
        with self.assertRaises(ConsulError):
            ConsulClientSettings(url="https://consul.local:8501", client_cert_file="/certs/client.pem")


class TestServiceRegistration(TestCase):
    def test_to_payload(self) -> None:
        registration = ServiceRegistration(
            service_id="adcm@host-1",
            address="10.92.40.33",
            port=8000,
            health_check_url="http://10.92.40.33:8000/api/health/ready",
            datacenter="dc1",
            tags=["adcm", "backend", "uuid-1"],
            meta={"status_service_url": "http://10.92.40.33:8000/status"},
            check_interval="10s",
            check_timeout="5s",
            deregister_critical_service_after="5m",
        )

        payload = registration.to_payload()

        self.assertEqual(payload["ID"], "adcm@host-1")
        self.assertEqual(payload["Name"], "adcm")
        self.assertEqual(payload["Datacenter"], "dc1")
        self.assertEqual(payload["Tags"], ["adcm", "backend", "uuid-1"])
        self.assertEqual(payload["Address"], "10.92.40.33")
        self.assertEqual(payload["Port"], 8000)
        self.assertEqual(payload["Meta"], {"status_service_url": "http://10.92.40.33:8000/status"})
        self.assertEqual(len(payload["Checks"]), 1)
        check = payload["Checks"][0]
        self.assertEqual(check["HTTP"], "http://10.92.40.33:8000/api/health/ready")
        self.assertEqual(check["Interval"], "10s")
        self.assertEqual(check["Timeout"], "5s")
        self.assertEqual(check["DeregisterCriticalServiceAfter"], "5m")

    def test_payload_without_datacenter(self) -> None:
        registration = ServiceRegistration(
            service_id="adcm@host-1",
            address="10.92.40.33",
            port=8000,
            health_check_url="http://10.92.40.33:8000/api/health/ready",
        )
        self.assertNotIn("Datacenter", registration.to_payload())


class TestConsulBackend(TestCase):
    def setUp(self) -> None:
        ConsulBackend.reset()
        self.addCleanup(ConsulBackend.reset)
        self.settings = ConsulClientSettings(
            url="https://consul.local:8501/",
            datacenter="dc1",
            acl_token="token",
            cacert_file="/certs/ca.pem",
            client_cert_file="/certs/client.pem",
            client_key_file="/certs/client.key",
        )

    def test_session_is_configured_with_tls_and_token(self) -> None:
        backend = ConsulBackend(self.settings)
        self.assertEqual(backend._session.headers["X-Consul-Token"], "token")
        self.assertEqual(backend._session.verify, "/certs/ca.pem")
        self.assertEqual(backend._session.cert, ("/certs/client.pem", "/certs/client.key"))

    def test_singleton_lifecycle(self) -> None:
        self.assertIsNone(ConsulBackend.instance())
        backend = ConsulBackend.initialize(self.settings)
        self.assertIs(ConsulBackend.instance(), backend)
        ConsulBackend.reset()
        self.assertIsNone(ConsulBackend.instance())

    def test_register_sends_payload_with_datacenter(self) -> None:
        backend = ConsulBackend(self.settings)
        session = MagicMock()
        session.put.return_value = MagicMock(ok=True)
        backend._session = session

        registration = ServiceRegistration(
            service_id="adcm@host-1",
            address="10.92.40.33",
            port=8000,
            health_check_url="http://10.92.40.33:8000/api/health/ready",
            datacenter="dc1",
        )
        backend.register(registration)

        session.put.assert_called_once()
        _, kwargs = session.put.call_args
        self.assertEqual(kwargs["params"], {"dc": "dc1"})
        self.assertEqual(kwargs["json"]["ID"], "adcm@host-1")

    def test_register_raises_on_error_status(self) -> None:
        backend = ConsulBackend(self.settings)
        session = MagicMock()
        session.put.return_value = MagicMock(ok=False, status_code=500, text="boom")
        backend._session = session

        with self.assertRaises(ConsulError):
            backend.register(
                ServiceRegistration(
                    service_id="adcm@host-1",
                    address="10.92.40.33",
                    port=8000,
                    health_check_url="http://10.92.40.33:8000/api/health/ready",
                )
            )

    def test_deregister(self) -> None:
        backend = ConsulBackend(self.settings)
        session = MagicMock()
        session.put.return_value = MagicMock(ok=True)
        backend._session = session

        backend.deregister("adcm@host-1")

        url = session.put.call_args[0][0]
        self.assertTrue(url.endswith("/v1/agent/service/deregister/adcm@host-1"))

    def test_check_connection(self) -> None:
        backend = ConsulBackend(self.settings)
        session = MagicMock()
        backend._session = session

        session.get.return_value = MagicMock(ok=True)
        self.assertTrue(backend.check_connection())

        session.get.return_value = MagicMock(ok=False)
        self.assertFalse(backend.check_connection())

        session.get.side_effect = RequestException("unreachable")
        self.assertFalse(backend.check_connection())
