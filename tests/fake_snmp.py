"""An in-memory stand-in for an SNMP agent, built from {oid: value} pairs."""

from netmapper.collectors.snmp_client import SnmpError


def _t(oid):
    return tuple(int(p) for p in oid.split("."))


class FakeSnmp:
    def __init__(self, data, fail=False):
        self.data = sorted((_t(k), v) for k, v in data.items())
        self.fail = fail
        self.closed = False

    def get(self, oid):
        if self.fail:
            raise SnmpError("timeout")
        key = _t(oid)
        return next((v for k, v in self.data if k == key), None)

    def walk(self, oid):
        if self.fail:
            raise SnmpError("timeout")
        base = _t(oid)
        return [(k[len(base):], v) for k, v in self.data if k[: len(base)] == base and k != base]

    def close(self):
        self.closed = True


def snmprec(data: dict) -> str:
    """Format {oid: value} as an snmpsim .snmprec file."""
    rows = []
    for oid, value in data.items():
        if isinstance(value, bytes):
            rows.append((oid, "4x", value.hex()))
        elif isinstance(value, int):
            rows.append((oid, "2", str(value)))
        else:
            rows.append((oid, "6", value))
    rows.sort(key=lambda r: tuple(int(p) for p in r[0].split(".")))
    return "".join(f"{o}|{t}|{v}\n" for o, t, v in rows)
