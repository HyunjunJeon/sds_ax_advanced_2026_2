import httpx
import pytest

from day02.errors import ServiceError
from day02.openviking.client import VikingClient


@pytest.mark.parametrize("code", [401, 403, 404, 500])
def test_http_failures_not_empty_search(code):
    transport = httpx.MockTransport(lambda request: httpx.Response(code, json={"secret": "should-not-be-printed"}))
    with VikingClient("http://test", "secret-key", transport=transport) as client:
        with pytest.raises(ServiceError) as error:
            client.find("x", "viking://resources")
        assert error.value.status_code == code
        assert "secret-key" not in str(error.value)
        assert "should-not-be-printed" not in str(error.value)


def test_successful_empty_find():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"result": {"resources": []}}))
    with VikingClient("http://test", "key", transport=transport) as client:
        assert client.find("x", "viking://resources") == []


def test_application_error_not_empty_search():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "error"}))
    with VikingClient("http://test", "key", transport=transport) as client:
        with pytest.raises(ServiceError):
            client.find("x", "viking://resources")


def test_connection_failure_distinct():
    def fail(request):
        raise httpx.ConnectError("connection failed", request=request)
    with VikingClient("http://test", "key", transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(ServiceError, match="연결 실패"):
            client.find("x", "viking://resources")
