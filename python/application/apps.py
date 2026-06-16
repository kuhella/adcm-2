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

import os
import sys
import logging

from django.apps import AppConfig

logger = logging.getLogger("adcm")


class ApplicationConfig(AppConfig):
    name = "application"

    def ready(self):
        if not self._is_server_process():
            return

        self._setup_consul()

    @staticmethod
    def _is_server_process() -> bool:
        if any(cmd in sys.argv for cmd in ("runserver", "uwsgi")):
            return True

        if "uwsgi" in sys.modules:
            return True

        return "RUN_MAIN" in os.environ

    @staticmethod
    def _setup_consul() -> None:
        from application.startup.consul import register_in_consul  # noqa: PLC0415

        try:
            register_in_consul()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to register ADCM in Consul")
