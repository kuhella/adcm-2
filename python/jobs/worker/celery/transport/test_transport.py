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
Integration tests for the ``pgnotify`` kombu transport.

Requires a running PostgreSQL instance. Set ``TEST_PG_DSN`` env var or
defaults to ``postgresql://testuser:testpass@localhost:5432/testdb``.
"""

from __future__ import annotations

from queue import Empty
import os
import time
import base64
import unittest
import threading

from kombu import Connection, Consumer, Exchange, Producer, Queue
from kombu.transport import TRANSPORT_ALIASES
import psycopg

from jobs.worker.celery.transport.transport import Channel, Transport

# Register the transport alias before any Connection is created.
TRANSPORT_ALIASES["pgnotify"] = "jobs.worker.celery.transport.transport:Transport"

TEST_PG_DSN = os.environ.get("TEST_PG_DSN", "postgresql://testuser:testpass@localhost:5432/testdb")
BROKER_URL = f"pgnotify+{TEST_PG_DSN}"


def _clean_tables(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            for table in ("kombu_pg_message", "kombu_pg_binding", "kombu_pg_queue"):
                cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE")  # noqa: S608
        conn.commit()


class TestPgNotifyTransport(unittest.TestCase):
    """Basic transport operations: queue CRUD, put/get, purge."""

    @classmethod
    def setUpClass(cls):
        _clean_tables(TEST_PG_DSN)

    def setUp(self):
        self.conn = Connection(BROKER_URL)
        self.conn.connect()
        self.channel: Channel = self.conn.channel()

    def tearDown(self):
        self.channel.close()
        self.conn.close()

    def test_transport_resolves(self):
        transport = self.conn.transport
        self.assertIsInstance(transport, Transport)
        self.assertEqual(transport.driver_name, "pgnotify")

    def test_new_queue_and_has_queue(self):
        queue_name = "test_queue_create"
        self.channel._new_queue(queue_name)
        self.assertTrue(self.channel._has_queue(queue_name))

    def test_has_queue_nonexistent(self):
        self.assertFalse(self.channel._has_queue("nonexistent_queue_xyz"))

    def test_put_and_get(self):
        queue_name = "test_queue_put_get"
        self.channel._new_queue(queue_name)

        payload = {"body": "hello", "properties": {}, "content-type": "application/json"}
        self.channel._put(queue_name, payload)

        result = self.channel._get(queue_name)
        self.assertEqual(result["body"], "hello")

    def test_get_empty_raises(self):
        queue_name = "test_queue_empty"
        self.channel._new_queue(queue_name)
        with self.assertRaises(Empty):
            self.channel._get(queue_name)

    def test_fifo_ordering(self):
        queue_name = "test_queue_fifo"
        self.channel._new_queue(queue_name)

        for i in range(5):
            self.channel._put(queue_name, {"seq": i})

        for i in range(5):
            msg = self.channel._get(queue_name)
            self.assertEqual(msg["seq"], i)

    def test_size(self):
        queue_name = "test_queue_size"
        self.channel._new_queue(queue_name)
        self.assertEqual(self.channel._size(queue_name), 0)

        self.channel._put(queue_name, {"a": 1})
        self.channel._put(queue_name, {"b": 2})
        self.assertEqual(self.channel._size(queue_name), 2)

    def test_purge(self):
        queue_name = "test_queue_purge"
        self.channel._new_queue(queue_name)
        self.channel._put(queue_name, {"x": 1})
        self.channel._put(queue_name, {"y": 2})
        self.channel._put(queue_name, {"z": 3})

        count = self.channel._purge(queue_name)
        self.assertEqual(count, 3)
        self.assertEqual(self.channel._size(queue_name), 0)

    def test_delete_queue(self):
        queue_name = "test_queue_delete"
        self.channel._new_queue(queue_name)
        self.channel._put(queue_name, {"data": True})
        self.channel._delete(queue_name)
        self.assertFalse(self.channel._has_queue(queue_name))


class TestPgNotifyFanout(unittest.TestCase):
    """Fanout exchange delivery via LISTEN/NOTIFY."""

    @classmethod
    def setUpClass(cls):
        _clean_tables(TEST_PG_DSN)

    def setUp(self):
        self.conn = Connection(BROKER_URL)
        self.conn.connect()
        self.channel: Channel = self.conn.channel()

    def tearDown(self):
        self.channel.close()
        self.conn.close()

    def test_queue_bind_fanout(self):
        exchange_name = "test_fanout_exchange"
        queue_name = "test_fanout_queue"

        self.channel.exchange_declare(exchange=exchange_name, type="fanout")
        self.channel._new_queue(queue_name)
        self.channel._queue_bind(exchange_name, "", "", queue_name)

        self.assertIn(queue_name, self.channel._fanout_queues)

    def test_get_table(self):
        exchange_name = "test_binding_exchange"
        self.channel.exchange_declare(exchange=exchange_name, type="direct")
        self.channel._new_queue("test_binding_q1")
        self.channel._queue_bind(exchange_name, "rk1", "rk1", "test_binding_q1")

        table = self.channel.get_table(exchange_name)
        self.assertTrue(len(table) >= 1)
        queue_names = [row[2] for row in table]
        self.assertIn("test_binding_q1", queue_names)


class TestPgNotifyListenNotify(unittest.TestCase):
    """Test that LISTEN/NOTIFY wakes up the consumer."""

    @classmethod
    def setUpClass(cls):
        _clean_tables(TEST_PG_DSN)

    def test_notify_wakes_poll(self):
        conn = Connection(BROKER_URL)
        conn.connect()
        channel: Channel = conn.channel()

        queue_name = "test_listen_wake"
        channel._new_queue(queue_name)

        payload = {"body": "notify-test"}

        # Publish from a separate thread after a short delay
        def delayed_publish():
            time.sleep(0.5)
            pub_conn = Connection(BROKER_URL)
            pub_conn.connect()
            pub_channel = pub_conn.channel()
            pub_channel._put(queue_name, payload)
            pub_channel.close()
            pub_conn.close()

        t = threading.Thread(target=delayed_publish)
        t.start()

        # poll_listen should detect the notification
        channel._poll_listen(timeout=3.0)
        t.join(timeout=5)

        # Now get the message
        msg = channel._get(queue_name)
        self.assertEqual(msg["body"], "notify-test")

        channel.close()
        conn.close()


class TestPgNotifyKombuIntegration(unittest.TestCase):
    """Higher-level kombu Producer/Consumer integration."""

    @classmethod
    def setUpClass(cls):
        _clean_tables(TEST_PG_DSN)

    def test_producer_consumer_roundtrip(self):
        exchange = Exchange("test_kombu_exchange", type="direct")
        queue = Queue("test_kombu_queue", exchange=exchange, routing_key="test.key")

        received = []

        with Connection(BROKER_URL) as conn:
            # Produce
            producer = Producer(conn)
            producer.publish(
                {"message": "hello-kombu"},
                exchange=exchange,
                routing_key="test.key",
                declare=[queue],
                serializer="json",
            )

            # Consume
            def callback(body, message):
                received.append(body)
                message.ack()

            with Consumer(conn, queues=[queue], callbacks=[callback]):
                conn.drain_events(timeout=3.0)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["message"], "hello-kombu")

    def test_multiple_messages(self):
        exchange = Exchange("test_multi_exchange", type="direct")
        queue = Queue("test_multi_queue", exchange=exchange, routing_key="multi.key")

        received = []

        with Connection(BROKER_URL) as conn:
            producer = Producer(conn)
            for i in range(3):
                producer.publish(
                    {"seq": i},
                    exchange=exchange,
                    routing_key="multi.key",
                    declare=[queue],
                    serializer="json",
                )

            def callback(body, message):
                received.append(body)
                message.ack()

            with Consumer(conn, queues=[queue], callbacks=[callback]):
                for _ in range(3):
                    conn.drain_events(timeout=3.0)

        self.assertEqual(len(received), 3)
        seqs = [m["seq"] for m in received]
        self.assertEqual(seqs, [0, 1, 2])


class TestCeleryIntegration(unittest.TestCase):
    """Test Celery app with the pgnotify transport."""

    @classmethod
    def setUpClass(cls):
        _clean_tables(TEST_PG_DSN)

    def test_celery_send_task_and_fetch(self):
        from celery import Celery

        app = Celery("test-app", broker=BROKER_URL, backend="rpc://")

        @app.task(name="test.add")
        def add(x, y):
            return x + y

        # Send the task via apply_async (it goes into the pgnotify queue)
        with app.connection_for_write() as conn:
            producer = app.amqp.Producer(conn)
            producer.publish(
                {"task": "test.add", "args": [2, 3], "id": "test-id-123"},
                routing_key="celery",
                serializer="json",
            )

        # Verify the message is in the queue
        with app.connection_for_read() as conn:
            channel = conn.channel()
            try:
                msg = channel._get("celery")
                body = base64.b64decode(msg["body"]).decode("utf-8")
                self.assertIn("test.add", body)
            except Empty:
                self.fail("Expected a task message in the 'celery' queue")
            finally:
                channel.close()

    def test_celery_control_ping_setup(self):
        """Verify the Celery app can be configured with pgnotify without errors."""
        from celery import Celery

        app = Celery("test-control", broker=BROKER_URL)

        # Verify connection works
        with app.connection_for_write() as conn:
            conn.connect()
            transport = conn.transport
            self.assertIsInstance(transport, Transport)
            self.assertTrue(transport.Channel.supports_fanout)

    def test_celery_control_broadcast_via_fanout(self):
        """Control commands use pidbox fanout through the pgnotify transport."""
        from celery import Celery

        app = Celery("test-pidbox", broker=BROKER_URL)

        # The pidbox exchange is a fanout exchange named "{hostname}.pidbox"
        # Verify we can publish a control command through the transport
        pidbox_exchange = Exchange("celery.pidbox", type="fanout")
        reply_queue = Queue(
            "test_pidbox_reply",
            exchange=pidbox_exchange,
            routing_key="",
            auto_delete=True,
        )

        received = []

        with app.connection_for_write() as conn:
            # Bind a consumer to the pidbox fanout exchange
            def on_message(body, message):
                received.append(body)
                message.ack()

            with Consumer(conn, queues=[reply_queue], callbacks=[on_message]):
                # Publish a control command (like ping) to the fanout exchange
                producer = Producer(conn)
                producer.publish(
                    {
                        "method": "ping",
                        "arguments": {},
                        "destination": None,
                    },
                    exchange=pidbox_exchange,
                    routing_key="",
                    serializer="json",
                    declare=[reply_queue],
                )

                # Drain — the fanout message should arrive instantly via NOTIFY
                conn.drain_events(timeout=3.0)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["method"], "ping")

    def test_fanout_broadcast_reaches_multiple_consumers(self):
        """Fanout exchange delivers to all bound consumers (simulates multi-worker pidbox)."""
        from celery import Celery

        app = Celery("test-multi-pidbox", broker=BROKER_URL)

        fan_exchange = Exchange("test_multi_fan", type="fanout")
        q1 = Queue("test_fan_worker_1", exchange=fan_exchange, routing_key="")
        q2 = Queue("test_fan_worker_2", exchange=fan_exchange, routing_key="")

        received_1 = []
        received_2 = []

        with app.connection_for_write() as conn:

            def cb1(body, message):
                received_1.append(body)
                message.ack()

            def cb2(body, message):
                received_2.append(body)
                message.ack()

            with Consumer(conn, queues=[q1], callbacks=[cb1]):
                with Consumer(conn, queues=[q2], callbacks=[cb2]):
                    producer = Producer(conn)
                    producer.publish(
                        {"command": "revoke", "task_id": "abc-123"},
                        exchange=fan_exchange,
                        routing_key="",
                        serializer="json",
                        declare=[q1, q2],
                    )

                    # Single NOTIFY delivers to all bound queues at once
                    conn.drain_events(timeout=3.0)

        self.assertEqual(len(received_1), 1)
        self.assertEqual(received_1[0]["command"], "revoke")
        self.assertEqual(len(received_2), 1)
        self.assertEqual(received_2[0]["task_id"], "abc-123")


if __name__ == "__main__":
    unittest.main()
