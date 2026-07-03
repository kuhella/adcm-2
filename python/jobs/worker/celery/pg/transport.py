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
Kombu ``Transport`` / ``Channel`` for PostgreSQL with fanout via LISTEN/NOTIFY.

Storage (direct/topic task queues) lives in SQL tables and is claimed with
``SELECT ... FOR UPDATE SKIP LOCKED``. Fanout (pidbox control commands) rides
``LISTEN``/``NOTIFY``, letting the standard Celery Consumer/pidbox boot normally
so ``revoke(terminate=True)`` works without the Consul control transport.
"""

from __future__ import annotations

from contextlib import suppress
from json import dumps, loads
from queue import Empty
import hashlib
import logging
import threading

from kombu.transport import virtual
from kombu.utils import cached_property
from kombu.utils.encoding import bytes_to_str
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from jobs.worker.celery.pg.listen import ListenConnection, sqlalchemy_url_to_dsn
from jobs.worker.celery.pg.models import Binding, Message, Queue, metadata
from jobs.worker.celery.pg.poller import FanoutPoller

logger = logging.getLogger(__name__)

# One engine per DSN, shared across channels of a process.
_ENGINES: dict[str, tuple] = {}
_ENGINE_LOCK = threading.RLock()

# Fully-qualified path kombu resolves this transport by. Used as the broker URL
# scheme prefix: ``<TRANSPORT>+postgresql+psycopg://...`` (kombu splits on the
# first ``+``, handing the remaining SQLAlchemy URL to the transport verbatim).
TRANSPORT = f"{__name__}:Transport"


def make_broker_url(sqla_url: str) -> str:
    """Build a ``broker_url`` selecting this transport for `sqla_url`."""
    return f"{TRANSPORT}+{sqla_url}"


# Postgres NOTIFY channel names are identifiers (<= 63 bytes). Exchange and
# queue names may contain dots (``celery.pidbox``), so we derive a safe,
# collision-resistant channel name rather than passing them through verbatim.
_FANOUT_PREFIX = "kombu_fanout_"
_WAKEUP_PREFIX = "kombu_queue_"


def _channel_for(prefix: str, name: str) -> str:
    digest = hashlib.blake2s(name.encode("utf-8"), digest_size=8).hexdigest()
    return f"{prefix}{digest}"


def fanout_channel_name(exchange: str) -> str:
    """PG NOTIFY channel that broadcasts to everyone bound to `exchange`."""
    return _channel_for(_FANOUT_PREFIX, exchange)


def wakeup_channel_name(queue: str) -> str:
    """PG NOTIFY channel that signals a new message is waiting on `queue`."""
    return _channel_for(_WAKEUP_PREFIX, queue)


class Channel(virtual.Channel):
    """A single AMQP channel over the PostgreSQL transport."""

    supports_fanout = True

    _session = None

    def __init__(self, connection, **kwargs) -> None:
        super().__init__(connection, **kwargs)
        # queues (by kombu queue name) currently consumed as fanout
        self.active_fanout_queues: set[str] = set()
        # pg NOTIFY channel -> kombu queue, for fanout delivery routing
        self._fanout_channel_to_queue: dict[str, str] = {}
        # task queues we're LISTENing on for new-message wakeups
        self.active_wakeup_queues: set[str] = set()
        # pg wakeup channel -> kombu queue, to pump the right queue on NOTIFY
        self._wakeup_channel_to_queue: dict[str, str] = {}
        # delivery_tag -> backend row id, so ack/restore can delete the row
        self._claimed_rows: dict[str, int] = {}

    # -- engine / session -------------------------------------------------

    def _open(self) -> tuple:
        dsn = self.connection.client.hostname
        if dsn not in _ENGINES:
            with _ENGINE_LOCK:
                if dsn not in _ENGINES:
                    engine = create_engine(dsn)
                    metadata.create_all(engine)
                    _ENGINES[dsn] = (engine, sessionmaker(bind=engine))
        return _ENGINES[dsn]

    @property
    def session(self):
        if self._session is None:
            _, session_factory = self._open()
            self._session = session_factory()
        return self._session

    @cached_property
    def listen_connection(self) -> ListenConnection:
        dsn = sqlalchemy_url_to_dsn(self.connection.client.hostname)
        return ListenConnection(dsn)

    # -- storage (direct/topic queues) ------------------------------------

    def _get_or_create_queue(self, queue: str) -> Queue:
        obj = self.session.query(Queue).filter(Queue.name == queue).first()
        if obj is None:
            obj = Queue(queue)
            self.session.add(obj)
            try:
                self.session.commit()
            except OperationalError:
                self.session.rollback()
                obj = self.session.query(Queue).filter(Queue.name == queue).one()
        return obj

    def _new_queue(self, queue, **kwargs) -> None:  # noqa: ARG002
        self._get_or_create_queue(queue)

    def _put(self, queue, message, **kwargs) -> None:  # noqa: ARG002
        obj = self._get_or_create_queue(queue)
        self.session.add(Message(dumps(message), obj))
        # NOTIFY inside the same transaction as the INSERT: Postgres only
        # delivers the notification on COMMIT, so a woken consumer is guaranteed
        # to see the row. Payload is empty — it's a wakeup signal, the message
        # itself stays in the table.
        self.session.execute(text("SELECT pg_notify(:chan, '')"), {"chan": wakeup_channel_name(queue)})
        try:
            self.session.commit()
        except OperationalError:
            self.session.rollback()
            raise

    def _get(self, queue, timeout=None):  # noqa: ARG002
        obj = self._get_or_create_queue(queue)
        try:
            msg = (
                self.session.query(Message)
                .filter(Message.queue_id == obj.id, Message.visible.is_(True))
                .order_by(Message.sent_at, Message.id)
                .limit(1)
                .with_for_update(skip_locked=True)
                .first()
            )
            if msg is None:
                raise Empty()
            # Decode before mutating state: a poison payload must not leave the
            # row hidden-forever, so we drop it and skip rather than claim it.
            try:
                decoded = loads(bytes_to_str(msg.payload))
            except (ValueError, TypeError):
                logger.warning("Discarding undecodable message id=%s on queue %r", msg.id, queue)
                self.session.delete(msg)
                raise Empty() from None
            # Claim: hide from other consumers; remember the row so basic_ack /
            # _restore can delete it once the worker is done with the message.
            delivery_tag = decoded.get("properties", {}).get("delivery_tag")
            if delivery_tag is not None:
                self._claimed_rows[delivery_tag] = msg.id
            msg.visible = False
            return decoded
        finally:
            self.session.commit()

    def basic_ack(self, delivery_tag, multiple=False):
        # `multiple` is unsupported (delivery tags are uuids, not ordered) —
        # mirrors the virtual base, which also ignores it.
        super().basic_ack(delivery_tag, multiple=multiple)
        row_id = self._claimed_rows.pop(delivery_tag, None)
        if row_id is not None:
            self._delete_message_row(row_id)

    def _restore(self, message) -> None:
        # The base re-puts a fresh copy; drop the original claimed row first,
        # otherwise it leaks in the table (invisible) forever.
        row_id = self._claimed_rows.pop(message.delivery_tag, None)
        if row_id is not None:
            self._delete_message_row(row_id)
        super()._restore(message)

    def _delete_message_row(self, row_id: int) -> None:
        self.session.query(Message).filter(Message.id == row_id).delete(synchronize_session=False)
        self.session.commit()

    def _size(self, queue) -> int:
        obj = self._get_or_create_queue(queue)
        return self.session.query(Message).filter(Message.queue_id == obj.id).count()

    def _purge(self, queue) -> int:
        obj = self._get_or_create_queue(queue)
        count = self.session.query(Message).filter(Message.queue_id == obj.id).delete(synchronize_session=False)
        self.session.commit()
        return count

    def _delete(self, queue, *args, **kwargs) -> None:  # noqa: ARG002
        # Drop the queue with its messages and persisted bindings. This is how
        # ephemeral pidbox reply queues get cleaned up (auto_delete), so the
        # binding table does not grow unbounded.
        self.session.query(Binding).filter(Binding.queue == queue).delete(synchronize_session=False)
        obj = self.session.query(Queue).filter(Queue.name == queue).first()
        if obj is not None:
            self.session.query(Message).filter(Message.queue_id == obj.id).delete(synchronize_session=False)
            self.session.delete(obj)
        self.session.commit()

    # -- fanout (control commands / pidbox) -------------------------------

    def _put_fanout(self, exchange, message, routing_key, **kwargs) -> None:  # noqa: ARG002
        """Broadcast a message to every worker LISTENing on the exchange."""
        self.listen_connection.notify(fanout_channel_name(exchange), dumps(message))

    def _queue_bind(self, exchange, routing_key, pattern, queue) -> None:
        # Persist the binding so a publisher in ANOTHER process can route a
        # direct/topic message to this queue (fanout still routes via NOTIFY
        # channels; persisting uniformly is harmless). This is what makes
        # cross-process pidbox replies work.
        stmt = (
            pg_insert(Binding)
            .values(exchange=exchange, routing_key=routing_key or "", pattern=pattern or "", queue=queue or "")
            .on_conflict_do_nothing(constraint="uq_kombu_binding")
        )
        self.session.execute(stmt)
        self.session.commit()

    def get_table(self, exchange):
        # Merge the in-memory bindings (this process) with the persisted ones
        # (all processes) so direct/topic lookups see queues bound elsewhere.
        try:
            in_memory = super().get_table(exchange)
        except KeyError:
            in_memory = []
        rows = (
            self.session.query(Binding.routing_key, Binding.pattern, Binding.queue)
            .filter(Binding.exchange == exchange)
            .all()
        )
        merged = {tuple(entry) for entry in in_memory} | {tuple(row) for row in rows}
        return [list(entry) for entry in merged]

    # -- consume: LISTEN on fanout exchange or task-queue wakeup channel ---

    def basic_consume(self, queue, no_ack, callback, consumer_tag, **kwargs):
        exchange = self._fanout_exchange_for(queue)
        if exchange is not None:
            channel_name = fanout_channel_name(exchange)
            self.active_fanout_queues.add(queue)
            self._fanout_channel_to_queue[channel_name] = queue
            self.listen_connection.listen(channel_name)
        else:
            channel_name = wakeup_channel_name(queue)
            self.active_wakeup_queues.add(queue)
            self._wakeup_channel_to_queue[channel_name] = queue
            self.listen_connection.listen(channel_name)
        if self.connection is not None:
            self.connection.fanout_poller.add(self)
        return super().basic_consume(queue, no_ack, callback, consumer_tag, **kwargs)

    def basic_cancel(self, consumer_tag):
        queue = self._tag_to_queue.get(consumer_tag)
        if queue in self.active_fanout_queues:
            exchange = self._fanout_exchange_for(queue)
            if exchange is not None:
                channel_name = fanout_channel_name(exchange)
                self.active_fanout_queues.discard(queue)
                self._fanout_channel_to_queue.pop(channel_name, None)
                self.listen_connection.unlisten(channel_name)
        elif queue in self.active_wakeup_queues:
            channel_name = wakeup_channel_name(queue)
            self.active_wakeup_queues.discard(queue)
            self._wakeup_channel_to_queue.pop(channel_name, None)
            self.listen_connection.unlisten(channel_name)
        return super().basic_cancel(consumer_tag)

    def handle_notifications(self) -> None:
        """
        Drain pending NOTIFYs from the LISTEN socket and dispatch each.

        Fanout channels carry the message in the payload and are delivered
        directly. Task-queue wakeup channels are empty signals: they mean "a
        row is waiting", so we pump the queue via the normal ``_get`` path.
        """
        for note in self.listen_connection.drain():
            queue = self._fanout_channel_to_queue.get(note.channel)
            if queue is not None:
                self.connection._deliver(loads(note.payload), queue)
                continue
            queue = self._wakeup_channel_to_queue.get(note.channel)
            if queue is not None:
                self._pump_queue(queue)

    def _pump_queue(self, queue: str) -> None:
        """Deliver ready messages for `queue` while prefetch allows."""
        while self.qos.can_consume():
            try:
                message = self._get(queue)
            except Empty:
                break
            self.connection._deliver(message, queue)

    def _fanout_exchange_for(self, queue: str) -> str | None:
        """Return the exchange name if `queue` is bound to a fanout exchange."""
        for exchange, meta in self.state.exchanges.items():
            if meta.get("type") != "fanout":
                continue
            if any(binding[2] == queue for binding in meta.get("table", [])):
                return exchange
        return None

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        if self.connection is not None:
            self.connection.fanout_poller.discard(self)
        listen_connection = self.__dict__.get("listen_connection")
        if listen_connection is not None:
            listen_connection.close()
        if self._session is not None:
            self._session.close()
            self._session = None
        super().close()


class Transport(virtual.Transport):
    """PostgreSQL transport with LISTEN/NOTIFY fanout."""

    Channel = Channel

    can_parse_url = True
    default_port = 0
    driver_type = "sql"
    driver_name = "postgresql"
    connection_errors = (OperationalError,)

    # Both fanout and task queues are delivered via NOTIFY on the event loop;
    # this is only the backstop poll interval (see register_with_event_loop).
    polling_interval = 1.0

    implements = virtual.Transport.implements.extend(
        asynchronous=True,
        exchange_type=frozenset(["direct", "topic", "fanout"]),
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # base `self.cycle` (FairCycle) still drains task queues; this only
        # tracks the LISTEN fds for fanout.
        self.fanout_poller = FanoutPoller()

    def driver_version(self) -> str:
        import sqlalchemy

        return sqlalchemy.__version__

    def register_with_event_loop(self, connection, loop) -> None:
        poller = self.fanout_poller
        on_readable = self.on_readable

        def on_poll_start() -> None:
            for fd in tuple(poller.fds):
                loop.add_reader(fd, on_readable, fd)

        loop.on_tick.add(on_poll_start)

        # New messages wake consumers instantly via NOTIFY (see `_put`). This
        # timer is a backstop, not the primary path: it re-pumps queues for
        # messages that arrived while prefetch was full (a slot frees on ack)
        # and recovers any NOTIFY missed across a LISTEN reconnect.
        loop.call_repeatedly(self.polling_interval, self._drain_task_queues, connection)

    def on_readable(self, fileno) -> None:
        self.fanout_poller.on_readable(fileno)

    def _drain_task_queues(self, connection) -> None:
        with suppress(Empty, OSError):
            super().drain_events(connection, timeout=0)
