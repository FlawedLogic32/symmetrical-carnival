import ipaddress

from netmapper.collectors.local import parse_linux_neigh, parse_windows_arp, slow_ping_sweep

WINDOWS_ARP = """
Interface: 192.168.1.50 --- 0xb
  Internet Address      Physical Address      Type
  192.168.1.1           00-11-22-33-44-55     dynamic
  192.168.1.20          a4-5e-60-12-34-56     dynamic
  192.168.1.255         ff-ff-ff-ff-ff-ff     static
  224.0.0.22            01-00-5e-00-00-16     static
"""

LINUX_NEIGH = """192.168.1.1 dev eth0 lladdr 00:11:22:33:44:55 REACHABLE
192.168.1.9 dev eth0  FAILED
192.168.1.20 dev eth0 lladdr a4:5e:60:12:34:56 STALE
"""


def test_parse_windows_arp_skips_broadcast_and_multicast():
    hosts = parse_windows_arp(WINDOWS_ARP)
    assert [(h.ip, h.mac) for h in hosts] == [
        ("192.168.1.1", "00:11:22:33:44:55"),
        ("192.168.1.20", "a4:5e:60:12:34:56"),
    ]
    assert hosts[0].sources == ["arp-cache"]


def test_parse_linux_neigh():
    hosts = parse_linux_neigh(LINUX_NEIGH)
    assert [h.ip for h in hosts] == ["192.168.1.1", "192.168.1.20"]


def test_ping_sweep_is_sequential_and_paced():
    pinged, sleeps = [], []

    def fake_ping(ip, timeout):
        pinged.append(ip)
        return ip.endswith(".2")

    alive = slow_ping_sweep([ipaddress.IPv4Network("10.0.0.0/29")], 0.5, 500,
                            ping=fake_ping, sleep=sleeps.append)
    assert pinged == [f"10.0.0.{i}" for i in range(1, 7)]
    assert sleeps == [0.5] * 6
    assert alive == ["10.0.0.2"]
