Extreme Fabric Engine
---------------------

Caveats of the ``fe`` community driver for Extreme Networks Fabric
Engine (formerly VOSS), in the format of NAPALM's driver caveats
(``docs/support/<driver>.rst``).


Minimum Version
~~~~~~~~~~~~~~~

Fabric Engine 9.x with ``transport="ssh"`` (the default); the ``https``
transport needs Fabric Engine 9.3.1 or later.

Tested releases (GNS3 VMs of Extreme, 2026-11; hardware not tested):

================== ======================== ========================= ================================
Release            ``ssh``: NAPALM getters   ``ssh``: config tests     ``https`` (OpenAPI)
================== ======================== ========================= ================================
VOSS 8.10.1.0      pass [#v1]_              pass [#v1]_               no OpenAPI
Fabric Engine 9.1  pass                     pass                      no OpenAPI
Fabric Engine 9.2  pass                     pass                      no OpenAPI
Fabric Engine 9.3  pass                     pass                      refused (license of the image)
Fabric Engine 9.3.1 pass                    pass                      getters and config tests pass
Fabric Engine 9.4  pass                     pass                      all, including commit confirm
                                                                      and ``rollback()``
================== ======================== ========================= ================================

.. [#v1] VOSS 8.10 only offers the SSH host key types ``ssh-rsa`` and
   ``ssh-dss``, which paramiko 4 and later reject (``Incompatible ssh peer
   (no acceptable host key)``); it works with paramiko 3.x. VOSS 8.x is end
   of life and not supported.

Commit confirm, replace and ``rollback()`` were only run live on 9.4.


Prerequisites
_____________

The default transport ``ssh`` uses the CLI over SSH (Netmiko
``extreme_vsp``) and works on every release.

The ``https`` transport uses the switch's OpenAPI (REST/JSON). It needs
Fabric Engine 9.3.1 or later, a Premier (or Pilot / Extreme Platform ONE)
license and has to be enabled::

    configure terminal
    application
    openapi local-mgmt enable

The server listens on TCP 9443 (TLS, self-signed factory certificate) up to
about three minutes later (``show application openapi``). The driver logs in
with the given user and sends the token it gets (valid for 24 hours, renewed
when it expires).

Like NAPALM's NX-OS driver, uploading files needs SSH as well (see
`What needs SSH`_). For it the driver opens an SSH connection with the same
credentials on port 22 when it is first used. SCP needs
``boot config flags sshd`` and the SSH server.

The transport argument
______________________

=============== ======================================
_               fe
=============== ======================================
**Default**     ``ssh``
**Supported**   ``https``, ``ssh``
=============== ======================================

``port`` is the port of the selected transport: ``22`` for ``ssh``, ``9443``
for ``https`` by default. With ``https``, the additional SSH connection
always uses port 22 (like NX-OS).


Optional arguments
__________________

* :code:`transport` - Protocol to connect with, see `The transport argument`_ (default: ``ssh``).
* :code:`port` - Port of the transport (default: ``22`` for ``ssh``, ``9443`` for ``https``).
* :code:`ssl_verify` - Check the API's TLS certificate: ``True``, ``False`` or the path of a CA bundle, like the NX-OS driver (default: ``False``).
* :code:`save_config` - Run ``save config`` after a successful commit (default: ``True``).
* :code:`startup_config_file` - File returned as ``startup`` by ``get_config()`` (default: ``/intflash/config.cfg``).
* :code:`force_no_enable` - SSH only: do not enter privileged mode (``enable``). ``show running-config`` needs it (default: ``False``).
* :code:`config_method` - How a merge is applied: ``cli`` (configuration lines) or ``file`` (SCP upload and ``source``, like IOS) (default: ``cli``).
* :code:`allow_reboot` - Allow replace commits, ``rollback()`` and ``commit_config(revert_in=...)``, which reboot the switch (default: ``False``).
* :code:`dest_file_system` - Where the driver stores its files on the switch (default: ``/intflash``).
* :code:`candidate_cfg` - Filename of the replace candidate (default: ``napalm_candidate.cfg``).
* :code:`merge_cfg` - Filename of the merge candidate for ``config_method="file"`` (default: ``napalm_merge.cfg``).
* :code:`rollback_cfg` - Filename of the configuration saved before every commit (default: ``napalm_rollback.cfg``).

All Netmiko arguments (``secret``, ``key_file``, ``use_keys``,
``ssh_config_file``, ``conn_timeout``, ...) are passed to the SSH connection.
The driver ``timeout`` (default 60 s) is also the read timeout of every
command and API request.


Where the data comes from
~~~~~~~~~~~~~~~~~~~~~~~~~

With ``https``, getters whose data the API has completely read it from the
API's JSON; all others run the same CLI commands as the ``ssh`` transport
through the API's CLI operation (``POST /rest/openapi/v0/operation/system/cli``,
the same output as over SSH), so reading the switch never needs SSH. A
getter's result always comes from one source.

