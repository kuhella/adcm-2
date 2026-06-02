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
Celery ``Control`` / ``Inspect`` replacements that use Consul KV as the
command/response transport.

Control flow
------------

On the caller (Django / scheduler) side:

    >>> app.control.broadcast("ping")
    # 1. allocate a fresh ``command_id`` (uuid4)
    # 2. PUT command payload at
    #        CONSUL_KV_COMMAND_PREFIX/<command_id>
    # 3. poll responses under
    #        CONSUL_KV_RESPONSE_PREFIX/<command_id>/<worker.hostname>
    #    every ``CONSUL_KV_RESPONSE_POLL_INTERVAL`` seconds until either
    #    all alive workers have responded or the timeout elapsed.
    # 4. return per-worker results

"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from functools import cached_property
from time import monotonic, sleep
from typing import Annotated, Any, Iterable
from uuid import uuid4

from celery.app.control import Control, Inspect
from pydantic import BaseModel, Field

from jobs.worker.celery.consul.client import ConsulKVClient
from jobs.worker.celery.custom import ADCMCelery

DEFAULT_TIMEOUT = 1.0


@dataclass(frozen=True)
class Command(BaseModel):
    """A control/inspect command published to Consul KV.

    The worker bootstep reads instances of this dataclass from
    ``CONSUL_KV_COMMAND_PREFIX/<id>`` and executes them locally.
    """

    id: str
    method: str
    arguments: Annotated[dict[str, Any], Field(default_factory=dict)]
    destination: Annotated[list[str] | None, Field(default=None)]


class ConsulControl(Control):
    """Celery :class:`Control` replacement that broadcasts via Consul KV."""

    def __init__(self, app):
        if not isinstance(app, ADCMCelery):
            raise TypeError("This worker step relies on ADCM Celery implementation")

        super().__init__(app)
        self.publisher = ConsulCommandPublisher(
            client=app.consul_client,
            collect_interval=app.conf.adcm_consul.kv_response_poll_interval,
            command_prefix=app.conf.adcm_consul.kv_command_prefix,
            response_prefix=app.conf.adcm_consul.kv_response_prefix,
        )

    @cached_property
    def inspect(self):
        """Return a :class:`ConsulInspect` bound to this app."""
        return self.app.subclass_with_self(ConsulInspect, reverse="control.inspect")

    # Celery's Control exposes ``broadcast`` as the single entry point used
    # by revoke/ping/rate_limit/... We override it so that every control
    # command is routed through Consul instead of the Kombu mailbox.
    def broadcast(  # noqa: PLR0913
        self,
        command: str,
        arguments: dict[str, Any] | None = None,
        destination: Iterable[str] | None = None,
        connection=None,  # noqa: ARG002
        reply: bool = False,
        timeout: float | None = None,
        limit: int | None = None,
        callback=None,  # noqa: ARG002
        channel=None,  # noqa: ARG002
        pattern=None,  # noqa: ARG002
        matcher=None,  # noqa: ARG002
        **extra_kwargs,  # noqa: ARG002
    ):
        return self.publisher.send(
            method=command,
            arguments=arguments or {},
            destination=tuple(destination) if destination else None,
            reply=reply,
            timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
            limit=limit,
        )


class ConsulInspect(Inspect):
    """Inspect implementation that delegates to :class:`ConsulControl`."""

    def _request(self, command, **arguments):
        return self.app.control.broadcast(
            command,
            arguments=arguments,
            destination=self.destination,
            timeout=self.timeout if self.timeout is not None else DEFAULT_TIMEOUT,
            reply=True,
            limit=self.limit,
        )


@dataclass(slots=True)
class ConsulCommandPublisher:
    """
    Publishes a command to Consul KV and optionally collects per-worker
    responses.
    """

    client: ConsulKVClient
    collect_interval: float
    command_prefix: str
    response_prefix: str

    def send(
        self,
        *,
        method: str,
        arguments: dict[str, Any],
        destination: tuple[str, ...] | None,
        reply: bool,
        timeout: float,
        limit: int | None,
    ) -> list[dict[str, Any]] | None:
        command = Command(
            id=uuid4().hex,
            method=method,
            arguments=arguments,
            destination=destination,
        )

        command_key = with_prefix(self.command_prefix, command.id)
        self.client.put(command_key, command.model_dump(mode="json"))

        if not reply:
            return None

        try:
            return self._collect_responses(
                command_id=command.id,
                destination=destination,
                timeout=timeout,
                limit=limit,
            )
        finally:
            self._cleanup(command_id=command.id, command_key=command_key)

    def _collect_responses(
        self,
        *,
        command_id: str,
        destination: tuple[str, ...] | None,
        timeout: float,
        limit: int | None,
    ) -> list[dict[str, Any]]:
        response_prefix = with_prefix(self.response_prefix, command_id)
        expected = set(destination) if destination else None

        deadline = monotonic() + max(timeout, 0.0)
        seen: dict[str, Any] = {}

        while True:
            for full_key, value in self.client.list_pairs(response_prefix).items():
                hostname = full_key.rsplit("/", 1)[-1]
                seen[hostname] = value

            if expected is not None and expected.issubset(seen):
                break
            if limit is not None and len(seen) >= limit:
                break
            if monotonic() >= deadline:
                break

            sleep(self.collect_interval)

        return [{hostname: value} for hostname, value in seen.items()]

    def _cleanup(self, *, command_id: str, command_key: str) -> None:
        # Responses are removed after collection so that stale data does not
        # leak between calls; the command key is deleted only as a safety net
        # (the worker normally deletes it right after consuming).
        for key, recurse in ((with_prefix(self.response_prefix, command_id), True), (command_key, False)):
            with suppress(Exception):
                self.client.delete(key, recurse=recurse)


def with_prefix(prefix: str, *path: str) -> str:
    return f"{prefix}{'/'.join( path)}"
