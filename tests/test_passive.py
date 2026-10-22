import socket
import struct
import threading
import time
from unittest import mock

import pytest
from fake_snmp import FakeSnmp
from test_snmp_device import core_switch

from netmapper.collectors import dhcp, listen
from netmapper.collectors import snmp_device as sd
from netmapper.config import Config
from netmapper.discovery import merge_hosts, run_discovery
from netmapper.init import local_ipv4
from netmapper.models import DiscoveryRun, Host
from netmapper.topology import build_topology

DNSMASQ = """\
1761234567 a4:5e:60:01:01:01 192.168.1.20 laptop-anna 01:a4:5e:60:01:01:01
1761234567 3c:22:fb:00:00:09 192.168.1.31 * *
"""

ISC = """\
lease 192.168.1.50 {
  starts 4 2026/10/22 09:00:00;
  binding state active;
  hardware ethernet 00:1b:63:aa:bb:cc;
  client-hostname "printer-2f";
}
lease 192.168.1.51 {
  binding state free;
  hardware ethernet 00:1b:63:aa:bb:dd;
}
lease 192.168.1.50 {
  binding state active;
  hardware ethernet 00:1b:63:aa:bb:cc;
  client-hostname "printer-2f";
}
"""

# Windows PowerShell 5 Export-Csv output.
WINDOWS_CSV = """\
#TYPE Microsoft.Management.Infrastructure.CimInstance#root/Microsoft/Windows/DHCP/DhcpServerv4Lease
"IPAddress","ScopeId","ClientId","HostName","AddressState"
"10.0.0.15","10.0.0.0","a4-5e-60-02-02-02","pc-reception.corp.local","Active"
"10.0.0.16","10.0.0.0","a4-5e-60-02-02-03","","ActiveReservation"
"10.0.0.17","10.0.0.0","a4-5e-60-02-02-04","old-pc","Expired"
"""

# "Export List" from the Windows DHCP console (tab separated).
WINDOWS_MMC = ("Client IP Address\tName\tLease Expiration\tType\tUnique ID\tDescription\n"
               "10.0.0.40\tcam-door.corp.local\t23/10/2026 09:00\tDHCP\t001122334466\t\n")

KEA = ("address,hwaddr,client_id,valid_lifetime,expire,subnet_id,fqdn_fwd,fqdn_rev,hostname,state\n"
       "10.1.0.5,aa:00:00:00:00:05,,3600,1761234567,1,0,0,nas,0\n"
       "10.1.0.6,aa:00:00:00:00:06,,3600,1761234567,1,0,0,gone,2\n")


def test_dnsmasq():
    hosts = dhcp.parse_leases(DNSMASQ)
    assert [(h.ip, h.mac, h.name) for h in hosts] == [
        ("192.168.1.20", "a4:5e:60:01:01:01", "laptop-anna"),
        ("192.168.1.31", "3c:22:fb:00:00:09", None)]


def test_isc_keeps_active_leases_once():
    hosts = dhcp.parse_leases(ISC)
    assert [(h.ip, h.mac, h.name) for h in hosts] == [
        ("192.168.1.50", "00:1b:63:aa:bb:cc", "printer-2f")]


def test_windows_exports():
    hosts = dhcp.parse_leases(WINDOWS_CSV, source="dhcp@dc01")
    assert [(h.ip, h.mac, h.name) for h in hosts] == [
        ("10.0.0.15", "a4:5e:60:02:02:02", "pc-reception"),
        ("10.0.0.16", "a4:5e:60:02:02:03", None)]
    assert hosts[0].sources == ["dhcp@dc01"]
    (cam,) = dhcp.parse_leases(WINDOWS_MMC)
    assert (cam.ip, cam.mac, cam.name) == ("10.0.0.40", "00:11:22:33:44:66", "cam-door")


def test_kea_skips_expired():
    assert [h.name for h in dhcp.parse_leases(KEA)] == ["nas"]


def test_unknown_text_gives_nothing():
    assert dhcp.parse_leases("hello\nworld\n") == []
    assert dhcp.parse_leases("") == []


def test_utf16_lease_file(tmp_path):
    path = tmp_path / "leases.csv"
    path.write_bytes(WINDOWS_CSV.encode("utf-16"))
    assert len(dhcp.read_lease_file(path)) == 2


def test_windows_dhcp_server_name_is_checked():
    hosts, error = dhcp.read_windows_dhcp("dc01; Remove-Item C:\\")
    assert hosts == [] and "doesn't look like" in error


def test_windows_dhcp_reads_powershell_csv():
    with mock.patch.object(dhcp, "_run", return_value=WINDOWS_CSV) as run:
        hosts, error = dhcp.read_windows_dhcp("dc01.corp.local")
    assert error is None and len(hosts) == 2
    assert "Get-DhcpServerv4Lease -ComputerName 'dc01.corp.local'" in run.call_args[0][0][-1]
    with mock.patch.object(dhcp, "_run", return_value=""):
        hosts, error = dhcp.read_windows_dhcp("dc01")
    assert hosts == [] and "DHCP Users" in error


