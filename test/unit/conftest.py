"""Test fixtures for the NAPALM getter tests (mocked device)."""

import re

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
        self._pending = None

    def write_channel(self, data):
        pass

    def find_prompt(self, *args, **kwargs):
        return "5520-24T-FabricEngine:1#"

    def send_command(self, command, **kwargs):
        self.commands.append(command)
        if command == "y" and self._pending:
            action, self._pending = self._pending, None
            return action()
        if self._pending:  # real switch: any other answer aborts
            self._pending = None
            return ""
        if command.startswith("save config file "):
            path = command.split()[-1]

            def save():
                self.flash[path] = "running config"
                return (
                    f"CP-1: Save config to file {path} successful.\n"
                    'WARNING: Choice Primary Node Config file is "/intflash/config.cfg".'
                )

            if path not in self.flash:
                return save()
            self._pending = save
            question = f"File [{path}] already existing, overwrite (y/n) ? "  # real (9.3)
            # the driver must wait for it with expect_string, not for the prompt
            assert re.search(kwargs.get("expect_string", "^$"), question)
            return question
        if command.startswith("dir ") and command.endswith(" -l"):
            directory = command.split()[1].rstrip("/")
            lines = [
                "",
                "       -------- INTFLASH --------",
                "  size          date       time       name",
                "--------       ------     ------    --------",
            ]
            for path, content in sorted(self.flash.items()):
                if path.rsplit("/", 1)[0] == directory:
                    lines.append(f"{len(content):8d}    Nov-17-2026  16:28   {path}")
            lines.append(
                "total: 6260543488       used: 202092544         free: 6058450944        bytes"
            )
            return "\n".join(lines)
        if command.startswith("source "):
            return ""
        filename = "{}.txt".format(self.sanitize_text(command))
        return self.read_txt_file(self.find_file(filename))

    def send_command_timing(self, command, **kwargs):
        self.commands.append(command)
        if command == "y" and self._pending:
            action, self._pending = self._pending, None
            return action()
        if self._pending:  # real switch: any other answer aborts
            self._pending = None
            return ""
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
        if command.startswith("delete "):
            path = command.split()[1]

            def delete():
                self.flash.pop(path, None)
                return ""

            self._pending = delete
            return "Are you sure (y/n) ? "  # real prompt (9.4)
        if command == "reset":
            self._pending = None  # real prompt; the driver must not need it
            return "Are you sure you want to reset the switch (y/n) ? "
        if command == "reset -y":
            self.rebooted = True
            raise OSError("Socket is closed")
        return ""

    def send_config_set(self, config_commands, **kwargs):
        self.sent_config.extend(config_commands)
        return "\n".join(config_commands)

    def exit_config_mode(self, *args, **kwargs):
        return ""

    def save_config(self, *args, **kwargs):
        self.commands.append("save config")
        return "save config\nSave config to file /intflash/config.cfg successful."
