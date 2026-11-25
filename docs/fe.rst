Extreme Fabric Engine
---------------------

Caveats of the ``fe`` community driver for Extreme Networks Fabric
Engine (formerly VOSS), in the format of NAPALM's driver caveats
(``docs/support/<driver>.rst``).


Minimum Version
~~~~~~~~~~~~~~~

Fabric Engine 9.4.2 (the OpenAPI needs no license from this release on).
Fabric Engine 9.3.1 and 9.4.0 also work with a Premier (Pilot / Extreme
Platform ONE) license; 9.3.0 refuses the OpenAPI and 9.1 / 9.2 do not have it.
Older VOSS releases are not supported.


Prerequisites
_____________

The driver uses the switch's API only, there is no SSH. Enable it with::

    application
    openapi local-mgmt enable

The server then listens on TCP 9443 (TLS 1.2, self-signed certificate by
default); it can take up to about 3 minutes until ``show application openapi``
shows ``Server State Local Port : Enabled``. The user needs a role that may
run the CLI commands (``rwa``).


Optional arguments
__________________

* :code:`port` - Port of the API (default: ``9443``).
* :code:`ssl_verify` - Check the API's TLS certificate: ``True`` or the path of a CA bundle (default: ``False``).
* :code:`save_config` - Run ``save config`` after a successful commit (default: ``True``).
* :code:`startup_config_file` - File returned as ``startup`` by ``get_config()`` (default: ``/intflash/config.cfg``).

The driver ``timeout`` (default 60 s) is the timeout of every request. The
session token (valid 24 hours) is requested on ``open()`` and renewed once
on a ``401``.


Data per getter
~~~~~~~~~~~~~~~

====================================== ==============================================================
Method                                 Source
====================================== ==============================================================
``get_facts``                          API ``state/system``, ``configuration/dns``, ``state/ports``
``get_environment``                    CLI ``show sys-info``, ``show khi performance cpu|memory``
``get_interfaces``                     CLI ``show interfaces gigabitEthernet``, ``show clock``
``get_interfaces_ip``                  CLI ``show ip interface vrfids 0-512``,
                                       ``show ipv6 address interface``
``get_interfaces_counters``            CLI ``show interfaces gigabitEthernet statistics|statistics
                                       verbose|error``
``get_lldp_neighbors[_detail]``        API ``state/lldp``
``get_arp_table``                      API ``state/forwarding/ip-arp``
``get_mac_address_table``              API ``state/forwarding/mac-fdb``
``get_vlans``                          API ``configuration/vlan``, ``state/vlan``
``get_ntp_servers``                    API ``configuration/ntp``
``get_snmp_information``               API ``configuration/snmp``, ``state/system``
``get_config``                         CLI ``show running-config [verbose]``, ``more <startup file>``
``cli``                                any command (``POST operation/system/cli``); CLI errors are
                                       returned as output
====================================== ==============================================================

The API has no CPU, memory, temperature or counter data, so those getters use
the CLI operation. Its output is the CLI's, without the ``Command Execution
Time`` banner; pipes (``| include``) are rejected.

``get_snmp_information`` returns the SNMPv1/v2c community strings in plain
text (the CLI never displays them), so treat its result as a secret. The API
lists a community once per VRF context (``c``, ``c::1``, ``c::512``).
``get_ntp_servers`` also lists the server the cloud agent sets, which the
running configuration does not show.


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
* Every API request starts in privileged EXEC mode. Configuration lines are
  sent as one request (``configure terminal`` ... ``end``) and the switch
  skips the commands after the first one it counts as failed.
* Commands that ask for a password (``username``, ``cli password``) must not
  be sent through the CLI operation: on 9.4 this crashed the switch's main
  process.
* Values the switch does not report are ``-1`` / ``-1.0`` / ``""``, never
  guessed (NAPALM convention), e.g. ``tx_errors`` in
  ``get_interfaces_counters``.
