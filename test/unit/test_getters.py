"""Run the standard NAPALM getter test-suite against mocked device output."""

import pytest
from napalm.base.test.getters import BaseTestGetters


@pytest.mark.usefixtures("set_device_parameters")
class TestGetter(BaseTestGetters):
    """Test get_* methods (SSH transport)."""


@pytest.mark.usefixtures("set_device_parameters")
class TestGetterOpenApi(BaseTestGetters):
    """Test get_* methods with transport="openapi" (cases ``*_openapi``)."""

    transport = "openapi"
