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

from unittest.mock import Mock, patch
import os

from application.di.providers.environment import ConsulSettings
from cm.legacy.status_api import get_status_service_url, set_external_status_service_url
from django.test import SimpleTestCase, override_settings
from integrations.consul import ServiceRegistration

from jobs.worker.celery.bootsteps import build_worker_registration, ttl_pass_interval
from jobs.worker.celery.bootsteps.status_service import (
    StatusServiceUrlResolutionError,
    StatusServiceUrlStep,
    build_status_service_url_from_adcm_url,
    extract_status_service_url,
    resolve_external_status_service_url,
)


class TestServiceRegistrationPayload(SimpleTestCase):
    def test_ttl_check_payload(self):
        registration = ServiceRegistration(
            service_id="celery@fe2db162d69b",
            name="celery",
            datacenter="dc1",
            tags=["adcm", "celery", "adcm-uuid"],
            health_check_ttl="30s",
            deregister_critical_service_after="5m",
            check_id="service:celery@fe2db162d69b:ttl",
        )

        payload = registration.to_payload()

        self.assertEqual(payload["ID"], "celery@fe2db162d69b")
        self.assertEqual(payload["Name"], "celery")
        self.assertEqual(payload["Datacenter"], "dc1")
        self.assertEqual(payload["Tags"], ["adcm", "celery", "adcm-uuid"])
        # a worker has no address / port to advertise
        self.assertNotIn("Address", payload)
        self.assertNotIn("Port", payload)
        self.assertEqual(
            payload["Checks"],
            [
                {
                    "TTL": "30s",
                    "DeregisterCriticalServiceAfter": "5m",
                    "CheckID": "service:celery@fe2db162d69b:ttl",
                }
            ],
        )

    def test_http_check_payload_still_supported(self):
        registration = ServiceRegistration(
            service_id="adcm@host",
            name="adcm",
            address="adcm.local",
            port=8000,
            health_check_url="http://adcm.local:8000/api/health/live",
            meta={"status_service_url": "http://adcm.local:8000/status/api/v1/"},
        )

        payload = registration.to_payload()

        self.assertEqual(payload["Address"], "adcm.local")
        self.assertEqual(payload["Port"], 8000)
        self.assertNotIn("Datacenter", payload)
        self.assertEqual(len(payload["Checks"]), 1)
        self.assertEqual(payload["Checks"][0]["HTTP"], "http://adcm.local:8000/api/health/live")
        self.assertNotIn("TTL", payload["Checks"][0])


class TestWorkerRegistration(SimpleTestCase):
    def test_tags_include_uuid_when_present(self):
        registration = build_worker_registration(
            hostname="celery@node",
            datacenter="dc1",
            adcm_uuid="the-uuid",
            ttl="30s",
            deregister_after="5m",
        )

        self.assertEqual(registration.service_id, "celery@node")
        self.assertEqual(registration.name, "celery")
        self.assertEqual(registration.tags, ["adcm", "celery", "the-uuid"])
        self.assertEqual(registration.health_check_ttl, "30s")
        self.assertEqual(registration.check_id, "service:celery@node:ttl")

    def test_tags_without_uuid(self):
        registration = build_worker_registration(
            hostname="celery@node",
            datacenter=None,
            adcm_uuid=None,
            ttl="30s",
            deregister_after="5m",
        )

        self.assertEqual(registration.tags, ["adcm", "celery"])

    def test_ttl_pass_interval(self):
        self.assertEqual(ttl_pass_interval("30s"), 15.0)
        self.assertEqual(ttl_pass_interval("2m"), 60.0)
        self.assertEqual(ttl_pass_interval("1s"), 1.0)  # clamped to the minimum
        self.assertEqual(ttl_pass_interval("nonsense"), 15.0)  # unparsable -> default


class TestConsulClientSettings(SimpleTestCase):
    @patch.dict(os.environ, {"CONSUL_URL": "http://localhost:8500"}, clear=True)
    def test_defaults(self):
        parsed = ConsulSettings().consul  # pyright: ignore[reportCallIssue]

        self.assertIsNone(parsed.datacenter)
        self.assertEqual(parsed.health_check_interval, "10s")
        self.assertEqual(parsed.health_check_timeout, "5s")
        self.assertEqual(parsed.health_check_ttl, "30s")
        self.assertEqual(parsed.deregister_critical_service_after, "5m")

    @patch.dict(
        os.environ,
        {
            "CONSUL_URL": "http://localhost:8500",
            "CONSUL_DATACENTER": "dc1",
            "CONSUL_HEALTH_CHECK_INTERVAL": "20s",
            "CONSUL_HEALTH_CHECK_TIMEOUT": "3s",
            "CONSUL_HEALTH_CHECK_TTL": "10s",
            "CONSUL_DEREGISTER_CRITICAL_SERVICE_AFTER": "1m",
        },
        clear=True,
    )
    def test_parsed_from_env(self):
        parsed = ConsulSettings().consul  # pyright: ignore[reportCallIssue]

        self.assertEqual(parsed.datacenter, "dc1")
        self.assertEqual(parsed.health_check_interval, "20s")
        self.assertEqual(parsed.health_check_timeout, "3s")
        self.assertEqual(parsed.health_check_ttl, "10s")
        self.assertEqual(parsed.deregister_critical_service_after, "1m")


