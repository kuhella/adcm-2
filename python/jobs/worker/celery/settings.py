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
from typing import Annotated

from pydantic import AfterValidator, Field, SecretStr
from pydantic_core import Url
from pydantic_settings import BaseSettings, SettingsConfigDict


def _normalize_kv_prefix(prefix: str) -> str:
    """Normalize a KV prefix: strip leading slash, ensure trailing slash."""
    return f"{prefix.strip('/')}/"


class EnvDBSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="db_")

    user: str
    # prefix looks to be ignored when alias is used
    password: Annotated[SecretStr, Field(alias="db_pass")]
    name: str
    host: str
    port: str

    options: Annotated[dict, Field(default_factory=dict)]


class EnvWorkerSettings(BaseSettings):
    # seconds
    job_worker_celery_heartbeat_interval: Annotated[float, Field(default=5.0)]


class EnvConsulSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="consul_")

    # server connection
    url: Url
    datacenter: Annotated[str | None, Field(default=None)]
    cacert_file: Annotated[str | None, Field(default=None)]
    acl_token: Annotated[SecretStr | None, Field(default=None)]

    # requests
    http_timeout: Annotated[float, Field(default=5.0)]
    http_pool_size: Annotated[int, Field(default=10)]

    # client (kv)
    kv_command_prefix: Annotated[str, Field(default="celery/command"), AfterValidator(_normalize_kv_prefix)]
    kv_response_prefix: Annotated[str, Field(default="celery/response"), AfterValidator(_normalize_kv_prefix)]
    kv_command_poll_interval: Annotated[float, Field(default=1.0)]
    kv_response_poll_interval: Annotated[float, Field(default=0.5)]


@dataclass(slots=True)
class CelerySettings:
    # Connections
    db_url: str
    broker_url: str
    result_backend: str

    # ADCM specifics
    adcm_worker: EnvWorkerSettings
    adcm_consul: EnvConsulSettings

    # Various
    result_extended: bool = True
    broker_connection_retry_on_startup: bool = True
    timezone: str = "UTC"
