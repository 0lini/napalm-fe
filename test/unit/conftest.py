"""Test fixtures for the NAPALM getter tests (mocked device)."""

import os
from urllib.parse import urlencode

import pytest
from napalm.base.test import conftest as parent_conftest
from napalm.base.test.double import BaseTestDouble

from napalm_fe import fe


@pytest.fixture(scope="class")
def set_device_parameters(request):
    """Set up the class."""

    def fin():
        request.cls.device.close()

    request.addfinalizer(fin)

    request.cls.driver = fe.FEDriver
    request.cls.patched_driver = PatchedFEDriver
    request.cls.vendor = "fe"
    # like parent_conftest.set_device_parameters, with the transport of the class
    driver = request.cls.patched_driver if parent_conftest.NAPALM_TEST_MOCK else request.cls.driver
    request.cls.device = driver(
        parent_conftest.NAPALM_HOSTNAME,
        parent_conftest.NAPALM_USERNAME,
        parent_conftest.NAPALM_PASSWORD,
        timeout=60,
        optional_args={**parent_conftest.NAPALM_OPTIONAL_ARGS, "transport": request.cls.transport},
    )
    request.cls.device.open()


# Cases for transport="https" end in OPENAPI_CASE_SUFFIX and hold the JSON
# responses (plus CLI output for the commands the driver runs through the API)
OPENAPI_CASE_SUFFIX = "_openapi"


def pytest_generate_tests(metafunc):
    """Generate test cases dynamically: each test class gets its transport's cases."""
    if not metafunc.function.__dict__.get("build_test_cases", False):
        return
    openapi = getattr(metafunc.cls, "transport", "ssh") == "https"
    path = os.path.join(os.path.dirname(__file__), "mocked_data", metafunc.function.__name__)
    cases = sorted(
        case
        for case in (os.listdir(path) if os.path.isdir(path) else [])
        if os.path.isdir(os.path.join(path, case)) and case.endswith(OPENAPI_CASE_SUFFIX) == openapi
    )
    metafunc.parametrize("test_case", cases)


class PatchedFEDriver(fe.FEDriver):
    """Driver whose SSH session (and OpenAPI session) is replaced by a fake."""

    def __init__(self, hostname, username, password, timeout=60, optional_args=None):
        super().__init__(hostname, username, password, timeout, optional_args)
        self.patched_attrs = ["device"]
        self.device = FakeFEDevice()
        if self.transport == "https":
            self.patched_attrs.append("api")
            self.api = FakeOpenApiSession(self.device)

    def open(self):
        pass

    def close(self):
        pass


class FakeOpenApiSession(BaseTestDouble):
    """OpenApiSession replacement: GET returns the JSON file of the request.

    The file name is the path (and query) with every character that is not a
    letter or digit replaced by ``_``, like the CLI files:
    ``/v0/state/forwarding/ip-arp?vr_name=TEST`` ->
    ``_v0_state_forwarding_ip_arp_vr_name_TEST.json``.

    CLI commands return the CLI output files.
    """

    token = "fake-token"

    def __init__(self, switch):
        super().__init__()
        self.switch = switch
        self.requests = []
        self.config = []
        self.saved = []

    def get(self, path, params=None):
        request = path + ("?" + urlencode(params) if params else "")
        self.requests.append(request)
        return self.read_json_file(self.find_file(self.sanitize_text(request) + ".json"))

    def cli(self, commands, continue_on_failure=True, timeout=None):
        """POST operation/system/cli: the CLI output files, like the SSH fake."""
        self.requests.extend(f"cli: {command}" for command in commands)
        if not continue_on_failure:  # configuration lines (_send_config)
            self.config.extend(commands)
            return ["" for _ in commands]
        return [self._output(command) for command in commands]

    def _output(self, command):
        return self.read_txt_file(self.find_file(self.sanitize_text(command) + ".txt"))

    def save_config(self, name=None):
        """POST operation/system/config/:save (no prompt, overwrites)."""
        self.saved.append(name)


class _FakeTransport:
    def is_active(self):
        return True


class _FakeRemoteConn:
    transport = _FakeTransport()


class FakeFEDevice(BaseTestDouble):
    """Netmiko replacement.

    ``show`` commands return the content of the mocked_data files.
    """

    remote_conn = _FakeRemoteConn()

    def __init__(self):
        super().__init__()
        self.sent_config = []
        self.commands = []

    def write_channel(self, data):
        pass

    def find_prompt(self, *args, **kwargs):
        return "5520-24T-FabricEngine:1#"

    def send_command(self, command, **kwargs):
        self.commands.append(command)
        filename = "{}.txt".format(self.sanitize_text(command))
        return self.read_txt_file(self.find_file(filename))

    def send_config_set(self, config_commands, **kwargs):
        self.sent_config.extend(config_commands)
        return "\n".join(config_commands)

    def exit_config_mode(self, *args, **kwargs):
        return ""

    def save_config(self, *args, **kwargs):
        self.commands.append("save config")
        return "save config\nSave config to file /intflash/config.cfg successful."