def _mdns_announcement(name: str, ip: str) -> bytes:
    labels = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
    header = struct.pack("!6H", 0, 0x8400, 0, 2, 0, 0)
    a = labels + struct.pack("!HHIH", 1, 0x8001, 120, 4) + socket.inet_aton(ip)
    # A second record whose name is just a pointer back to the first one.
    ptr = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 120, 4) + socket.inet_aton("10.9.9.9")
    return header + a + ptr


def test_mdns_names():
    packet = _mdns_announcement("printer-2f.local", "192.168.1.50")
    assert listen.mdns_names(packet) == {"192.168.1.50": "printer-2f", "10.9.9.9": "printer-2f"}
    assert listen.mdns_names(b"\x00" * 5) == {}
    assert listen.mdns_names(packet[:20]) == {}
    loop = struct.pack("!6H", 0, 0, 0, 1, 0, 0) + b"\xc0\x0c"
    assert listen.mdns_names(loop) == {}


def test_listener_hears_multicast_and_sends_nothing(monkeypatch):
    group, port = "239.255.77.77", 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(listen, "GROUPS", [("mdns", group, port)])
    sent = []
    real_socket = socket.socket

    class Watched(real_socket):
        def sendto(self, *args):
            sent.append(args)
            return super().sendto(*args)

    monkeypatch.setattr(listen.socket, "socket", Watched)
    me = local_ipv4()
    if me is None:
        pytest.skip("no network interface")

    def announce():
        time.sleep(0.3)
        with real_socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 0)  # never leaves this PC
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(me))
            for _ in range(5):
                s.sendto(_mdns_announcement("tv.local", me), (group, port))
                time.sleep(0.1)

    threading.Thread(target=announce, daemon=True).start()
    heard = listen.listen(1.5)
    assert sent == []
    if not heard:
        pytest.skip("multicast loopback not available in this environment")
    assert [(h.ip, h.name, h.sources) for h in heard] == [(me, "tv", ["mdns"])]


def test_merge_folds_ip_only_sightings_into_mac_hosts():
    hosts = merge_hosts(
        [Host("192.168.1.20", "a4:5e:60:01:01:01", ["arp-cache"])],
        [Host(None, "a4:5e:60:01:01:01", ["fdb@sw"])],
        [Host("192.168.1.20", "a4:5e:60:01:01:01", ["dhcp"], name="laptop-anna")],
        [Host("192.168.1.20", None, ["mdns"], name="annas-macbook"),
         Host("192.168.1.99", None, ["ssdp"])],
    )
    assert [(h.ip, h.mac, h.name, h.sources) for h in hosts] == [
        ("192.168.1.20", "a4:5e:60:01:01:01", "laptop-anna",
         ["arp-cache", "fdb@sw", "dhcp", "mdns"]),
        ("192.168.1.99", None, None, ["ssdp"])]


def test_run_discovery_adds_dhcp_and_heard_hosts(tmp_path):
    leases = tmp_path / "leases"
    leases.write_text(DNSMASQ + "1761234567 a4:5e:60:07:07:07 10.99.0.1 elsewhere *\n")
    cfg = Config(subnets=[__import__("ipaddress").IPv4Network("192.168.1.0/24")],
                 snmp_enabled=False, dhcp_lease_files=[leases, tmp_path / "missing"],
                 listen_seconds=1)
    heard = [Host("192.168.1.77", None, ["ssdp"]), Host("8.8.8.8", None, ["mdns"])]
    with mock.patch("netmapper.discovery.local.read_arp_cache", return_value=[]), \
            mock.patch("netmapper.discovery.listen.listen", return_value=heard) as lis:
        run = run_discovery(cfg)
    lis.assert_called_once()
    assert {h.ip for h in run.hosts} == {"192.168.1.20", "192.168.1.31", "192.168.1.77"}
    assert any("not found" in n for n in run.notes)
    topo = build_topology(run)
    assert "laptop-anna" in {n.label for n in topo.nodes}


def test_saved_runs_from_before_names_still_load():
    raw = DiscoveryRun(started_at="t").to_dict()
    raw["hosts"] = [{"ip": "10.0.0.1", "mac": None, "sources": ["arp-cache"]}]
    assert DiscoveryRun.from_dict(raw).hosts[0].name is None


def test_router_arp_from_ip_net_to_physical():
    data = {k: v for k, v in core_switch().items() if not k.startswith(sd.IP_NET_TO_MEDIA_PHYS)}
    data[f"{sd.IP_NET_TO_PHYSICAL_PHYS}.1.1.4.192.168.1.21"] = bytes.fromhex("a45e60010102")
    data[f"{sd.IP_NET_TO_PHYSICAL_PHYS}.1.2.16.{'.'.join(['254', '128'] + ['0'] * 14)}"] = \
        bytes.fromhex("a45e60010103")  # IPv6, ignored
    dev = sd.collect_device(FakeSnmp(data), "192.168.1.1")
    assert [(a.ip, a.mac) for a in dev.arp] == [("192.168.1.21", "a4:5e:60:01:01:02")]
