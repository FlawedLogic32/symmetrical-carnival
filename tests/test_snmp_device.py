import ipaddress
from unittest import mock

from fake_snmp import FakeSnmp

from netmapper.collectors import snmp_device as sd
from netmapper.config import Config, SnmpCredential
from netmapper.discovery import run_discovery
from netmapper.models import Host

LLDP_TM = "0"  # lldpTimeMark, which the collector ignores


def core_switch(neighbor_ip="192.168.1.3"):
    """A switch with an LLDP neighbour on port 1, a CDP phone on port 2,
    and three MACs learned on port 3 (an unmanaged switch hanging off it)."""
    mac = lambda *b: ".".join(str(x) for x in b)  # noqa: E731
    d = {
        sd.SYS_NAME: b"core-sw",
        sd.SYS_LOCATION: b"Ground floor comms room",
        sd.SYS_DESCR: b"Example switch",
        sd.SYS_OBJECT_ID: "1.3.6.1.4.1.9.1.1",
        sd.LLDP_LOC_CHASSIS_ID: bytes.fromhex("00aa00aa00aa"),
        f"{sd.IF_NAME}.1": b"Gi1/0/1",
        f"{sd.IF_NAME}.2": b"Gi1/0/2",
        f"{sd.IF_NAME}.3": b"Gi1/0/3",
        f"{sd.IF_PHYS_ADDRESS}.1": bytes.fromhex("00aa00aa0001"),
        f"{sd.DOT1D_BASE_PORT_IFINDEX}.1": 1,
        f"{sd.DOT1D_BASE_PORT_IFINDEX}.2": 2,
        f"{sd.DOT1D_BASE_PORT_IFINDEX}.3": 3,
        # LLDP neighbour on local port 1, using a MAC as its port id so the
        # collector should fall back to the port description.
        f"{sd.LLDP_LOC_PORT_ID}.1": bytes.fromhex("00aa00aa0001"),
        f"{sd.LLDP_LOC_PORT_DESC}.1": b"Gi1/0/1",
        f"{sd.LLDP_REM}.5.{LLDP_TM}.1.1": bytes.fromhex("00bb00bb00bb"),
        f"{sd.LLDP_REM}.7.{LLDP_TM}.1.1": b"port 24",
        f"{sd.LLDP_REM}.9.{LLDP_TM}.1.1": b"access-sw",
        f"{sd.LLDP_REM}.12.{LLDP_TM}.1.1": bytes([0x20, 0x00]),  # bridge
        f"{sd.LLDP_REM_MAN_ADDR_IF_SUBTYPE}.{LLDP_TM}.1.1.1.4.{neighbor_ip}": 2,
        # CDP phone on ifIndex 2
        f"{sd.CDP_CACHE}.4.2.1": bytes([192, 168, 1, 40]),
        f"{sd.CDP_CACHE}.6.2.1": b"SEP001122334455",
        f"{sd.CDP_CACHE}.7.2.1": b"Port 1",
        f"{sd.CDP_CACHE}.8.2.1": b"Cisco IP Phone 8841",
        f"{sd.CDP_CACHE}.9.2.1": bytes([0, 0, 0, 0x90]),
        # Q-BRIDGE FDB on VLAN 10
        f"{sd.DOT1Q_FDB_PORT}.10.{mac(0, 0x11, 0x22, 0x33, 0x44, 0x55)}": 2,
        f"{sd.DOT1Q_FDB_PORT}.10.{mac(0xa4, 0x5e, 0x60, 1, 1, 1)}": 3,
        f"{sd.DOT1Q_FDB_PORT}.10.{mac(0xa4, 0x5e, 0x60, 1, 1, 2)}": 3,
        f"{sd.DOT1Q_FDB_PORT}.10.{mac(0xa4, 0x5e, 0x60, 1, 1, 3)}": 3,
        f"{sd.DOT1Q_FDB_PORT}.10.{mac(0, 0xaa, 0, 0xaa, 0, 0xaa)}": 0,  # self, port 0
        f"{sd.DOT1Q_FDB_STATUS}.10.{mac(0, 0xaa, 0, 0xaa, 0, 0xaa)}": 4,
        # ARP
        f"{sd.IP_NET_TO_MEDIA_PHYS}.1.192.168.1.20": bytes.fromhex("a45e60010101"),
    }
    return d


