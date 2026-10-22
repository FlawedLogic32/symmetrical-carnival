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
