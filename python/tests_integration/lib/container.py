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

from testcontainers.core.container import DockerContainer
from testcontainers.postgres import PostgresContainer


def container_ip(container: DockerContainer) -> str:
    """Return a container's docker-network IP address.

    The top-level ``NetworkSettings.IPAddress`` is only populated for the
    default ``bridge`` network; on setups where the container is attached to a
    user-defined network (e.g. Docker Desktop on macOS) it is empty and the IP
    lives under ``NetworkSettings.Networks.<name>.IPAddress``. Fall back to that.
    """
    c = container.get_wrapped_container()
    c.reload()
    net = c.attrs["NetworkSettings"]

    ip = net.get("IPAddress")
    if ip:
        return ip

    for network in (net.get("Networks") or {}).values():
        ip = network.get("IPAddress")
        if ip:
            return ip

    raise RuntimeError(f"could not determine container IP address from NetworkSettings: {net}")


def db_env_from_container(postgres: PostgresContainer) -> dict[str, str]:
    return {"DB_HOST": container_ip(postgres), "DB_PORT": "5432"}
