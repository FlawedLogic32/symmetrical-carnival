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
from netmapper.config import SnmpCredential, load_config
from netmapper.snmp_setup import diagnose, run_setup, try_credential

RESPONDER = shutil.which("snmpsim-command-responder")
pytestmark = pytest.mark.skipif(RESPONDER is None, reason="snmpsim not installed")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# SNMPv3 user on the simulated switch. snmpsim's AES256 is the Cisco ("c") variant. SHA-1
# keys are too short for AES-256, so the two ways of extending them give different keys.
V3_USER = ["--v3-user=netmapper", "--v3-auth-key=auth-pass-1", "--v3-auth-proto=SHA",
           "--v3-priv-key=priv-pass-1", "--v3-priv-proto=AES256"]


def _v3(**kw) -> SnmpCredential:
    base = dict(username="netmapper", auth_protocol="sha", auth_key="auth-pass-1",
                priv_protocol="aes256c", priv_key="priv-pass-1", context="public")
    return SnmpCredential("3", **{**base, **kw})


@pytest.fixture(scope="module")
def agent_port():
    work = Path(tempfile.mkdtemp(prefix="netmapper-sim-"))
    (work / "data").mkdir()
    (work / "data" / "public.snmprec").write_text(snmprec(core_switch()))
    port = _free_port()
    args = [RESPONDER, "--v3-engine-id=auto", *V3_USER, f"--data-dir={work / 'data'}",
            f"--cache-dir={work / 'cache'}", f"--agent-udpv4-endpoint=127.0.0.1:{port}"]
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


def _v3_client(port, cred):
    # snmpsim picks its data by v3 context name; real switches use the default one.
    cred = dataclasses.replace(cred, context="public")
    return PySnmpClient("127.0.0.1", cred, port=port, timeout=0.5, retries=0)


def test_real_snmpv3_matches_fake(agent_port):
    expected = dataclasses.asdict(sd.collect_device(FakeSnmp(core_switch()), "sw"))
    client = _v3_client(agent_port, _v3())
    try:
        got = dataclasses.asdict(sd.collect_device(client, "sw"))
    finally:
        client.close()
    assert got == expected


@pytest.mark.parametrize("cred, meaning", [
    (_v3(username="someone"), "doesn't know that username"),
    (_v3(auth_key="wrong-pass-1"), "authentication passphrase"),
    (_v3(priv_key="wrong-pass-1"), "encryption (privacy) passphrase"),
    (_v3(priv_protocol=None, priv_key=None), "different security level"),
])
def test_snmpv3_mistakes_are_diagnosed(agent_port, cred, meaning):
    ok, message = try_credential("127.0.0.1", cred, lambda ip, c: _v3_client(agent_port, c))
    assert not ok
    assert meaning in diagnose(message)


def test_setup_wizard_finds_the_aes256_variant(agent_port, tmp_path):
    from test_snmp_setup import Script, _config, _v3_answers

    path = _config(tmp_path)
    answers = _v3_answers()
    answers[6] = "aes256"
    answers[10] = "127.0.0.1"
    script = Script(answers)
    assert run_setup(path, lambda ip, c: _v3_client(agent_port, c), script.prompter()) == 0
    assert "works with aes256c" in script.text and "core-sw" in script.text
    assert load_config(path).snmp_credentials[0].priv_protocol == "aes256c"