class TestStatusServiceUrlResolution(SimpleTestCase):
    def test_extract_from_discovery(self):
        entries = [
            {"Service": {"Meta": {"status_service_url": "http://adcm.local:8000/status/api/v1/"}}},
        ]

        self.assertEqual(
            extract_status_service_url(entries),
            "http://adcm.local:8000/status/api/v1/",
        )

    def test_extract_skips_entries_without_meta(self):
        entries = [
            {"Service": {}},
            {"Service": {"Meta": {}}},
            {"Service": {"Meta": {"status_service_url": "http://second:8000/status/api/v1/"}}},
        ]

        self.assertEqual(extract_status_service_url(entries), "http://second:8000/status/api/v1/")

    def test_extract_returns_none_when_absent(self):
        self.assertIsNone(extract_status_service_url([]))

    def test_build_url_from_adcm_url(self):
        self.assertEqual(
            build_status_service_url_from_adcm_url("http://adcm.local:8000", "/status/api/v1/"),
            "http://adcm.local:8000/status/api/v1/",
        )

    def test_build_url_strips_path_and_query(self):
        self.assertEqual(
            build_status_service_url_from_adcm_url("http://adcm.local:8000/ui/foo?x=1", "status/api/v1/"),
            "http://adcm.local:8000/status/api/v1/",
        )

    def test_resolve_prefers_consul_discovery(self):
        backend = Mock()
        backend.discover.return_value = [
            {"Service": {"Meta": {"status_service_url": "http://discovered:8000/status/api/v1/"}}}
        ]

        url = resolve_external_status_service_url(
            consul_backend=backend,
            adcm_uuid="the-uuid",
            default_adcm_url="http://fallback:8000",
            status_base_path="/status/api/v1/",
        )

        self.assertEqual(url, "http://discovered:8000/status/api/v1/")
        backend.discover.assert_called_once_with("adcm", tag="the-uuid")

    def test_resolve_falls_back_to_default_adcm_url_when_meta_missing(self):
        backend = Mock()
        backend.discover.return_value = [{"Service": {"Meta": {}}}]

        url = resolve_external_status_service_url(
            consul_backend=backend,
            adcm_uuid=None,
            default_adcm_url="http://fallback:8000",
            status_base_path="/status/api/v1/",
        )

        self.assertEqual(url, "http://fallback:8000/status/api/v1/")

    def test_resolve_falls_back_to_default_adcm_url_when_discovery_fails(self):
        backend = Mock()
        backend.discover.side_effect = RuntimeError("bootsteps down")

        url = resolve_external_status_service_url(
            consul_backend=backend,
            adcm_uuid=None,
            default_adcm_url="http://fallback:8000",
            status_base_path="/status/api/v1/",
        )

        self.assertEqual(url, "http://fallback:8000/status/api/v1/")

    def test_resolve_without_consul_uses_default_adcm_url(self):
        url = resolve_external_status_service_url(
            consul_backend=None,
            adcm_uuid=None,
            default_adcm_url="http://fallback:8000",
            status_base_path="/status/api/v1/",
        )

        self.assertEqual(url, "http://fallback:8000/status/api/v1/")

    def test_resolve_returns_none_without_any_source(self):
        self.assertIsNone(
            resolve_external_status_service_url(
                consul_backend=None,
                adcm_uuid=None,
                default_adcm_url=None,
                status_base_path="/status/api/v1/",
            )
        )


class TestStatusServiceUrlStep(SimpleTestCase):
    def setUp(self):
        # isolate the module-level override set by set_external_status_service_url
        patcher = patch("cm.legacy.status_api._external_status_service_url", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _make_parent(*, consul, default_adcm_url: str | None) -> Mock:
        parent = Mock()
        parent.app.conf.consul = consul
        parent.app.conf.default_adcm_url = default_adcm_url
        parent.app.conf.status_service_base_path = "/status/api/v1/"
        parent.app.di_container.get.return_value.get_uuid.return_value = "the-uuid"
        return parent

    def test_start_fails_when_url_cannot_be_resolved(self):
        parent = self._make_parent(consul=None, default_adcm_url=None)

        with self.assertRaises(StatusServiceUrlResolutionError):
            StatusServiceUrlStep(parent).start(parent)

    def test_start_fails_when_discovery_is_empty_and_no_default_url(self):
        backend = Mock()
        backend.discover.return_value = []
        parent = self._make_parent(consul=backend, default_adcm_url=None)

        with self.assertRaises(StatusServiceUrlResolutionError):
            StatusServiceUrlStep(parent).start(parent)

    def test_start_sets_external_url_when_resolved(self):
        parent = self._make_parent(consul=None, default_adcm_url="http://adcm.local:8000")

        StatusServiceUrlStep(parent).start(parent)

        self.assertEqual(get_status_service_url(), "http://adcm.local:8000/status/api/v1/")


@override_settings(INTERNAL_STATUS_SERVICE_URL="http://localhost:8020/api/v1/")
class TestStatusApiBaseUrl(SimpleTestCase):
    def setUp(self):
        # isolate the module-level override set by set_external_status_service_url
        patcher = patch("cm.legacy.status_api._external_status_service_url", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_internal_url_used_in_backend(self):
        self.assertEqual(get_status_service_url(), "http://localhost:8020/api/v1/")

    def test_external_url_preferred_when_resolved(self):
        set_external_status_service_url("http://adcm.local:8000/status/api/v1/")

        self.assertEqual(get_status_service_url(), "http://adcm.local:8000/status/api/v1/")
