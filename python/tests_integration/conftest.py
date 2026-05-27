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

from pathlib import Path
from typing import Generator
import re

from faker import Faker
from testcontainers.core.generic import DockerContainer
from testcontainers.core.wait_strategies import CompositeWaitStrategy, LogMessageWaitStrategy
from testcontainers.postgres import PostgresContainer
import pytest
import requests

from tests_integration.constants import ADCM_DATA_VOLUME, POSTGRESQL_MIN_IMAGE
from tests_integration.lib.bundles import BundlePacker, SimpleBundlePacker
from tests_integration.lib.client import YAAClient
from tests_integration.lib.container import db_env_from_container


def pytest_addoption(parser) -> None:
    parser.addoption("--adcm-image", help="fully specified ADCM image to use")


# General Purpose


@pytest.fixture(scope="session")
def bundle_packer(tmpdir_factory: pytest.TempdirFactory) -> BundlePacker:
    target_dir = tmpdir_factory.mktemp("bundles")
    return SimpleBundlePacker(target_dir=Path(target_dir))


@pytest.fixture(scope="session")
def faker() -> Faker:
    return Faker()


@pytest.fixture(scope="session")
def adcm_image(request) -> str:
    return request.config.getoption("--adcm-image")


# Non-ADCM environment


@pytest.fixture(scope="session")
def postgres() -> Generator[PostgresContainer, None, None]:
    with PostgresContainer(POSTGRESQL_MIN_IMAGE) as pg:
        yield pg


# ADCM environment


@pytest.fixture(scope="session")
def database_env(postgres: PostgresContainer) -> dict:
    return db_env_from_container(postgres) | {
        "DB_USER": postgres.username,
        "DB_PASS": postgres.password,
        "DB_NAME": postgres.dbname,
    }


@pytest.fixture(scope="session")
def adcm(
    adcm_image: str,
    database_env: dict,
) -> Generator[DockerContainer, None, None]:
    extra_env = {"DEFAULT_JOB_EXECUTION_ENVIRONMENT": "celery"}
    adcm_env = database_env | extra_env

    container = DockerContainer(adcm_image, env=adcm_env, volumes=[ADCM_DATA_VOLUME], ports=[8000]).waiting_for(
        CompositeWaitStrategy(
            LogMessageWaitStrategy("Run main wsgi application"), LogMessageWaitStrategy("Run scheduler")
        )
    )
    with container as adcm:
        yield adcm


@pytest.fixture(scope="session")
def adcm_worker_celery(
    adcm_image: str,
    database_env: dict,
) -> Generator[DockerContainer, None, None]:
    container = DockerContainer(
        adcm_image,
        env=database_env,
        volumes=[ADCM_DATA_VOLUME],
        command="celery --workdir /adcm/python -A jobs.worker.celery.worker worker -l INFO",
    ).waiting_for(LogMessageWaitStrategy(re.compile(r"celery@[A-z0-9]+ ready\.")))

    with container as worker:
        yield worker


@pytest.fixture(scope="function")
def client(faker: Faker, adcm: DockerContainer) -> Generator[YAAClient, None, None]:
    with requests.Session() as session:
        api_root = f"http://{adcm.get_container_host_ip()}:{adcm.get_exposed_port(8000)}/api/v2"
        client = YAAClient(session=session, api_root=api_root, faker=faker)
        client.login_as_admin()
        yield client
