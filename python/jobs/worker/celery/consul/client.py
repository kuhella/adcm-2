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

"""
Consul KV access for the custom Celery control/inspect transport.

The actual HTTP work (pooled session, ACL token, TLS/mTLS, datacenter scoping)
is delegated to the shared :class:`~integrations.consul.ConsulBackend`, the same
process-wide client used for worker registration and discovery. :class:`ConsulKVClient`
is a thin adapter that keeps the KV-specific method names expected by the
control/inspect transport while reusing that single connection pool.

Callers obtain the client via :func:`get_consul_kv_client`.
"""

from __future__ import annotations

from typing import Any

from integrations.consul import ConsulBackend, ConsulError

from jobs.worker.celery.consul import registrar


class ConsulKVError(RuntimeError):
    """Raised for any unexpected Consul KV HTTP response."""


class ConsulKVClient:
    """KV-focused adapter over the shared :class:`ConsulBackend`.

    Only the endpoints required by the control/inspect transport are exposed:
    :meth:`put`, :meth:`get`, :meth:`list_keys`, :meth:`list_pairs` and
    :meth:`delete`. Each call is forwarded to the backend's ``kv_*`` methods.
    """

    def __init__(self, backend: ConsulBackend) -> None:
        self._backend = backend

    def put(self, key: str, value: Any) -> None:
        """Store ``value`` at ``key``. ``value`` is json-encoded automatically."""
        try:
            self._backend.kv_put(key, value)
        except ConsulError as error:
            raise ConsulKVError(str(error)) from error

    def get(self, key: str) -> Any | None:
        """Fetch the value at ``key`` or ``None`` if it does not exist."""
        try:
            return self._backend.kv_get(key)
        except ConsulError as error:
            raise ConsulKVError(str(error)) from error

    def list_keys(self, prefix: str) -> list[str]:
        """Return the full list of keys under ``prefix`` (recursive)."""
        try:
            return self._backend.kv_list_keys(prefix)
        except ConsulError as error:
            raise ConsulKVError(str(error)) from error

    def list_pairs(self, prefix: str) -> dict[str, Any]:
        """Return ``{key: value}`` for every key under ``prefix`` (recursive)."""
        try:
            return self._backend.kv_list_pairs(prefix)
        except ConsulError as error:
            raise ConsulKVError(str(error)) from error

    def delete(self, key: str, *, recurse: bool = False) -> None:
        """Delete ``key`` (or everything under it when ``recurse`` is true)."""
        try:
            self._backend.kv_delete(key, recurse=recurse)
        except ConsulError as error:
            raise ConsulKVError(str(error)) from error


__all__ = [
    "ConsulKVClient",
    "ConsulKVError",
    "get_consul_kv_client",
    "reset_consul_kv_client",
]


def get_consul_kv_client() -> ConsulKVClient:
    """Return a :class:`ConsulKVClient` backed by the shared Consul backend."""
    return ConsulKVClient(backend=registrar.get_consul_backend())


def reset_consul_kv_client() -> None:
    """Drop the cached Consul backend (mostly useful for tests)."""
    registrar.reset_consul_backend()
