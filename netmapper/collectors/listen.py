"""Listen, without sending anything, for devices announcing themselves.

Printers, TVs, phones, IoT gadgets and PCs regularly send multicast messages to
everyone on their network segment: mDNS ("_ipp._tcp.local" and friends), SSDP/UPnP
NOTIFY and M-SEARCH, and LLMNR name lookups from Windows PCs. Listening for those for
a minute finds devices that never show up in an ARP cache, without netmapper sending
a single packet. (Joining the multicast groups makes the OS send an IGMP membership
report, which every Windows PC already does for these groups.)

Off by default: set ``listen_seconds`` under ``[discovery]``. Windows Firewall may ask
whether to allow netmapper to receive on the network the first time; nothing is heard
until it is allowed.
"""

from __future__ import annotations

import logging
import select
import socket
import struct
import time
from collections.abc import Callable

from ..models import Host

log = logging.getLogger(__name__)

# (source label, multicast group, UDP port)
GROUPS = [
    ("mdns", "224.0.0.251", 5353),
    ("ssdp", "239.255.255.250", 1900),
    ("llmnr", "224.0.0.252", 5355),
]


def _read_name(data: bytes, offset: int) -> tuple[str, int]:
    """Read a DNS name, following compression pointers. Returns (name, next offset)."""
    labels: list[str] = []
    end = None
    for _ in range(64):  # guards against pointer loops in a malformed packet
        if offset >= len(data):
            raise ValueError("name runs past the end")
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("truncated pointer")
            end = end if end is not None else offset + 2
            offset = ((length & 0x3F) << 8) | data[offset + 1]
            continue
        offset += 1
        if length == 0:
            return ".".join(labels), end if end is not None else offset
        labels.append(data[offset:offset + length].decode("utf-8", errors="replace"))
        offset += length
    raise ValueError("too many labels")


def mdns_names(data: bytes) -> dict[str, str]:
    """IPv4 address -> host name, from the A records in an mDNS packet."""
    names: dict[str, str] = {}
    try:
        qd, an, ns, ar = struct.unpack("!4H", data[4:12])
        offset = 12
        for _ in range(qd):
            _, offset = _read_name(data, offset)
            offset += 4
        for _ in range(an + ns + ar):
            name, offset = _read_name(data, offset)
            rtype, _, _, rdlen = struct.unpack("!HHIH", data[offset:offset + 10])
            offset += 10
            if rtype == 1 and rdlen == 4:
                ip = socket.inet_ntoa(data[offset:offset + 4])
                short = name.removesuffix(".local").split(".")[0]
                if short:
                    names.setdefault(ip, short)
            offset += rdlen
    except (ValueError, struct.error, OSError):
        pass
    return names


def _open(group: str, port: int, interface_ip: str | None) -> socket.socket | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.bind(("", port))
        iface = socket.inet_aton(interface_ip) if interface_ip else struct.pack("!I", 0)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                        socket.inet_aton(group) + iface)
        sock.setblocking(False)
        return sock
    except OSError as exc:
        log.info("can't listen on UDP %d (%s): %s", port, group, exc)
        sock.close()
        return None


def listen(seconds: float, interface_ip: str | None = None,
           clock: Callable[[], float] = time.monotonic) -> list[Host]:
    """Listen for ``seconds`` and return each device heard (IP, and a name if it gave one).

    Nothing is ever sent from these sockets.
    """
    sockets = {}
    for label, group, port in GROUPS:
        if sock := _open(group, port, interface_ip):
            sockets[sock] = label
    if not sockets:
        log.warning("passive listening: could not open any multicast socket")
        return []
    heard: dict[str, Host] = {}
    deadline = clock() + seconds
    log.info("listening for %.0fs for devices announcing themselves (sends nothing)", seconds)
    try:
        while (remaining := deadline - clock()) > 0:
            ready, _, _ = select.select(list(sockets), [], [], min(remaining, 1.0))
            for sock in ready:
                try:
                    data, (ip, _) = sock.recvfrom(9000)
                except OSError:
                    continue
                label = sockets[sock]
                host = heard.setdefault(ip, Host(ip=ip, mac=None, sources=[]))
                if label not in host.sources:
                    host.sources.append(label)
                if label == "mdns" and not host.name:
                    host.name = mdns_names(data).get(ip)
    finally:
        for sock in sockets:
            sock.close()
    if interface_ip:
        heard.pop(interface_ip, None)
    log.info("passive listening: heard %d devices", len(heard))
    return list(heard.values())
