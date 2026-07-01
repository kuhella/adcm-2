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
Configuration for Consul integration (worker discovery).

All values are resolved from environment variables with sensible defaults.
"""

from __future__ import annotations

import os


def _getenv(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _getfloat(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as err:
        raise RuntimeError(f"Environment variable {name!r} must be a number, got {raw!r}") from err


# Consul server connection.
CONSUL_URL: str | None = _getenv("CONSUL_URL")
CONSUL_DATACENTER: str | None = _getenv("CONSUL_DATACENTER")
CONSUL_CACERT_FILE: str | None = _getenv("CONSUL_CACERT_FILE")
CONSUL_ACL_TOKEN: str | None = _getenv("CONSUL_ACL_TOKEN")
CONSUL_HTTP_TIMEOUT: float = _getfloat("CONSUL_HTTP_TIMEOUT", 5.0)
