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


from testcontainers.core.generic import DockerContainer

from tests_integration.constants import API_V2_BUNDLES
from tests_integration.lib.bundles import BundlePacker
from tests_integration.lib.client import YAAClient


class Smoke:
    def test_run_simple_task(
        self, adcm_worker: DockerContainer, client: YAAClient, bundle_packer: BundlePacker
    ) -> None:
        packed_bundle = bundle_packer.pack_from_dir(API_V2_BUNDLES / "cluster_one")

        try:
            bundle = client.upload_bundle(packed_bundle)
        except AssertionError as e:
            # todo need to make it sane
            if "Bundle already exists" not in str(e):
                raise

            bundle = client.do_request("GET", "bundles").json()["results"][0]

        cluster_id = client.create_cluster(bundle)["id"]

        actions = client.do_request("GET", "clusters", cluster_id, "actions").json()
        task = client.do_request("POST", "clusters", cluster_id, "actions", actions[0]["id"], "run").json()

        client.expect_task_is_finished(task, expected_status="success")

        stdout, _ = map(bytes.decode, adcm_worker.get_logs())
        assert "ERROR" not in stdout
        # will fail until ADCM-8123 is fixed
        # assert "ERROR" not in stderr
