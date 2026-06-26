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
``ConsulRegistrationStep`` - worker :class:`~celery.bootsteps.StartStopStep`
that registers the Celery worker as a Consul service (FR1).

The step is best-effort and entirely opt-in: when ``CONSUL_URL`` is not set it
does nothing, so a worker keeps starting normally without Consul. When Consul is
configured the step:

* registers the worker as a service with a TTL health check (no address/port,
  since the worker is not an HTTP service);
* refreshes the TTL check on a timer so the service stays healthy;
* deregisters the worker on shutdown.

Any Consul error is logged and never propagated, so an unreachable Consul agent
does not crash the worker.
"""

from __future__ import annotations

from celery import bootsteps
from celery.utils.nodenames import gethostname
from integrations.consul import ServiceRegistration

from jobs.scheduler.logger import logger
from jobs.worker.celery.consul import registrar
from jobs.worker.celery.consul import settings as consul_settings


class ConsulRegistrationStep(bootsteps.StartStopStep):
    """Register/deregister the worker in Consul and keep its TTL check alive."""

    requires = {"celery.worker.components:Timer"}

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.hostname = f"celery@{gethostname()}"
        self._registration: ServiceRegistration | None = None
        self._tref = None

    def start(self, work_controller) -> None:
        if not consul_settings.is_consul_configured():
            logger.debug("Consul not configured; ConsulRegistrationStep will not start.")
            return

        registration = registrar.build_worker_registration(hostname=self.hostname, adcm_uuid=_get_adcm_uuid())

        try:
            backend = registrar.get_consul_backend()
            backend.register(registration)
            backend.pass_check(registration.ttl_check_id, note="worker started")
        except Exception:  # noqa: BLE001
            logger.error("Failed to register Celery worker in Consul")
            return

        self._registration = registration
        self._tref = work_controller.timer.call_repeatedly(
            secs=consul_settings.CONSUL_HEALTH_CHECK_TTL_REFRESH_INTERVAL,
            fun=self._refresh_ttl,
        )
        logger.info(
            f"Celery worker {self.hostname} registered in Consul as service "
            f"{registration.name!r} (ttl={consul_settings.CONSUL_HEALTH_CHECK_TTL})"
        )

    def stop(self, work_controller) -> None:
        _ = work_controller
        if self._tref is not None:
            self._tref.cancel()
            self._tref = None

        if self._registration is None:
            return

        try:
            registrar.get_consul_backend().deregister(self._registration.service_id)
            logger.info(f"Celery worker {self.hostname} deregistered from Consul")
        except Exception:  # noqa: BLE001
            logger.error("Failed to deregister Celery worker from Consul")
        finally:
            self._registration = None

    def _refresh_ttl(self) -> None:
        if self._registration is None:
            return
        try:
            registrar.get_consul_backend().pass_check(self._registration.ttl_check_id)
        except Exception:  # noqa: BLE001
            logger.error("Failed to refresh Consul TTL check for Celery worker")


def _get_adcm_uuid() -> str | None:
    """Best-effort lookup of the ADCM uuid used to tag the worker service."""
    try:
        from cm.impl.adcm.repo import ADCMRepo

        uuid = ADCMRepo().get_uuid()
    except Exception:  # noqa: BLE001
        logger.error("Failed to resolve ADCM uuid for Consul worker registration")
        return None

    return str(uuid) if uuid else None
