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

from application.di.providers.environment import parse_consul_settings_from_env


class TestParseConsulSettingsFromEnv(TestCase):
    def test_disabled_without_url(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(parse_consul_settings_from_env())

    def test_parses_connection_credentials(self) -> None:
        env = {
            "CONSUL_URL": "https://consul.local:8501",
            "CONSUL_DATACENTER": "dc1",
            "CONSUL_ACL_TOKEN": "secret-token",
            "CONSUL_CACERT_FILE": "/certs/ca.pem",
            "CONSUL_CLIENT_CERT_FILE": "/certs/client.pem",
            "CONSUL_CLIENT_KEY_FILE": "/certs/client.key",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            settings = parse_consul_settings_from_env()

        self.assertIsNotNone(settings)
        self.assertEqual(settings.url, "https://consul.local:8501")
        self.assertEqual(settings.datacenter, "dc1")
        self.assertEqual(settings.acl_token, "secret-token")
        self.assertEqual(settings.cacert_file, "/certs/ca.pem")
        self.assertEqual(settings.client_cert_file, "/certs/client.pem")
        self.assertEqual(settings.client_key_file, "/certs/client.key")
