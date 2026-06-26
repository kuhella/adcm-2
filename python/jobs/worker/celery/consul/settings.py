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
Configuration for the Consul KV storage backend used by Celery's custom
control/inspect commands.

All values are resolved from environment variables with sensible defaults.
"""

from __future__ import annotations

import os
import re

_DURATION_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "ms": 0.001}
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")


def _parse_duration_seconds(value: str) -> float:
    """Parse a Consul/Go duration string (e.g. ``"30s"``, ``"1m30s"``) into seconds.

    Falls back to ``0.0`` for values that cannot be parsed so callers can apply
    their own minimum.
    """
    matches = _DURATION_RE.findall(value.strip())
    if not matches:
        return 0.0
    return sum(float(amount) * _DURATION_UNITS[unit] for amount, unit in matches)


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


def _getint(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as err:
        raise RuntimeError(f"Environment variable {name!r} must be an integer, got {raw!r}") from err


# Consul server connection. CONSUL_URL / CONSUL_DATACENTER / CONSUL_CACERT_FILE
CONSUL_URL: str | None = _getenv("CONSUL_URL")
CONSUL_DATACENTER: str | None = _getenv("CONSUL_DATACENTER")
CONSUL_CACERT_FILE: str | None = _getenv("CONSUL_CACERT_FILE")
CONSUL_CLIENT_CERT_FILE: str | None = _getenv("CONSUL_CLIENT_CERT_FILE")
CONSUL_CLIENT_KEY_FILE: str | None = _getenv("CONSUL_CLIENT_KEY_FILE")
CONSUL_ACL_TOKEN: str | None = _getenv("CONSUL_ACL_TOKEN")
CONSUL_HTTP_TIMEOUT: float = _getfloat("CONSUL_HTTP_TIMEOUT", 5.0)
# Maximum number of HTTP connections kept alive inside the shared session pool.
CONSUL_HTTP_POOL_SIZE: int = _getint("CONSUL_HTTP_POOL_SIZE", 10)

# Worker service registration (FR1).
# The worker registers itself as a Consul service named CONSUL_SERVICE_NAME with
# a TTL health check that it refreshes every CONSUL_HEALTH_CHECK_TTL_REFRESH_INTERVAL
# seconds. Consul deregisters the service automatically once the check has been
# critical for CONSUL_DEREGISTER_CRITICAL_SERVICE_AFTER.
CONSUL_SERVICE_NAME: str = _getenv("CONSUL_SERVICE_NAME", "celery") or "celery"
CONSUL_HEALTH_CHECK_TTL: str = _getenv("CONSUL_HEALTH_CHECK_TTL", "30s") or "30s"
CONSUL_DEREGISTER_CRITICAL_SERVICE_AFTER: str = _getenv("CONSUL_DEREGISTER_CRITICAL_SERVICE_AFTER", "5m") or "5m"
# How often the worker reports the TTL check as passing. Defaults to ~1/3 of the
# TTL so a couple of missed refreshes still keep the service healthy.
CONSUL_HEALTH_CHECK_TTL_REFRESH_INTERVAL: float = _getfloat(
    "CONSUL_HEALTH_CHECK_TTL_REFRESH_INTERVAL", max(_parse_duration_seconds(CONSUL_HEALTH_CHECK_TTL) / 3, 1.0)
)

# KV paths.
CONSUL_KV_COMMAND_PREFIX: str | None = _getenv("CONSUL_KV_COMMAND_PREFIX", "celery/command")
CONSUL_KV_RESPONSE_PREFIX: str | None = _getenv("CONSUL_KV_RESPONSE_PREFIX", "celery/response")

# Polling intervals (seconds).
# - Command: worker polls `CONSUL_KV_COMMAND_PREFIX/*` to pick up new commands.
# - Response: control/inspect client polls `CONSUL_KV_RESPONSE_PREFIX/<cmd_id>/*`
#   to collect per-worker responses until the timeout is reached.
CONSUL_KV_COMMAND_POLL_INTERVAL: float = _getfloat("CONSUL_KV_COMMAND_POLL_INTERVAL", 1.0)
CONSUL_KV_RESPONSE_POLL_INTERVAL: float = _getfloat("CONSUL_KV_RESPONSE_POLL_INTERVAL", 0.5)


def is_enabled() -> bool:
    """Return True if all mandatory settings for Consul-based control are set."""
    return bool(CONSUL_URL and CONSUL_KV_COMMAND_PREFIX and CONSUL_KV_RESPONSE_PREFIX)


def is_consul_configured() -> bool:
    """Return True if Consul is configured (``CONSUL_URL`` is set).

    Gates worker service registration (FR1) and worker discovery (FR2), both of
    which only require a reachable Consul agent.
    """
    return bool(CONSUL_URL)


def normalize_prefix(prefix: str) -> str:
    """Normalize a KV prefix: strip leading slash, ensure trailing slash."""
    return prefix.lstrip("/").rstrip("/") + "/"
