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


import adcm.init_django  # noqa: F401, isort:skip

from kombu.transport import TRANSPORT_ALIASES

from jobs.worker.celery.consul.discovery import ConsulWorkerDiscoveryStep
from jobs.worker.celery.utils import CustomCelery, CustomWorkerStep

# Register the pgnotify transport so that broker_url="pgnotify+postgresql://..."
# is resolved to our custom transport implementation.
TRANSPORT_ALIASES["pgnotify"] = "jobs.worker.celery.transport.transport:Transport"

app = CustomCelery("job-runner")
app.config_from_object("jobs.worker.celery.settings")
app.autodiscover_tasks(packages=["jobs.worker"])
app.steps["worker"].add(CustomWorkerStep)
app.steps["worker"].add(ConsulWorkerDiscoveryStep)
