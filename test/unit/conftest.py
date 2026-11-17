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


class _FakeTransport:
    def is_active(self):
        return True


class _FakeRemoteConn:
    transport = _FakeTransport()


class FakeExtremeFEDevice(BaseTestDouble):
    """Netmiko replacement.

    ``show`` commands return the content of the mocked_data files. File and
    configuration commands (``save config file``, ``ls``, ``copy``, ``source``,
    ``reset``) are simulated on an in-memory flash; their output follows real
    Fabric Engine 9.4 output where it is known.
    """

    remote_conn = _FakeRemoteConn()

    def __init__(self):
        super().__init__()
        self.sent_config = []
        self.commands = []
        self.flash = {"/intflash/config.cfg": "startup config"}
        self.rebooted = False
        self.scheduled_reset = None
        self._pending = None

    def write_channel(self, data):
        pass

    def send_command(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith("save config file "):
            path = command.split()[-1]
            self.flash[path] = "running config"
            return (
                f"CP-1: Save config to file {path} successful.\n"
                'WARNING: Choice Primary Node Config file is "/intflash/config.cfg".'
            )
        if command.startswith("ls "):
            path = command.split()[-1]
            directory, _, name = path.rpartition("/")
            if path not in self.flash:
                return f'Listing Directory {directory}:\nCan\'t open "{path}".'
            size = len(self.flash[path])
            return (
                f"Listing Directory {directory}:\n"
                f"-rwx------  1 0       0          {size:5d} Nov 16 12:24  {name}"
            )
        if command.startswith("source "):
            return ""
        filename = "{}.txt".format(self.sanitize_text(command))
        return self.read_txt_file(self.find_file(filename))

    def send_command_timing(self, command, **kwargs):
        self.commands.append(command)
        if command == "y" and self._pending:
            action, self._pending = self._pending, None
            return action()
        if command.startswith("copy "):
            _, src, dst = command.split()
            if dst not in self.flash:  # real switch: no output for a new file
                self.flash[dst] = self.flash[src]
                return ""

            def copy():
                self.flash[dst] = self.flash[src]
                return ""

            self._pending = copy
            return f"File [{dst}] already existing, overwrite (y/n) ? "
        if command == "reset":
            self._pending = None  # real prompt; the driver must not need it
            return "Are you sure you want to reset the switch (y/n) ? "
        if command == "reset -y":
            self.rebooted = True
            raise OSError("Socket is closed")
        if command.startswith("reset -in "):
            when = command.split()[-1]

            def schedule():
                self.scheduled_reset = when
                return ""  # real success output not seen yet

            self._pending = schedule
            return "Are you sure you want to perform a scheduled reset? (y/n) ? "
        if command == "reset -cancel":
            if self.scheduled_reset is None:
                return "Warning! No ongoing scheduled reset!"
            self.scheduled_reset = None
            return "PLACEHOLDER: real output of a successful cancel not seen yet"
        return ""

    def send_config_set(self, config_commands, **kwargs):
        self.sent_config.extend(config_commands)
        return "\n".join(config_commands)

    def exit_config_mode(self, *args, **kwargs):
        return ""

    def save_config(self, *args, **kwargs):
        self.commands.append("save config")
        return "save config\nSave config to file /intflash/config.cfg successful."
