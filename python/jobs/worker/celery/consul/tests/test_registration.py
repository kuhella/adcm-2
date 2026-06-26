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

from unittest import TestCase, mock

from jobs.worker.celery.consul import registrar, registration
from jobs.worker.celery.consul import settings as consul_settings


def make_step() -> registration.ConsulRegistrationStep:
    step = registration.ConsulRegistrationStep.__new__(registration.ConsulRegistrationStep)
    step.hostname = "celery@host1"
    step._registration = None
    step._tref = None
    return step


class TestConsulRegistrationStep(TestCase):
    def test_start_noop_when_consul_not_configured(self) -> None:
        step = make_step()
        backend = mock.Mock()

        with (
            mock.patch.object(consul_settings, "is_consul_configured", return_value=False),
            mock.patch.object(registrar, "get_consul_backend", return_value=backend),
        ):
            step.start(work_controller=mock.Mock())

        backend.register.assert_not_called()
        self.assertIsNone(step._registration)

    def test_start_registers_and_passes_check(self) -> None:
        step = make_step()
        backend = mock.Mock()
        work_controller = mock.Mock()

        with (
            mock.patch.object(consul_settings, "is_consul_configured", return_value=True),
            mock.patch.object(registrar, "get_consul_backend", return_value=backend),
            mock.patch.object(registration, "_get_adcm_uuid", return_value="uuid-1"),
        ):
            step.start(work_controller=work_controller)

        self.assertIsNotNone(step._registration)
        backend.register.assert_called_once()
        registration_arg = backend.register.call_args.args[0]
        self.assertEqual(registration_arg.service_id, "celery@host1")
        self.assertEqual(registration_arg.tags, ["adcm", "celery", "uuid-1"])
        backend.pass_check.assert_called_once_with(registration_arg.ttl_check_id, note="worker started")
        work_controller.timer.call_repeatedly.assert_called_once()

    def test_start_swallows_consul_errors(self) -> None:
        step = make_step()
        backend = mock.Mock()
        backend.register.side_effect = RuntimeError("consul down")

        with (
            mock.patch.object(consul_settings, "is_consul_configured", return_value=True),
            mock.patch.object(registrar, "get_consul_backend", return_value=backend),
            mock.patch.object(registration, "_get_adcm_uuid", return_value=None),
        ):
            step.start(work_controller=mock.Mock())

        self.assertIsNone(step._registration)

    def test_stop_deregisters_and_cancels_timer(self) -> None:
        step = make_step()
        backend = mock.Mock()
        tref = mock.Mock()
        step._tref = tref
        step._registration = registrar.build_worker_registration(hostname="celery@host1", adcm_uuid="uuid-1")

        with mock.patch.object(registrar, "get_consul_backend", return_value=backend):
            step.stop(work_controller=mock.Mock())

        tref.cancel.assert_called_once()
        backend.deregister.assert_called_once_with("celery@host1")
        self.assertIsNone(step._registration)

    def test_refresh_ttl_passes_check(self) -> None:
        step = make_step()
        backend = mock.Mock()
        step._registration = registrar.build_worker_registration(hostname="celery@host1", adcm_uuid="uuid-1")

        with mock.patch.object(registrar, "get_consul_backend", return_value=backend):
            step._refresh_ttl()

        backend.pass_check.assert_called_once_with(step._registration.ttl_check_id)
