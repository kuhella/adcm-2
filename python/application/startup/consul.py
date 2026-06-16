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
Startup wiring that registers the ADCM backend as a Consul service and
deregisters it on ``SIGINT`` / ``SIGTERM``.

Connection credentials, the ADCM URL and the ADCM uuid are resolved from the DI
container (the same approach used for the secrets backend during startup), while
health-check tuning and ``STATUS_SERVICE_BASE_PATH`` come from Django settings.

Registration is best-effort: any failure is logged and never aborts ADCM
startup, so an unreachable Consul agent does not prevent the backend from
serving requests.
"""

from __future__ import annotations

from types import FrameType
from typing import Callable
from urllib.parse import urlsplit
import os
import signal
import socket
import logging

from core.scenarios.adcm import ADCMUUID, DefaultURL
from dishka import Container
from django.conf import settings
from integrations.consul import ClientSettings, ConsulBackend, ServiceRegistration

logger = logging.getLogger("adcm")

_DEFAULT_PORT_BY_SCHEME = {"http": 80, "https": 443}
_PREVIOUS_SIGNAL_HANDLERS: dict[int, Callable | int | None] = {}


class ConsulConfigurationError(RuntimeError):
    """Raised when Consul-related configuration is inconsistent."""


def build_service_registration(
    *, connection: ClientSettings, adcm_url: str, adcm_uuid: str | None
) -> ServiceRegistration:
    parts = urlsplit(adcm_url)
    if not parts.scheme or not parts.hostname:
        raise ConsulConfigurationError(f"DEFAULT_ADCM_URL is not a valid URL: {adcm_url!r}")

    base_url = f"{parts.scheme}://{parts.netloc}"
    port = parts.port or _DEFAULT_PORT_BY_SCHEME.get(parts.scheme, 80)
    container_id = socket.gethostname()

    status_service_base_path = settings.STATUS_SERVICE_BASE_PATH
    tags = ["adcm", "backend"]
    if adcm_uuid:
        tags.append(adcm_uuid)

    return ServiceRegistration(
        service_id=f"adcm@{container_id}",
        name="adcm",
        datacenter=connection.datacenter,
        tags=tags,
        address=parts.hostname,
        port=port,
        meta={"status_service_url": _join_url(base_url, status_service_base_path)},
        health_check_url=_join_url(base_url, "/api/health/ready"),
        check_interval=settings.CONSUL_HEALTH_CHECK_INTERVAL,
        check_timeout=settings.CONSUL_HEALTH_CHECK_TIMEOUT,
        deregister_critical_service_after=settings.CONSUL_DEREGISTER_CRITICAL_SERVICE_AFTER,
    )


def register_adcm_in_consul(*, container: Container) -> None:
    """Initialize the :class:`ConsulBackend` singleton and register ADCM (best-effort)."""
    connection = container.get(ClientSettings | None)
    if connection is None:
        return

    adcm_url = container.get(DefaultURL | None)
    if adcm_url is None:
        # FR3 guarantees this can't happen for a correctly configured deployment,
        # but we keep registration defensive so it never raises during startup.
        logger.error("Skipping Consul registration: DEFAULT_ADCM_URL is not set")
        return

    backend = ConsulBackend.initialize(connection)

    try:
        registration = build_service_registration(
            connection=connection, adcm_url=str(adcm_url), adcm_uuid=container.get(ADCMUUID | None)
        )
        backend.register(registration)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to register ADCM in Consul")
        return

    _install_deregistration_handlers(service_id=registration.service_id)
    logger.info("ADCM registered in Consul as %s", registration.service_id)


def deregister_adcm_from_consul(service_id: str) -> None:
    backend = ConsulBackend.instance()
    if backend is None:
        return

    try:
        backend.deregister(service_id)
        logger.info("ADCM deregistered from Consul (%s)", service_id)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to deregister ADCM from Consul")


def _install_deregistration_handlers(*, service_id: str) -> None:
    def handler(signum: int, frame: FrameType | None) -> None:
        deregister_adcm_from_consul(service_id)

        previous = _PREVIOUS_SIGNAL_HANDLERS.get(signum)
        if callable(previous):
            previous(signum, frame)
        elif previous in (signal.SIG_DFL, None):
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            _PREVIOUS_SIGNAL_HANDLERS[signum] = signal.signal(signum, handler)
        except ValueError:
            # signal handlers can only be installed in the main thread
            logger.warning("Could not install Consul deregistration handler for signal %s", signum)


def _join_url(base_url: str, path: str) -> str:
    if not path:
        return base_url
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"
