"""Test fixtures for the NAPALM getter tests (mocked device)."""

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
    parent_conftest.set_device_parameters(request)


def pytest_generate_tests(metafunc):
    """Generate test cases dynamically (one per directory in mocked_data)."""
    parent_conftest.pytest_generate_tests(metafunc, __file__)


class PatchedFEDriver(fe.FEDriver):
    """Driver whose SSH session is replaced by a fake."""

    def __init__(self, hostname, username, password, timeout=60, optional_args=None):
        super().__init__(hostname, username, password, timeout, optional_args)
        self.patched_attrs = ["device"]
        self.device = FakeFEDevice()

    def open(self):
        pass

    def close(self):
        pass


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
        self.flash = {"/intflash/config.cfg": "startup config"}

    def write_channel(self, data):
        pass

    def find_prompt(self, *args, **kwargs):
        return "5520-24T-FabricEngine:1#"

    def send_command(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith("source "):
            return ""
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
