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


from core.legacy.job.runners import TaskRunner
from core.types import TaskID
import dishka

from jobs.worker.celery.custom import di_task
from jobs.worker.celery.worker import app


@app.task(bind=True, track_started=True)
@di_task
def run_task(*_, task_id: TaskID, task_runner: dishka.FromDishka[TaskRunner], **_kw) -> None:
    task_runner.run(task_id=task_id)
