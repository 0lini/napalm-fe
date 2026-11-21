"""NAPALM community driver for Extreme Networks Fabric Engine (formerly VOSS)."""

from importlib.metadata import PackageNotFoundError, version

from napalm_fe.fe import FEDriver

try:
    __version__ = version("napalm-fe")
except PackageNotFoundError:  # pragma: no cover - not installed
    __version__ = "0.0.0"

__all__ = ("FEDriver", "__version__")