============================== ========================================================= ==========================================================
Method                         ``https``                                                 ``ssh`` / CLI commands
============================== ========================================================= ==========================================================
``get_facts``                  ``v0/state/system``, ``v0/configuration/dns``,            ``show sys-info``, ``show interfaces gigabitEthernet``,
                               ``v1/state/ports``                                        ``show running-config | include domain-name``
``get_environment``            CLI through the API                                       ``show sys-info``, ``show khi performance cpu|memory``
``get_interfaces``             CLI through the API (and ``show clock``)                  ``show interfaces gigabitEthernet``
``get_interfaces_ip``          CLI through the API                                       ``show ip interface vrfids 0-512``,
                                                                                         ``show ipv6 address interface``
``get_interfaces_counters``    CLI through the API                                       ``show interfaces gigabitEthernet statistics|statistics
                                                                                         verbose|error``
``get_lldp_neighbors[_detail]`` ``v0/state/lldp``                                        ``show lldp neighbor [port <port>]``
``get_arp_table``              ``v0/state/forwarding/ip-arp``                            ``show ip arp [vrf <name>]``
``get_mac_address_table``      ``v0/state/forwarding/mac-fdb``                           ``show vlan mac-address-entry``
``get_vlans``                  ``v0/configuration/vlan``, ``v1/state/vlan``              ``show vlan basic``, ``show vlan members``
``get_ntp_servers``            ``v0/configuration/ntp``                                  ``show running-config``
``get_snmp_information``       ``v0/configuration/snmp``, ``v0/state/system``            ``show sys-info``
``get_config``                 CLI through the API                                       ``show running-config [verbose]``, ``more <startup file>``
``cli``                        CLI through the API                                       any command
============================== ========================================================= ==========================================================

Differences between the transports (Fabric Engine 9.4):

* ``get_snmp_information``: the CLI never displays SNMPv1/v2c community
  strings (``show snmp-server community`` masks them), so ``community`` is
  empty over SSH. The API returns them in plain text (an entry with a write
  view is ``rw``).
* ``get_vlans``: the API also lists the internal VLANs 4092 (``Mgmt``) and
  4094 (``Null``, the unassigned ports), which ``show vlan basic`` hides.
* ``get_ntp_servers``: the API also lists a server set by the cloud agent
  (ExtremeCloud IQ), which the running configuration does not show.


What needs SSH
______________

With ``https`` only uploading a file needs SSH: the API has no file
transfer, so ``load_merge_candidate`` with ``config_method="file"`` and
``load_replace_candidate`` copy the candidate with SCP. Everything else goes
through the API, verified on Fabric Engine 9.4:

* configuration lines of a merge: the CLI operation with ``configure
  terminal``, the lines and ``end`` (each request starts in privileged EXEC
  mode); without ``continue_on_failure`` the switch skips the lines after
  the first failed one
* ``save config`` and saving ``rollback_cfg``:
  ``POST v0/operation/system/config/:save`` (overwrites without asking)
* ``source <file> debug`` (``config_method="file"``)
* replace and ``rollback()``: ``copy`` and ``reset -y`` through the CLI
  operation; it confirms (y/n) questions itself (``copy`` overwrites the boot
  configuration). Tested: ``rollback()`` rebooted into ``rollback_cfg``
* ``delete`` of the replace candidate (``discard_config``)
* commit confirm: ``reset -in`` and ``reset -cancel`` through the CLI
  operation (``reboot/:schedule`` is not used: it only accepts 10 to 360
  minutes)
* ``dir`` and the switch log for ``has_pending_commit``

As the SSH connection uses port 22 (like NX-OS), uploads do not work with
``https`` if SSH is only reachable on another port (port forwarding / NAT);
use ``transport="ssh"`` then.


Differences of the API's CLI operation
______________________________________

* Pipes are rejected: ``cli(["show running-config | include location"])``
  returns ``% Invalid input detected``.
* ``source <file> debug`` stops at the first rejected line; over SSH the
  following lines are still applied.
* Questions (y/n) are answered by the switch itself, ``copy`` onto an
  existing file only echoes ``File [...] already existing,``.
* The switch counts any output containing "Error" as failed. In a merge, a
  line that prints such a text stops the merge (the following lines are
  skipped) even if it was accepted.

The API has more that the driver does not use yet:
``operation/system/config/recovery`` (``:timeout``, ``:cancel``,
``undo-list``: automatic undo of changes made through the API's
configuration endpoints, not of CLI lines), ``operation/system/ping`` and
``traceroute``, ``state/forwarding/ipv6-nd``, ``state/transceiver/{port}``,
``configuration/accounts`` and ``state/route``.


