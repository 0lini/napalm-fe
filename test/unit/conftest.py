"""Test fixtures for the NAPALM getter tests (mocked device)."""

import pytest
from napalm.base.test import conftest as parent_conftest
from napalm.base.test.double import BaseTestDouble

from napalm_extreme_fe import extreme_fe


@pytest.fixture(scope="class")
def set_device_parameters(request):
    """Set up the class."""

    def fin():
        request.cls.device.close()

    request.addfinalizer(fin)

    request.cls.driver = extreme_fe.ExtremeFEDriver
    request.cls.patched_driver = PatchedExtremeFEDriver
    request.cls.vendor = "extreme_fe"
    parent_conftest.set_device_parameters(request)


def pytest_generate_tests(metafunc):
    """Generate test cases dynamically."""
    parent_conftest.pytest_generate_tests(metafunc, __file__)


class PatchedExtremeFEDriver(extreme_fe.ExtremeFEDriver):
    """Driver whose SSH session is replaced by a fake device."""

    def __init__(self, hostname, username, password, timeout=60, optional_args=None):
        super().__init__(hostname, username, password, timeout, optional_args)
        self.patched_attrs = ["device"]
        self.device = FakeExtremeFEDevice()

    def open(self):
        pass

    def close(self):
        pass


class FakeExtremeFEDevice(BaseTestDouble):
    """Netmiko replacement returning the content of the mocked_data files."""

    def __init__(self):
        super().__init__()
        self.sent_config = []

    def is_alive(self):
        return True

    def send_command(self, command, **kwargs):
        filename = "{}.txt".format(self.sanitize_text(command))
        return self.read_txt_file(self.find_file(filename))

    def send_config_set(self, config_commands, **kwargs):
        self.sent_config.extend(config_commands)
        return "\n".join(config_commands)

    def save_config(self, *args, **kwargs):
        return "save config\nSave config to file /intflash/config.cfg successful."

    def exit_config_mode(self, *args, **kwargs):
        return ""
