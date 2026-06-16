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

import signal
import logging

from cm.models import ADCM
from django.conf import settings
from integrations.consul import ConsulBackend, ConsulSettings, get_container_id

logger = logging.getLogger("adcm")


def is_consul_enabled() -> bool:
    return bool(settings.CONSUL_URL)


def register_in_consul() -> ConsulBackend | None:
    if not is_consul_enabled():
        return None

    default_adcm_url = settings.DEFAULT_ADCM_URL
    if not default_adcm_url:
        raise RuntimeError("DEFAULT_ADCM_URL is mandatory when Consul registration is enabled (CONSUL_URL is set).")

    consul_settings = ConsulSettings(
        url=settings.CONSUL_URL,
        datacenter=settings.CONSUL_DATACENTER,
        acl_token=settings.CONSUL_ACL_TOKEN,
        cacert_file=settings.CONSUL_CACERT_FILE,
        client_cert_file=settings.CONSUL_CLIENT_CERT_FILE,
        client_key_file=settings.CONSUL_CLIENT_KEY_FILE,
        health_check_interval=settings.CONSUL_HEALTH_CHECK_INTERVAL,
        health_check_timeout=settings.CONSUL_HEALTH_CHECK_TIMEOUT,
        deregister_critical_service_after=settings.CONSUL_DEREGISTER_CRITICAL_SERVICE_AFTER,
    )

    backend = ConsulBackend.initialize(consul_settings)

    container_id = get_container_id()
    service_id = f"adcm@{container_id}"

    adcm_uuid = str(ADCM.objects.values_list("uuid", flat=True).get())

    status_service_url = default_adcm_url.rstrip("/") + settings.STATUS_SERVICE_BASE_PATH

    backend.register_service(
        service_id=service_id,
        adcm_url=default_adcm_url,
        adcm_uuid=adcm_uuid,
        status_service_url=status_service_url,
    )

    _register_shutdown_handlers(backend)

    logger.info("Consul service registration completed successfully")
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
