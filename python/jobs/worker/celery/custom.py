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

from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import wraps
from typing import Iterable

from celery import Celery, bootsteps
from celery.app.control import Control
from celery.worker import WorkController
from core.legacy.job.runners import JobFilterPredicate, always_true
from dishka.integrations.base import wrap_injection
import dishka

from jobs.scheduler._types import UTC, CeleryTaskState
from jobs.scheduler.logger import logger
from jobs.worker.celery import repo
from jobs.worker.celery.consul.client import ConsulKVClient
from jobs.worker.celery.models import DBTables
from jobs.worker.celery.settings import CelerySettings


@dataclass(slots=True)
class RepoPingInspector:
    interval: timedelta

    def ping(self) -> set[str]:
        """
        Returns set of alive (there are timestamps not further than `2 * heartbeat_interval` seconds) celery workers.
        """
        threshold = datetime.now(tz=UTC) - self.interval

        return repo.retrieve_alive_workers(threshold=threshold)


class ADCMCelery(Celery):
    def __init__(
        self,
        *args,
        # it should be a class, idk why it works this way
        control: type[Control] | None = None,
        adcm_di_providers: Iterable[dishka.Provider],
        adcm_settings: CelerySettings,
        adcm_consul_client: ConsulKVClient,
        **kwargs,
    ):
        super().__init__(*args, control=control, **kwargs)

        # risky, but should be safe for now
        self.config_from_object(adcm_settings)

        self.di_container = dishka.make_container(*adcm_di_providers, context={JobFilterPredicate: always_true})
        self.ping_inspector = RepoPingInspector(
            timedelta(seconds=2 * self.conf.adcm_worker.job_worker_celery_heartbeat_interval)
        )
        self.consul_client = adcm_consul_client

    def ping(self) -> set[str]:
        return self.ping_inspector.ping()


class CustomWorkerStep(bootsteps.StartStopStep):
    """
    Modifies worker on start behaviour:
      - creates db tables if not exists
      - sets final status for stale tasks
      - starts custom db-driven heartbeat
    """

    requires = {"celery.worker.components:Timer"}

    def __init__(self, *args, **kwargs):
        parent: WorkController = args[0]
        if not isinstance(parent.app, ADCMCelery):
            raise TypeError("This worker step relies on ADCM Celery implementation")

        super().__init__(*args, **kwargs)

        # those assignments may not be required, keeping for now
        self.hostname = parent.hostname
        self.worker_heartbeat_interval = parent.app.conf.adcm_worker.job_worker_celery_heartbeat_interval
        self.db_url = parent.app.conf.db_url

    def start(self, parent):
        repo.init_tables(self.db_url)
        self._update_taskmeta_table()
        self._start_heartbeat(work_controller=parent)

    def _update_taskmeta_table(self) -> None:
        final_status = CeleryTaskState.FAILURE

        to_update = repo.retrieve_running_worker_tasks(hostname=self.hostname)
        if to_update:
            repo.update_worker_tasks(ids=to_update, status=final_status)

        logger.debug(
            f"Table {DBTables.taskmeta} updated: ({len(to_update)}) rows affected. "
            f"Set {final_status} status to tasks of {self.hostname} worker."
        )

    def _start_heartbeat(self, work_controller) -> None:
        work_controller.timer.call_repeatedly(
            secs=self.worker_heartbeat_interval,
            fun=repo.write_heartbeat,
            args=(self.hostname,),
        )

        logger.debug(f"DB heartbeat started at {self.hostname} worker.")


# kept DI function in here, because they are deeply dependant on `ADCMCelery` structure


def container_from_argument(*args):
    return args[1]["adcm_di_container"]


def di_task(func):
    func_with_injection = wrap_injection(func=func, is_async=False, container_getter=container_from_argument)

    @wraps(func)
    def enter_request_scope(*args, **kwargs):
        with args[0].app.di_container(scope=dishka.Scope.REQUEST) as container:
            return func_with_injection(*args, adcm_di_container=container, **kwargs)

    return enter_request_scope
