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

from faker import Faker
from testcontainers.core.generic import DockerContainer
from testcontainers.core.wait_strategies import CompositeWaitStrategy, LogMessageWaitStrategy
from testcontainers.postgres import PostgresContainer
import pytest
import psycopg
import requests

from tests_integration.constants import ADCM_DATA_VOLUME, INTEGRATION_BUNDLES, POSTGRESQL_MIN_IMAGE
from tests_integration.lib.bundles import BundlePacker, SimpleBundlePacker
from tests_integration.lib.celery import WORKER_ID_LOGS_REGEX, celery_command
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


@pytest.fixture(scope="module")
def postgres() -> Generator[PostgresContainer, None, None]:
    with PostgresContainer(POSTGRESQL_MIN_IMAGE) as pg:
        yield pg


@pytest.fixture(scope="session")
def consul() -> Generator[DockerContainer, None, None]:
    container = DockerContainer("hashicorp/consul:2.0", ports=[8500]).waiting_for(
        LogMessageWaitStrategy("Log data will now stream in as it occurs")
    )

    with container as consul:
        yield consul


@pytest.fixture(scope="session")
def consul_env(consul: DockerContainer) -> dict:
    c = consul.get_wrapped_container()
    c.reload()
    ip = c.attrs["NetworkSettings"]["IPAddress"]
    port = 8500
    return {"CONSUL_URL": f"http://{ip}:{port}"}


@pytest.fixture(scope="module")
def database_env(postgres: PostgresContainer) -> dict:
    return db_env_from_container(postgres) | {
        "DB_USER": postgres.username,
        "DB_PASS": postgres.password,
        "DB_NAME": postgres.dbname,
    }


@pytest.fixture(scope="module")
def pg_conn(database_env: dict, postgres: PostgresContainer) -> Generator[psycopg.Connection, None, None]:
    with psycopg.connect(
        host=postgres.get_container_host_ip(),
        port=postgres.get_exposed_port(5432),
        user=database_env["DB_USER"],
        password=database_env["DB_PASS"],
        dbname=database_env["DB_NAME"],
    ) as conn:
        yield conn


# ADCM related


@pytest.fixture(scope="session")
def scheduler_celery_env() -> dict:
    return {"DEFAULT_JOB_EXECUTION_ENVIRONMENT": "celery", "FEATURE_JOB_SCHEDULER": "new"}


@pytest.fixture(scope="module")
def adcm_main_env(database_env: dict) -> dict:
    return database_env


@pytest.fixture(scope="module")
def adcm_main_container(adcm_image: str, adcm_main_env: dict) -> DockerContainer:
    return (
        DockerContainer(adcm_image, env=adcm_main_env, volumes=[ADCM_DATA_VOLUME], ports=[8000])
        .waiting_for(
            CompositeWaitStrategy(
                LogMessageWaitStrategy("Run main wsgi application"), LogMessageWaitStrategy("Run scheduler")
            )
        )
        .with_envs(LOG_LEVEL="INFO")
    )


@pytest.fixture(scope="module")
def adcm_main(adcm_main_container: DockerContainer) -> Generator[DockerContainer, None, None]:
    with adcm_main_container as container:
        yield container


@pytest.fixture(scope="module")
def adcm_worker_env(adcm_main_env: dict) -> dict:
    return adcm_main_env


@pytest.fixture(scope="module")
def adcm_worker_container(adcm_image: str, adcm_worker_env: dict) -> DockerContainer:
    return (
        DockerContainer(
            adcm_image,
            env=adcm_worker_env,
            volumes=[ADCM_DATA_VOLUME],
            command=celery_command("worker -l INFO"),
        )
        .waiting_for(LogMessageWaitStrategy(WORKER_ID_LOGS_REGEX))
        .with_envs(LOG_LEVEL="INFO")
    )


@pytest.fixture(scope="module")
def adcm_worker(adcm_main, adcm_worker_container: DockerContainer) -> Generator[DockerContainer, None, None]:
    with adcm_worker_container as container:
        yield container


@pytest.fixture(scope="module")
def client(faker: Faker, adcm_main: DockerContainer) -> Generator[YAAClient, None, None]:
    with requests.Session() as session:
        api_root = f"http://{adcm_main.get_container_host_ip()}:{adcm_main.get_exposed_port(8000)}/api/v2"
        client = YAAClient(session=session, api_root=api_root, faker=faker)
        client.login_as_admin()
        yield client


# Bundles


@pytest.fixture(scope="module")
def various_actions_bundle(client: YAAClient, bundle_packer: BundlePacker) -> dict:
    packed_bundle = bundle_packer.pack_from_dir(INTEGRATION_BUNDLES / "various_actions")
    return client.upload_bundle(packed_bundle)


@pytest.fixture(scope="module")
def provider_bundle(client: YAAClient, bundle_packer: BundlePacker) -> dict:
    packed_bundle = bundle_packer.pack_from_dir(INTEGRATION_BUNDLES / "provider")
    return client.upload_bundle(packed_bundle)
