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

from jobs.worker.celery.consul import registrar
from jobs.worker.celery.consul import settings as consul_settings


class TestBuildWorkerRegistration(TestCase):
    def test_tags_include_adcm_uuid(self) -> None:
        registration = registrar.build_worker_registration(hostname="celery@host1", adcm_uuid="uuid-1")

        self.assertEqual(registration.service_id, "celery@host1")
        self.assertEqual(registration.name, consul_settings.CONSUL_SERVICE_NAME)
        self.assertEqual(registration.tags, ["adcm", "celery", "uuid-1"])
        self.assertEqual(registration.check_ttl, consul_settings.CONSUL_HEALTH_CHECK_TTL)
        self.assertIsNone(registration.address)
        self.assertIsNone(registration.port)

    def test_tags_without_uuid(self) -> None:
        registration = registrar.build_worker_registration(hostname="celery@host1", adcm_uuid=None)

        self.assertEqual(registration.tags, ["adcm", "celery"])

    def test_registration_payload_has_no_address_or_port(self) -> None:
        registration = registrar.build_worker_registration(hostname="celery@host1", adcm_uuid="uuid-1")
        payload = registration.to_payload()

        self.assertNotIn("Address", payload)
        self.assertNotIn("Port", payload)
        self.assertIn("TTL", payload["Checks"][0])


class TestDiscoverActiveWorkers(TestCase):
    def test_discover_active_workers_uses_backend(self) -> None:
        backend = mock.Mock()
        backend.get_healthy_service_ids.return_value = {"celery@host1", "celery@host2"}

        with mock.patch.object(registrar, "get_consul_backend", return_value=backend):
            result = registrar.discover_active_workers()

        self.assertEqual(result, {"celery@host1", "celery@host2"})
        backend.get_healthy_service_ids.assert_called_once_with(consul_settings.CONSUL_SERVICE_NAME)


class TestBuildClientSettings(TestCase):
    def test_raises_when_url_missing(self) -> None:
        with mock.patch.object(consul_settings, "CONSUL_URL", None):
            with self.assertRaises(RuntimeError):
                registrar.build_client_settings()

    def test_builds_settings_from_env(self) -> None:
        with (
            mock.patch.object(consul_settings, "CONSUL_URL", "http://consul:8500"),
            mock.patch.object(consul_settings, "CONSUL_DATACENTER", "dc1"),
            mock.patch.object(consul_settings, "CONSUL_ACL_TOKEN", "token"),
        ):
            settings = registrar.build_client_settings()

        self.assertEqual(settings.url, "http://consul:8500")
        self.assertEqual(settings.datacenter, "dc1")
        self.assertEqual(settings.acl_token, "token")
