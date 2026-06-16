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

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import ClassVar
from urllib.parse import urlparse
import os
import signal
import socket
import logging

from requests import Session
from requests.adapters import HTTPAdapter

logger = logging.getLogger("adcm")


class ConsulError(RuntimeError):
    """Raised for any unexpected Consul HTTP response."""


@dataclass(frozen=True, slots=True)
class ConsulSettings:
    url: str
    datacenter: str | None = None
    acl_token: str | None = None
    cacert_file: str | None = None
    client_cert_file: str | None = None
    client_key_file: str | None = None
    health_check_interval: str = "10s"
    health_check_timeout: str = "5s"
    deregister_critical_service_after: str = "5m"


def _get_container_id() -> str:
    cgroup_path = Path("/proc/self/cgroup")
    if cgroup_path.exists():
        for line in cgroup_path.read_text().splitlines():
            parts = line.strip().split("/")
            if len(parts) > 2 and len(parts[-1]) >= 12:
                return parts[-1][:12]

    hostname = socket.gethostname()
    return hostname[:12] if len(hostname) >= 12 else hostname


def _parse_host_port(url: str) -> tuple[str, int]:
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


class ConsulBackend:
    _instance: ClassVar[ConsulBackend | None] = None
    _lock: ClassVar[Lock] = Lock()

    def __init__(self, settings: ConsulSettings) -> None:
        self._settings = settings
        self._session = self._build_session()
        self._service_id: str | None = None

    @classmethod
    def get_instance(cls) -> ConsulBackend | None:
        return cls._instance

    @classmethod
    def initialize(cls, settings: ConsulSettings) -> ConsulBackend:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(settings)
            return cls._instance

    def _build_session(self) -> Session:
        session = Session()

        if self._settings.acl_token:
            session.headers["X-Consul-Token"] = self._settings.acl_token

        if self._settings.cacert_file:
            session.verify = self._settings.cacert_file
        else:
            session.verify = True

        if self._settings.client_cert_file and self._settings.client_key_file:
            session.cert = (self._settings.client_cert_file, self._settings.client_key_file)

        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        return session

    @property
    def base_url(self) -> str:
        return self._settings.url.rstrip("/")

    def check_connectivity(self) -> bool:
        try:
            params: dict[str, str] = {}
            if self._settings.datacenter:
                params["dc"] = self._settings.datacenter

            response = self._session.get(f"{self.base_url}/v1/status/leader", params=params, timeout=5.0)
            return response.ok
        except Exception:  # noqa: BLE001
            return False

    def register_service(
        self,
        *,
        adcm_url: str,
        status_service_base_path: str | None = None,
    ) -> None:
        container_id = _get_container_id()
        service_id = f"adcm@{container_id}"
        host, port = _parse_host_port(adcm_url)

        adcm_uuid = os.getenv("ADCM_UUID", container_id)

        meta: dict[str, str] = {}
        if status_service_base_path:
            meta["status_service_url"] = status_service_base_path

        payload: dict = {
            "ID": service_id,
            "Name": "adcm",
            "Tags": ["adcm", "backend", adcm_uuid],
            "Address": host,
            "Port": port,
            "Meta": meta,
            "Checks": [
                {
                    "HTTP": f"{adcm_url.rstrip('/')}/api/health/ready",
                    "Interval": self._settings.health_check_interval,
                    "Timeout": self._settings.health_check_timeout,
                    "DeregisterCriticalServiceAfter": self._settings.deregister_critical_service_after,
                }
            ],
        }

        if self._settings.datacenter:
            payload["Datacenter"] = self._settings.datacenter

        response = self._session.put(
            f"{self.base_url}/v1/agent/service/register",
            json=payload,
            timeout=10.0,
        )

        if not response.ok:
            raise ConsulError(
                f"Failed to register service in Consul: status={response.status_code} body={response.text!r}"
            )

        self._service_id = service_id
        logger.info("Registered ADCM in Consul: id=%s address=%s port=%d", service_id, host, port)

    def deregister_service(self) -> None:
        if not self._service_id:
            return

        try:
            response = self._session.put(
                f"{self.base_url}/v1/agent/service/deregister/{self._service_id}",
                timeout=10.0,
            )
            if response.ok:
                logger.info("Deregistered ADCM from Consul: id=%s", self._service_id)
            else:
                logger.warning(
                    "Failed to deregister from Consul: status=%d body=%s",
                    response.status_code,
                    response.text,
                )
        except Exception:  # noqa: BLE001
            logger.exception("Exception during Consul deregistration")
        finally:
            self._service_id = None


def build_consul_settings_from_env() -> ConsulSettings | None:
    consul_url = os.getenv("CONSUL_URL")
    if not consul_url:
        return None

    return ConsulSettings(
        url=consul_url,
        datacenter=os.getenv("CONSUL_DATACENTER"),
        acl_token=os.getenv("CONSUL_ACL_TOKEN"),
        cacert_file=os.getenv("CONSUL_CACERT_FILE"),
        client_cert_file=os.getenv("CONSUL_CLIENT_CERT_FILE"),
        client_key_file=os.getenv("CONSUL_CLIENT_KEY_FILE"),
        health_check_interval=os.getenv("CONSUL_HEALTH_CHECK_INTERVAL", "10s"),
        health_check_timeout=os.getenv("CONSUL_HEALTH_CHECK_TIMEOUT", "5s"),
        deregister_critical_service_after=os.getenv("CONSUL_DEREGISTER_CRITICAL_SERVICE_AFTER", "5m"),
    )


def setup_consul_service_registration() -> ConsulBackend | None:
    settings = build_consul_settings_from_env()
    if settings is None:
        return None

    default_adcm_url = os.getenv("DEFAULT_ADCM_URL")
    if not default_adcm_url:
        raise RuntimeError("DEFAULT_ADCM_URL is mandatory when Consul registration is enabled (CONSUL_URL is set).")

    backend = ConsulBackend.initialize(settings)
    backend.register_service(
        adcm_url=default_adcm_url,
        status_service_base_path=os.getenv("STATUS_SERVICE_BASE_PATH"),
    )

    _register_shutdown_handlers(backend)

    return backend


def _register_shutdown_handlers(backend: ConsulBackend) -> None:
    original_sigterm = signal.getsignal(signal.SIGTERM)
    original_sigint = signal.getsignal(signal.SIGINT)

    def _handle_shutdown(signum, frame):
        backend.deregister_service()

        original = original_sigterm if signum == signal.SIGTERM else original_sigint
        if callable(original):
            original(signum, frame)

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)
