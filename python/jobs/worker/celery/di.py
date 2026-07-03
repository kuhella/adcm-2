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
from dishka import Provider, Scope, provide

# CustomWorkerStep is imported for the (currently disabled) worker-step registration below.
from jobs.worker.celery.custom import ADCMCelery, CustomWorkerStep  # noqa: F401
from jobs.worker.celery.pg.transport import make_broker_url
from jobs.worker.celery.settings import CelerySettings, EnvDBSettings, EnvWorkerSettings


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

        return CelerySettings(
            db_url=connection_str,
            # PostgreSQL LISTEN/NOTIFY broker; control commands ride native
            # Celery pidbox over its fanout (see jobs.worker.celery.pg).
            broker_url=make_broker_url(connection_str),
            result_backend=f"db+{connection_str}",
            adcm_worker=worker,
        )

    @provide
    def celery(self, providers: Iterable[Provider], celery_settings: CelerySettings) -> Celery:
        app = ADCMCelery(
            adcm_di_providers=providers,
            adcm_settings=celery_settings,
        )

        app.autodiscover_tasks(packages=["jobs.worker"])
        # worker-step registration currently disabled:
        # app.steps["worker"].add(CustomWorkerStep)

        return app
