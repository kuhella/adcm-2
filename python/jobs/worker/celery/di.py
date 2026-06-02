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

from typing import Iterable

from celery import Celery
from celery.bootsteps import Step
from dishka import Provider, Scope, provide

from jobs.worker.celery.consul.bootstep import ConsulListenerStep
from jobs.worker.celery.consul.client import ConsulKVClient
from jobs.worker.celery.consul.control import ConsulControl
from jobs.worker.celery.custom import ADCMCelery, CustomWorkerStep
from jobs.worker.celery.settings import CelerySettings, EnvConsulSettings, EnvDBSettings, EnvWorkerSettings


class CeleryProvider(Provider):
    scope = Scope.APP

    @provide
    def di_providers(self) -> Iterable[Provider]:
        from application.di.containers import get_main_providers

        return get_main_providers()

    @provide
    def celery_settings(self) -> CelerySettings:
        # todo silent/customize errors?
        db = EnvDBSettings()  # pyright: ignore[reportCallIssue]
        connection_str = (
            f"postgresql+psycopg://{db.user}:{db.password.get_secret_value()}@{db.host}:{db.port}/{db.name}"
        )
        if db.options:
            options_str = "&".join(f"{key}={value}" for key, value in db.options.items())
            connection_str = f"{connection_str}?{options_str}"

        worker = EnvWorkerSettings()  # pyright: ignore[reportCallIssue]
        consul = EnvConsulSettings()  # pyright: ignore[reportCallIssue]

        return CelerySettings(
            db_url=connection_str,
            broker_url=f"sqla+{connection_str}",
            result_backend=f"db+{connection_str}",
            adcm_worker=worker,
            adcm_consul=consul,
        )

    @provide
    def consul_client(self, settings: CelerySettings) -> Iterable[ConsulKVClient]:
        consul_settings = settings.adcm_consul

        verify: str | bool = True
        if consul_settings.cacert_file:
            verify = consul_settings.cacert_file

        token = None
        if consul_settings.acl_token is not None:
            token = consul_settings.acl_token.get_secret_value()

        client = ConsulKVClient(
            base_url=str(consul_settings.url),
            datacenter=consul_settings.datacenter,
            token=token,
            verify=verify,
            timeout=consul_settings.http_timeout,
            pool_size=consul_settings.http_pool_size,
        )

        yield client

        client.close()

    @provide
    def celery(
        self, providers: Iterable[Provider], celery_settings: CelerySettings, consul_client: ConsulKVClient
    ) -> Celery:
        app = ADCMCelery(
            control=ConsulControl,
            adcm_di_providers=providers,
            adcm_settings=celery_settings,
            adcm_consul_client=consul_client,
        )

        app.autodiscover_tasks(packages=["jobs.worker"])

        worker_steps: tuple[type[Step], ...] = (CustomWorkerStep, ConsulListenerStep)

        for step in worker_steps:
            app.steps["worker"].add(step)  # pyright: ignore[reportOptionalSubscript]

        return app