def test_collect_device_reads_every_table():
    dev = sd.collect_device(FakeSnmp(core_switch()), "192.168.1.2")
    assert dev.sys_name == "core-sw"
    assert dev.sys_location == "Ground floor comms room"
    assert dev.chassis_id == "00:aa:00:aa:00:aa"
    assert [i.name for i in dev.interfaces] == ["Gi1/0/1", "Gi1/0/2", "Gi1/0/3"]

    lldp, cdp = dev.neighbors
    assert (lldp.protocol, lldp.local_port, lldp.remote_name, lldp.remote_port) == \
        ("lldp", "Gi1/0/1", "access-sw", "port 24")
    assert lldp.remote_mgmt_ip == "192.168.1.3"
    assert lldp.remote_chassis_id == "00:bb:00:bb:00:bb"
    assert lldp.remote_capabilities == ["bridge"]

    assert (cdp.protocol, cdp.local_port, cdp.remote_name) == ("cdp", "Gi1/0/2", "SEP001122334455")
    assert cdp.remote_mgmt_ip == "192.168.1.40"
    assert cdp.remote_capabilities == ["host", "telephone"]

    ports = sorted((f.port, f.mac, f.vlan) for f in dev.fdb)
    assert len(ports) == 4  # the switch's own MAC is dropped
    assert [p for p, _, _ in ports].count("Gi1/0/3") == 3
    assert all(v == 10 for _, _, v in ports)

    assert [(a.ip, a.mac) for a in dev.arp] == [("192.168.1.20", "a4:5e:60:01:01:01")]
    assert dev.errors == []


def test_falls_back_to_plain_bridge_mib():
    d = {
        sd.SYS_NAME: b"old-sw",
        f"{sd.IF_DESCR}.5": b"port5",
        f"{sd.DOT1D_BASE_PORT_IFINDEX}.1": 5,
        f"{sd.DOT1D_FDB_PORT}.0.17.34.51.68.85": 1,
    }
    dev = sd.collect_device(FakeSnmp(d), "10.0.0.2")
    assert [(f.mac, f.port, f.vlan) for f in dev.fdb] == [("00:11:22:33:44:55", "port5", None)]


def test_connect_tries_credentials_in_order():
    creds = [SnmpCredential("2c", community="wrong"), SnmpCredential("2c", community="right")]
    made = []

    def factory(ip, cred):
        client = FakeSnmp(core_switch(), fail=cred.community == "wrong")
        made.append(client)
        return client

    client = sd.connect("192.168.1.2", creds, factory)
    assert client is made[1]
    assert made[0].closed


def test_run_discovery_crawls_neighbours_inside_subnets():
    agents = {
        "192.168.1.2": core_switch(neighbor_ip="192.168.1.3"),
        "192.168.1.3": {sd.SYS_NAME: b"access-sw", sd.SYS_OBJECT_ID: "1.3.6.1.4.1.1"},
    }
    queried = []

    def factory(ip, cred):
        queried.append(ip)
        return FakeSnmp(agents.get(ip, {}), fail=ip not in agents)

    cfg = Config(subnets=[ipaddress.IPv4Network("192.168.1.0/24")], snmp_seeds=["192.168.1.2"],
                 snmp_credentials=[SnmpCredential("2c", community="public")])
    arp = [Host(ip="192.168.1.20", mac="a4:5e:60:01:01:01", sources=["arp-cache"]),
           Host(ip="10.9.9.9", mac="00:00:00:00:99:99", sources=["arp-cache"])]
    with mock.patch("netmapper.discovery.local.read_arp_cache", return_value=arp):
        run = run_discovery(cfg, snmp_factory=factory)

    assert queried == ["192.168.1.2", "192.168.1.3"]
    assert [d.sys_name for d in run.devices] == ["core-sw", "access-sw"]
    by_mac = {h.mac: h for h in run.hosts}
    assert "00:00:00:00:99:99" not in by_mac  # outside the configured subnet
    merged = by_mac["a4:5e:60:01:01:01"]
    assert merged.ip == "192.168.1.20"
    assert merged.sources == ["arp-cache", "arp@192.168.1.2", "fdb@192.168.1.2"]


def test_neighbours_outside_subnets_are_not_followed():
    queried = []

    def factory(ip, cred):
        queried.append(ip)
        return FakeSnmp(core_switch(neighbor_ip="172.16.0.1"))

    cfg = Config(subnets=[ipaddress.IPv4Network("192.168.1.0/24")], snmp_seeds=["192.168.1.2"],
                 snmp_credentials=[SnmpCredential("2c", community="public")])
    with mock.patch("netmapper.discovery.local.read_arp_cache", return_value=[]):
        run_discovery(cfg, snmp_factory=factory)
    assert queried == ["192.168.1.2"]
