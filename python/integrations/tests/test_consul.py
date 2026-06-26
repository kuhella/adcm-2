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
from unittest import TestCase

from integrations.consul import ClientSettings, ConsulBackend, ConsulError, ServiceRegistration


class FakeResponse:
    def __init__(self, *, ok: bool = True, status_code: int = 200, text: str = "", json_data: Any = None) -> None:
        self.ok = ok
        self.status_code = status_code
        self.text = text
        self._json_data = json_data

    def json(self) -> Any:
        return self._json_data


class FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.verify: Any = True
        self.cert: Any = None
        self.calls: list[dict[str, Any]] = []
        self.responses: list[FakeResponse] = []

    def mount(self, *_args, **_kwargs) -> None:  # pragma: no cover - noop
        pass

    def _record(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0) if self.responses else FakeResponse()

    def put(self, url, **kwargs):
        return self._record("PUT", url, **kwargs)

    def get(self, url, **kwargs):
        return self._record("GET", url, **kwargs)

    def delete(self, url, **kwargs):
        return self._record("DELETE", url, **kwargs)

    def close(self) -> None:  # pragma: no cover - noop
        pass


def make_backend(datacenter: str | None = None) -> tuple[ConsulBackend, FakeSession]:
    backend = ConsulBackend(ClientSettings(url="http://consul:8500", datacenter=datacenter))
    session = FakeSession()
    backend._session = session  # noqa: SLF001
    return backend, session


class TestServiceRegistrationPayload(TestCase):
    def test_ttl_check_payload_omits_address_and_port(self) -> None:
        registration = ServiceRegistration(
            service_id="celery@host1",
            name="celery",
            datacenter="dc1",
            tags=["adcm", "celery", "uuid-1"],
            check_ttl="30s",
            deregister_critical_service_after="10m",
        )

        payload = registration.to_payload()

        self.assertEqual(payload["ID"], "celery@host1")
        self.assertEqual(payload["Name"], "celery")
        self.assertEqual(payload["Tags"], ["adcm", "celery", "uuid-1"])
        self.assertEqual(payload["Datacenter"], "dc1")
        self.assertNotIn("Address", payload)
        self.assertNotIn("Port", payload)
        self.assertEqual(
            payload["Checks"],
            [
                {
                    "CheckID": "service:celery@host1:ttl",
                    "TTL": "30s",
                    "DeregisterCriticalServiceAfter": "10m",
                }
            ],
        )

    def test_http_check_payload_keeps_address_and_port(self) -> None:
        registration = ServiceRegistration(
            service_id="adcm@host1",
            name="adcm",
            address="10.0.0.1",
            port=8000,
            health_check_url="http://10.0.0.1:8000/api/health/live",
            check_interval="10s",
            check_timeout="5s",
        )

        payload = registration.to_payload()

        self.assertEqual(payload["Address"], "10.0.0.1")
        self.assertEqual(payload["Port"], 8000)
        self.assertEqual(payload["Checks"][0]["HTTP"], "http://10.0.0.1:8000/api/health/live")
        self.assertNotIn("TTL", payload["Checks"][0])


class TestConsulBackend(TestCase):
    def test_register_ttl_service(self) -> None:
        backend, session = make_backend(datacenter="dc1")
        registration = ServiceRegistration(service_id="celery@host1", name="celery", check_ttl="30s")

        backend.register(registration)

        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call["method"], "PUT")
        self.assertTrue(call["url"].endswith("/v1/agent/service/register"))
        self.assertEqual(call["params"], {"dc": "dc1"})
        self.assertEqual(call["json"], registration.to_payload())

    def test_pass_check(self) -> None:
        backend, session = make_backend()

        backend.pass_check("service:celery@host1:ttl", note="alive")

        call = session.calls[0]
        self.assertEqual(call["method"], "PUT")
        self.assertTrue(call["url"].endswith("/v1/agent/check/pass/service:celery@host1:ttl"))
        self.assertEqual(call["params"], {"note": "alive"})

    def test_pass_check_raises_on_error(self) -> None:
        backend, session = make_backend()
        session.responses.append(FakeResponse(ok=False, status_code=500, text="boom"))

        with self.assertRaises(ConsulError):
            backend.pass_check("service:celery@host1:ttl")

    def test_get_healthy_service_ids(self) -> None:
        backend, session = make_backend(datacenter="dc1")
        session.responses.append(
            FakeResponse(
                json_data=[
                    {"Service": {"ID": "celery@host1", "Service": "celery"}},
                    {"Service": {"ID": "celery@host2", "Service": "celery"}},
                    {"Service": {}},
                ]
            )
        )

        result = backend.get_healthy_service_ids("celery")

        self.assertEqual(result, {"celery@host1", "celery@host2"})
        call = session.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertTrue(call["url"].endswith("/v1/health/service/celery"))
        self.assertEqual(call["params"], {"dc": "dc1", "passing": "true"})

    def test_get_healthy_service_ids_empty(self) -> None:
        backend, session = make_backend()
        session.responses.append(FakeResponse(json_data=[]))

        self.assertEqual(backend.get_healthy_service_ids("celery"), set())
