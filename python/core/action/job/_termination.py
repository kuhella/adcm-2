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

from core.result import Fail, Success
from core.types import JobID, TaskID


class TerminationSignaller(Protocol):
    def signal_termination_for_task(self, task_id: TaskID) -> Success[None] | Fail[str]:
        ...

    def signal_termination_for_job(self, job_id: JobID) -> Success[None] | Fail[str]:
        ...


class DirectOSTerminationSignaller(TerminationSignaller):
    ...


@dataclass(slots=True)
class IndirectRepoTerminationSignaller(TerminationSignaller):
    ...
