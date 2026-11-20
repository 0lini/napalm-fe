"""Run the standard NAPALM getter test-suite against mocked device output."""

import pytest
from napalm.base.test.getters import BaseTestGetters


@pytest.mark.usefixtures("set_device_parameters")
class TestGetter(BaseTestGetters):
    """Test get_* methods with transport="ssh"."""

    transport = "ssh"


@pytest.mark.usefixtures("set_device_parameters")
class TestGetterHttps(BaseTestGetters):
    """Test get_* methods with transport="https" (cases ``*_openapi``)."""

    transport = "https"
