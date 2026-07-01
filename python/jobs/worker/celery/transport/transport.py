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
``pgnotify`` — kombu transport backed by PostgreSQL tables + LISTEN/NOTIFY.

Features
========
* Type: Virtual
* Supports Direct: yes
* Supports Topic: yes
* Supports Fanout: yes  (via LISTEN/NOTIFY channels)
* Supports Priority: no
* Supports TTL: no

Connection String
=================

.. code-block::

    pgnotify+postgresql://user:pass@host:port/dbname

The ``pgnotify+`` prefix is stripped; the remainder is passed directly
to :func:`psycopg.connect`.

Architecture
============

Two ``psycopg`` connections per channel:

* **query connection** – transactional; used for INSERT / SELECT / DELETE.
* **listen connection** – ``autocommit=True``; runs ``LISTEN`` commands and
  blocks in ``connection.notifies()`` during ``drain_events``.

Direct queues use a ``kombu_pg_message`` table with
``SELECT … FOR UPDATE SKIP LOCKED`` for safe concurrent consumption.
LISTEN/NOTIFY provides instant wake-up so there is no polling delay.

Fanout exchanges (used by Celery's pidbox for ``revoke``, ``ping``, …)
publish the serialised message as the NOTIFY payload on a per-exchange
channel.  All workers LISTENing on that channel receive the notification
simultaneously.
"""

from __future__ import annotations

from contextlib import suppress
from json import dumps, loads
from queue import Empty
import select
import logging
import threading

from kombu.transport import virtual
from kombu.utils.encoding import bytes_to_str
import psycopg

from jobs.worker.celery.transport.tables import DDL, FANOUT_CHANNEL_PREFIX, QUEUE_CHANNEL_PREFIX

logger = logging.getLogger(__name__)


class Channel(virtual.Channel):
    """kombu channel backed by PostgreSQL + LISTEN/NOTIFY."""

    supports_fanout = True

    # ---- state ----------------------------------------------------------------

    _query_conn: psycopg.Connection | None = None
    _listen_conn: psycopg.Connection | None = None

    def __init__(self, connection, **kwargs):
        super().__init__(connection, **kwargs)
        self._fanout_queues: dict[str, tuple[str, str]] = {}
        self._fanout_buffer: list[tuple[str, dict]] = []
        self._listen_lock = threading.Lock()
        self._listening_channels: set[str] = set()
        self._tables_created = False

    # ---- connections ----------------------------------------------------------

    @property
    def conninfo_str(self) -> str:
        """Build a psycopg DSN from the kombu connection info."""
        client = self.connection.client
        hostname = client.hostname
        if hostname and hostname.startswith("pgnotify+"):
            hostname = hostname[len("pgnotify+") :]
        if hostname and hostname.startswith("postgresql+psycopg://"):
            hostname = f"postgresql://{hostname[len('postgresql+psycopg://'):]}"
        return hostname or ""

    @property
    def query_conn(self) -> psycopg.Connection:
        if self._query_conn is None or self._query_conn.closed:
            self._query_conn = psycopg.connect(self.conninfo_str, autocommit=False)
            self._ensure_tables(self._query_conn)
        return self._query_conn

    @property
    def listen_conn(self) -> psycopg.Connection:
        if self._listen_conn is None or self._listen_conn.closed:
            self._listen_conn = psycopg.connect(self.conninfo_str, autocommit=True)
        return self._listen_conn

    def _ensure_tables(self, conn: psycopg.Connection) -> None:
        if self._tables_created:
            return
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
        self._tables_created = True

    # ---- queue management -----------------------------------------------------

    def _new_queue(self, queue, **kwargs):  # noqa: ARG002
        with self.query_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO kombu_pg_queue (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
                (queue,),
            )
        self.query_conn.commit()
        self._ensure_listen(QUEUE_CHANNEL_PREFIX + self._sanitize_channel(queue))

    def _has_queue(self, queue, **kwargs):  # noqa: ARG002
        with self.query_conn.cursor() as cur:
            cur.execute("SELECT EXISTS(SELECT 1 FROM kombu_pg_queue WHERE name = %s)", (queue,))
            row = cur.fetchone()
        return row is not None and row[0]

    def _delete(self, queue, *args, **kwargs):  # noqa: ARG002
        with self.query_conn.cursor() as cur:
            cur.execute("DELETE FROM kombu_pg_queue WHERE name = %s", (queue,))
            cur.execute(
                "DELETE FROM kombu_pg_binding WHERE queue = %s",
                (queue,),
            )
        self.query_conn.commit()
        self._fanout_queues.pop(queue, None)

    # ---- publish / consume ---------------------------------------------------

    def _put(self, queue, payload, **kwargs):  # noqa: ARG002
        with self.query_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO kombu_pg_queue (name) VALUES (%s) ON CONFLICT (name) DO NOTHING RETURNING id",
                (queue,),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute("SELECT id FROM kombu_pg_queue WHERE name = %s", (queue,))
                row = cur.fetchone()
            queue_id = row[0]
            cur.execute(
                "INSERT INTO kombu_pg_message (queue_id, payload) VALUES (%s, %s)",
                (queue_id, dumps(payload)),
            )
            channel_name = QUEUE_CHANNEL_PREFIX + self._sanitize_channel(queue)
            cur.execute("SELECT pg_notify(%s, %s)", (channel_name, "new"))
        self.query_conn.commit()

    def _get(self, queue, timeout=None):  # noqa: ARG002
        with self.query_conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM kombu_pg_message
                WHERE id = (
                    SELECT m.id
                    FROM kombu_pg_message m
                    JOIN kombu_pg_queue q ON q.id = m.queue_id
                    WHERE q.name = %s
                    ORDER BY m.sent_at, m.id
                    LIMIT 1
                    FOR UPDATE OF m SKIP LOCKED
                )
                RETURNING payload
                """,
                (queue,),
            )
            row = cur.fetchone()
        self.query_conn.commit()

        if row is None:
            raise Empty()

        return loads(bytes_to_str(row[0])) if isinstance(row[0], (bytes, memoryview)) else row[0]

    def _size(self, queue):
        with self.query_conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM kombu_pg_message m
                JOIN kombu_pg_queue q ON q.id = m.queue_id
                WHERE q.name = %s
                """,
                (queue,),
            )
            row = cur.fetchone()
        return row[0] if row else 0

    def _purge(self, queue):
        with self.query_conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM kombu_pg_message
                WHERE queue_id = (SELECT id FROM kombu_pg_queue WHERE name = %s)
                """,
                (queue,),
            )
            count = cur.rowcount
        self.query_conn.commit()
        return count

    # ---- fanout ---------------------------------------------------------------

    def _queue_bind(self, exchange, routing_key, pattern, queue):  # noqa: ARG002
        if self.typeof(exchange).type == "fanout":
            self._fanout_queues[queue] = (exchange, routing_key or "")
            fan_channel = FANOUT_CHANNEL_PREFIX + self._sanitize_channel(exchange)
            self._ensure_listen(fan_channel)

        with self.query_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kombu_pg_binding (exchange, routing_key, queue)
                VALUES (%s, %s, %s)
                ON CONFLICT (exchange, routing_key, queue) DO NOTHING
                """,
                (exchange, routing_key or "", queue or ""),
            )
        self.query_conn.commit()

    def get_table(self, exchange):
        with self.query_conn.cursor() as cur:
            cur.execute(
                "SELECT routing_key, routing_key, queue FROM kombu_pg_binding WHERE exchange = %s",
                (exchange,),
            )
            rows = cur.fetchall()
        return [tuple(row) for row in rows]

    def _put_fanout(self, exchange, message, routing_key, **kwargs):  # noqa: ARG002
        fan_channel = FANOUT_CHANNEL_PREFIX + self._sanitize_channel(exchange)
        serialised = dumps(message)
        if len(serialised) > 7900:
            logger.warning(
                "Fanout payload exceeds PG NOTIFY limit (%d bytes), message may be truncated",
                len(serialised),
            )
        conn = self.listen_conn
        with conn.cursor() as cur:
            cur.execute("SELECT pg_notify(%s, %s)", (fan_channel, serialised))

    # ---- LISTEN / drain_events -----------------------------------------------

    def _ensure_listen(self, pg_channel: str) -> None:
        with self._listen_lock:
            if pg_channel in self._listening_channels:
                return
            conn = self.listen_conn
            with conn.cursor() as cur:
                cur.execute(psycopg.sql.SQL("LISTEN {}").format(psycopg.sql.Identifier(pg_channel)))
            self._listening_channels.add(pg_channel)

    def _poll(self, cycle, callback, timeout=None):
        self._drain_fanout_buffer(callback)

        if self._poll_listen(timeout=0.0):
            self._drain_fanout_buffer(callback)

        try:
            cycle.get(callback)
        except Empty:
            if timeout is not None:
                self._poll_listen(timeout=timeout)
                self._drain_fanout_buffer(callback)
                cycle.get(callback)
            else:
                raise

    def _poll_listen(self, timeout: float | None = None) -> bool:
        conn = self.listen_conn
        fd = conn.fileno()
        if fd < 0:
            return False

        wait = timeout if timeout is not None else 0.0
        ready = select.select([fd], [], [], wait)
        if not ready[0]:
            return False

        received = False
        for notify in conn.notifies(timeout=0, stop_after=100):
            received = True
            channel_name = notify.channel
            payload_str = notify.payload

            if channel_name.startswith(FANOUT_CHANNEL_PREFIX):
                try:
                    message = loads(payload_str)
                    exchange = channel_name[len(FANOUT_CHANNEL_PREFIX) :]
                    for queue, (ex, _rk) in self._fanout_queues.items():
                        if self._sanitize_channel(ex) == exchange:
                            self._fanout_buffer.append((queue, message))
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to parse fanout notification on channel %r", channel_name)

        return received

    def _drain_fanout_buffer(self, callback):  # noqa: ARG002
        while self._fanout_buffer:
            queue, message = self._fanout_buffer.pop(0)
            cb = self.connection._callbacks.get(queue)
            if cb:
                cb(message)

    # ---- cleanup --------------------------------------------------------------

    def close(self):
        super().close()
        for conn in (self._query_conn, self._listen_conn):
            if conn is not None and not conn.closed:
                with suppress(Exception):
                    conn.close()
        self._query_conn = None
        self._listen_conn = None

    # ---- helpers --------------------------------------------------------------

    @staticmethod
    def _sanitize_channel(name: str) -> str:
        return name.replace(".", "_").replace("-", "_").replace("/", "_")


class Transport(virtual.Transport):
    """kombu transport using PostgreSQL tables + LISTEN/NOTIFY."""

    Channel = Channel

    can_parse_url = True
    default_port = 5432
    polling_interval = None
    driver_type = "sql"
    driver_name = "pgnotify"
    connection_errors = (psycopg.OperationalError,)
    channel_errors = (psycopg.ProgrammingError, psycopg.IntegrityError)

    def driver_version(self):
        return psycopg.__version__
