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

from time import monotonic, sleep
import json

from testcontainers.core.generic import DockerContainer
import pytest

from tests_integration.lib.celery import celery_command, extract_worker_id_from_container
from tests_integration.lib.client import YAAClient
from tests_integration.test_as_containers.cases import Smoke

pytestmark = [pytest.mark.usefixtures("adcm_worker")]

# The celery task that executes a single ADCM job (see jobs.worker.tasks.RUN_JOB_TASK_NAME).
RUN_JOB_TASK_NAME = "adcm:jobs:job-execute"


@pytest.fixture(scope="module")
def adcm_main_env(database_env: dict, scheduler_celery_env: dict) -> dict:
    return database_env | scheduler_celery_env


class TestAction(Smoke):
    def test_terminate(self, various_actions_bundle: dict, client: YAAClient):
        bundle = various_actions_bundle
        action_name = "controlled_ansible"
        run_payload = {"configuration": {"config": {"sleep": 10, "fail_step": None}, "adcmMeta": {}}}

        cluster_id = client.create_cluster(bundle)["id"]

        action = self.get_action_by_name(cluster_id, client=client, name=action_name)
        task = self.run_cluster_action(cluster_id, action["id"], payload=run_payload, client=client)

        job_id = task["childJobs"][0]["id"]

        client.expect_job_enters_status(job_id, status_is="running")

        client.do_request("POST", "jobs", job_id, "terminate")

        client.expect_task_enters_status(task["id"], status_is="success")

    def test_inspect_active_shows_running_job(
        self, various_actions_bundle: dict, client: YAAClient, adcm_main: DockerContainer, adcm_worker: DockerContainer
    ):
        """While an action's job is running, `inspect active` reports the
        executing `adcm:jobs:job-execute` celery task."""
        run_payload = {"configuration": {"config": {"sleep": 20, "fail_step": None}, "adcmMeta": {}}}

        cluster_id = client.create_cluster(various_actions_bundle)["id"]
        action = self.get_action_by_name(cluster_id, client=client, name="controlled_ansible")
        task = self.run_cluster_action(cluster_id, action["id"], payload=run_payload, client=client)
        job_id = task["childJobs"][0]["id"]
        client.expect_job_enters_status(job_id, status_is="running")

        worker_id = extract_worker_id_from_container(adcm_worker)
        result = adcm_main.exec(celery_command("inspect active --json"))
        assert result.exit_code == 0, result.output.decode()
        response = json.loads(result.output.decode())

        active_task_names = [entry["name"] for entry in response.get(worker_id, [])]
        assert RUN_JOB_TASK_NAME in active_task_names, response

        client.do_request("POST", "jobs", job_id, "terminate")

    def test_result_returns_completed_task_result(
        self, various_actions_bundle: dict, client: YAAClient, adcm_main: DockerContainer, pg_conn
    ):
        """`celery result <task_id>` fetches a finished task's value from the
        `db+postgresql` result backend."""
        run_payload = {"configuration": {"config": {"sleep": 1, "fail_step": None}, "adcmMeta": {}}}

        cluster_id = client.create_cluster(various_actions_bundle)["id"]
        action = self.get_action_by_name(cluster_id, client=client, name="controlled_ansible")
        task = self.run_cluster_action(cluster_id, action["id"], payload=run_payload, client=client)
        # Allow time for the job to run to completion (ansible startup is slow
        # under emulation); the default 5s is only enough to reach "running".
        client.expect_task_enters_status(task["id"], status_is="success", timeout=180)

        # A finished job-execute stores its result under its celery task id.
        with pg_conn.cursor() as cur:
            cur.execute("SELECT task_id FROM celery_taskmeta WHERE status = 'SUCCESS' LIMIT 1")
            row = cur.fetchone()
        assert row, "no SUCCESS task found in celery_taskmeta"
        celery_task_id = row[0]

        # Exit 0 means the value was fetched from the backend and returned
        # immediately; a task id absent from the backend would instead block the
        # CLI (AsyncResult.get waits for a pending result). job-execute returns
        # None on success, which `celery result` prints as an empty line.
        result = adcm_main.exec(celery_command(f"result {celery_task_id}"))
        assert result.exit_code == 0, result.output.decode()


def test_broadcast_ping(adcm_main: DockerContainer, adcm_worker: DockerContainer):
    worker_id = extract_worker_id_from_container(adcm_worker)
    result = adcm_main.exec(celery_command("inspect ping --json"))
    assert result.exit_code == 0
    response = json.loads(result.output.decode())
    assert response == {worker_id: {"ok": "pong"}}


# Remote-control commands (celery CLI, https://docs.celeryq.dev/en/stable/reference/cli.html)
# ride the pidbox mailbox: the request fans out to the worker and the reply is
# published back to a per-request reply queue. Over the pg transport that reply
# only routes back because exchange bindings are shared across processes
# (Channel._queue_bind / get_table). These tests assert the whole family of
# reply-returning control commands round-trips, not just `ping`.

# `celery inspect <cmd> --json` returns a per-worker dict: {worker_id: <reply>}.
#
# `conf` and `report` currently reply with an error: Celery serializes `app.conf`,
# which holds ADCM's `CelerySettings` (a `@dataclass(slots=True)`), and that slotted
# dataclass is not serializable by Celery's conf inspector
# (TypeError: descriptor '__weakref__' for 'CelerySettings' ...). The pidbox
# round-trip itself works — only the payload errors — so they are xfailed until
# the config is made inspectable.
_CONF_XFAIL = pytest.mark.xfail(reason="CelerySettings(slots=True) is not serializable by `inspect conf`/`report`")
INSPECT_COMMANDS = (
    "ping",
    "stats",
    "active",
    "reserved",
    "scheduled",
    "revoked",
    "registered",
    "active_queues",
    pytest.param("conf", marks=_CONF_XFAIL),
    pytest.param("report", marks=_CONF_XFAIL),
    "clock",
)

