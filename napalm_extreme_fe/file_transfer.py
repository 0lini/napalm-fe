"""Netmiko file transfer (SCP) support for Extreme Networks Fabric Engine.

Netmiko has no file transfer class for Fabric Engine yet. This one follows
Netmiko's ``BaseFileTransfer`` interface so it can be contributed upstream
unchanged (registered as ``extreme_fe`` / ``extreme_voss`` / ``extreme_vsp``).

Fabric Engine differences to the base (Cisco) implementation:

* files are listed with ``ls <path>`` (Unix ``ls -l`` style output)
* there is no on-box MD5 command, so files are verified by size
* ``ls`` does not report free space, so the space check is skipped
"""

import os
import re
from typing import Any, Dict, Optional

from netmiko.scp_handler import BaseFileTransfer

DEFAULT_FILE_SYSTEM = "/intflash"

# -rwx------  1 0       0          14385 Nov 16 12:24  config.cfg
LS_LINE_RE = re.compile(
    r"^(?P<perms>[-dlrwxsStT]{10})\s+\d+\s+\S+\s+\S+\s+(?P<size>\d+)\s+"
    r"\w{3}\s+\d+\s+[\d:]+\s+(?P<name>\S+?)/?\s*$"
)


def parse_ls(output: str) -> Dict[str, Dict[str, Any]]:
    """Parse ``ls`` output into ``{name: {"size": int, "is_dir": bool}}``."""
    entries: Dict[str, Dict[str, Any]] = {}
    for line in output.splitlines():
        match = LS_LINE_RE.match(line.strip())
        if not match or match.group("name") in (".", ".."):
            continue
        entries[match.group("name")] = {
            "size": int(match.group("size")),
            "is_dir": match.group("perms").startswith("d"),
        }
    return entries


class ExtremeFEFileTransfer(BaseFileTransfer):
    """SCP file transfer for Fabric Engine (formerly VOSS / VSP)."""

    def __init__(
        self,
        ssh_conn: Any,
        source_file: str,
        dest_file: str,
        file_system: Optional[str] = DEFAULT_FILE_SYSTEM,
        direction: str = "put",
        hash_supported: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            ssh_conn=ssh_conn,
            source_file=source_file,
            dest_file=dest_file,
            file_system=file_system or DEFAULT_FILE_SYSTEM,
            direction=direction,
            hash_supported=hash_supported,
            **kwargs,
        )

    def _remote_entry(self, remote_file: str) -> Optional[Dict[str, Any]]:
        output = self.ssh_ctl_chan._send_command_str(f"ls {self.file_system}/{remote_file}")
        name = remote_file.rsplit("/", 1)[-1]
        return parse_ls(output).get(name)

    def remote_space_available(self, search_pattern: str = "") -> int:
        """Fabric Engine's ``ls`` does not report free space."""
        raise NotImplementedError("Free space is not reported by Fabric Engine")

    def verify_space_available(self, search_pattern: str = "") -> bool:
        """Free space cannot be determined; assume it is available for 'put'."""
        if self.direction == "get":
            return self.local_space_available() > self.file_size
        return True

    def check_file_exists(self, remote_cmd: str = "") -> bool:
        """Check whether the destination file exists."""
        if self.direction == "put":
            entry = self._remote_entry(self.dest_file)
            return entry is not None and not entry["is_dir"]
        return super().check_file_exists(remote_cmd)

    def remote_file_size(self, remote_cmd: str = "", remote_file: Optional[str] = None) -> int:
        """Return the size of the remote file in bytes."""
        if remote_file is None:
            remote_file = self.dest_file if self.direction == "put" else self.source_file
        entry = self._remote_entry(remote_file)
        if entry is None:
            raise IOError(f"Unable to find file on remote system: {remote_file}")
        return int(entry["size"])

    def remote_md5(self, base_cmd: str = "", remote_file: Optional[str] = None) -> str:
        """Fabric Engine has no on-box MD5 command."""
        raise NotImplementedError("MD5 is not supported on Fabric Engine")

    def compare_md5(self) -> bool:
        """No MD5 on Fabric Engine: compare the file sizes instead."""
        return self.verify_file()

    def verify_file(self) -> bool:
        """Verify the transfer by comparing local and remote file sizes."""
        if self.direction == "put":
            try:
                return self.remote_file_size() == self.file_size
            except IOError:
                return False
        return os.path.exists(self.dest_file) and os.stat(self.dest_file).st_size == self.file_size

    def enable_scp(self, cmd: str = "") -> None:
        """SCP is served by the SSH server (``boot config flags sshd``)."""
        raise NotImplementedError("Enable SSH with 'boot config flags sshd' and 'ssh'")

    def disable_scp(self, cmd: str = "") -> None:
        """SCP is served by the SSH server (``boot config flags sshd``)."""
        raise NotImplementedError("SCP is part of the SSH server on Fabric Engine")
