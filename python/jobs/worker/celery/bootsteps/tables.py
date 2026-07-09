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

from celery import bootsteps
from celery.backends.database import SessionManager

from jobs.scheduler.logger import logger


class ResultBackendTablesStep(bootsteps.StartStopStep):
    """Celery's database result backend creates its tables lazily on first use, but the
    scheduler reads ``celery_taskmeta`` directly (``jobs.scheduler.utils``), so table
    creation must be deterministic"""

    def start(self, parent) -> None:
        session = SessionManager()
        engine = session.get_engine(parent.app.conf.db_url)
        session.prepare_models(engine)
        logger.info("Celery result backend tables are ensured to exist")

    def stop(self, parent) -> None:
        _ = parent
