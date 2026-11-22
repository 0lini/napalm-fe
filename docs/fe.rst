Extreme Fabric Engine
---------------------

Caveats of the ``fe`` community driver for Extreme Networks Fabric
Engine (formerly VOSS), in the format of NAPALM's driver caveats
(``docs/support/<driver>.rst``).


Minimum Version
~~~~~~~~~~~~~~~

Fabric Engine 9.x.

Tested releases (GNS3 VMs of Extreme, 2026-11; hardware not tested):

=================== ======================== =========================
Release             NAPALM getter suite      config tests
=================== ======================== =========================
VOSS 8.10.1.0       pass [#v1]_              pass [#v1]_
Fabric Engine 9.1   pass                     pass
Fabric Engine 9.2   pass                     pass
Fabric Engine 9.3   pass                     pass
Fabric Engine 9.3.1 pass                     pass
Fabric Engine 9.4   pass                     pass
=================== ======================== =========================

.. [#v1] VOSS 8.10 only offers the SSH host key types ``ssh-rsa`` and
   ``ssh-dss``, which paramiko 4 and later reject (``Incompatible ssh peer
   (no acceptable host key)``); it works with paramiko 3.x. VOSS 8.x is end
   of life and not supported.


Prerequisites
_____________

Like NAPALM's IOS driver, the driver connects over SSH (Netmiko device type
``extreme_vsp``) and parses the CLI output. ``show running-config`` needs
privileged mode, which the driver enters after the login (``enable``).


Optional arguments
__________________

* :code:`save_config` - Run ``save config`` after a successful commit (default: ``True``).
* :code:`startup_config_file` - File returned as ``startup`` by ``get_config()`` (default: ``/intflash/config.cfg``).
* :code:`force_no_enable` - Do not enter privileged mode (``enable``). ``show running-config`` needs it (default: ``False``).

All Netmiko arguments (``port``, ``secret``, ``key_file``, ``use_keys``,
``ssh_config_file``, ``conn_timeout``, ...) are passed to the SSH connection.
The driver ``timeout`` (default 60 s) is also the read timeout of every
command.


Commands per getter
~~~~~~~~~~~~~~~~~~~

============================== ==========================================================
Method                         CLI commands
============================== ==========================================================
``get_facts``                  ``show sys-info``, ``show interfaces gigabitEthernet name``,
                               ``show ip dns``
``get_environment``            ``show sys-info``, ``show khi performance cpu|memory``
``get_interfaces``             ``show interfaces gigabitEthernet``
``get_interfaces_ip``          ``show ip interface vrfids 0-512``,
                               ``show ipv6 address interface``
``get_interfaces_counters``    ``show interfaces gigabitEthernet statistics|statistics
                               verbose|error``
``get_lldp_neighbors[_detail]`` ``show lldp neighbor [port <port>]``
``get_arp_table``              ``show ip arp [vrf <name>]``
``get_mac_address_table``      ``show vlan mac-address-entry``
``get_vlans``                  ``show vlan basic``, ``show vlan members``
``get_ntp_servers``            ``show ntp server``
``get_snmp_information``       ``show sys-info``
``get_config``                 ``show running-config [verbose]``, ``more <startup file>``
``cli``                        any command; CLI errors are returned as output
============================== ==========================================================

``get_snmp_information`` returns an empty ``community``: the CLI never
displays SNMPv1/v2c community strings (``show snmp-server community`` masks
them).


Notes on configuration merging
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``load_merge_candidate`` keeps the lines in memory; ``commit_config`` sends
them in configuration mode. The first line the switch rejects stops the merge
and raises ``MergeConfigException``; the lines before it stay applied.

A merge is **not atomic**. Fabric Engine can only roll back by rebooting, so
a failed merge is not rolled back automatically (like napalm-dellos10). The
configuration is saved (``save config``) after a successful merge unless
``save_config`` is ``False``. Replace, ``rollback()`` and commit confirm
(``revert_in``) are not supported.

Diffs
_____

Fabric Engine has no on-box diff and netutils has no parser for it, so the
driver compares offline. The diff only contains the lines that would change
something (like the IOS driver's incremental diff): ``+line`` is added,
``-line`` removed (``no ...``), context lines (``interface ...``) have no
prefix::

    interface gigabitEthernet 1/2
    +name "new port"
    exit


Known gotchas
_____________

* VRF names are stored in lower case (``ip vrf TEST`` becomes ``test``).
* Log messages printed into the session (``logging screen``) are removed
  from all output except ``show logging ...``.
* A new system name (``prompt "<name>"`` or ``sys name``) changes the SSH
  prompt; the driver updates Netmiko's base prompt after a commit (like
  NX-OS).
* Values the switch does not report are ``-1`` / ``-1.0`` / ``""``, never
  guessed (NAPALM convention), e.g. ``tx_errors`` in
  ``get_interfaces_counters``.
