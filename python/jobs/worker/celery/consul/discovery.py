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
Consul service discovery for the status service.

:class:`ConsulServiceDiscoveryClient` uses the shared :class:`ConsulClient`
(injected) to query Consul's health API and resolve service URLs.

:func:`get_status_service_url` is the public entry point — it transparently
discovers the URL from Consul when ``CONSUL_URL`` is set, with a TTL-based
cache, and falls back to ``STATUS_SERVICE_URL`` otherwise.
"""

from __future__ import annotations

from threading import Lock
from time import monotonic
from typing import ClassVar
import logging

from jobs.worker.celery.consul import settings as consul_settings
from jobs.worker.celery.consul.client import ConsulClient, ConsulError, get_consul_client

logger = logging.getLogger(__name__)


class ConsulDiscoveryError(ConsulError):
    """Raised when service discovery fails."""


class ConsulServiceDiscoveryClient:
    """
    Consul service discovery client (uses a :class:`ConsulClient` for HTTP).

    Queries ``/v1/health/service/<name>`` to find passing instances.
    """

    def __init__(self, client: ConsulClient) -> None:
        self._client = client

    def discover_service(self, service_name: str) -> str:
        """
        Query Consul for healthy instances of ``service_name``.

        Returns the URL (``http(s)://address:port/path``) of the first passing instance.

        Raises :class:`ConsulDiscoveryError` if no healthy instance is found.
        """
        url = f"{self._client.base_url}/v1/health/service/{service_name}"
        params = self._client.query_params(passing="true")

        response = self._client.session.get(url, params=params, timeout=self._client.timeout)
        if not response.ok:
            raise ConsulDiscoveryError(
                f"Consul health query for {service_name!r} failed: status={response.status_code} body={response.text!r}"
            )

        entries = response.json()
        if not entries:
            raise ConsulDiscoveryError(f"No healthy instances of {service_name!r} found in Consul")

        service_info = entries[0]["Service"]
        address = service_info.get("Address") or entries[0]["Node"]["Address"]
        port = service_info["Port"]

        meta = service_info.get("Meta") or {}
        scheme = meta.get("scheme", "http")
        base_path = meta.get("base_path", "/api/v1/")

        return f"{scheme}://{address}:{port}{base_path}"


_discovery_lock = Lock()


class _DiscoveryClientRegistry:
    """Process-wide singleton for :class:`ConsulServiceDiscoveryClient`."""

    _instance: ClassVar[ConsulServiceDiscoveryClient | None] = None

    @classmethod
    def get(cls) -> ConsulServiceDiscoveryClient:
        if cls._instance is not None:
            return cls._instance
        with _discovery_lock:
            if cls._instance is None:
                cls._instance = ConsulServiceDiscoveryClient(get_consul_client())
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        with _discovery_lock:
            cls._instance = None


def get_discovery_client() -> ConsulServiceDiscoveryClient:
    """Return the process-wide :class:`ConsulServiceDiscoveryClient` singleton."""
    return _DiscoveryClientRegistry.get()


def reset_discovery_client() -> None:
    """Drop the cached discovery client (mostly useful for tests)."""
    _DiscoveryClientRegistry.reset()


_cached_url: str | None = None
_cached_url_timestamp: float = 0.0
_url_cache_lock = Lock()


def get_status_service_url() -> str:
    """
    Resolve the status service URL.

    Strategy:
      - When ``CONSUL_URL`` is set, discover the service from Consul
        (cached for ``CONSUL_HEALTH_CHECK_TTL`` seconds).
      - Otherwise, fall back to the ``STATUS_SERVICE_URL`` env var
        (default ``http://localhost:8020/api/v1/``).
    """
    if not consul_settings.is_discovery_enabled():
        return consul_settings.STATUS_SERVICE_URL

    global _cached_url, _cached_url_timestamp  # noqa: PLW0603

    now = monotonic()
    if _cached_url and (now - _cached_url_timestamp) < consul_settings.CONSUL_HEALTH_CHECK_TTL:
        return _cached_url

    with _url_cache_lock:
        # Double-check after acquiring lock
        if _cached_url and (now - _cached_url_timestamp) < consul_settings.CONSUL_HEALTH_CHECK_TTL:
            return _cached_url

        try:
            client = get_discovery_client()
            url = client.discover_service(consul_settings.STATUS_SERVICE_NAME)
            _cached_url = url
            _cached_url_timestamp = monotonic()
            logger.info("Discovered status service at %s", url)
            return url
        except Exception:  # noqa: BLE001
            logger.exception("Failed to discover status service from Consul, using fallback URL")
            return consul_settings.STATUS_SERVICE_URL
