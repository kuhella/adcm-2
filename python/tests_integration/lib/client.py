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

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from time import sleep
from typing import Callable, Literal

from faker import Faker
from requests import Response, Session

from tests_integration.lib.date import now


def status_is_2xx(status: int) -> bool:
    return 200 <= status < 300


@dataclass(slots=True)
class YAAClient:
    api_root: str
    session: Session
    faker: Faker

    def do_request(
        self,
        method: Literal["GET", "POST"],
        *path: str | int,
        json: dict | list[dict] | None = None,
        params: dict | None = None,
        files: dict | None = None,
        is_status_correct: Callable[[int], bool] = status_is_2xx,
    ) -> Response:
        ep_call = getattr(self.session, method.lower())
        url = f"{self.api_root}/{'/'.join(map(str, path))}/"
        response = ep_call(url=url, json=json, files=files, params=params)
        assert is_status_correct(
            response.status_code
        ), f"Status is unexpected ({response.status_code}) for {method} to {url}, answer: {response.text}"
        return response

    def login_as_admin(self) -> None:
        response = self.do_request("POST", "login", json={"username": "admin", "password": "admin"})
        self.session.headers["X-CSRFToken"] = response.cookies["csrftoken"]
        self.session.headers["Referer"] = str(self.api_root)

    def upload_bundle(self, packed_bundle: Path) -> dict:
        with packed_bundle.open(mode="rb") as f:
            response = self.do_request("POST", "bundles", files={"file": f})

        data = response.json()

        licensed_prototype_ids = (
            p["id"]
            for p in self.do_request("GET", "prototypes").json()["results"]
            if p["bundle"]["id"] == data["id"] and p["license"]["status"] == "unaccepted"
        )

        for prototype_id in licensed_prototype_ids:
            self.do_request("POST", "prototypes", prototype_id, "license", "accept")

        return data

    def create_provider(self, bundle: dict) -> dict:
        bundle_id = bundle["id"]
        prototype_id = next(
            entry["id"]
            for entry in self.do_request("GET", "prototypes").json()["results"]
            if entry["type"] == "provider" and entry["bundle"]["id"] == bundle_id
        )
        name = self.faker.unique.name()

        response = self.do_request("POST", "hostproviders", json={"name": name, "prototypeId": prototype_id})

        return response.json()

    def create_host(self, provider: dict, cluster: dict | None = None) -> dict:
        name = self.faker.unique.domain_name()

        payload = {"name": name, "hostproviderId": provider["id"]}
        if cluster:
            payload["clusterId"] = cluster["id"]

        response = self.do_request("POST", "hosts", json=payload)

        return response.json()

    def create_cluster(self, bundle: dict) -> dict:
        bundle_id = bundle["id"]
        prototype_id = next(
            entry["id"]
            for entry in self.do_request("GET", "prototypes").json()["results"]
            if entry["type"] == "cluster" and entry["bundle"]["id"] == bundle_id
        )
        name = self.faker.unique.name()

        response = self.do_request("POST", "clusters", json={"name": name, "prototypeId": prototype_id})

        return response.json()

    def add_services(self, cluster: dict, names: set[str] | list[str]) -> list[dict]:
        cluster_prototype = self.do_request("GET", "prototypes", cluster["prototype"]["id"]).json()
        bundle_id = cluster_prototype["bundle"]["id"]
        prototypes = self.do_request(
            "GET", "prototypes", params={"bundle_id__eq": bundle_id, "type__eq": "service"}
        ).json()["results"]
        payload = [{"prototypeId": p["id"]} for p in prototypes if p["name"] in names and p["type"] == "service"]
        return self.do_request("POST", "clusters", cluster["id"], "services", json=payload).json()

    def change_config(
        self, owner: dict, *, values: dict | None = None, meta: dict | None = None, mode: Literal["set"] = "set"
    ) -> dict:
        if owner["type"] != "cluster":
            raise NotImplementedError(f"not implemented for {owner=}")

        endpoint = "clusters", owner["id"], "configs"

        match mode:
            case "set":
                payload = {"config": values or {}, "adcmMeta": meta or {}}
                return self.do_request("POST", *endpoint, json=payload).json()

            case _:
                raise NotImplementedError(f"mode {mode} isn't supported")

    def expect_task_is_finished(
        self, task: dict, *, expected_status: str = "success", timeout: int = 15, period: float = 1.0
    ):
        task_id = task["id"]
        deadline = now() + timedelta(seconds=timeout)
        task_status = "unknown"
        final_statuses = {"success", "failed", "aborted", "broken", "revoked"}

        while now() < deadline:
            task_status = self.do_request("GET", "tasks", task_id).json()["status"]
            if task_status in final_statuses:
                break

            sleep(period)

        assert task_status == expected_status, f"Unexpected final status of task: {task_status=}, {expected_status=}"

    def expect_job_enters_status(self, job_id: int, status_is: str | set[str]):
        timeout = 5
        period = 0.1
        deadline = now() + timedelta(seconds=timeout)

        expected_statuses = {status_is} if isinstance(status_is, str) else status_is

        actual_status = None

        while now() < deadline:
            actual_status = self.do_request("GET", "jobs", job_id).json()["status"]

            if actual_status in expected_statuses:
                break

            sleep(period)

        assert actual_status in expected_statuses, f"Job status mismatch: {actual_status=} {expected_statuses=}"
