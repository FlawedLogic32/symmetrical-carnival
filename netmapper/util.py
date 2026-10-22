"""Small helpers for MAC addresses, IPs and SNMP octet strings."""

from __future__ import annotations

import ipaddress
import re
import string

_MAC_TEXT = re.compile(
    r"^[0-9a-f]{2}([-:])(?:[0-9a-f]{2}\1){4}[0-9a-f]{2}$"  # aa-bb-.. or aa:bb:..
    r"|^[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}$"  # aabb.ccdd.eeff
    r"|^[0-9a-f]{12}$",
    re.IGNORECASE,
)


def normalize_mac(value: str | bytes | None) -> str | None:
    """Return a MAC as lowercase ``aa:bb:cc:dd:ee:ff``, or None if it isn't one.

    Accepts raw 6-byte values (as SNMP returns them) and the common text forms:
    ``aa-bb-cc-dd-ee-ff`` (Windows), ``aa:bb:...`` and ``aabb.ccdd.eeff`` (Cisco).
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        if len(value) != 6:
            return None
        return ":".join(f"{b:02x}" for b in value)
    value = value.strip()
    if not _MAC_TEXT.match(value):
        return None
    digits = re.sub(r"[-:.]", "", value).lower()
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def is_unicast_mac(mac: str | None) -> bool:
    """False for broadcast, multicast and all-zero MACs, which aren't real devices."""
    if not mac:
        return False
    first = int(mac[:2], 16)
    if first & 0x01:  # group bit: broadcast and multicast
        return False
    return mac != "00:00:00:00:00:00"


def octets_to_text(value: bytes | str | None) -> str | None:
    """Decode an SNMP string. Printable text is returned as-is, binary as a MAC or hex."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    text = value.rstrip(b"\x00").decode("utf-8", errors="replace")
    if text and all(ch in string.printable for ch in text):
        return text.strip()
    mac = normalize_mac(value)
    if mac:
        return mac
    return value.hex(":")


def octets_to_ipv4(value: bytes | str | None) -> str | None:
    if isinstance(value, (bytes, bytearray)) and len(value) == 4:
        return str(ipaddress.IPv4Address(bytes(value)))
    if isinstance(value, str):
        try:
            return str(ipaddress.IPv4Address(value))
        except ValueError:
            return None
    return None


def ip_in_networks(ip: str | None, networks: list[ipaddress.IPv4Network]) -> bool:
    if not ip or not networks:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in networks)
