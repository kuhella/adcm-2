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

import json

from testcontainers.core.generic import DockerContainer
import pytest

from tests_integration.lib.celery import celery_command, extract_worker_id_from_container
from tests_integration.test_as_containers.cases import Smoke


@pytest.fixture(scope="module")
def adcm_main_env(database_env: dict, scheduler_celery_env: dict, consul_env: dict) -> dict:
    return database_env | consul_env | scheduler_celery_env


class TestSmoke(Smoke):
    pytestmark = [pytest.mark.usefixtures("adcm_worker")]


@pytest.mark.usefixtures("consul")
def test_broadcast_ping(adcm_main: DockerContainer, adcm_worker: DockerContainer):
    worker_id = extract_worker_id_from_container(adcm_worker)
    result = adcm_main.exec(celery_command("inspect ping --json"))
    assert result.exit_code == 0
    response = json.loads(result.output.decode())
    assert response == [{worker_id: {"ok": "pong"}}]
