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
Transport-level tests for the PostgreSQL LISTEN/NOTIFY kombu transport.

These exercise the fanout path that Celery's pidbox rides — the mechanism that
makes ``app.control.revoke(...)`` reach a running worker — against a real
PostgreSQL instance, without booting a full worker container. Fanout delivery
is normally driven by the async event loop calling ``on_readable``; here we
drive it deterministically by invoking ``handle_notifications`` once the LISTEN
socket has data, which is exactly what the event loop does.
"""

from time import monotonic, sleep
from typing import Callable

from kombu import Connection, Consumer, Exchange, Producer, Queue
from testcontainers.postgres import PostgresContainer
import pytest
import psycopg

# Celery broadcasts control commands (revoke/ping/rate_limit) over the pidbox
# fanout exchange; we reuse the real name so the test mirrors production wiring.
PIDBOX_EXCHANGE = Exchange("celery.pidbox", type="fanout")

TRANSPORT_PATH = "jobs.worker.celery.pg.transport:Transport"


def _broker_url(postgres: PostgresContainer) -> str:
    host = postgres.get_container_host_ip()
    port = postgres.get_exposed_port(5432)
    creds = f"{postgres.username}:{postgres.password}"
    sqla_url = f"postgresql+psycopg://{creds}@{host}:{port}/{postgres.dbname}"
    return f"{TRANSPORT_PATH}+{sqla_url}"


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 10.0, interval: float = 0.1) -> bool:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return True
        sleep(interval)
    return False


def test_revoke_command_broadcast_reaches_worker_mailbox(postgres: PostgresContainer) -> None:
    """A revoke control command published to the pidbox fanout exchange is
    delivered to a subscribed worker mailbox over LISTEN/NOTIFY."""
    url = _broker_url(postgres)
    received: list[dict] = []

    def on_message(body: dict, message) -> None:
        received.append(body)
        message.ack()

    revoke_command = {"method": "revoke", "arguments": {"task_id": "task-abc-123"}, "destination": None}

    with Connection(url) as consumer_conn:
        mailbox = Queue("worker.mailbox", exchange=PIDBOX_EXCHANGE, auto_delete=True)
        consumer = Consumer(consumer_conn, queues=[mailbox], callbacks=[on_message], accept=["json"])
        consumer.consume()  # issues LISTEN on the pidbox fanout channel

        # Publish from a distinct connection so the NOTIFY genuinely round-trips
        # through PostgreSQL rather than staying inside one process/socket.
        with Connection(url) as producer_conn:
            Producer(producer_conn).publish(
                revoke_command,
                exchange=PIDBOX_EXCHANGE,
                declare=[PIDBOX_EXCHANGE],
                serializer="json",
            )

        delivered = _wait_until(lambda: _pump(consumer, received))
        assert delivered, "revoke command was not delivered to the mailbox"

    assert received == [revoke_command]


def test_task_queue_wakeup_delivers_message(postgres: PostgresContainer) -> None:
    """A message put on a direct (task) queue wakes a LISTENing consumer via the
    per-queue NOTIFY channel and is delivered through the storage `_get` path."""
    url = _broker_url(postgres)
    received: list[dict] = []

    def on_message(body: dict, message) -> None:
        received.append(body)
        message.ack()

    task_message = {"task": "run_job", "id": "job-42"}

    with Connection(url) as consumer_conn:
        task_queue = Queue("adcm.tasks", auto_delete=True)
        consumer = Consumer(consumer_conn, queues=[task_queue], callbacks=[on_message], accept=["json"])
        consumer.consume()  # issues LISTEN on the per-queue wakeup channel

        with Connection(url) as producer_conn:
            Producer(producer_conn).publish(
                task_message,
                routing_key="adcm.tasks",
                declare=[task_queue],
                serializer="json",
            )

        delivered = _wait_until(lambda: _pump(consumer, received))
        assert delivered, "task message was not delivered after NOTIFY wakeup"

    assert received == [task_message]


def test_acked_message_is_deleted_from_table(postgres: PostgresContainer) -> None:
    """Acking a task message removes its backend row, so the table does not grow
    without bound as tasks are processed."""
    url = _broker_url(postgres)
    received: list[dict] = []

    def on_message(body: dict, message) -> None:
        received.append(body)
        message.ack()

    with Connection(url) as consumer_conn:
        task_queue = Queue("adcm.acktest", auto_delete=True)
        consumer = Consumer(consumer_conn, queues=[task_queue], callbacks=[on_message], accept=["json"])
        consumer.consume()

        with Connection(url) as producer_conn:
            Producer(producer_conn).publish(
                {"task": "x"}, routing_key="adcm.acktest", declare=[task_queue], serializer="json"
            )

        assert _wait_until(lambda: _pump(consumer, received)), "message was not delivered"
        # ack ran inside the callback; the row must be gone now.
        assert _message_count(postgres, "adcm.acktest") == 0


def test_rejected_message_is_redelivered_then_cleaned_up(postgres: PostgresContainer) -> None:
    """A message rejected with requeue is redelivered (not lost), and once
    finally acked no rows leak in the table."""
    url = _broker_url(postgres)
    deliveries: list[dict] = []

    def on_message(body: dict, message) -> None:
        deliveries.append(body)
        if len(deliveries) == 1:
            message.requeue()  # reject with requeue=True -> _restore re-puts it
        else:
            message.ack()

    with Connection(url) as consumer_conn:
        task_queue = Queue("adcm.requeue", auto_delete=True)
        consumer = Consumer(consumer_conn, queues=[task_queue], callbacks=[on_message], accept=["json"])
        consumer.consume()

        with Connection(url) as producer_conn:
            Producer(producer_conn).publish(
                {"task": "y"}, routing_key="adcm.requeue", declare=[task_queue], serializer="json"
            )

        redelivered = _wait_until(
            lambda: (consumer.channel.handle_notifications() or len(deliveries) >= 2),
            timeout=15.0,
        )
        assert redelivered, "message was not redelivered after requeue"

    assert deliveries == [{"task": "y"}, {"task": "y"}]
    assert _message_count(postgres, "adcm.requeue") == 0


def _pump(consumer: Consumer, received: list) -> bool:
    """Simulate the event loop firing on a readable LISTEN socket, then report
    whether anything has been delivered so far."""
    consumer.channel.handle_notifications()
    return bool(received)


def _message_count(postgres: PostgresContainer, queue_name: str) -> int:
    with psycopg.connect(
        host=postgres.get_container_host_ip(),
        port=postgres.get_exposed_port(5432),
        user=postgres.username,
        password=postgres.password,
        dbname=postgres.dbname,
    ) as conn:
        row = conn.execute(
            "SELECT count(*) FROM kombu_message m " "JOIN kombu_queue q ON m.queue_id = q.id " "WHERE q.name = %s",
            (queue_name,),
        ).fetchone()
    return row[0] if row else 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
