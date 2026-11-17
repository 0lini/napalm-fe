"""Tests for the configuration management workflow."""

import pytest
from napalm.base.exceptions import CommitError, MergeConfigException, ReplaceConfigException

from .conftest import PatchedExtremeFEDriver


@pytest.fixture
def driver():
    return PatchedExtremeFEDriver("host", "user", "pass")


CONFIG = """
# comment
interface gigabitEthernet 1/2
name "new port"
exit
"""


def test_merge_compare_commit(driver):
    driver.load_merge_candidate(config=CONFIG)
    assert driver.compare_config() == ('+interface gigabitEthernet 1/2\n+name "new port"\n+exit')
    driver.commit_config()
    assert driver.device.sent_config == [
        "interface gigabitEthernet 1/2",
        'name "new port"',
        "exit",
    ]
    assert driver.compare_config() == ""


def test_merge_from_file(driver, tmp_path):
    path = tmp_path / "cfg.txt"
    path.write_text("ntp server 10.0.0.1\n")
    driver.load_merge_candidate(filename=str(path))
    assert driver.compare_config() == "+ntp server 10.0.0.1"


def test_merge_missing_file(driver):
    with pytest.raises(MergeConfigException):
        driver.load_merge_candidate(filename="/does/not/exist")


def test_discard(driver):
    driver.load_merge_candidate(config="ntp server 10.0.0.1")
    driver.discard_config()
    assert driver.compare_config() == ""


def test_commit_without_candidate(driver):
    with pytest.raises(MergeConfigException):
        driver.commit_config()


def test_commit_error(driver):
    driver.device.send_config_set = lambda lines, **kw: (
        "foo\n% Invalid input detected at '^' marker."
    )
    driver.load_merge_candidate(config="foo")
    with pytest.raises(CommitError):
        driver.commit_config()


def test_commit_no_save(driver):
    driver.save_on_commit = False
    driver.device.save_config = lambda *a, **kw: pytest.fail("save_config must not be called")
    driver.load_merge_candidate(config="ntp server 10.0.0.1")
    driver.commit_config()


def test_replace_not_supported(driver):
    with pytest.raises(ReplaceConfigException):
        driver.load_replace_candidate(config="foo")
