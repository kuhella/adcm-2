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
``ConsulListenerStep`` - worker :class:`~celery.bootsteps.StartStopStep` that
polls Consul KV for control/inspect commands targeted at the current worker,
executes them locally via Celery's ``Panel`` registry, writes the result
back to the response KV path and finally removes the original command.
"""

from __future__ import annotations

from typing import Any, Iterable
import logging

from celery import bootsteps
from celery.utils.collections import AttributeDict
from celery.utils.functional import pass1
from celery.worker import control as worker_control
from pydantic import ValidationError

from jobs.scheduler.logger import logger
from jobs.worker.celery.consul.client import ConsulKVClient
from jobs.worker.celery.consul.control import Command, with_prefix
from jobs.worker.celery.settings import EnvConsulSettings


class ConsulListenerStep(bootsteps.StartStopStep):
    """
    Periodically consume commands from
    ``CONSUL_KV_COMMAND_PREFIX/<command_id>``.

    For every command directed at the current worker (either unscoped or
    explicitly listing ``hostname`` in ``destination``) the handler registered
    in :mod:`celery.worker.control`'s ``Panel`` is invoked; the return value
    is stored at
    ``CONSUL_KV_RESPONSE_PREFIX/<command_id>/<worker.hostname>`` and the
    original command key is removed so that it is not re-executed by another
    worker in the pool.
    """

    requires = {"celery.worker.components:Timer"}

    def __init__(self, parent, *args, **kwargs) -> None:
        super().__init__(parent, *args, **kwargs)
        self.hostname = parent.hostname
        self._tref = None

    def start(self, parent) -> None:
        consul_settings: EnvConsulSettings = parent.app.conf.adcm_consul

        interval = consul_settings.kv_command_poll_interval
        consumer = ConsulCommandConsumer(app=parent.app, hostname=self.hostname, client=parent.app.consul_client)

        self._tref = parent.timer.call_repeatedly(
            secs=interval,
            fun=consumer.poll_once,
        )
        logger.info(
            f"Consul control listener started at {self.hostname} "
            f"(interval={interval}s, prefix={consul_settings.kv_command_prefix})"
        )

    def stop(self, parent) -> None:
        _ = parent
        if self._tref is not None:
            self._tref.cancel()
            self._tref = None


class ConsulCommandConsumer:
    """
    Stateful helper used by :class:`ConsulListenerStep` to execute one
    poll iteration.
    """

    def __init__(self, *, app, hostname: str, client: ConsulKVClient) -> None:
        self._app = app
        self._hostname = hostname
        self._client = client
        self._panel_state = _build_panel_state(app=app, hostname=hostname)

        self._command_prefix = app.conf.adcm_consul.kv_command_prefix
        self._response_prefix = app.conf.adcm_consul.kv_response_prefix

    def poll_once(self) -> None:
        try:
            pairs = self._client.list_pairs(self._command_prefix)
        except Exception:  # noqa: BLE001
            logger.error("Failed to list Consul control commands")
            return

        for key, payload in pairs.items():
            self._handle_one(key=key, payload=payload)

    def _handle_one(self, *, key: str, payload: Any) -> None:
        try:
            command = Command.model_validate(payload)
        except ValidationError:
            logger.exception(f"Skipping unparsable Consul control command at {key!r}")
            self._safe_delete(key)
            return

        if not self._matches_destination(command.destination):
            # command is for other workers; leave it alone
            return

        result = self._execute(command)
        self._publish_response(command_id=command.id, result=result)
        self._safe_delete(key)

    def _matches_destination(self, destination: Iterable[str] | None) -> bool:
        if not destination:
            return True
        return self._hostname in destination

    def _execute(self, command: Command) -> dict[str, Any]:
        if command.method == "revoke":
            logging.warning("revoke called on %s", command.arguments)
        handler = worker_control.Panel.data.get(command.method)
        if handler is None:
            return {"error": f"No such control command: {command.method!r}"}

        try:
            return handler(self._panel_state, **command.arguments)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"Consul control command {command.method!r} failed")
            return {"error": repr(exc)}

    def _publish_response(self, *, command_id: str, result: dict[str, Any]) -> None:
        key = with_prefix(self._response_prefix, command_id, self._hostname)
        try:
            self._client.put(key, result)
        except Exception:  # noqa: BLE001
            logger.error(f"Failed to publish Consul control response for command {command_id!r}")

    def _safe_delete(self, key: str) -> None:
        try:
            self._client.delete(key)
        except Exception:  # noqa: BLE001
            logger.error(f"Failed to delete processed Consul control command {key!r}")


def _build_panel_state(*, app, hostname: str):
    """
    Construct a state object accepted by Celery ``Panel`` handlers.

    Celery's own :class:`~celery.worker.pidbox.Pidbox` builds a very similar
    ``AttributeDict`` for the mailbox listener; we reuse the same shape so
    that shipped control commands (``ping``, ``stats``, ``active``, ...) work
    unchanged when dispatched via Consul.
    """
    return AttributeDict(
        app=app,
        hostname=hostname,
        consumer=None,
        tset=pass1,
    )
