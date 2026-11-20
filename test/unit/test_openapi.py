"""Tests for the OpenAPI session and the https transport plumbing."""

import re

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


# Real reply (Fabric Engine 9.4) of POST operation/system/cli?continue_on_failure=true:
# "show sys-info" counts as failed because its output contains "System Error Info"
CLI_REPLY = {
    "data": [
        {
            "cliInput": "show clock",
            "cliOutput": "\nFri Nov 20 16:31:55 2026 UTC\n\n",
            "statusCode": 200,
        },
        {
            "cliInput": "show bogus-command",
            "cliOutput": "        ^\r\n% Invalid input detected at '^' marker.\r\n",
            "statusCode": 400,
        },
        {
            "cliInput": "show sys-info",
            "cliOutput": "\nSystem Error Info : \n\tError Code                : 0\n",
            "statusCode": 400,
        },
    ],
    "metadata": {"failed": 2, "skipped": 0, "succeeded": 1},
}


def test_cli(monkeypatch):
    session, http = _session(
        monkeypatch, [FakeResponse(200, {"token": "t1"}), FakeResponse(207, CLI_REPLY)]
    )
    session.open()
    commands = ["show clock", "show bogus-command", "show sys-info"]
    outputs = session.cli(commands)
    method, url, _, kwargs = http.requests[-1]
    assert (method, url.rsplit("/rest/openapi", 1)[1]) == ("POST", "/v0/operation/system/cli")
    assert kwargs["params"] == {"continue_on_failure": "true"} and kwargs["json"] == commands
    # all outputs are returned, whatever the switch's statusCode; "\r\n" -> "\n"
    assert outputs[0] == "\nFri Nov 20 16:31:55 2026 UTC\n\n"
    assert outputs[1] == "        ^\n% Invalid input detected at '^' marker.\n"
    assert "System Error Info" in outputs[2]


def test_cli_output_count_checked(monkeypatch):
    reply = {"data": CLI_REPLY["data"][:1], "metadata": {}}
    session, _ = _session(
        monkeypatch, [FakeResponse(200, {"token": "t1"}), FakeResponse(200, reply)]
    )
    session.open()
    with pytest.raises(CommandErrorException, match="1 outputs for 2 commands"):
        session.cli(["show clock", "show sys-info"])


def test_openapi_cli_getter_without_ssh():
    """CLI getters run over the API (same output as SSH, without the banner):
    last_flapped then comes from "show clock"."""
    driver = _api_driver("test_get_interfaces", "fabric_engine_9.3")
    banner = re.compile(r"^\*+\n\s*Command Execution Time:.*\n\*+\n", re.M)
    files = {
        "show interfaces gigabitEthernet": banner.sub(
            "",
            driver.device.read_txt_file(
                driver.device.find_file("show_interfaces_gigabitEthernet.txt")
            ),
        ),
        "show clock": "\nWed Nov 18 19:21:18 2026 UTC\n\n",
    }
    driver.api.cli = lambda commands: [files[c] for c in commands]
    interfaces = driver.get_interfaces()
    assert interfaces["1/1"]["last_flapped"] == 782.0  # as in the SSH case
    assert driver.device.commands == []  # nothing over SSH


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
    driver = ExtremeFeDriver("192.0.2.10", "rwa", "secret", optional_args={"ssl_verify": False})
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

    driver = PatchedExtremeFeDriver("h", "u", "p", optional_args={"transport": "https"})
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
    driver = ExtremeFeDriver("192.0.2.10", "rwa", "secret", optional_args={"transport": "https"})
    with pytest.raises(ConnectionClosedException):
        driver._api_get("/v0/state/system")
    with pytest.raises(ConnectionClosedException):
        driver._ssh()


def test_save_config(monkeypatch):
    session, http = _session(
        monkeypatch,
        [FakeResponse(200, {"token": "t1"}), FakeResponse(204), FakeResponse(204)],
    )
    session.open()
    session.save_config()
    session.save_config("/intflash/napalm_rollback.cfg")
    (_, url1, _, kwargs1), (_, _, _, kwargs2) = http.requests[1:]
    assert url1.endswith("/rest/openapi/v0/operation/system/config/:save")
    assert kwargs1["json"] == {}  # boot configuration file ("save config")
    assert kwargs2["json"] == {"name": "/intflash/napalm_rollback.cfg"}


# Real reply (Fabric Engine 9.4) of POST operation/system/cli?continue_on_failure=false:
# the commands after the failed one are skipped (statusCode 409)
CONFIG_REPLY = {
    "data": [
        {
            "cliInput": "configure terminal",
            "cliOutput": "Enter configuration commands, one per line.  End with CNTL/Z.\r\n",
            "statusCode": 200,
        },
        {
            "cliInput": "vlan create 10 type port-mstprstp 0",
            "cliOutput": "\n\x07Error: The specified VLAN ID is invalid or already in use \n\n",
            "statusCode": 400,
        },
        {"cliInput": "end", "cliOutput": "", "statusCode": 409},
    ],
    "metadata": {"failed": 1, "skipped": 1, "succeeded": 1},
}


def test_cli_stop_on_failure(monkeypatch):
    # the skipped commands (409) are left out
    reply = CONFIG_REPLY
    session, http = _session(
        monkeypatch, [FakeResponse(200, {"token": "t1"}), FakeResponse(207, reply)]
    )
    session.open()
    commands = ["configure terminal", "vlan create 10 type port-mstprstp 0", "end"]
    outputs = session.cli(commands, continue_on_failure=False, timeout=120)
    assert len(outputs) == 2
    assert http.requests[-1][3]["params"] == {"continue_on_failure": "false"}
    assert http.requests[-1][3]["timeout"] == 120


def test_insecure_warning_disabled_without_verify(monkeypatch):
    disabled = []
    monkeypatch.setattr(openapi.urllib3, "disable_warnings", disabled.append)
    session, _ = _session(monkeypatch, [FakeResponse(200, {"token": "t1"})])
    session.open()  # verify=False is the default, like NX-OS
    assert disabled == [openapi.urllib3.exceptions.InsecureRequestWarning]
    session, _ = _session(monkeypatch, [FakeResponse(200, {"token": "t1"})], verify=True)
    session.open()
    assert len(disabled) == 1


def test_reboot_schedule_and_cancel(monkeypatch):
    session, http = _session(
        monkeypatch,
        [FakeResponse(200, {"token": "t1"}), FakeResponse(204), FakeResponse(204)],
    )
    session.open()
    session.schedule_reboot(15)
    session.cancel_reboot()
    (_, url1, _, kwargs1), (_, url2, _, _) = http.requests[1:]
    assert url1.endswith("/v0/operation/system/reboot/:schedule")
    assert kwargs1["json"] == {"delay": 15}  # minutes
    assert url2.endswith("/v0/operation/system/reboot/:cancel")
