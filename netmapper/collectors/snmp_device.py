"""Read one managed switch or router over SNMP and turn its tables into records.

Everything here is a read of standard MIBs, the same queries any network
monitoring system makes:

* SNMPv2-MIB / IF-MIB: name, description, interface names
* LLDP-MIB and CISCO-CDP-MIB: which devices are plugged into which port
* BRIDGE-MIB / Q-BRIDGE-MIB: the MAC addresses learned on each port
* IP-MIB: the device's ARP table (MAC to IP), mainly from routers and L3 switches.
  The older ipNetToMediaTable is read first; newer devices that only fill in
  ipNetToPhysicalTable are read from that instead.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable

from ..config import SnmpCredential
from ..models import ArpEntry, FdbEntry, Interface, Neighbor, SnmpDevice
from ..util import is_unicast_mac, normalize_mac, octets_to_ipv4, octets_to_text
from .snmp_client import Snmp, SnmpError

log = logging.getLogger(__name__)

SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"

IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
IF_PHYS_ADDRESS = "1.3.6.1.2.1.2.2.1.6"
IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"

DOT1D_BASE_PORT_IFINDEX = "1.3.6.1.2.1.17.1.4.1.2"
DOT1D_FDB_PORT = "1.3.6.1.2.1.17.4.3.1.2"
DOT1D_FDB_STATUS = "1.3.6.1.2.1.17.4.3.1.3"
DOT1Q_FDB_PORT = "1.3.6.1.2.1.17.7.1.2.2.1.2"
DOT1Q_FDB_STATUS = "1.3.6.1.2.1.17.7.1.2.2.1.3"
FDB_STATUS_SELF = 4  # the switch's own MAC, not a connected device

IP_NET_TO_MEDIA_PHYS = "1.3.6.1.2.1.4.22.1.2"
IP_NET_TO_PHYSICAL_PHYS = "1.3.6.1.2.1.4.35.1.4"

LLDP_LOC_CHASSIS_ID = "1.0.8802.1.1.2.1.3.2.0"
LLDP_LOC_PORT_ID = "1.0.8802.1.1.2.1.3.7.1.3"
LLDP_LOC_PORT_DESC = "1.0.8802.1.1.2.1.3.7.1.4"
LLDP_REM = "1.0.8802.1.1.2.1.4.1.1"
LLDP_REM_CHASSIS_ID = 5
LLDP_REM_PORT_ID = 7
LLDP_REM_PORT_DESC = 8
LLDP_REM_SYS_NAME = 9
LLDP_REM_SYS_DESC = 10
LLDP_REM_CAP_ENABLED = 12
LLDP_REM_MAN_ADDR_IF_SUBTYPE = "1.0.8802.1.1.2.1.4.2.1.3"

CDP_CACHE = "1.3.6.1.4.1.9.9.23.1.2.1.1"
CDP_ADDRESS = 4
CDP_DEVICE_ID = 6
CDP_DEVICE_PORT = 7
CDP_PLATFORM = 8
CDP_CAPABILITIES = 9

# LLDP capability BITS, most significant bit of the first byte is bit 0.
LLDP_CAPS = ["other", "repeater", "bridge", "wlan-ap", "router", "telephone", "docsis", "station"]
CDP_CAPS = {0x01: "router", 0x02: "bridge", 0x04: "bridge", 0x08: "switch", 0x10: "host",
            0x40: "repeater", 0x80: "telephone"}


def _safe_walk(snmp: Snmp, oid: str, device: SnmpDevice):
    try:
        return snmp.walk(oid)
    except SnmpError as exc:
        device.errors.append(f"walk {oid}: {exc}")
        return []


def _lldp_caps(value) -> list[str]:
    if not isinstance(value, (bytes, bytearray)) or not value:
        return []
    return [name for i, name in enumerate(LLDP_CAPS) if value[0] & (0x80 >> i)]


def _cdp_caps(value) -> list[str]:
    if isinstance(value, (bytes, bytearray)):
        value = int.from_bytes(value, "big") if value else 0
    if not isinstance(value, int):
        return []
    return sorted({name for bit, name in CDP_CAPS.items() if value & bit})


def _port_label(text: str | None) -> str | None:
    """Port IDs are sometimes a MAC; prefer something a person would recognise."""
    if text and not normalize_mac(text):
        return text
    return None


def collect_interfaces(snmp: Snmp, device: SnmpDevice) -> dict[int, str]:
    names = {idx[0]: octets_to_text(v) for idx, v in _safe_walk(snmp, IF_NAME, device)}
    if not names:
        names = {idx[0]: octets_to_text(v) for idx, v in _safe_walk(snmp, IF_DESCR, device)}
    macs = {idx[0]: normalize_mac(v) for idx, v in _safe_walk(snmp, IF_PHYS_ADDRESS, device)}
    device.interfaces = [
        Interface(if_index=i, name=names[i] or str(i), mac=macs.get(i)) for i in sorted(names)
    ]
    return {i: (n or str(i)) for i, n in names.items()}


def collect_lldp(snmp: Snmp, device: SnmpDevice, if_names: dict[int, str]) -> None:
    loc_ids = {idx[0]: octets_to_text(v) for idx, v in _safe_walk(snmp, LLDP_LOC_PORT_ID, device)}
    loc_desc = {idx[0]: octets_to_text(v) for idx, v in _safe_walk(snmp, LLDP_LOC_PORT_DESC, device)}

    def local_name(port_num: int) -> str:
        return (_port_label(loc_ids.get(port_num)) or loc_desc.get(port_num)
                or if_names.get(port_num) or str(port_num))

    # Index is (timeMark, localPortNum, remIndex, ...). timeMark is ignored.
    rows: dict[tuple[int, int], dict[int, object]] = defaultdict(dict)
    for idx, value in _safe_walk(snmp, LLDP_REM, device):
        column, key = idx[0], (idx[2], idx[3])
        rows[key][column] = value

    mgmt: dict[tuple[int, int], str] = {}
    for idx, _ in _safe_walk(snmp, LLDP_REM_MAN_ADDR_IF_SUBTYPE, device):
        # (timeMark, localPort, remIndex, addrSubtype, addrLen, addr bytes...)
        if len(idx) >= 9 and idx[3] == 1 and idx[4] == 4:
            mgmt.setdefault((idx[1], idx[2]), ".".join(str(b) for b in idx[5:9]))

    for (port_num, rem_index), cols in sorted(rows.items()):
        device.neighbors.append(Neighbor(
            protocol="lldp",
            local_port=local_name(port_num),
            remote_name=octets_to_text(cols.get(LLDP_REM_SYS_NAME)) or None,
            remote_port=(_port_label(octets_to_text(cols.get(LLDP_REM_PORT_ID)))
                         or octets_to_text(cols.get(LLDP_REM_PORT_DESC))
                         or octets_to_text(cols.get(LLDP_REM_PORT_ID))),
            remote_chassis_id=octets_to_text(cols.get(LLDP_REM_CHASSIS_ID)),
            remote_mgmt_ip=mgmt.get((port_num, rem_index)),
            remote_description=octets_to_text(cols.get(LLDP_REM_SYS_DESC)),
            remote_capabilities=_lldp_caps(cols.get(LLDP_REM_CAP_ENABLED)),
        ))


def collect_cdp(snmp: Snmp, device: SnmpDevice, if_names: dict[int, str]) -> None:
    rows: dict[tuple[int, int], dict[int, object]] = defaultdict(dict)
    for idx, value in _safe_walk(snmp, CDP_CACHE, device):
        column, key = idx[0], (idx[1], idx[2])  # index is (ifIndex, deviceIndex)
        rows[key][column] = value
    for (if_index, _), cols in sorted(rows.items()):
        device.neighbors.append(Neighbor(
            protocol="cdp",
            local_port=if_names.get(if_index, str(if_index)),
            remote_name=octets_to_text(cols.get(CDP_DEVICE_ID)),
            remote_port=octets_to_text(cols.get(CDP_DEVICE_PORT)),
            remote_mgmt_ip=octets_to_ipv4(cols.get(CDP_ADDRESS)),
            remote_description=octets_to_text(cols.get(CDP_PLATFORM)),
            remote_capabilities=_cdp_caps(cols.get(CDP_CAPABILITIES)),
        ))


def collect_fdb(snmp: Snmp, device: SnmpDevice, if_names: dict[int, str]) -> None:
    bridge_to_if = {idx[0]: v for idx, v in _safe_walk(snmp, DOT1D_BASE_PORT_IFINDEX, device)
                    if isinstance(v, int)}

    def add(mac_octets: tuple[int, ...], bridge_port, status, vlan) -> None:
        mac = normalize_mac(bytes(mac_octets))
        if not is_unicast_mac(mac) or status == FDB_STATUS_SELF or not bridge_port:
            return
        if_index = bridge_to_if.get(bridge_port)
        port = if_names.get(if_index) if if_index is not None else None
        device.fdb.append(FdbEntry(mac=mac, port=port or f"bridge-port-{bridge_port}",
                                   if_index=if_index, vlan=vlan))

    # Q-BRIDGE (VLAN-aware) first; index is (fdbId, 6 MAC bytes).
    q_status = {idx: v for idx, v in _safe_walk(snmp, DOT1Q_FDB_STATUS, device)}
    q_rows = _safe_walk(snmp, DOT1Q_FDB_PORT, device)
    for idx, port in q_rows:
        if len(idx) == 7:
            add(idx[1:], port, q_status.get(idx), idx[0])
    if q_rows:
        return
    # Plain BRIDGE-MIB; index is the 6 MAC bytes.
    d_status = {idx: v for idx, v in _safe_walk(snmp, DOT1D_FDB_STATUS, device)}
    for idx, port in _safe_walk(snmp, DOT1D_FDB_PORT, device):
        if len(idx) == 6:
            add(idx, port, d_status.get(idx), None)


def collect_arp(snmp: Snmp, device: SnmpDevice) -> None:
    for idx, value in _safe_walk(snmp, IP_NET_TO_MEDIA_PHYS, device):
        if len(idx) != 5:
            continue
        mac = normalize_mac(value)
        if is_unicast_mac(mac):
            device.arp.append(ArpEntry(ip=".".join(map(str, idx[1:])), mac=mac, if_index=idx[0]))
    if device.arp:
        return
    # Index is (ifIndex, addressType, addressLength, address bytes...). IPv4 only.
    for idx, value in _safe_walk(snmp, IP_NET_TO_PHYSICAL_PHYS, device):
        if len(idx) != 7 or idx[1] != 1 or idx[2] != 4:
            continue
        mac = normalize_mac(value)
        if is_unicast_mac(mac):
            device.arp.append(ArpEntry(ip=".".join(map(str, idx[3:])), mac=mac, if_index=idx[0]))


def collect_device(snmp: Snmp, ip: str) -> SnmpDevice:
    device = SnmpDevice(ip=ip)
    device.sys_name = octets_to_text(snmp.get(SYS_NAME))
    device.sys_descr = octets_to_text(snmp.get(SYS_DESCR))
    oid = snmp.get(SYS_OBJECT_ID)
    device.sys_object_id = str(oid) if oid is not None else None
    try:
        device.chassis_id = octets_to_text(snmp.get(LLDP_LOC_CHASSIS_ID))
    except SnmpError:
        pass
    if_names = collect_interfaces(snmp, device)
    collect_lldp(snmp, device, if_names)
    collect_cdp(snmp, device, if_names)
    collect_fdb(snmp, device, if_names)
    collect_arp(snmp, device)
    return device


ClientFactory = Callable[[str, SnmpCredential], Snmp]


def connect(ip: str, credentials: list[SnmpCredential], factory: ClientFactory) -> Snmp | None:
    """Try each credential in turn with a single GET; return the first that works."""
    for cred in credentials:
        client = factory(ip, cred)
        try:
            if client.get(SYS_OBJECT_ID) is not None:
                log.info("%s: SNMP answered with %s", ip, cred.label())
                return client
        except SnmpError as exc:
            log.debug("%s: %s failed: %s", ip, cred.label(), exc)
        close = getattr(client, "close", None)
        if close:
            close()
    return None
