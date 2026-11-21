"""Netmiko file transfer (SCP) support for Extreme Networks Fabric Engine.

Netmiko has no file transfer class for Fabric Engine yet. This one follows
Netmiko's ``BaseFileTransfer`` interface and naming so it can be contributed
upstream unchanged: Netmiko's device type for Fabric Engine / VOSS is
``extreme_vsp`` (``ExtremeVspSSH`` in ``netmiko/extreme/extreme_vsp_ssh.py``),
so the class is ``ExtremeVspFileTransfer``, to be registered in
``FILE_TRANSFER_MAP`` as ``"extreme_vsp"`` (like ``ExtremeExosFileTransfer``).

Fabric Engine differences to the base (Cisco) implementation:

* files are listed with ``dir <directory> -l`` (size, date, full path,
  ``<DIR>`` for directories); its last line holds the free space
  (``total: ... used: ... free: <n> bytes``)
* MD5 digests are calculated with ``file-checksum md5 <file>``
* the SCP server closes the channel without the final acknowledgement of an
  upload (Fabric Engine 9.3: ``No response from server``, OpenSSH:
  ``lost connection``) although the file is written completely; ``put_file``
  ignores this error, ``verify_file`` (size, MD5) decides
"""

import os
import re
from typing import Any, Dict, Optional

from netmiko.scp_handler import BaseFileTransfer
from scp import SCPException

DEFAULT_FILE_SYSTEM = "/intflash"
DIR_FREE_RE = re.compile(r"free:\s*(\d+)\s*bytes", re.I)
MD5_RE = re.compile(r"\b([0-9a-fA-F]{32})\b")

# "dir /intflash -l" (Fabric Engine 9.4):
#     9852    Nov-17-2026  16:28   /intflash/test1.cfg
#     4096    Nov-17-2026  16:41   /intflash/var            <DIR>
DIR_LINE_RE = re.compile(
    r"^\s*(?P<size>\d+)\s+\w{3}-\d{2}-\d{4}\s+\d{2}:\d{2}\s+(?P<path>/\S+)\s*(?P<dir><DIR>)?\s*$"
)


def parse_dir(output: str) -> Dict[str, Dict[str, Any]]:
    """Parse ``dir <directory> -l`` into ``{path: {"size": int, "is_dir": bool}}``."""
    entries: Dict[str, Dict[str, Any]] = {}
    for line in output.splitlines():
        match = DIR_LINE_RE.match(line)
        if match:
            entries[match.group("path")] = {
                "size": int(match.group("size")),
                "is_dir": bool(match.group("dir")),
            }
    return entries


def parse_dir_free(output: str) -> Optional[int]:
    """Free bytes from the last line of ``dir``, or None."""
    match = DIR_FREE_RE.search(output)
    return int(match.group(1)) if match else None


class ExtremeVspFileTransfer(BaseFileTransfer):
    """SCP file transfer for Fabric Engine (formerly VOSS / VSP)."""

    def __init__(
        self,
        ssh_conn: Any,
        source_file: str,
        dest_file: str,
        file_system: Optional[str] = DEFAULT_FILE_SYSTEM,
        direction: str = "put",
        hash_supported: bool = True,
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

    def _dir(self, directory: str) -> str:
        return self.ssh_ctl_chan._send_command_str(f"dir {directory} -l", read_timeout=60)

    def _remote_entry(self, remote_file: str) -> Optional[Dict[str, Any]]:
        path = f"{self.file_system.rstrip('/')}/{remote_file}"
        return parse_dir(self._dir(path.rsplit("/", 1)[0])).get(path)

    def remote_space_available(self, search_pattern: str = "") -> int:
        """Free bytes on the file system, from the last line of ``dir <fs> -l``.

        Fabric Engine 9.4: ``total: 6260543488  used: 202080256  free: 6058463232  bytes``
        """
        output = self._dir(self.file_system)
        match = re.search(search_pattern, output, re.I) if search_pattern else None
        free = int(match.group(1)) if match else parse_dir_free(output)
        if free is None:
            raise ValueError(f"Free space not found in the output of 'dir':\n{output[-500:]}")
        return free

    def verify_space_available(self, search_pattern: str = "") -> bool:
        """Whether the file fits.

        Like Netmiko's Cisco implementation, a ``ValueError`` is raised if the
        free space cannot be read, so nothing is uploaded blindly.
        """
        if self.direction == "get":
            return self.local_space_available() > self.file_size
        return self.remote_space_available(search_pattern) > self.file_size

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

    @staticmethod
    def process_md5(md5_output: str, pattern: str = "") -> str:
        """Extract the MD5 digest from the ``file-checksum md5`` output."""
        match = MD5_RE.search(md5_output)
        if not match:
            raise ValueError(f"Invalid output from MD5 command: {md5_output}")
        return match.group(1).lower()

    def remote_md5(
        self, base_cmd: str = "file-checksum md5", remote_file: Optional[str] = None
    ) -> str:
        """Calculate the MD5 digest of the remote file on the switch."""
        if remote_file is None:
            remote_file = self.dest_file if self.direction == "put" else self.source_file
        output = self.ssh_ctl_chan._send_command_str(
            f"{base_cmd} {self.file_system}/{remote_file}", read_timeout=300
        )
        return self.process_md5(output)

    def put_file(self) -> None:
        """SCP copy the file to the switch.

        The switch writes the file but closes the channel without the final
        acknowledgement (Fabric Engine 9.3), which the scp library
        reports as ``No response from server``. Only that error is ignored:
        whether the file arrived is checked by ``verify_file``.
        """
        destination = f"{self.file_system}/{self.dest_file}"
        try:
            self.scp_conn.scp_transfer_file(self.source_file, destination)
        except SCPException as exc:
            if "No response from server" not in str(exc):
                raise
        # Must close the SCP connection to get the file written (flush)
        self.scp_conn.close()

    def verify_file(self) -> bool:
        """Verify the transfer: same size, and same MD5 digest if enabled."""
        if self.direction == "put":
            try:
                if self.remote_file_size() != self.file_size:
                    return False
            except IOError:
                return False
        elif not (
            os.path.exists(self.dest_file) and os.stat(self.dest_file).st_size == self.file_size
        ):
            return False
        if self.source_md5 is None:  # hash_supported=False
            return True
        return self.compare_md5()

    def enable_scp(self, cmd: str = "") -> None:
        """SCP is served by the SSH server (``boot config flags sshd``)."""
        raise NotImplementedError("Enable SSH with 'boot config flags sshd' and 'ssh'")

    def disable_scp(self, cmd: str = "") -> None:
        """SCP is served by the SSH server (``boot config flags sshd``)."""
        raise NotImplementedError("SCP is part of the SSH server on Fabric Engine")
