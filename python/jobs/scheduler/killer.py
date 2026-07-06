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
from functools import partial
from typing import Any, Callable
import os
import time
import signal

from celery import Celery
from core.action import ExecutionStatus
from core.action.job import JobRepoI
from django.db.transaction import atomic
import dishka

from jobs.scheduler._types import JobShortInfo, TaskRunnerEnvironment, TaskShortInfo
from jobs.scheduler.logger import logger
from jobs.scheduler.repo import (
    get_planned_for_termination,
    lock_job_for_termination,
    lock_task_for_termination,
    retrieve_job,
    retrieve_task,
)
from jobs.scheduler.utils import UTC


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(slots=True)
class Clock:
    period: timedelta

    next_tick_after: datetime = datetime.min.replace(tzinfo=UTC)

    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] = utc_now

    def sleep_until_next_tick(self) -> None:
        now = self.now()

        until_next_tick = (self.next_tick_after - now).total_seconds()
        if until_next_tick > 0:
            self.sleep(until_next_tick)

        self.next_tick_after = now + self.period


def send_adcm_task_revoke_command(task: TaskShortInfo, container: dishka.Container):
    celery_app = container.get(Celery)
    celery_app.control.revoke(task_id=task.worker["worker_id"], terminate=True)


def send_adcm_job_revoke_command(job: JobShortInfo, container: dishka.Container):
    celery_app = container.get(Celery)
    celery_app.control.revoke(task_id=job.worker["worker_id"], terminate=True)


def run_killer_in_loop(container: dishka.Container) -> None:
    task_killer_registry: dict[TaskRunnerEnvironment, Callable[[TaskShortInfo], Any]] = {
        TaskRunnerEnvironment.LOCAL: lambda x: os.kill(int(x.worker["worker_id"]), signal.SIGTERM),
        TaskRunnerEnvironment.CELERY: partial(send_adcm_task_revoke_command, container=container),
    }

    job_killer_registry: dict[TaskRunnerEnvironment, Callable[[JobShortInfo], Any]] = {
        TaskRunnerEnvironment.LOCAL: lambda x: os.kill(int(x.worker["worker_id"]), signal.SIGTERM),
        TaskRunnerEnvironment.CELERY: partial(send_adcm_job_revoke_command, container=container),
    }

    logger.info("Job killer started (pid: %s)", os.getpid())

    from jobs.scheduler import settings

    repo: JobRepoI = container.get(JobRepoI)

    clock = Clock(period=timedelta(seconds=settings.JOB_TERMINATION_POLL_INTERVAL))

    while True:
        clock.sleep_until_next_tick()

        try:
            jobs_to_terminate, tasks_to_terminate = get_planned_for_termination()

            for job_id in jobs_to_terminate:
                job = None
                # Only DB work under the row lock; claim it and mark TERMINATING.
                with atomic(), lock_job_for_termination(job_id) as locked_job_id:
                    if locked_job_id:
                        job = retrieve_job(job_id=locked_job_id)
                        # todo write logs on update result, since killer is already working on termination
                        repo.change_job_status(id=locked_job_id, previous=job.status, new=ExecutionStatus.TERMINATING)
                # Sending the kill/revoke is I/O (broker publish or signal): do it
                # after the transaction commits so the row lock is not held across it.
                if job is not None:
                    job_killer_registry[job.worker["environment"]](job)

            for task_id in tasks_to_terminate:
                task = None
                with atomic(), lock_task_for_termination(task_id) as locked_task_id:
                    if locked_task_id:
                        task = retrieve_task(task_id=locked_task_id)
                        # todo write logs on update result, since killer is already working on termination
                        repo.change_task_status(
                            id=locked_task_id, previous=task.status, new=ExecutionStatus.TERMINATING
                        )
                if task is not None:
                    task_killer_registry[task.worker["environment"]](task)

        except Exception:  # noqa: BLE001
            logger.exception("Job killer iteration failed")
