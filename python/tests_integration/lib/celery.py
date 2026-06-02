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

import re

from testcontainers.core.generic import DockerContainer

WORKER_ID_LOGS_REGEX = re.compile(r"(celery@[A-z0-9]+) ready\.")


def celery_command(command: str) -> str:
    return f"celery --workdir /adcm/python -A jobs.worker.celery.worker {command}"


def extract_worker_id(logs: str) -> str:
    matched = WORKER_ID_LOGS_REGEX.search(logs)
    if not matched:
        message = f"Failed to find worker id with {WORKER_ID_LOGS_REGEX} in logs:\n{logs}"
        raise ValueError(message)

    return matched.groups()[0]


def extract_worker_id_from_container(container: DockerContainer) -> str:
    stderr = container.get_logs()[1].decode()
    return extract_worker_id(stderr)
