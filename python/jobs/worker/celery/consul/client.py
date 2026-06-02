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
Thin HTTP client for the Consul KV endpoints used by the custom Celery
control/inspect transport.

Recycle TCP/TLS connections instead of opening a new one for every operation.

TLS and ACL options are picked up from environment/settings so that both
ACL (token) and TLS (CA file) based hardening are supported out of the box.
"""


from base64 import b64decode
from typing import Any
import json as jsonlib

from requests import Session
from requests.adapters import HTTPAdapter


class ConsulKVError(RuntimeError):
    """Raised for any unexpected Consul KV HTTP response."""


class ConsulKVClient:
    """
    Minimal Consul KV client with pooled HTTP connections.

    Only the endpoints required by the control/inspect transport are
    exposed: :meth:`put`, :meth:`get`, :meth:`list_keys`, :meth:`list_pairs`
    and :meth:`delete`.
    """

    _KV_PATH = "/v1/kv"

    def __init__(
        self,
        *,
        base_url: str,
        datacenter: str | None = None,
        token: str | None = None,
        verify: str | bool = True,
        timeout: float = 5.0,
        pool_size: int = 10,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._datacenter = datacenter
        self._timeout = timeout

        session = Session()
        if token:
            session.headers["X-Consul-Token"] = token
        session.verify = verify
        # Reuse the same pool of TCP connections across all kv requests.
        adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        self._session = session

    def put(self, key: str, value: Any) -> None:
        """Store ``value`` at ``key``. ``value`` is json-encoded automatically."""
        url = f"{self._base_url}{self._KV_PATH}/{_clean(key)}"
        response = self._session.put(
            url,
            params=self._query(),
            data=_encode_value(value),
            timeout=self._timeout,
        )
        if not response.ok or response.text.strip() != "true":
            raise ConsulKVError(
                f"Failed to PUT consul kv {key!r}: status={response.status_code} body={response.text!r}"
            )

    def get(self, key: str) -> Any | None:
        """Fetch the value at ``key`` or ``None`` if it does not exist."""
        url = f"{self._base_url}{self._KV_PATH}/{_clean(key)}"
        response = self._session.get(url, params=self._query(), timeout=self._timeout)
        if response.status_code == 404:
            return None
        if not response.ok:
            raise ConsulKVError(
                f"Failed to GET consul kv {key!r}: status={response.status_code} body={response.text!r}"
            )
        payload = response.json()
        if not payload:
            return None
        return _decode_value(payload[0].get("Value"))

    def list_keys(self, prefix: str) -> list[str]:
        """Return the full list of keys under ``prefix`` (recursive)."""
        url = f"{self._base_url}{self._KV_PATH}/{_clean(prefix)}"
        response = self._session.get(url, params=self._query(keys="true"), timeout=self._timeout)
        if response.status_code == 404:
            return []
        if not response.ok:
            raise ConsulKVError(
                f"Failed to LIST consul kv {prefix!r}: status={response.status_code} body={response.text!r}"
            )
        data = response.json()
        return list(data) if data else []

    def list_pairs(self, prefix: str) -> dict[str, Any]:
        """Return ``{key: value}`` for every key under ``prefix`` (recursive)."""
        url = f"{self._base_url}{self._KV_PATH}/{_clean(prefix)}"
        response = self._session.get(url, params=self._query(recurse="true"), timeout=self._timeout)
        if response.status_code == 404:
            return {}
        if not response.ok:
            raise ConsulKVError(
                f"Failed to RECURSE consul kv {prefix!r}: status={response.status_code} body={response.text!r}"
            )
        payload = response.json() or []
        return {entry["Key"]: _decode_value(entry.get("Value")) for entry in payload}

    def delete(self, key: str, *, recurse: bool = False) -> None:
        """Delete ``key`` (or everything under it when ``recurse`` is true)."""
        url = f"{self._base_url}{self._KV_PATH}/{_clean(key)}"
        params = self._query()
        if recurse:
            params["recurse"] = "true"
        response = self._session.delete(url, params=params, timeout=self._timeout)
        if not response.ok:
            raise ConsulKVError(
                f"Failed to DELETE consul kv {key!r}: status={response.status_code} body={response.text!r}"
            )

    def close(self) -> None:
        self._session.close()

    def _query(self, **extra: str) -> dict[str, str]:
        params: dict[str, str] = {}
        if self._datacenter:
            params["dc"] = self._datacenter
        params.update(extra)
        return params


def _encode_value(value: Any) -> bytes:
    return jsonlib.dumps(value, ensure_ascii=False, default=str).encode("utf-8")


def _decode_value(raw: str | None) -> Any | None:
    if raw is None:
        return None
    decoded = b64decode(raw).decode("utf-8")
    try:
        return jsonlib.loads(decoded)
    except jsonlib.JSONDecodeError:
        return decoded


def _clean(key: str) -> str:
    return key.lstrip("/")
