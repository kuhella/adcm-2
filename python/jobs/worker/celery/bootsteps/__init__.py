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

"""Worker bootsteps: startup/shutdown hooks wired into the Celery worker blueprint."""

from jobs.worker.celery.bootsteps.consul import ConsulRegistrationStep, build_worker_registration, ttl_pass_interval
from jobs.worker.celery.bootsteps.status_service import StatusServiceUrlStep
from jobs.worker.celery.bootsteps.tables import ResultBackendTablesStep

__all__ = [
    "ConsulRegistrationStep",
    "ResultBackendTablesStep",
    "StatusServiceUrlStep",
    "build_worker_registration",
    "ttl_pass_interval",
]
