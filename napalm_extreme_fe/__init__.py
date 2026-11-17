"""NAPALM community driver for Extreme Networks Fabric Engine (formerly VOSS)."""

from importlib.metadata import PackageNotFoundError, version

from napalm_extreme_fe.extreme_fe import ExtremeFEDriver

try:
    __version__ = version("napalm-extreme-fe")
except PackageNotFoundError:  # pragma: no cover - not installed
    __version__ = "0.0.0"

__all__ = ("ExtremeFEDriver", "__version__")
