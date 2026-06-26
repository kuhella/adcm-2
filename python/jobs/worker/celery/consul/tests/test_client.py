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

from integrations.consul import ConsulError

from jobs.worker.celery.consul import registrar
from jobs.worker.celery.consul.client import ConsulKVClient, ConsulKVError, get_consul_kv_client


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.error: ConsulError | None = None

    def kv_put(self, key, value):
        self.calls.append(("put", key, value))
        self._maybe_raise()

    def kv_get(self, key):
        self.calls.append(("get", key))
        self._maybe_raise()
        return {"value": key}

    def kv_list_keys(self, prefix):
        self.calls.append(("list_keys", prefix))
        self._maybe_raise()
        return [prefix]

    def kv_list_pairs(self, prefix):
        self.calls.append(("list_pairs", prefix))
        self._maybe_raise()
        return {prefix: 1}

    def kv_delete(self, key, *, recurse=False):
        self.calls.append(("delete", key, recurse))
        self._maybe_raise()

    def _maybe_raise(self) -> None:
        if self.error is not None:
            raise self.error


class TestConsulKVClient(TestCase):
    def test_delegates_to_backend(self) -> None:
        backend = FakeBackend()
        client = ConsulKVClient(backend=backend)

        client.put("k", {"a": 1})
        self.assertEqual(client.get("k"), {"value": "k"})
        self.assertEqual(client.list_keys("p/"), ["p/"])
        self.assertEqual(client.list_pairs("p/"), {"p/": 1})
        client.delete("k", recurse=True)

        self.assertEqual(
            backend.calls,
            [
                ("put", "k", {"a": 1}),
                ("get", "k"),
                ("list_keys", "p/"),
                ("list_pairs", "p/"),
                ("delete", "k", True),
            ],
        )

    def test_translates_backend_error(self) -> None:
        backend = FakeBackend()
        backend.error = ConsulError("boom")
        client = ConsulKVClient(backend=backend)

        with self.assertRaises(ConsulKVError):
            client.put("k", 1)

    def test_get_consul_kv_client_uses_shared_backend(self) -> None:
        backend = FakeBackend()
        with mock.patch.object(registrar, "get_consul_backend", return_value=backend):
            client = get_consul_kv_client()

        self.assertIsInstance(client, ConsulKVClient)
        client.put("k", 1)
        self.assertEqual(backend.calls, [("put", "k", 1)])
