"""Runs the real pysnmp client against a simulated switch (snmpsim).

Skipped unless snmpsim is installed: ``pip install snmpsim pysmi``.
"""

import dataclasses
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from fake_snmp import FakeSnmp, snmprec
from test_snmp_device import core_switch

from netmapper.collectors import snmp_device as sd
from netmapper.collectors.snmp_client import PySnmpClient
from netmapper.config import SnmpCredential

RESPONDER = shutil.which("snmpsim-command-responder")
pytestmark = pytest.mark.skipif(RESPONDER is None, reason="snmpsim not installed")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def agent_port():
    work = Path(tempfile.mkdtemp(prefix="netmapper-sim-"))
    (work / "data").mkdir()
    (work / "data" / "public.snmprec").write_text(snmprec(core_switch()))
    port = _free_port()
    args = [RESPONDER, f"--data-dir={work / 'data'}", f"--cache-dir={work / 'cache'}",
            f"--agent-udpv4-endpoint=127.0.0.1:{port}"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        # snmpsim refuses to run as root.
        os.chmod(work, 0o777)
        for p in work.rglob("*"):
            os.chmod(p, 0o777)
        args += ["--process-user=nobody", "--process-group=nogroup"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = PySnmpClient("127.0.0.1", SnmpCredential("2c", community="public"),
                          port=port, timeout=0.5, retries=0)
    try:
        for _ in range(40):
            try:
                if client.get(sd.SYS_NAME):
                    break
            except Exception:  # noqa: BLE001 - still starting up
                time.sleep(0.25)
        else:
            pytest.skip("snmpsim did not start")
        yield port
    finally:
        client.close()
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(work, ignore_errors=True)


@pytest.mark.parametrize("version", ["1", "2c"])
def test_real_client_matches_fake(agent_port, version):
    expected = dataclasses.asdict(sd.collect_device(FakeSnmp(core_switch()), "sw"))
    client = PySnmpClient("127.0.0.1", SnmpCredential(version, community="public"), port=agent_port)
    try:
        got = dataclasses.asdict(sd.collect_device(client, "sw"))
    finally:
        client.close()
    assert got == expected


def test_wrong_community_gives_up_quickly(agent_port):
    start = time.monotonic()
    client = sd.connect("127.0.0.1", [SnmpCredential("2c", community="wrong")],
                        lambda ip, cred: PySnmpClient(ip, cred, port=agent_port,
                                                      timeout=0.5, retries=0))
    assert client is None
    assert time.monotonic() - start < 5
