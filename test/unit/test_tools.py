"""Tests for the helper scripts in tools/."""

import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "tools", f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def capture():
    return _load("capture")


def test_commands_to_capture(capture):
    commands = capture.commands_to_capture(vrf=None)
    assert "show sys-info" in commands
    assert "show interfaces gigabitEthernet name" in commands  # optional "?" command
    assert not any(c.startswith("?") for c in commands)
    assert not any("arp vrf" in c for c in commands)
    assert len(commands) == len(set(commands))
    assert all(c.split()[0] in ("show", "more", "ls", "file-checksum") for c in commands)
    assert "show ip arp vrf blue" in capture.commands_to_capture(vrf="blue")


def test_find_identifiers_and_replace(capture):
    outputs = {
        "show sys-info": "  SysName      : core-sw-1\n  Serial#      : AB1234567-C0001\n",
        "show running-config": (
            'ip domain-name "corp.example.org"\n#  CardSerial# : AB1234567-C0001'
        ),
    }
    replacements = capture.find_identifiers(outputs)
    assert replacements == {
        "core-sw-1": "fe-sw-01",
        "corp.example.org": "example.com",
        "AB1234567-C0001": "SN0000000000",
    }
    text = "core-sw-1 core-sw-10 core-sw-1.corp.example.org AB1234567-C0001"
    assert capture.apply_replacements(text, replacements) == (
        "fe-sw-01 core-sw-10 fe-sw-01.example.com SN0000000000"
    )


def test_ip_anonymizer(capture):
    anonymize = capture.ip_anonymizer()
    text = (
        "SysDescr : 5320 (9.4.2.0)\n"
        "Default Version : 3.4.2.11\n"
        "Vlan10 172.16.5.1 255.255.255.0 0.0.0.255\n"
        "ntp server 172.16.5.1\n"
        "ntp server 8.8.8.8\n"
        "default 0.0.0.0 224.0.0.5\n"
    )
    assert anonymize(text) == (
        "SysDescr : 5320 (9.4.2.0)\n"
        "Default Version : 3.4.2.11\n"
        "Vlan10 10.0.0.1 255.255.255.0 0.0.0.255\n"
        "ntp server 10.0.0.1\n"
        "ntp server 10.0.0.2\n"
        "default 0.0.0.0 224.0.0.5\n"
    )


def test_parse_replace(capture):
    assert capture.parse_replace(["a=b", "c=d=e"]) == {"a": "b", "c": "d=e"}
    with pytest.raises(SystemExit):
        capture.parse_replace(["nope"])


def test_smoke_test_importable():
    smoke = _load("smoke_test")
    assert {name for name, _, _ in smoke.GETTERS} >= {"get_facts", "get_interfaces"}