Notes on configuration merging
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The running configuration is saved to ``rollback_cfg`` before every commit
(like IOS). Then:

* ``config_method="cli"`` (default): the lines are sent in configuration
  mode. The first line the switch rejects stops the merge and raises
  ``MergeConfigException``; the lines before it stay applied.
* ``config_method="file"``: the candidate is copied to the switch with SCP
  on load (verified with ``file-checksum md5``) and applied with
  ``source <file> debug`` (without ``debug`` the switch does not report
  rejected lines). A rejected line raises ``MergeConfigException``; unlike
  ``cli``, the lines after it are still applied over SSH (through the API,
  ``source`` stops at the rejected line).

A merge is **not atomic**. Fabric Engine can only roll back by rebooting, so
a failed merge is not rolled back automatically (like napalm-dellos10); the
error names ``rollback_cfg`` and how to restore it. The configuration is
saved (``save config``) after a successful merge unless ``save_config`` is
``False``.

Diffs
_____

Fabric Engine has no on-box diff and netutils has no parser for it, so the
driver compares offline. For a merge, the diff only contains the lines that
would change something (like the IOS driver's incremental diff): ``+line``
is added, ``-line`` removed (``no ...``), context lines (``interface ...``)
have no prefix::

    interface gigabitEthernet 1/2
    +name "new port"
    exit

For a replace, every line that differs between the running configuration and
the candidate is listed with ``-`` / ``+``.


Notes on configuration replacement
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Fabric Engine has no ``configure replace``. Like ExtremeCloud IQ Site
Engine's configuration restore, ``commit_config()`` copies the candidate over
the boot configuration (``startup_config_file``) and reboots the switch. This
requires ``allow_reboot=True``; otherwise ``ReplaceConfigException`` is
raised. The driver closes the connection; call ``open()`` again once the
switch is back.


Rollback
~~~~~~~~

``rollback()`` installs ``rollback_cfg`` as the boot configuration and
reboots (requires ``allow_reboot=True``). While a ``revert_in`` commit is
pending, it reverts that commit now (``reset -y``), like IOS
``configure revert now``.


Commit confirm
~~~~~~~~~~~~~~

``commit_config(revert_in=<seconds>)`` (merge only, requires
``allow_reboot=True``) works like ExtremeCloud IQ Site Engine:

1. the current configuration is saved (``save config``) - this also saves
   earlier unsaved changes
2. a reset is scheduled with ``reset -in`` (rounded down to whole minutes,
   like IOS; 1 minute to 9 days 23:59)
3. the change is applied **without** saving

``confirm_commit()`` cancels the reset (``reset -cancel``) and saves. If it
is not called in time, the switch reboots into the configuration saved in
step 1. ``has_pending_commit()`` reads the switch log (no command shows a
scheduled reset), so it also finds a commit made by another session.


Available configuration templates
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``load_template()`` (NAPALM base method) renders a template from
``napalm_fe/templates`` and loads it as a merge candidate; commit it with
``commit_config()``. Same names and variables as NAPALM's IOS / NX-OS / EOS
templates, verified on Fabric Engine 9.4:

* :code:`set_hostname` (``hostname``) - ``prompt "<hostname>"``: sets the
  system name; the running configuration shows it as ``prompt``, so
  ``compare_config()`` recognises an unchanged hostname. After the commit the
  driver updates Netmiko's base prompt (like NX-OS).
* :code:`set_ntp_servers` / :code:`delete_ntp_servers` (``servers``) -
  ``[no] ntp server <server>``.
* :code:`snmp_config` (``location``, ``contact``, ``community``) - a community
  with ``mode: rw`` is added to the read-write group ``v1v2grp``, all others
  to the read-only group ``readgrp``; its index and security name are the
  community string. ``chassis_id`` is ignored (not settable on Fabric
  Engine).
* :code:`delete_snmp_config` (same variables) - ``no snmp-server location``,
  ``no snmp-server contact``, ``no snmp-server community <community>``.

There are no NTP peer, probe or user templates (no NTP peers on Fabric
Engine; users come with ``get_users``).


Known gotchas
_____________

* VRF names are stored in lower case (``ip vrf TEST`` becomes ``test``);
  ``get_arp_table(vrf=...)`` ignores the case.
* Log messages printed into the session (``logging screen``) are removed
  from all output except ``show logging ...``.
* ``config_method="file"`` and replace leave their files in
  ``dest_file_system``.
* Values the switch does not report are ``-1`` / ``-1.0`` / ``""``, never
  guessed (NAPALM convention), e.g. ``tx_errors`` in
  ``get_interfaces_counters``.