# A real, registered ADCM task name — required by rate_limit / time_limit.
_ADCM_TASK = RUN_JOB_TASK_NAME

# `celery control <cmd> --json` returns a list of per-worker acks: [{worker_id: {"ok": ...}}].
# Only non-destructive commands are exercised (no shutdown / pool_restart / autoscale,
# which would disrupt the module-shared worker).
CONTROL_COMMANDS = (
    ("enable_events", ()),
    ("disable_events", ()),
    ("rate_limit", (_ADCM_TASK, "100/m")),
    ("time_limit", (_ADCM_TASK, "60", "30")),
)


@pytest.mark.parametrize("command", INSPECT_COMMANDS)
def test_inspect_command_replies_over_pg(command: str, adcm_main: DockerContainer, adcm_worker: DockerContainer):
    worker_id = extract_worker_id_from_container(adcm_worker)
    result = adcm_main.exec(celery_command(f"inspect {command} --json"))
    assert result.exit_code == 0, result.output.decode()
    response = json.loads(result.output.decode())
    assert worker_id in response, response
    reply = response[worker_id]
    assert not (isinstance(reply, dict) and "error" in reply), reply


def test_inspect_registered_lists_adcm_tasks(adcm_main: DockerContainer, adcm_worker: DockerContainer):
    worker_id = extract_worker_id_from_container(adcm_worker)
    result = adcm_main.exec(celery_command("inspect registered --json"))
    assert result.exit_code == 0, result.output.decode()
    response = json.loads(result.output.decode())
    assert _ADCM_TASK in response[worker_id], response


@pytest.mark.parametrize("command, args", CONTROL_COMMANDS, ids=[c[0] for c in CONTROL_COMMANDS])
def test_control_command_replies_over_pg(
    command: str, args: tuple[str, ...], adcm_main: DockerContainer, adcm_worker: DockerContainer
):
    worker_id = extract_worker_id_from_container(adcm_worker)
    result = adcm_main.exec(celery_command(f"control {command} {' '.join(args)} --json"))
    assert result.exit_code == 0, result.output.decode()
    response = json.loads(result.output.decode())
    assert isinstance(response, list) and response, response
    reply = response[0].get(worker_id)
    assert isinstance(reply, dict) and "ok" in reply, response


def test_inspect_scheduled_shows_countdown_task(adcm_main: DockerContainer, adcm_worker: DockerContainer):
    """A task enqueued with a countdown sits in the worker's ETA schedule, and
    `inspect scheduled` reports it (nested under each entry's ``request``)."""
    worker_id = extract_worker_id_from_container(adcm_worker)
    submit = adcm_main.exec(celery_command(f"call {RUN_JOB_TASK_NAME} --countdown 600"))
    assert submit.exit_code == 0, submit.output.decode()

    result = adcm_main.exec(celery_command("inspect scheduled --json"))
    assert result.exit_code == 0, result.output.decode()
    response = json.loads(result.output.decode())

    scheduled_task_names = [entry["request"]["name"] for entry in response.get(worker_id, [])]
    assert RUN_JOB_TASK_NAME in scheduled_task_names, response


def test_inspect_revoked_shows_revoked_id(adcm_main: DockerContainer, adcm_worker: DockerContainer):
    """After `control revoke <id>`, the id appears in `inspect revoked`."""
    worker_id = extract_worker_id_from_container(adcm_worker)
    revoked_id = "revoked-00000000-0000-0000-0000-000000000000"

    ack = adcm_main.exec(celery_command(f"control revoke {revoked_id} --json"))
    assert ack.exit_code == 0, ack.output.decode()

    result = adcm_main.exec(celery_command("inspect revoked --json"))
    assert result.exit_code == 0, result.output.decode()
    response = json.loads(result.output.decode())

    assert revoked_id in response.get(worker_id, []), response


def test_status_lists_online_worker(adcm_main: DockerContainer, adcm_worker: DockerContainer):
    """`celery status` lists workers online (a pidbox ping under the hood)."""
    worker_id = extract_worker_id_from_container(adcm_worker)
    result = adcm_main.exec(celery_command("status --json"))
    assert result.exit_code == 0, result.output.decode()
    # `status` prints the per-worker JSON dict followed by a "N node online." line
    response = json.loads(result.output.decode().splitlines()[0])
    assert worker_id in response, response


def test_list_bindings_reflects_added_consumer(adcm_main: DockerContainer):
    """A binding created on the worker by `control add_consumer` becomes visible
    to `list bindings` run from another process.
    """
    queue, routing_key = "pg_probe_queue", "pg_probe_rk"

    baseline = adcm_main.exec(celery_command("list bindings"))
    assert baseline.exit_code == 0, baseline.output.decode()
    assert "celery" in baseline.output.decode() and queue not in baseline.output.decode()

    try:
        ack = adcm_main.exec(celery_command(f"control add_consumer {queue} celery direct {routing_key}"))
        assert ack.exit_code == 0, ack.output.decode()

        deadline = monotonic() + 30
        output = ""
        while monotonic() < deadline:
            listing = adcm_main.exec(celery_command("list bindings"))
            assert listing.exit_code == 0, listing.output.decode()
            output = listing.output.decode()
            if queue in output:
                break
            sleep(1)

        assert queue in output and routing_key in output, output
    finally:
        adcm_main.exec(celery_command(f"control cancel_consumer {queue}"))
