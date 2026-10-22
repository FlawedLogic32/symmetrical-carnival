"""Run the built executable the way a user would. Used by CI after building the .exe.

    python packaging/smoke_test.py dist/netmapper.exe

Checks: --version, init, an ARP-only run on this machine, double-click mode, and (if
snmpsim is installed) a full run against a simulated switch over real SNMP.
"""

import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))


def run(exe, *args, cwd, check=True):
    env = dict(os.environ, NETMAPPER_NO_BROWSER="1")
    result = subprocess.run([str(exe), *args], cwd=cwd, capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, timeout=300, env=env)
    print(f"$ netmapper {' '.join(args)}  (exit {result.returncode})")
    print(result.stdout)
    if result.stderr.strip():
        print(result.stderr)
    if check and result.returncode != 0:
        raise SystemExit(f"netmapper {' '.join(args)} failed")
    return result


def expect(path: Path, text: str | None = None):
    if not path.exists():
        raise SystemExit(f"missing {path}")
    if text and text not in path.read_text(encoding="utf-8"):
        raise SystemExit(f"{path} does not mention {text!r}")
    print(f"ok: {path.name}" + (f" mentions {text!r}" if text else ""))


def show_arp_parsing() -> None:
    """Print this machine's real ARP cache next to what netmapper makes of it."""
    from netmapper.collectors import local

    cmd = ["arp", "-a"] if local.IS_WINDOWS else ["ip", "-4", "neigh", "show"]
    try:
        raw = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    except OSError as exc:
        print(f"could not run {cmd[0]}: {exc}")
        return
    print(f"$ {' '.join(cmd)}\n{raw}")
    hosts = local.read_arp_cache()
    print(f"parsed {len(hosts)} hosts: {[(h.ip, h.mac) for h in hosts]}")
    lines = [ln for ln in raw.splitlines() if re.search(r"\d+\.\d+\.\d+\.\d+\s+\S+-\S+", ln)]
    unicast = [ln for ln in lines if not re.search(r"\s(ff-ff|01-00-5e)", ln, re.I)]
    if unicast and not hosts:
        raise SystemExit("arp -a lists unicast entries but none were parsed")


def main(exe: Path) -> None:
    exe = exe.resolve()
    run(exe, "--version", cwd=ROOT)
    show_arp_parsing()

    work = Path(tempfile.mkdtemp(prefix="netmapper-smoke-"))
    run(exe, "init", cwd=work)
    print((work / "config.toml").read_text(encoding="utf-8"))
    run(exe, "discover", "--show", cwd=work)
    for name in ("latest.json", "topology-latest.json", "network-latest.html",
                 "network-latest.drawio"):
        expect(work / "runs" / name)

    double = Path(tempfile.mkdtemp(prefix="netmapper-dbl-"))
    run(exe, cwd=double)  # no arguments, as if double-clicked
    expect(double / "config.toml")
    expect(double / "runs" / "network-latest.html", "Network map")

    responder = shutil.which("snmpsim-command-responder")
    if not responder:
        print("snmpsim not installed; skipping the SNMP run")
        return
    from fake_snmp import snmprec
    from test_snmp_device import core_switch

    sim = Path(tempfile.mkdtemp(prefix="netmapper-sim-"))
    (sim / "data").mkdir()
    (sim / "data" / "public.snmprec").write_text(snmprec(core_switch()))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    args = [responder, f"--data-dir={sim / 'data'}", f"--cache-dir={sim / 'cache'}",
            f"--agent-udpv4-endpoint=127.0.0.1:{port}"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        for p in [sim, *sim.rglob("*")]:
            os.chmod(p, 0o777)
        args += ["--process-user=nobody", "--process-group=nogroup"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(8)
        snmp = work / "snmp"
        snmp.mkdir()
        (snmp / "config.toml").write_text(
            '[discovery]\nsubnets = ["192.168.1.0/24"]\n'
            f'[snmp]\nseeds = ["127.0.0.1"]\nport = {port}\n'
            '[[snmp.credentials]]\nversion = "2c"\ncommunity = "public"\n', encoding="utf-8")
        out = run(exe, "discover", "--show", cwd=snmp).stdout
        if "1 managed devices" not in out:
            raise SystemExit("the simulated switch was not read over SNMP")
        expect(snmp / "runs" / "network-latest.html", "core-sw")
        expect(snmp / "runs" / "network-latest.drawio", "Unmanaged switch")
    finally:
        proc.terminate()
    print("smoke test passed")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
