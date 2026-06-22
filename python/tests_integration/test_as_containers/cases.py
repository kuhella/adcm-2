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

import json

import pytest
import psycopg

from tests_integration.constants import INTEGRATION_BUNDLES
from tests_integration.lib.bundles import BundlePacker
from tests_integration.lib.client import YAAClient

STAGES = json.dumps(
    [
        {
            "name": "stage-1",
            "display_name": "Stage With Operation",
            "steps": [
                {
                    "name": "op-step",
                    "display_name": "operation step",
                    "ui_options": {"button_name": "wow"},
                    "scripts_template": {
                        "engine": {"type": "python"},
                        "file": {"path": "wizard/scripts-from-config.py", "entrypoint": "main"},
                    },
                }
            ],
        }
    ]
)

SCRIPTS_SUCCESS = json.dumps([{"name": "job-1", "script": "scripts/pass.yaml", "script_type": "ansible"}])
SCRIPTS_CONFIG_BASED = json.dumps(
    [
        {"name": "job-1", "script": "scripts/outcome-from-config.yaml", "script_type": "ansible"},
        {"name": "job-2", "script": "scripts/pass.yaml", "script_type": "ansible"},
    ]
)


class Smoke:
    @pytest.fixture(scope="module")
    def various_actions_bundle(self, client: YAAClient, bundle_packer: BundlePacker) -> dict:
        packed_bundle = bundle_packer.pack_from_dir(INTEGRATION_BUNDLES / "various_actions")
        return client.upload_bundle(packed_bundle)

    @pytest.fixture(scope="module")
    def provider_bundle(self, client: YAAClient, bundle_packer: BundlePacker) -> dict:
        packed_bundle = bundle_packer.pack_from_dir(INTEGRATION_BUNDLES / "provider")
        return client.upload_bundle(packed_bundle)

    def get_action_by_name(self, cluster_id: int, client: YAAClient, name: str) -> dict:
        actions = client.do_request("GET", "clusters", cluster_id, "actions").json()
        action_id = next(action["id"] for action in actions if action["name"] == name)
        return client.do_request("GET", "clusters", cluster_id, "actions", action_id).json()

    def run_cluster_action(self, cluster_id: int, action_id: int, payload: dict, client: YAAClient) -> dict:
        return client.do_request("POST", "clusters", cluster_id, "actions", action_id, "run", json=payload).json()

    @pytest.mark.parametrize(
        [
            "action_name",
            "fail_step",
            "expected_state",
            "expected_multi_state",
            "expected_outcome",
            "expected_job_statuses",
        ],
        [
            [
                "controlled_ansible",
                None,
                "task-success",
                ["task-success-ms"],
                "success",
                ["success", "success", "success"],
            ],
            ["controlled_ansible", "first", "created", ["first-fail-ms"], "failed", ["failed", "created", "created"]],
            [
                "controlled_ansible",
                "second",
                "second-fail-state",
                ["second-fail-ms"],
                "failed",
                ["success", "failed", "created"],
            ],
            [
                "controlled_ansible",
                "third",
                "third-fail-state",
                ["task-fail-ms"],
                "failed",
                ["success", "success", "failed"],
            ],
            [
                "controlled_ansible_with_task_on_fail",
                None,
                "task-success",
                [],
                "success",
                ["success", "success", "success"],
            ],
            [
                "controlled_ansible_with_task_on_fail",
                "first",
                "task-fail",
                ["first-fail-ms"],
                "failed",
                ["failed", "created", "created"],
            ],
            [
                "controlled_ansible_with_task_on_fail",
                "second",
                "second-fail-state",
                ["second-fail-ms"],
                "failed",
                ["success", "failed", "created"],
            ],
            [
                "controlled_ansible_with_task_on_fail",
                "third",
                "third-fail-state",
                [],
                "failed",
                ["success", "success", "failed"],
            ],
            [
                "controlled_ansible_with_task_no_on_success",
                None,
                "created",
                [],
                "success",
                ["success", "success", "success"],
            ],
            [
                "controlled_ansible_with_task_no_on_success",
                "first",
                "created",
                ["first-fail-ms"],
                "failed",
                ["failed", "created", "created"],
            ],
            [
                "controlled_ansible_with_task_no_on_success",
                "second",
                "second-fail-state",
                ["second-fail-ms"],
                "failed",
                ["success", "failed", "created"],
            ],
            [
                "controlled_ansible_with_task_no_on_success",
                "third",
                "third-fail-state",
                [],
                "failed",
                ["success", "success", "failed"],
            ],
        ],
    )
    def test_run_task(
        self,
        action_name: str,
        fail_step: str | None,
        expected_state: str,
        expected_multi_state: str,
        expected_outcome: str,
        expected_job_statuses: list[str],
        various_actions_bundle: dict,
        # adcm_worker: DockerContainer,
        client: YAAClient,
    ) -> None:
        run_payload = {"configuration": {"config": {"sleep": 0, "fail_step": fail_step}, "adcmMeta": {}}}
        bundle = various_actions_bundle

        cluster_id = client.create_cluster(bundle)["id"]

        action = self.get_action_by_name(cluster_id, client=client, name=action_name)
        task = self.run_cluster_action(cluster_id, action["id"], payload=run_payload, client=client)

        client.expect_task_is_finished(task, expected_status=expected_outcome)

        cluster = client.do_request("GET", "clusters", cluster_id).json()
        actual_state = cluster["state"]
        assert actual_state == expected_state, f"Object state is incorrect: {actual_state=}, {expected_state=}"
        actual_multi_state = cluster["multiState"]
        assert (
            actual_multi_state == expected_multi_state
        ), f"Object multi state is incorrect: {actual_multi_state=}, {expected_multi_state=}"

        task = client.do_request("GET", "tasks", task["id"]).json()
        job_statuses = [job["status"] for job in task["childJobs"]]

        assert (
            job_statuses == expected_job_statuses
        ), f"Job statuses are incorrect: {job_statuses=} != {expected_job_statuses=}"

        # stdout, _ = map(bytes.decode, adcm_worker.get_logs())
        # assert "ERROR" not in stdout, f"There are errors in stdout:\n{stdout}"
        # will fail until ADCM-8123 is fixed
        # assert "ERROR" not in stderr

    @pytest.mark.parametrize(
        ["should_block", "concern_type"],
        [pytest.param(True, "lock", id="lock"), pytest.param(False, "flag", id="flag")],
    )
    @pytest.mark.parametrize(
        ["fail_step", "task_status"],
        [pytest.param(None, "success", id="task-success"), pytest.param("second", "failed", id="task-failed")],
    )
    def test_lock_set_on_task_start_removed_on_finish(
        self,
        should_block: bool,
        concern_type: str,
        fail_step: str | None,
        task_status: str,
        various_actions_bundle: dict,
        client: YAAClient,
    ):
        run_payload = {
            "configuration": {"config": {"sleep": 1, "fail_step": fail_step}, "adcmMeta": {}},
            "shouldBlockObject": should_block,
        }
        action_name = "controlled_ansible"
        bundle = various_actions_bundle

        cluster_id = client.create_cluster(bundle)["id"]

        action = self.get_action_by_name(cluster_id, client=client, name=action_name)
        task = self.run_cluster_action(cluster_id, action["id"], payload=run_payload, client=client)
        first_job_id = task["childJobs"][0]["id"]

        client.expect_job_enters_status(first_job_id, {"queued", "running"})

        concerns = client.do_request("GET", "clusters", cluster_id).json()["concerns"]

        assert len(concerns) == 1
        concern, *_ = concerns
        assert concern["type"] == concern_type
        assert concern["owner"] == {"id": cluster_id, "type": "cluster"}

        client.expect_task_is_finished(task, expected_status=task_status)

        concerns = client.do_request("GET", "clusters", cluster_id).json()["concerns"]

        assert concerns == []

    def test_wizard_operation_pass(self, various_actions_bundle: dict, client: YAAClient):
        action_name = "wizard_from_config_static_scripts"
        bundle = various_actions_bundle

        cluster_id = client.create_cluster(bundle)["id"]

        client.change_config(
            {"type": "cluster", "id": cluster_id},
            values={"wizard": {"stages": STAGES, "scripts": SCRIPTS_SUCCESS}, "job": {"should_pass": True}},
            mode="set",
        )

        action = self.get_action_by_name(cluster_id, client=client, name=action_name)
        processes_endpoint = "clusters", cluster_id, "actions", action["id"], "processes"

        process = client.do_request("POST", *processes_endpoint).json()
        process_id = process["id"]
        step_id = process["stages"][0]["steps"][0]["id"]

        operation_payload = {
            "method": "submit_step",
            "params": {"stepId": step_id, "processSyncKey": process["syncKey"]},
        }
        process = client.do_request("POST", *processes_endpoint, process_id, "operation", json=operation_payload).json()

        step = client.do_request("GET", *processes_endpoint, process_id, "steps", step_id).json()
        client.expect_task_is_finished(step["task"])

        cluster = client.do_request("GET", "clusters", cluster_id).json()
        actual_state = cluster["state"]
        assert (
            actual_state == "created"
        ), f'states should stay at "created" after operation finished, not {actual_state}'

        process = client.do_request("GET", *processes_endpoint, process_id).json()
        operation_payload = {"method": "complete", "params": {"processSyncKey": process["syncKey"]}}
        client.do_request("POST", *processes_endpoint, process_id, "operation", json=operation_payload)

        run_payload = {
            "process": {"id": process["id"]},
            "configuration": {"config": {"sleep": 0, "fail_step": None}, "adcmMeta": {}},
        }
        task = self.run_cluster_action(cluster_id, action["id"], payload=run_payload, client=client)

        client.expect_task_is_finished(task)

        cluster = client.do_request("GET", "clusters", cluster_id).json()
        actual_state = cluster["state"]
        assert (
            actual_state == "wizard-success"
        ), f'states should stay at "wizard-success" after operation finished, not {actual_state}'

    def test_wizard_operation_fail(self, various_actions_bundle: dict, client: YAAClient):
        action_name = "wizard_from_config_static_scripts"
        bundle = various_actions_bundle

        cluster_id = client.create_cluster(bundle)["id"]

        client.change_config(
            {"type": "cluster", "id": cluster_id},
            values={"wizard": {"stages": STAGES, "scripts": SCRIPTS_CONFIG_BASED}, "job": {"should_pass": False}},
            mode="set",
        )

        action = self.get_action_by_name(cluster_id, client=client, name=action_name)
        processes_endpoint = "clusters", cluster_id, "actions", action["id"], "processes"

        process = client.do_request("POST", *processes_endpoint).json()
        process_id = process["id"]
        step_id = process["stages"][0]["steps"][0]["id"]

        operation_payload = {
            "method": "submit_step",
            "params": {"stepId": step_id, "processSyncKey": process["syncKey"]},
        }
        client.do_request("POST", *processes_endpoint, process_id, "operation", json=operation_payload).json()

        step = client.do_request("GET", *processes_endpoint, process_id, "steps", step_id).json()

        client.expect_task_is_finished(step["task"], expected_status="failed")

        step = client.do_request("GET", *processes_endpoint, process_id, "steps", step_id).json()

        assert step["state"] == "created"
        assert step["task"]["status"] == "failed"
        assert step["task"]["childJobs"][0]["status"] == "failed"

        client.change_config(
            {"type": "cluster", "id": cluster_id},
            values={"wizard": {"stages": STAGES, "scripts": SCRIPTS_CONFIG_BASED}, "job": {"should_pass": True}},
            mode="set",
        )

        process = client.do_request("GET", *processes_endpoint, process_id).json()
        operation_payload = {
            "method": "submit_step",
            "params": {"stepId": step_id, "processSyncKey": process["syncKey"]},
        }
        client.do_request("POST", *processes_endpoint, process_id, "operation", json=operation_payload).json()

        step = client.do_request("GET", *processes_endpoint, process_id, "steps", step_id).json()

        client.expect_task_is_finished(step["task"])

        step = client.do_request("GET", *processes_endpoint, process_id, "steps", step_id).json()

        assert step["task"]["status"] == "success"
        assert step["task"]["childJobs"][0]["status"] == "success"
        assert step["state"] == "completed"

        cluster = client.do_request("GET", "clusters", cluster_id).json()
        actual_state = cluster["state"]
        assert (
            actual_state == "created"
        ), f'states should stay at "created" after operation finished, not {actual_state}'

        process = client.do_request("GET", *processes_endpoint, process_id).json()
        operation_payload = {"method": "complete", "params": {"processSyncKey": process["syncKey"]}}
        client.do_request("POST", *processes_endpoint, process_id, "operation", json=operation_payload)

        run_payload = {
            "process": {"id": process["id"]},
            "configuration": {"config": {"sleep": 0, "fail_step": "first"}, "adcmMeta": {}},
        }
        task = self.run_cluster_action(cluster_id, action["id"], payload=run_payload, client=client)

        client.expect_task_is_finished(task, expected_status="failed")

        cluster = client.do_request("GET", "clusters", cluster_id).json()
        actual_state = cluster["state"]
        assert (
            actual_state == "wizard-fail"
        ), f'states should become "wizard-fail" after operation finished, not {actual_state}'

    def test_mm_is_returned_when_not_set_via_plugin(
        self,
        provider_bundle: dict,
        various_actions_bundle: dict,
        client: YAAClient,
        pg_conn: psycopg.Connection,
    ):
        # add case for task failure
        bundle = various_actions_bundle

        cluster = client.create_cluster(bundle)

        provider = client.create_provider(provider_bundle)
        host = client.create_host(provider, cluster=cluster)

        service, *_ = client.add_services(cluster, {"with_mm_and_hc_acl"})
        service_endpoint = "clusters", cluster["id"], "services", service["id"]
        component, *_ = client.do_request("GET", *service_endpoint, "components").json()["results"]

        client.do_request(
            "POST", "clusters", cluster["id"], "mapping", json=[{"componentId": component["id"], "hostId": host["id"]}]
        )

        response = client.do_request("POST", *service_endpoint, "maintenance-mode", json={"maintenanceMode": "on"})
        assert response.json()["maintenanceMode"] == "changing"
        task, *_ = client.do_request(
            "GET",
            "tasks",
            params={"name": "adcm_turn_on_maintenance_mode", "owner_type": "service", "owner_id": service["id"]},
        ).json()["results"]
        client.expect_task_is_finished(task)
        service = client.do_request("GET", *service_endpoint).json()

        assert service["state"] == "turn-on-mm-success"
        # It is expected that mm is returned to before task start value (opposite of task name)
        # if plugin wasn't called during action execution
        assert service["maintenanceMode"] == "off"

        with pg_conn.cursor() as cur:
            cur.execute("UPDATE cm_service SET _maintenance_mode='on' WHERE id=%s", [service["id"]])
            pg_conn.commit()

        response = client.do_request("POST", *service_endpoint, "maintenance-mode", json={"maintenanceMode": "off"})
        assert response.json()["maintenanceMode"] == "changing"
        task, *_ = client.do_request(
            "GET",
            "tasks",
            params={"name": "adcm_turn_off_maintenance_mode", "owner_type": "service", "owner_id": service["id"]},
        ).json()["results"]
        client.expect_task_is_finished(task)
        service = client.do_request("GET", *service_endpoint).json()

        assert service["state"] == "turn-off-mm-success"
        assert service["maintenanceMode"] == "on"
