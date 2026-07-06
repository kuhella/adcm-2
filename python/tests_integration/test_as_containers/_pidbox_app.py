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

"""
Standalone Celery app on the pg LISTEN/NOTIFY transport, for the pidbox
regression test. Launched as ``celery -A ..._pidbox_app worker`` (a real
prefork worker → async event loop) and imported by the test to send controls.

The broker DSN comes from ``PIDBOX_TEST_SQLA_URL`` so the worker subprocess and
the test process point at the same PostgreSQL instance.
"""

import os

from celery import Celery
from jobs.worker.celery.pg.transport import make_broker_url

_SQLA = os.environ["PIDBOX_TEST_SQLA_URL"]

app = Celery("pg_pidbox_test", broker=make_broker_url(_SQLA), backend=f"db+{_SQLA}")
app.conf.update(broker_connection_retry_on_startup=True, worker_send_task_events=False)
