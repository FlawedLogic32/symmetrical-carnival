# netmapper

A re-runnable tool that discovers what is on a small or medium business network,
including how devices connect through managed and unmanaged switches, and turns it
into a network diagram.

It is built to be **quiet**. It asks the network what it already knows instead of
scanning it, so it shouldn't set off intrusion detection or security monitoring.

> Status: step 1 of 4 (discovery) is done. Topology building, diagram export and
> Windows packaging come next. For now the output is a JSON file per run.

## How it finds things

| Source | What it gives | Traffic it creates |
| --- | --- | --- |
| Local ARP cache (`arp -a`) | IP and MAC of hosts this PC has talked to recently | None |
| SNMP on managed switches and routers (read-only) | LLDP/CDP neighbours (switch-to-switch links), MAC table per port, router ARP table | A few SNMP GET/GETBULK requests per device, the same as any monitoring system |
| Slow ping sweep (**off by default**) | Fills the ARP cache with hosts that are up | One ping at a time, with a pause between them |

It never port scans, fingerprints operating systems, or sends SNMP writes. When it
follows LLDP/CDP neighbours to other switches, it only queries neighbours that are
network gear (switches, routers, access points) inside the subnets you configure,
never phones or PCs. There is also an opt-in setting that tries SNMP on every host it
finds. It's off by default because an SNMP sweep is exactly what IDS rules look for.

**Unmanaged switches** can't be asked anything. They show up in the data as several
MAC addresses learned on a single managed switch port. Step 2 turns that into an
"unmanaged switch" node on the diagram.

## Install (Windows)

1. Install Python 3.11 or newer from python.org (tick "Add python.exe to PATH").
2. In PowerShell, from this folder:

   ```powershell
   py -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install .
   ```

## Use

1. Copy `config.example.toml` to `config.toml`. Set your subnets, the IPs of your
   core switch and router, and an SNMP read-only community or v3 user.
2. Put secrets in environment variables rather than the file, for example:

   ```powershell
   $env:SNMP_COMMUNITY = "your-read-only-community"
   ```

3. Run:

   ```powershell
   netmapper discover -c config.toml
   ```

Each run is saved as `runs/discovery-<time>.json`, and `runs/latest.json` is always
the newest. Useful flags: `--no-snmp` (ARP cache only), `--ping-sweep` (turn the slow
sweep on for this run), `-v` (debug logging).

## Development

```bash
pip install -e ".[dev]"
pytest
```

The SNMP parsing is tested against an in-memory fake agent. If `snmpsim` is installed
(`pip install snmpsim pysmi`), `tests/test_snmp_live.py` also runs the real SNMP client
against a simulated switch over UDP.
