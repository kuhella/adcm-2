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
Consul service registry integration for the ADCM backend.

:class:`ConsulBackend` is a process-wide singleton that owns a pooled
``requests.Session`` (so TCP/TLS connections are recycled) built from
:class:`ClientSettings`. It supports ACL token authentication as well as TLS /
mutual-TLS for ``https``.
"""

from __future__ import annotations

from base64 import b64decode
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, ClassVar
import json as jsonlib

from requests import RequestException, Session
from requests.adapters import HTTPAdapter

DEFAULT_HEALTH_CHECK_INTERVAL = "10s"
DEFAULT_HEALTH_CHECK_TIMEOUT = "5s"
DEFAULT_DEREGISTER_CRITICAL_SERVICE_AFTER = "5m"
DEFAULT_HTTP_TIMEOUT = 5.0
DEFAULT_POOL_SIZE = 10


class ConsulError(RuntimeError):
    """Raised for unexpected Consul HTTP responses or transport errors."""


@dataclass(slots=True)
class ClientSettings:
    # main

    url: str
    datacenter: str | None = None
    acl_token: str | None = None

    # TLS / mTLS

    cacert_file: str | None = None
    client_cert_file: str | None = None
    client_key_file: str | None = None

    # transport tuning

    http_timeout: float = DEFAULT_HTTP_TIMEOUT
    pool_size: int = DEFAULT_POOL_SIZE


@dataclass(slots=True)
class ServiceRegistration:
    """Payload describing a service to register in Consul.

    Two health-check styles are supported:

    * HTTP check - set ``health_check_url`` (suited for HTTP services, requires
      ``address`` / ``port``).
    * TTL check - set ``check_ttl`` (suited for non-HTTP services like Celery
      workers that report liveness themselves). ``address`` / ``port`` are
      omitted from the payload when not provided.
    """

    service_id: str
    name: str
    address: str | None = None
    port: int | None = None
    health_check_url: str | None = None
    check_ttl: str | None = None
    datacenter: str | None = None
    tags: list[str] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)
    check_interval: str = DEFAULT_HEALTH_CHECK_INTERVAL
    check_timeout: str = DEFAULT_HEALTH_CHECK_TIMEOUT
    deregister_critical_service_after: str = DEFAULT_DEREGISTER_CRITICAL_SERVICE_AFTER

    @property
    def ttl_check_id(self) -> str:
        """Stable check id used to update this service's TTL check."""
        return f"service:{self.service_id}:ttl"

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ID": self.service_id,
            "Name": self.name,
            "Tags": list(self.tags),
            "Meta": dict(self.meta),
            "Checks": [self._build_check()],
        }
        if self.address is not None:
            payload["Address"] = self.address
        if self.port is not None:
            payload["Port"] = self.port
        if self.datacenter:
            payload["Datacenter"] = self.datacenter
        return payload

    def _build_check(self) -> dict[str, Any]:
        if self.check_ttl is not None:
            return {
                "CheckID": self.ttl_check_id,
                "TTL": self.check_ttl,
                "DeregisterCriticalServiceAfter": self.deregister_critical_service_after,
            }
        return {
            "HTTP": self.health_check_url,
            "Interval": self.check_interval,
            "Timeout": self.check_timeout,
            "DeregisterCriticalServiceAfter": self.deregister_critical_service_after,
        }


