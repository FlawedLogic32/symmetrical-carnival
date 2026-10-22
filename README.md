# netmapper

A re-runnable tool that discovers what is on a small or medium business network,
including how devices connect through managed and unmanaged switches, and turns it
into a network diagram.

It is built to be **quiet**. It asks the network what it already knows instead of
scanning it, so it shouldn't set off intrusion detection or security monitoring.

> Status: steps 1 and 2 of 4 (discovery and topology) are done. Diagram export and
> Windows packaging come next. For now the output is JSON plus a text outline.

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

## How it works out the connections

1. **LLDP/CDP** gives exact switch-to-switch, switch-to-router, phone and access point links.
2. **MAC tables between switches** link managed switches that don't run LLDP. If switch A
   sees B behind port 8, B sees A behind port 1, and nothing else known sits behind both,
   they're cabled A:8 to B:1.
3. **Endpoints** go on the switch port where their MAC is learned that isn't an uplink.
4. **Unmanaged switches** can't be asked anything, so they're inferred: an edge port with
   several endpoint MACs behind it gets an "unmanaged switch" node. Hosts behind a phone,
   an access point or a switch that couldn't be queried are attached to that device instead.
5. Hosts seen only in an ARP cache, with no switch port, go under "Location unknown".

Links worked out in steps 2 to 4 are marked as inferred. One known limit is that a server
running several VMs on one port also looks like an unmanaged switch.

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

Each run saves `runs/discovery-<time>.json` (the raw data) and `runs/topology-<time>.json`
(devices and links), plus `latest.json` and `topology-latest.json` for the newest. Useful
flags: `--show` (print the topology as an outline), `--no-snmp` (ARP cache only),
`--ping-sweep` (turn the slow sweep on for this run), `-v` (debug logging).

Other commands:

- `netmapper topology -c config.toml --show [RUN.json]` rebuilds the topology from a saved
  run without touching the network (defaults to the latest run).
- `netmapper update-oui -c config.toml` downloads the IEEE MAC vendor list once, so devices
  show their maker. It's the only command that goes to the internet.

Optional: set `resolve_names = true` under `[topology]` to label hosts by reverse DNS.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The SNMP parsing is tested against an in-memory fake agent. If `snmpsim` is installed
(`pip install snmpsim pysmi`), `tests/test_snmp_live.py` also runs the real SNMP client
against a simulated switch over UDP.
