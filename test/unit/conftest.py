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
    driver = request.cls.patched_driver if parent_conftest.NAPALM_TEST_MOCK else request.cls.driver
    request.cls.device = driver(
        parent_conftest.NAPALM_HOSTNAME,
        parent_conftest.NAPALM_USERNAME,
        parent_conftest.NAPALM_PASSWORD,
        timeout=60,
        optional_args=parent_conftest.NAPALM_OPTIONAL_ARGS,
    )
    request.cls.device.open()


def pytest_generate_tests(metafunc):
    """Generate test cases dynamically: every directory of the test's mocked data."""
    if not metafunc.function.__dict__.get("build_test_cases", False):
        return
    path = os.path.join(os.path.dirname(__file__), "mocked_data", metafunc.function.__name__)
    cases = sorted(
        case
        for case in (os.listdir(path) if os.path.isdir(path) else [])
        if os.path.isdir(os.path.join(path, case))
    )
    metafunc.parametrize("test_case", cases)


class PatchedFEDriver(fe.FEDriver):
    """Driver whose API session is replaced by a fake."""

    def __init__(self, hostname, username, password, timeout=60, optional_args=None):
        super().__init__(hostname, username, password, timeout, optional_args)
        self.patched_attrs = ["api"]
        self.api = FakeOpenApiSession()

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

    def __init__(self):
        super().__init__()
        self.requests = []
        self.config = []
        self.saved = []

    def get(self, path, params=None):
        request = path + ("?" + urlencode(params) if params else "")
        self.requests.append(request)
        return self.read_json_file(self.find_file(self.sanitize_text(request) + ".json"))

    def cli(self, commands, continue_on_failure=True, timeout=None):
        """POST operation/system/cli: the CLI output files."""
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
