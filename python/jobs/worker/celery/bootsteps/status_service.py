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

from typing import Any
from urllib.parse import urlsplit

from celery import bootsteps
from cm.legacy.status_api import set_external_status_service_url
from core.adcm import ADCMRepoI
from integrations.consul import ConsulBackend

from jobs.scheduler.logger import logger
from jobs.worker.celery.custom import read_adcm_uuid

ADCM_SERVICE_NAME = "adcm"
STATUS_SERVICE_URL_META = "status_service_url"


class StatusServiceUrlResolutionError(Exception):
    ...


def extract_status_service_url(entries: list[dict[str, Any]]) -> str | None:
    """Pick the ``status_service_url`` meta from the first ADCM discovery entry that has one."""
    for entry in entries:
        meta = (entry.get("Service") or {}).get("Meta") or {}
        url = meta.get(STATUS_SERVICE_URL_META)
        if url:
            return url

    return None


def build_status_service_url_from_adcm_url(adcm_url: str, status_base_path: str) -> str:
    parts = urlsplit(adcm_url)
    base_url = f"{parts.scheme}://{parts.netloc}"
    if not status_base_path:
        return base_url
    return f"{base_url.rstrip('/')}/{status_base_path.lstrip('/')}"


def resolve_external_status_service_url(
    *,
    consul_backend: ConsulBackend | None,
    adcm_uuid: str | None,
    default_adcm_url: str | None,
    status_base_path: str,
) -> str | None:
    if consul_backend is not None:
        url = _discover_status_service_url(backend=consul_backend, adcm_uuid=adcm_uuid)
        if url:
            return url

    if default_adcm_url:
        return build_status_service_url_from_adcm_url(default_adcm_url, status_base_path)

    return None


def _discover_status_service_url(*, backend: ConsulBackend, adcm_uuid: str | None) -> str | None:
    try:
        entries = backend.discover(ADCM_SERVICE_NAME, tag=adcm_uuid)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to discover ADCM in Consul")
        return None

    return extract_status_service_url(entries)


class StatusServiceUrlStep(bootsteps.StartStopStep):
    """Point ``status_api`` at the resolved external status service URL on worker start."""

    def start(self, parent) -> None:
        conf = parent.app.conf
        status_service_url = resolve_external_status_service_url(
            consul_backend=conf.consul,
            adcm_uuid=read_adcm_uuid(parent.app.di_container.get(ADCMRepoI)),
            default_adcm_url=conf.default_adcm_url,
            status_base_path=conf.status_service_base_path,
        )

        if not status_service_url:
            message = (
                "Could not resolve external status service url: "
                "no Consul discovery result and DEFAULT_ADCM_URL is not set. "
                "The worker cannot report status events without it, refusing to start."
            )
            raise StatusServiceUrlResolutionError(message)

        set_external_status_service_url(status_service_url)
        logger.info("Worker status events will be sent to %s", status_service_url)

    def stop(self, parent) -> None:
        _ = parent
