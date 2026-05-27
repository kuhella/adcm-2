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

from testcontainers.postgres import PostgresContainer


def db_env_from_container(postgres: PostgresContainer) -> dict[str, str]:
    c = postgres.get_wrapped_container()
    c.reload()
    db_ip = c.attrs["NetworkSettings"]["IPAddress"]
    port = "5432"
    return {"DB_HOST": db_ip, "DB_PORT": port}
