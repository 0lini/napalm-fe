"""Run the standard NAPALM getter test-suite against mocked API responses."""

import pytest
from napalm.base.test.getters import BaseTestGetters


@pytest.mark.usefixtures("set_device_parameters")
class TestGetter(BaseTestGetters):
    """Test get_* methods (mocked JSON responses and CLI output of the API)."""
