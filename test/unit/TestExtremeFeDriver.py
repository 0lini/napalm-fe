"""Configuration tests against a live Fabric Engine switch.

Modelled on NAPALM's ``test/ios/TestIOSDriver.py``. The file name does not
start with ``test_``, so the normal test run (and CI) does not collect it.
Run it explicitly against a LAB switch::

    NAPALM_HOSTNAME=<ip> NAPALM_USERNAME=<user> NAPALM_PASSWORD=<password> \\
    NAPALM_OPTIONAL_ARGS='{"port": 22}' pytest test/unit/TestExtremeFeDriver.py

The tests only change ``snmp-server location`` and restore the original value
afterwards. ``save_config`` is disabled, so nothing is written to the startup
configuration. ``config_method="file"`` leaves ``napalm_merge.cfg`` and
``napalm_rollback.cfg`` in ``/intflash``.
"""

import json
import os
import unittest

from napalm.base import exceptions

from napalm_extreme_fe import ExtremeFeDriver

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extreme_fe")


def read_file(name):
    with open(os.path.join(FIXTURES, name)) as handle:
        return handle.read().strip()


class _ConfigTests:
    """Merge tests, run once per config_method."""

    config_method = "cli"
    device: ExtremeFeDriver
    original_location: str

    @classmethod
    def setUpClass(cls):
        hostname = os.environ.get("NAPALM_HOSTNAME")
        if not hostname:
            raise unittest.SkipTest("NAPALM_HOSTNAME is not set (live switch tests)")
        optional_args = json.loads(os.environ.get("NAPALM_OPTIONAL_ARGS", "{}"))
        optional_args.update({"config_method": cls.config_method, "save_config": False})
        cls.device = ExtremeFeDriver(
            hostname,
            os.environ["NAPALM_USERNAME"],
            os.environ["NAPALM_PASSWORD"],
            optional_args=optional_args,
        )
        cls.device.open()
        cls.original_location = cls.device.get_snmp_information()["location"]

    @classmethod
    def tearDownClass(cls):
        cls.restore()
        cls.device.close()

    @classmethod
    def restore(cls):
        """Put the original SNMP location back."""
        cls.device.discard_config()
        if cls.original_location:
            restore = f'snmp-server location "{cls.original_location}"'
        else:
            restore = "no snmp-server location"
        cls.device.load_merge_candidate(config=restore)
        cls.device.commit_config()

    def test_merge_configuration(self):
        intended_diff = read_file("merge_good.diff")

        self.device.load_merge_candidate(filename=os.path.join(FIXTURES, "merge_good.conf"))
        diff = self.device.compare_config()
        self.device.commit_config()
        location = self.device.get_snmp_information()["location"]

        # Loading the same candidate again: nothing left to change
        self.device.load_merge_candidate(filename=os.path.join(FIXTURES, "merge_good.conf"))
        diff_after_commit = self.device.compare_config()
        self.restore()

        self.assertEqual(diff, intended_diff)
        self.assertEqual(location, "napalm-test")
        self.assertEqual(diff_after_commit, "")

    def test_merge_configuration_typo(self):
        self.device.load_merge_candidate(filename=os.path.join(FIXTURES, "merge_typo.conf"))
        try:
            with self.assertRaises(exceptions.MergeConfigException):
                self.device.commit_config()
        finally:
            self.restore()

    def test_merge_and_discard(self):
        self.device.load_merge_candidate(filename=os.path.join(FIXTURES, "merge_good.conf"))
        self.assertNotEqual(self.device.compare_config(), "")
        self.device.discard_config()
        self.assertEqual(self.device.compare_config(), "")
        self.assertEqual(self.device.get_snmp_information()["location"], self.original_location)


class TestConfigCLI(_ConfigTests, unittest.TestCase):
    """Merge by sending the lines in configuration mode."""

    config_method = "cli"


class TestConfigFile(_ConfigTests, unittest.TestCase):
    """Merge by SCP upload, restore point and 'source' (IOS style)."""

    config_method = "file"
