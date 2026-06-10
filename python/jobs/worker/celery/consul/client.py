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
Low-level Consul HTTP client hierarchy.

:class:`ConsulClient` is the base class responsible for HTTP session management,
connection pooling, TLS/mTLS, ACL token, and datacenter configuration.

:class:`ConsulKVClient` uses a :class:`ConsulClient` instance (injected) to
perform KV-specific operations for the Celery control/inspect transport.

A single process-wide :class:`ConsulClient` instance is built by
:func:`get_consul_client` and shared across all higher-level clients.
"""

from __future__ import annotations

from base64 import b64decode, b64encode
from threading import Lock
from typing import Any, ClassVar
import json as jsonlib

from requests import Session
from requests.adapters import HTTPAdapter

from jobs.worker.celery.consul import settings as consul_settings


class ConsulError(RuntimeError):
    """Base error for Consul client operations."""


class ConsulKVError(ConsulError):
    """Raised for any unexpected Consul KV HTTP response."""


class ConsulClient:
    """
    Base Consul HTTP client with pooled connections.

    Manages the shared HTTP session (connection pooling, TLS/mTLS, ACL token,
    datacenter).  Passed into higher-level clients via dependency injection.
    """

    def __init__(
        self,
        *,
        base_url: str,
        datacenter: str | None = None,
        token: str | None = None,
        verify: str | bool = True,
        cert: tuple[str, str] | None = None,
        timeout: float = 5.0,
        pool_size: int = 10,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._datacenter = datacenter
        self._timeout = timeout

        session = Session()
        if token:
            session.headers["X-Consul-Token"] = token
        session.verify = verify
        if cert:
            session.cert = cert
        adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        self._session = session

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def session(self) -> Session:
        return self._session

    def close(self) -> None:
        self._session.close()

    def query_params(self, **extra: str) -> dict[str, str]:
        """Build query params dict with datacenter and any extras."""
        params: dict[str, str] = {}
        if self._datacenter:
            params["dc"] = self._datacenter
        params.update(extra)
        return params


class ConsulKVClient:
    """
    Consul KV client that uses a :class:`ConsulClient` for HTTP operations.

    Only the endpoints required by the control/inspect transport are
    exposed: :meth:`put`, :meth:`get`, :meth:`list_keys`, :meth:`list_pairs`
    and :meth:`delete`.
    """

    _KV_PATH = "/v1/kv"

    def __init__(self, client: ConsulClient) -> None:
        self._client = client

    def put(self, key: str, value: Any) -> None:
        """Store ``value`` at ``key``. ``value`` is json-encoded automatically."""
        url = f"{self._client.base_url}{self._KV_PATH}/{_clean(key)}"
        response = self._client.session.put(
            url,
            params=self._client.query_params(),
            data=_encode_value(value),
            timeout=self._client.timeout,
        )
        if not response.ok or response.text.strip() != "true":
            raise ConsulKVError(
                f"Failed to PUT consul kv {key!r}: status={response.status_code} body={response.text!r}"
            )

    def get(self, key: str) -> Any | None:
        """Fetch the value at ``key`` or ``None`` if it does not exist."""
        url = f"{self._client.base_url}{self._KV_PATH}/{_clean(key)}"
        response = self._client.session.get(url, params=self._client.query_params(), timeout=self._client.timeout)
        if response.status_code == 404:
            return None
        if not response.ok:
            raise ConsulKVError(
                f"Failed to GET consul kv {key!r}: status={response.status_code} body={response.text!r}"
            )
        payload = response.json()
        if not payload:
            return None
        return _decode_value(payload[0].get("Value"))

    def list_keys(self, prefix: str) -> list[str]:
        """Return the full list of keys under ``prefix`` (recursive)."""
        url = f"{self._client.base_url}{self._KV_PATH}/{_clean(prefix)}"
        response = self._client.session.get(
            url, params=self._client.query_params(keys="true"), timeout=self._client.timeout
        )
        if response.status_code == 404:
            return []
        if not response.ok:
            raise ConsulKVError(
                f"Failed to LIST consul kv {prefix!r}: status={response.status_code} body={response.text!r}"
            )
        data = response.json()
        return list(data) if data else []

    def list_pairs(self, prefix: str) -> dict[str, Any]:
        """Return ``{key: value}`` for every key under ``prefix`` (recursive)."""
        url = f"{self._client.base_url}{self._KV_PATH}/{_clean(prefix)}"
        response = self._client.session.get(
            url, params=self._client.query_params(recurse="true"), timeout=self._client.timeout
        )
        if response.status_code == 404:
            return {}
        if not response.ok:
            raise ConsulKVError(
                f"Failed to RECURSE consul kv {prefix!r}: status={response.status_code} body={response.text!r}"
            )
        payload = response.json() or []
        return {entry["Key"]: _decode_value(entry.get("Value")) for entry in payload}

    def delete(self, key: str, *, recurse: bool = False) -> None:
        """Delete ``key`` (or everything under it when ``recurse`` is true)."""
        url = f"{self._client.base_url}{self._KV_PATH}/{_clean(key)}"
        params = self._client.query_params()
        if recurse:
            params["recurse"] = "true"
        response = self._client.session.delete(url, params=params, timeout=self._client.timeout)
        if not response.ok:
            raise ConsulKVError(
                f"Failed to DELETE consul kv {key!r}: status={response.status_code} body={response.text!r}"
            )

    def close(self) -> None:
        self._client.close()


def _encode_value(value: Any) -> bytes:
    return jsonlib.dumps(value, ensure_ascii=False, default=str).encode("utf-8")


def _decode_value(raw: str | None) -> Any | None:
    if raw is None:
        return None
    decoded = b64decode(raw).decode("utf-8")
    try:
        return jsonlib.loads(decoded)
    except jsonlib.JSONDecodeError:
        return decoded


def _clean(key: str) -> str:
    return key.lstrip("/")


__all__ = [
    "ConsulClient",
    "ConsulError",
    "ConsulKVClient",
    "ConsulKVError",
    "b64encode",
    "get_consul_client",
    "get_consul_kv_client",
    "reset_consul_client",
    "reset_consul_kv_client",
]


_client_lock = Lock()


class _ConsulClientRegistry:
    """Process-wide singleton for the base :class:`ConsulClient`."""

    _instance: ClassVar[ConsulClient | None] = None

    @classmethod
    def get(cls) -> ConsulClient:
        if cls._instance is not None:
            return cls._instance
        with _client_lock:
            if cls._instance is None:
                cls._instance = _build_client()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        with _client_lock:
            if cls._instance is not None:
                cls._instance.close()
                cls._instance = None


def _build_client() -> ConsulClient:
    if not consul_settings.CONSUL_URL:
        raise ConsulError("CONSUL_URL is not set: Consul client cannot be created.")

    verify: str | bool = True
    if consul_settings.CONSUL_CACERT_FILE:
        verify = consul_settings.CONSUL_CACERT_FILE

    cert: tuple[str, str] | None = None
    if consul_settings.CONSUL_CLIENT_CERT_FILE and consul_settings.CONSUL_CLIENT_KEY_FILE:
        cert = (consul_settings.CONSUL_CLIENT_CERT_FILE, consul_settings.CONSUL_CLIENT_KEY_FILE)

    return ConsulClient(
        base_url=consul_settings.CONSUL_URL,
        datacenter=consul_settings.CONSUL_DATACENTER,
        token=consul_settings.CONSUL_ACL_TOKEN,
        verify=verify,
        cert=cert,
        timeout=consul_settings.CONSUL_HTTP_TIMEOUT,
        pool_size=consul_settings.CONSUL_HTTP_POOL_SIZE,
    )


def get_consul_client() -> ConsulClient:
    """Return the process-wide :class:`ConsulClient` instance."""
    return _ConsulClientRegistry.get()


def reset_consul_client() -> None:
    """Drop the cached base client (resets all dependants)."""
    _ConsulClientRegistry.reset()


_kv_lock = Lock()


class _ConsulKVClientRegistry:
    """Process-wide singleton for :class:`ConsulKVClient`."""

    _instance: ClassVar[ConsulKVClient | None] = None

    @classmethod
    def get(cls) -> ConsulKVClient:
        if cls._instance is not None:
            return cls._instance
        with _kv_lock:
            if cls._instance is None:
                cls._instance = ConsulKVClient(get_consul_client())
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        with _kv_lock:
            cls._instance = None


def get_consul_kv_client() -> ConsulKVClient:
    """Return the process-wide :class:`ConsulKVClient` instance."""
    return _ConsulKVClientRegistry.get()


def reset_consul_kv_client() -> None:
    """Drop the cached KV client (mostly useful for tests)."""
    _ConsulKVClientRegistry.reset()