class ConsulBackend:
    """Process-wide Consul client owning a shared, TLS/token-aware HTTP session."""

    _instance: ClassVar[ConsulBackend | None] = None
    _lock: ClassVar[Lock] = Lock()

    def __init__(self, settings: ClientSettings) -> None:
        self._settings = settings
        self._base_url = settings.url.rstrip("/")
        self._timeout = settings.http_timeout

        session = Session()
        if settings.acl_token:
            session.headers["X-Consul-Token"] = settings.acl_token
        session.verify = settings.cacert_file if settings.cacert_file else True
        if settings.client_cert_file and settings.client_key_file:
            session.cert = (settings.client_cert_file, settings.client_key_file)
        adapter = HTTPAdapter(pool_connections=settings.pool_size, pool_maxsize=settings.pool_size)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        self._session = session

    @property
    def settings(self) -> ClientSettings:
        return self._settings

    @classmethod
    def initialize(cls, settings: ClientSettings) -> ConsulBackend:
        """Create (or replace) the shared backend instance."""
        with cls._lock:
            if cls._instance is not None:
                cls._instance.close()
            cls._instance = cls(settings)
            return cls._instance

    @classmethod
    def instance(cls) -> ConsulBackend | None:
        """Return the shared backend instance or ``None`` if not initialized."""
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Drop the cached instance (closes its session)."""
        with cls._lock:
            if cls._instance is not None:
                cls._instance.close()
                cls._instance = None

    def register(self, registration: ServiceRegistration) -> None:
        url = f"{self._base_url}/v1/agent/service/register"
        try:
            response = self._session.put(
                url, params=self._query(), json=registration.to_payload(), timeout=self._timeout
            )
        except RequestException as error:
            raise ConsulError(f"Failed to register service in Consul: {error}") from error

        if not response.ok:
            raise ConsulError(
                f"Failed to register service in Consul: status={response.status_code} body={response.text!r}"
            )

    def deregister(self, service_id: str) -> None:
        url = f"{self._base_url}/v1/agent/service/deregister/{service_id}"
        try:
            response = self._session.put(url, params=self._query(), timeout=self._timeout)
        except RequestException as error:
            raise ConsulError(f"Failed to deregister service in Consul: {error}") from error

        if not response.ok:
            raise ConsulError(
                f"Failed to deregister service in Consul: status={response.status_code} body={response.text!r}"
            )

    def pass_check(self, check_id: str, note: str = "") -> None:
        """Mark a TTL check as passing, resetting its expiration timer."""
        url = f"{self._base_url}/v1/agent/check/pass/{check_id}"
        params = self._query()
        if note:
            params["note"] = note
        try:
            response = self._session.put(url, params=params, timeout=self._timeout)
        except RequestException as error:
            raise ConsulError(f"Failed to pass Consul check {check_id!r}: {error}") from error

        if not response.ok:
            raise ConsulError(
                f"Failed to pass Consul check {check_id!r}: status={response.status_code} body={response.text!r}"
            )

    def get_healthy_service_ids(self, name: str) -> set[str]:
        """Return ids of instances of service ``name`` whose checks are all passing."""
        url = f"{self._base_url}/v1/health/service/{name}"
        try:
            response = self._session.get(url, params=self._query(passing="true"), timeout=self._timeout)
        except RequestException as error:
            raise ConsulError(f"Failed to query Consul service {name!r}: {error}") from error

        if not response.ok:
            raise ConsulError(
                f"Failed to query Consul service {name!r}: status={response.status_code} body={response.text!r}"
            )

        entries = response.json() or []
        return {service_id for entry in entries if (service_id := entry.get("Service", {}).get("ID"))}

    def kv_put(self, key: str, value: Any) -> None:
        """Store ``value`` at KV ``key``. ``value`` is json-encoded automatically."""
        url = f"{self._base_url}/v1/kv/{_clean_kv_key(key)}"
        try:
            response = self._session.put(url, params=self._query(), data=_encode_kv_value(value), timeout=self._timeout)
        except RequestException as error:
            raise ConsulError(f"Failed to PUT Consul kv {key!r}: {error}") from error

        if not response.ok or response.text.strip() != "true":
            raise ConsulError(f"Failed to PUT Consul kv {key!r}: status={response.status_code} body={response.text!r}")

    def kv_get(self, key: str) -> Any | None:
        """Fetch the value at KV ``key`` or ``None`` if it does not exist."""
        url = f"{self._base_url}/v1/kv/{_clean_kv_key(key)}"
        try:
            response = self._session.get(url, params=self._query(), timeout=self._timeout)
        except RequestException as error:
            raise ConsulError(f"Failed to GET Consul kv {key!r}: {error}") from error

        if response.status_code == 404:
            return None
        if not response.ok:
            raise ConsulError(f"Failed to GET Consul kv {key!r}: status={response.status_code} body={response.text!r}")

        payload = response.json()
        if not payload:
            return None
        return _decode_kv_value(payload[0].get("Value"))

    def kv_list_keys(self, prefix: str) -> list[str]:
        """Return the full list of keys under ``prefix`` (recursive)."""
        url = f"{self._base_url}/v1/kv/{_clean_kv_key(prefix)}"
        try:
            response = self._session.get(url, params=self._query(keys="true"), timeout=self._timeout)
        except RequestException as error:
            raise ConsulError(f"Failed to LIST Consul kv {prefix!r}: {error}") from error

        if response.status_code == 404:
            return []
        if not response.ok:
            raise ConsulError(
                f"Failed to LIST Consul kv {prefix!r}: status={response.status_code} body={response.text!r}"
            )

        data = response.json()
        return list(data) if data else []

    def kv_list_pairs(self, prefix: str) -> dict[str, Any]:
        """Return ``{key: value}`` for every key under ``prefix`` (recursive)."""
        url = f"{self._base_url}/v1/kv/{_clean_kv_key(prefix)}"
        try:
            response = self._session.get(url, params=self._query(recurse="true"), timeout=self._timeout)
        except RequestException as error:
            raise ConsulError(f"Failed to RECURSE Consul kv {prefix!r}: {error}") from error

        if response.status_code == 404:
            return {}
        if not response.ok:
            raise ConsulError(
                f"Failed to RECURSE Consul kv {prefix!r}: status={response.status_code} body={response.text!r}"
            )

        payload = response.json() or []
        return {entry["Key"]: _decode_kv_value(entry.get("Value")) for entry in payload}

    def kv_delete(self, key: str, *, recurse: bool = False) -> None:
        """Delete KV ``key`` (or everything under it when ``recurse`` is true)."""
        url = f"{self._base_url}/v1/kv/{_clean_kv_key(key)}"
        params = self._query()
        if recurse:
            params["recurse"] = "true"
        try:
            response = self._session.delete(url, params=params, timeout=self._timeout)
        except RequestException as error:
            raise ConsulError(f"Failed to DELETE Consul kv {key!r}: {error}") from error

        if not response.ok:
            raise ConsulError(
                f"Failed to DELETE Consul kv {key!r}: status={response.status_code} body={response.text!r}"
            )

    def check_connection(self) -> bool:
        """Return ``True`` when the Consul agent is reachable and responsive."""
        url = f"{self._base_url}/v1/status/leader"
        try:
            response = self._session.get(url, params=self._query(), timeout=self._timeout)
        except RequestException:
            return False

        return response.ok

    def close(self) -> None:
        self._session.close()

    def _query(self, **extra: str) -> dict[str, str]:
        params: dict[str, str] = {}
        if self._settings.datacenter:
            params["dc"] = self._settings.datacenter
        params.update(extra)
        return params


def _encode_kv_value(value: Any) -> bytes:
    """Serialize a python value to the json bytes Consul stores for a KV key."""
    return jsonlib.dumps(value, ensure_ascii=False, default=str).encode("utf-8")


def _decode_kv_value(raw: str | None) -> Any:
    """Decode the base64 ``Value`` returned by Consul back into a python value.

    Falls back to the raw string when the stored value is not valid json.
    """
    if raw is None:
        return None
    decoded = b64decode(raw).decode("utf-8")
    try:
        return jsonlib.loads(decoded)
    except jsonlib.JSONDecodeError:
        return decoded


def _clean_kv_key(key: str) -> str:
    """Strip a leading slash so KV keys are stored without an empty root segment."""
    return key.lstrip("/")
