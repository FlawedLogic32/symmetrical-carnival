"""A small synchronous, read-only SNMP client on top of pysnmp.

Only GET and WALK are exposed. Nothing in this tool ever sends an SNMP SET.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from ..config import SnmpCredential

# A walk returns (index, value) pairs. ``index`` is the part of the OID after the
# column being walked, as a tuple of ints, e.g. (1, 192, 168, 1, 1).
WalkResult = list[tuple[tuple[int, ...], Any]]


class SnmpError(Exception):
    pass


class Snmp(Protocol):
    """What the device collector needs. Tests supply a fake with the same shape."""

    def get(self, oid: str) -> Any: ...

    def walk(self, oid: str) -> WalkResult: ...


def _convert(value: Any) -> Any:
    """Turn pysnmp value objects into plain Python: bytes, int or str."""
    from pysnmp.proto import rfc1902

    if isinstance(value, rfc1902.OctetString):  # includes IpAddress
        return bytes(value.asOctets())
    if hasattr(value, "asTuple"):  # ObjectIdentifier; str() may give a MIB name
        return ".".join(str(p) for p in value.asTuple())
    if isinstance(value, (rfc1902.Integer32, rfc1902.Integer, rfc1902.Unsigned32,
                          rfc1902.Counter32, rfc1902.Counter64, rfc1902.Gauge32,
                          rfc1902.TimeTicks)):
        return int(value)
    return value.prettyPrint()


def _is_missing(value: Any) -> bool:
    from pysnmp.proto.rfc1905 import EndOfMibView, NoSuchInstance, NoSuchObject

    return isinstance(value, (NoSuchObject, NoSuchInstance, EndOfMibView))


def _oid_tuple(oid: str) -> tuple[int, ...]:
    return tuple(int(p) for p in oid.strip(".").split("."))


class PySnmpClient:
    def __init__(self, host: str, credential: SnmpCredential,
                 timeout: float = 2.0, retries: int = 1, port: int = 161):
        from pysnmp.hlapi.v3arch import asyncio as hlapi

        self._hlapi = hlapi
        self.host = host
        self.credential = credential
        self._loop = asyncio.new_event_loop()
        self._engine = hlapi.SnmpEngine()
        self._auth = self._auth_data(credential)
        self._target = self._loop.run_until_complete(
            hlapi.UdpTransportTarget.create((host, port), timeout=timeout, retries=retries)
        )
        self._context = hlapi.ContextData()

    def _auth_data(self, cred: SnmpCredential):
        h = self._hlapi
        if cred.version == "1":
            return h.CommunityData(cred.community, mpModel=0)
        if cred.version == "2c":
            return h.CommunityData(cred.community, mpModel=1)
        auth_protocols = {
            None: h.usmNoAuthProtocol,
            "md5": h.usmHMACMD5AuthProtocol,
            "sha": h.usmHMACSHAAuthProtocol,
            "sha256": h.usmHMAC192SHA256AuthProtocol,
            "sha512": h.usmHMAC384SHA512AuthProtocol,
        }
        priv_protocols = {
            None: h.usmNoPrivProtocol,
            "des": h.usmDESPrivProtocol,
            "aes": h.usmAesCfb128Protocol,
            "aes256": h.usmAesCfb256Protocol,
        }
        try:
            auth = auth_protocols[cred.auth_protocol.lower() if cred.auth_protocol else None]
            priv = priv_protocols[cred.priv_protocol.lower() if cred.priv_protocol else None]
        except KeyError as exc:
            raise SnmpError(f"unsupported SNMPv3 protocol {exc.args[0]!r}") from None
        return h.UsmUserData(cred.username, authKey=cred.auth_key, privKey=cred.priv_key,
                             authProtocol=auth, privProtocol=priv)

    def get(self, oid: str) -> Any:
        h = self._hlapi
        err, status, _, var_binds = self._loop.run_until_complete(
            h.get_cmd(self._engine, self._auth, self._target, self._context,
                      h.ObjectType(h.ObjectIdentity(oid)))
        )
        if err:
            raise SnmpError(str(err))
        if status:
            raise SnmpError(status.prettyPrint())
        value = var_binds[0][1]
        return None if _is_missing(value) else _convert(value)

    def walk(self, oid: str) -> WalkResult:
        return self._loop.run_until_complete(self._walk(oid))

    async def _walk(self, oid: str) -> WalkResult:
        h = self._hlapi
        base = _oid_tuple(oid)
        obj = h.ObjectType(h.ObjectIdentity(oid))
        if self.credential.version == "1":
            gen = h.walk_cmd(self._engine, self._auth, self._target, self._context, obj,
                             lexicographicMode=False)
        else:
            # GETBULK asks for 25 rows per request, so a big table takes few packets.
            gen = h.bulk_walk_cmd(self._engine, self._auth, self._target, self._context,
                                  0, 25, obj, lexicographicMode=False)
        rows: WalkResult = []
        async for err, status, _, var_binds in gen:
            if err:
                raise SnmpError(str(err))
            if status:
                raise SnmpError(status.prettyPrint())
            for name, value in var_binds:
                full = tuple(name)
                if full[: len(base)] != base or _is_missing(value):
                    continue
                rows.append((full[len(base):], _convert(value)))
        return rows

    def close(self) -> None:
        try:
            self._engine.close_dispatcher()
        except Exception:  # noqa: BLE001 - best effort cleanup
            pass
        # pysnmp leaves a timer task running; cancel it so closing the loop is clean.
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()
