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

import sys

from django.apps import AppConfig


class ApplicationConfig(AppConfig):
    name = "application"
    verbose_name = "application"

    def ready(self) -> None:
        from application.startup.consul import (  # noqa: PLC0415
            ensure_default_adcm_url_when_consul_configured,
            register_adcm_in_consul,
        )

        # FR3: fail fast for every entrypoint when the Consul/URL config is inconsistent.
        ensure_default_adcm_url_when_consul_configured()

        # FR1: register only when actually serving requests, not for management commands.
        if _is_serving_requests():
            register_adcm_in_consul()


def _is_serving_requests() -> bool:
    if "uwsgi" in sys.modules or "gunicorn" in sys.modules:
        return True

    return len(sys.argv) > 1 and sys.argv[1] == "runserver"
