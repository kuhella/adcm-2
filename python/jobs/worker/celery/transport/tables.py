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
SQL DDL and query helpers for the ``pgnotify`` kombu transport.

Tables
------
``kombu_pg_queue``
    Queue registry; one row per declared queue.

``kombu_pg_message``
    Durable message store with ``FOR UPDATE SKIP LOCKED`` consumption.

``kombu_pg_binding``
    Exchange-to-queue bindings; enables fanout delivery via LISTEN/NOTIFY.
"""

from __future__ import annotations

DDL = """
CREATE TABLE IF NOT EXISTS kombu_pg_queue (
    id    SERIAL  PRIMARY KEY,
    name  TEXT    NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS kombu_pg_message (
    id        BIGSERIAL    PRIMARY KEY,
    queue_id  INT          NOT NULL REFERENCES kombu_pg_queue(id) ON DELETE CASCADE,
    payload   JSONB        NOT NULL,
    sent_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_kombu_pg_message_fetch
    ON kombu_pg_message (queue_id, sent_at, id);

CREATE TABLE IF NOT EXISTS kombu_pg_binding (
    id           SERIAL  PRIMARY KEY,
    exchange     TEXT    NOT NULL,
    routing_key  TEXT    NOT NULL DEFAULT '',
    queue        TEXT    NOT NULL,
    UNIQUE (exchange, routing_key, queue)
);
"""

QUEUE_CHANNEL_PREFIX = "kombu_q_"
FANOUT_CHANNEL_PREFIX = "kombu_fan_"
