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
Service-discovery helpers backed by the shared
:class:`~integrations.consul.ConsulBackend`.

This module is the single place that bridges the env-based Celery Consul
settings to the reusable ``integrations.consul`` client. It is used both by:

* the worker registration bootstep (FR1) - to register/deregister the worker as
  a Consul service and to refresh its TTL health check;
* worker discovery (FR2) - to list the workers currently registered and healthy
  in Consul instead of querying the database heartbeat table.
"""

from __future__ import annotations

from threading import Lock
from typing import ClassVar

from integrations.consul import ClientSettings, ConsulBackend, ServiceRegistration

from jobs.worker.celery.consul import settings as consul_settings


def build_client_settings() -> ClientSettings:
    """Build :class:`ClientSettings` from the worker Consul environment settings."""
    if not consul_settings.CONSUL_URL:
        raise RuntimeError("CONSUL_URL is not set: Consul service discovery cannot be used.")

    return ClientSettings(
        url=consul_settings.CONSUL_URL,
        datacenter=consul_settings.CONSUL_DATACENTER,
        acl_token=consul_settings.CONSUL_ACL_TOKEN,
        cacert_file=consul_settings.CONSUL_CACERT_FILE,
        client_cert_file=consul_settings.CONSUL_CLIENT_CERT_FILE,
        client_key_file=consul_settings.CONSUL_CLIENT_KEY_FILE,
        http_timeout=consul_settings.CONSUL_HTTP_TIMEOUT,
        pool_size=consul_settings.CONSUL_HTTP_POOL_SIZE,
    )


def build_worker_registration(*, hostname: str, adcm_uuid: str | None) -> ServiceRegistration:
    """Describe the Celery worker as a Consul service with a TTL health check.

    No address/port is set: the worker is not an HTTP service, liveness is
    reported by the worker itself through the TTL check.
    """
    tags = ["adcm", "celery"]
    if adcm_uuid:
        tags.append(adcm_uuid)

    return ServiceRegistration(
        service_id=hostname,
        name=consul_settings.CONSUL_SERVICE_NAME,
        datacenter=consul_settings.CONSUL_DATACENTER,
        tags=tags,
        check_ttl=consul_settings.CONSUL_HEALTH_CHECK_TTL,
        deregister_critical_service_after=consul_settings.CONSUL_DEREGISTER_CRITICAL_SERVICE_AFTER,
    )


def discover_active_workers() -> set[str]:
    """Return ids of Celery workers currently healthy in Consul.

    The returned ids match the Celery worker hostnames (``celery@<host>``), the
    same values produced by the database-backed implementation.
    """
    backend = get_consul_backend()
    return backend.get_healthy_service_ids(consul_settings.CONSUL_SERVICE_NAME)


_backend_lock = Lock()


class _ConsulBackendRegistry:
    _instance: ClassVar[ConsulBackend | None] = None

    @classmethod
    def get(cls) -> ConsulBackend:
        if cls._instance is not None:
            return cls._instance
        with _backend_lock:
            if cls._instance is None:
                cls._instance = ConsulBackend(build_client_settings())
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        with _backend_lock:
            if cls._instance is not None:
                cls._instance.close()
                cls._instance = None


def get_consul_backend() -> ConsulBackend:
    """Return the process-wide Consul backend used for service discovery."""
    return _ConsulBackendRegistry.get()


def reset_consul_backend() -> None:
    """Drop the cached backend (mostly useful for tests)."""
    _ConsulBackendRegistry.reset()
