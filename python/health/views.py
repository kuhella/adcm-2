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

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.status import HTTP_200_OK, HTTP_503_SERVICE_UNAVAILABLE
from rest_framework.views import APIView

from health.checks import run_readiness_checks


class ReadinessView(APIView):
    permission_classes = (AllowAny,)
    authentication_classes = ()
    http_method_names = ["get"]

    @extend_schema(
        operation_id="getHealthCheck",
        summary="Readiness probe",
        description="Get information about adcm backend health.",
        tags=["health"],
        responses={
            HTTP_200_OK: OpenApiResponse(description="OK"),
            HTTP_503_SERVICE_UNAVAILABLE: OpenApiResponse(description="One or more dependencies are unavailable"),
        },
    )
    def get(self, request: Request, *args, **kwargs) -> Response:  # noqa: ARG002
        results = run_readiness_checks()
        ready = all(result.healthy for result in results)

        body = {
            "status": "ok" if ready else "unavailable",
            "checks": {result.name: {"healthy": result.healthy, "detail": result.detail} for result in results},
        }
        status_code = HTTP_200_OK if ready else HTTP_503_SERVICE_UNAVAILABLE

        return Response(data=body, status=status_code)
