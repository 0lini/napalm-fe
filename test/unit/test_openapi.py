"""Tests for the OpenAPI session and the openapi transport plumbing."""

import pytest
import requests
from napalm.base.exceptions import (
    CommandErrorException,
    ConnectionClosedException,
    ConnectionException,
)

from napalm_extreme_fe import openapi
from napalm_extreme_fe.extreme_fe import ExtremeFeDriver
from napalm_extreme_fe.openapi import OpenApiSession

# Real error body (Fabric Engine 9.4): "debugLog" holds the decoded token
VRF_ERROR = {
    "debugLog": "[__init__.py:94:verify_auth_token] decoded_payload :{'username': 'rwa', "
    "'source': '192.0.2.50', 'iat': 1795191457, 'nbf': 1795191457, 'exp': 1795277857, "
    "'nonce': 'xxxxxxxxxxxxxxxxxxxxxx'}",
    "errorCode": 404,
    "errorMessage": "VRF does not exist",
    "resource": "/rest/openapi/v0/state/forwarding/ip-arp?vr_name=TEST",
}


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code = status
        self.body = body

    def json(self):
        if self.body is None:
            raise ValueError("no JSON")
        return self.body


class FakeHttp:
    """requests.Session replacement: answers from a list, records the requests."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []
        self.headers = {}
        self.verify = None
        self.closed = False

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, dict(self.headers), kwargs))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def close(self):
        self.closed = True


def _session(monkeypatch, answers, **kwargs):
    http = FakeHttp(answers)
    monkeypatch.setattr(openapi.requests, "Session", lambda: http)
    session = OpenApiSession("192.0.2.10", "rwa", "secret", **kwargs)
    return session, http


def test_login_and_get(monkeypatch):
    session, http = _session(
        monkeypatch,
        [FakeResponse(200, {"token": "t1", "ttl": 86400}), FakeResponse(200, {"sysName": "sw"})],
        verify="/etc/ca.pem",
    )
    session.open()
    assert session.get("/v0/state/system") == {"sysName": "sw"}
    method, url, _, kwargs = http.requests[0]
    assert (method, url) == (
        "POST",
        "https://192.0.2.10:9443/rest/openapi/v0/operation/auth-token/:generate",
    )
    assert kwargs["json"] == {"username": "rwa", "password": "secret"}
    assert http.requests[1][2]["X-Auth-Token"] == "t1"
    assert http.verify == "/etc/ca.pem"
    session.close()
    assert http.closed and session.token is None
    # the token is never ended: :disable may switch token authentication off
    assert not any(":disable" in url for _, url, _, _ in http.requests)


def test_expired_token_is_renewed_once(monkeypatch):
    session, http = _session(
        monkeypatch,
        [
            FakeResponse(200, {"token": "t1"}),
            FakeResponse(401, {"errorCode": 401}),
            FakeResponse(200, {"token": "t2"}),
            FakeResponse(200, []),
        ],
    )
    session.open()
    assert session.get("/v0/state/lldp") == []
    assert http.requests[-1][2]["X-Auth-Token"] == "t2"


def test_error_body_without_debug_log(monkeypatch):
    session, _ = _session(
        monkeypatch, [FakeResponse(200, {"token": "t1"}), FakeResponse(404, VRF_ERROR)]
    )
    session.open()
    with pytest.raises(CommandErrorException) as exc:
        session.get("/v0/state/forwarding/ip-arp", {"vr_name": "TEST"})
    message = str(exc.value)
    assert "VRF does not exist" in message and "404" in message
    assert "decoded_payload" not in message and "192.0.2.50" not in message


def test_login_failures(monkeypatch):
    session, _ = _session(monkeypatch, [FakeResponse(401, {"errorCode": 401, "errorMessage": "x"})])
    with pytest.raises(ConnectionException, match="login failed"):
        session.open()
    session, _ = _session(monkeypatch, [FakeResponse(200, {"ttl": 86400})])
    with pytest.raises(ConnectionException, match="no token"):
        session.open()
    session, _ = _session(monkeypatch, [requests.ConnectionError("refused")])
    with pytest.raises(ConnectionException, match="refused"):
        session.open()


def test_error_without_json(monkeypatch):
    session, _ = _session(monkeypatch, [FakeResponse(200, {"token": "t1"}), FakeResponse(500)])
    session.open()
    with pytest.raises(CommandErrorException, match="HTTP 500$"):
        session.get("/v0/state/system")


def test_not_open():
    with pytest.raises(ConnectionException, match="not open"):
        OpenApiSession("192.0.2.10", "rwa", "secret").get("/v0/state/system")


def test_ipv6_host():
    assert OpenApiSession("2001:db8::10", "u", "p", port=8443).base_url == (
        "https://[2001:db8::10]:8443/rest/openapi"
    )


def test_driver_opens_only_the_api(monkeypatch):
    opened = []

    class Session:
        token = "t1"

        def __init__(self, hostname, username, password, port, verify, timeout):
            opened.append((hostname, port, verify, timeout))

        def open(self):
            pass

        def close(self):
            opened.append("closed")

    monkeypatch.setattr("napalm_extreme_fe.extreme_fe.OpenApiSession", Session)
    driver = ExtremeFeDriver(
        "192.0.2.10", "rwa", "secret", optional_args={"transport": "openapi", "verify": False}
    )
    driver.open()
    assert opened == [("192.0.2.10", 9443, False, 60)]
    assert driver.device is None  # no SSH until something needs the CLI
    assert driver.is_alive() == {"is_alive": True}

    ssh = []
    monkeypatch.setattr(
        driver, "_ssh_open", lambda: ssh.append(1) or setattr(driver, "device", "x")
    )
    assert driver._ssh() == "x" and driver._ssh() == "x"
    assert ssh == [1]  # opened once, on first use
    monkeypatch.setattr(driver, "_netmiko_close", lambda: ssh.append("closed"))
    driver.close()
    assert opened[-1] == "closed" and ssh[-1] == "closed"
    assert driver.is_alive() == {"is_alive": False}


def _api_driver(test, case="fabric_engine_9.4_openapi"):
    from .conftest import PatchedExtremeFeDriver

    driver = PatchedExtremeFeDriver("h", "u", "p", optional_args={"transport": "openapi"})
    for attr in ("device", "api"):
        getattr(driver, attr).current_test = test
        getattr(driver, attr).current_test_case = case
    return driver


def test_lldp_detail_for_one_port():
    driver = _api_driver("test_get_lldp_neighbors_detail")
    assert list(driver.get_lldp_neighbors_detail("1/1")) == ["1/1"]
    assert driver.get_lldp_neighbors_detail("1/9") == {}


def test_arp_vrf_case_and_unknown_vrf():
    driver = _api_driver("test_get_arp_table_with_vrf")
    # the switch stores VRF names in lower case ("test"); the lookup ignores the case
    assert driver.get_arp_table(vrf="test") == driver.get_arp_table(vrf="TEST")
    driver.api.get = lambda path, params=None: (
        [] if path.endswith("/ip-arp") else [{"name": "GlobalRouter"}, {"name": "test"}]
    )
    assert driver.get_arp_table(vrf="TEST") == []  # VRF exists, no entries
    with pytest.raises(CommandErrorException, match="does not exist"):
        driver.get_arp_table(vrf="NOPE")


def test_driver_without_connection():
    driver = ExtremeFeDriver("192.0.2.10", "rwa", "secret", optional_args={"transport": "openapi"})
    with pytest.raises(ConnectionClosedException):
        driver._api_get("/v0/state/system")
    with pytest.raises(ConnectionClosedException):
        driver._ssh()
