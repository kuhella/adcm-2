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
Consul-based Celery worker discovery.

``ConsulWorkerDiscoveryStep`` registers the Celery worker as a Consul service
on startup, periodically updates a TTL health check to signal liveness, and
deregisters the service on shutdown.

This is independent of the message transport — task delivery goes through the
``pgnotify`` transport while Consul provides a shared registry of live
workers for orchestrators and schedulers.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING

from celery import bootsteps
from celery.utils.nodenames import gethostname
from requests import RequestException, Session
from requests.adapters import HTTPAdapter

from jobs.scheduler.logger import logger
from jobs.worker.celery.consul import settings as consul_settings

if TYPE_CHECKING:
    from celery.worker import WorkController


DEFAULT_CHECK_TTL = "15s"
DEFAULT_DEREGISTER_AFTER = "1m"


class _ConsulServiceClient:
    """Minimal Consul agent HTTP client for service registration."""

    def __init__(
        self,
        *,
        base_url: str,
        datacenter: str | None = None,
        token: str | None = None,
        verify: str | bool = True,
        timeout: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._datacenter = datacenter
        self._timeout = timeout
        self._session = Session()
        if token:
            self._session.headers["X-Consul-Token"] = token
        self._session.verify = verify
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

    def register_service(self, *, service_id: str, name: str, tags: list[str], check_ttl: str) -> None:
        url = f"{self._base_url}/v1/agent/service/register"
        payload = {
            "ID": service_id,
            "Name": name,
            "Tags": tags,
            "Check": {
                "CheckID": f"service:{service_id}",
                "TTL": check_ttl,
                "DeregisterCriticalServiceAfter": DEFAULT_DEREGISTER_AFTER,
            },
        }
        params = self._query()
        response = self._session.put(url, params=params, json=payload, timeout=self._timeout)
        response.raise_for_status()

    def deregister_service(self, service_id: str) -> None:
        url = f"{self._base_url}/v1/agent/service/deregister/{service_id}"
        response = self._session.put(url, params=self._query(), timeout=self._timeout)
        response.raise_for_status()

    def ttl_pass(self, check_id: str) -> None:
        url = f"{self._base_url}/v1/agent/check/pass/{check_id}"
        response = self._session.put(url, params=self._query(), timeout=self._timeout)
        response.raise_for_status()

    def close(self) -> None:
        self._session.close()

    def _query(self) -> dict[str, str]:
        if self._datacenter:
            return {"dc": self._datacenter}
        return {}


class ConsulWorkerDiscoveryStep(bootsteps.StartStopStep):
    """
    Register the Celery worker as a Consul service with a TTL health check.

    The worker periodically passes the TTL check (via the timer) so that
    Consul marks it as healthy.  On shutdown the service is deregistered.
    """

    requires = {"celery.worker.components:Timer"}

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.hostname = f"celery@{gethostname()}"
        self._service_id = f"celery-worker@{gethostname()}"
        self._check_id = f"service:{self._service_id}"
        self._tref = None
        self._client: _ConsulServiceClient | None = None

    def start(self, work_controller: WorkController) -> None:
        if not consul_settings.CONSUL_URL:
            logger.debug("Consul URL not configured; worker discovery step will not start.")
            return

        verify: str | bool = True
        if consul_settings.CONSUL_CACERT_FILE:
            verify = consul_settings.CONSUL_CACERT_FILE

        self._client = _ConsulServiceClient(
            base_url=consul_settings.CONSUL_URL,
            datacenter=consul_settings.CONSUL_DATACENTER,
            token=consul_settings.CONSUL_ACL_TOKEN,
            verify=verify,
            timeout=consul_settings.CONSUL_HTTP_TIMEOUT,
        )

        try:
            self._client.register_service(
                service_id=self._service_id,
                name="celery-worker",
                tags=["celery", "worker", self.hostname],
                check_ttl=DEFAULT_CHECK_TTL,
            )
        except RequestException:
            logger.exception("Failed to register Celery worker in Consul")
            return

        logger.info("Celery worker registered in Consul as %s", self._service_id)

        self._tref = work_controller.timer.call_repeatedly(
            secs=5.0,
            fun=self._heartbeat,
        )

    def stop(self, work_controller: WorkController) -> None:
        _ = work_controller
        if self._tref is not None:
            self._tref.cancel()
            self._tref = None

        if self._client is not None:
            with suppress(RequestException):
                self._client.deregister_service(self._service_id)
                logger.info("Celery worker deregistered from Consul: %s", self._service_id)
            self._client.close()
            self._client = None

    def _heartbeat(self) -> None:
        if self._client is None:
            return
        try:
            self._client.ttl_pass(self._check_id)
        except RequestException:
            logger.warning("Failed to send Consul TTL heartbeat for %s", self._service_id)
