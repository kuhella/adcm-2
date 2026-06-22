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
from pathlib import Path
from typing import Protocol
import tarfile

from tests_integration.lib.date import now


class BundlePacker(Protocol):
    def pack_from_dir(self, source_dir: Path) -> Path:
        ...


@dataclass(slots=True)
class SimpleBundlePacker(BundlePacker):
    target_dir: Path

    def pack_from_dir(self, source_dir: Path) -> Path:
        bundle_file = self.target_dir / f"{source_dir.name}_{now().timestamp()}.tar"

        with tarfile.open(bundle_file, "w") as tar:
            for file in source_dir.iterdir():
                tar.add(name=file, arcname=file.name)

        return bundle_file
