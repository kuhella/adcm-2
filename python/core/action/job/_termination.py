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
from typing import Protocol
import os
import signal

from core.action import ExecutionStatus, Job, Task
from core.action.job import JobRepoI, JobUpdateDTO, TaskUpdateDTO
from core.result import Fail, Success


class TerminationSignaller(Protocol):
    def signal_termination_for_task(self, task: Task) -> Success[None] | Fail[str]:
        ...

    def signal_termination_for_job(self, job: Job) -> Success[None] | Fail[str]:
        ...


@dataclass(slots=True)
class DirectOSTerminationSignaller(TerminationSignaller):
    def signal_termination_for_task(self, task: Task) -> Success[None] | Fail[str]:
        return self.terminate_local_process(task.execution_env.pid)

    def signal_termination_for_job(self, job: Job) -> Success[None] | Fail[str]:
        return self.terminate_local_process(job.execution_env.pid)

    def terminate_local_process(self, pid: int) -> Success[None] | Fail[str]:
        if pid == 0:
            return Fail("termination is too early, try to execute later")

        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as e:
            return Fail(f"failed to terminate process: {e}")

        return Success(None)


@dataclass(slots=True)
class IndirectRepoTerminationSignaller(TerminationSignaller):
    repo: JobRepoI

    def signal_termination_for_task(self, task: Task) -> Success[None] | Fail[str]:
        self.repo.update_task(id=task.id, data=TaskUpdateDTO(status=ExecutionStatus.TERMINATING))
        return Success(None)

    def signal_termination_for_job(self, job: Job) -> Success[None] | Fail[str]:
        self.repo.update_job(id=job.id, data=JobUpdateDTO(status=ExecutionStatus.TERMINATING))
        return Success(None)
