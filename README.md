# netmapper

A re-runnable tool that discovers what is on a small or medium business network,
including how devices connect through managed and unmanaged switches, and draws it
as a network diagram.

It is built to be **quiet**. It asks the network what it already knows instead of
scanning it, so it shouldn't set off intrusion detection or security monitoring.

Each run produces:

- **`network-latest.html`**: the diagram, a list of what changed since the last run, and a
  searchable table of every device. It's a single self-contained file that works offline.
- **`network-latest.drawio`**: the same diagram, editable in [draw.io](https://www.drawio.com/)
  (desktop or browser). draw.io can also export it to PDF or PNG.
- **`network-latest.vsdx`**: the same diagram for Microsoft Visio. Links are glued to the
  devices, so they follow when you move things around, and each device's IP, MAC and
  vendor are in its Shape Data.
- JSON files with the raw data and the topology, for scripting.

## Quick start on Windows (no Python needed)

1. Download `netmapper-windows` from the latest run of the
   [CI workflow](../../actions/workflows/ci.yml) (or from a release), and unzip it to a folder.
2. Double-click `netmapper.exe`. On the first run it writes a `config.toml` for the network
   the PC is on, maps it using only the PC's ARP cache, and opens the diagram.
3. For the full picture, add read-only SNMP credentials for your managed switches to
   `config.toml` (see [Configuration](#configuration)) and run it again.

From a terminal you get more control:

```powershell
.\netmapper.exe init                 # write config.toml (won't overwrite)
.\netmapper.exe discover --open      # map, draw, open the diagram
.\netmapper.exe discover --show      # also print the topology as an outline
.\netmapper.exe topology --open      # redraw from the last run without touching the network
.\netmapper.exe update-oui           # one-off: download the MAC vendor list from the IEEE
```

Windows SmartScreen may warn the first time because the .exe isn't code-signed. Choose
"More info", then "Run anyway", or run it from source as below.

## How it finds things

| Source | What it gives | Traffic it creates |
| --- | --- | --- |
| Local ARP cache (`arp -a`) | IP and MAC of hosts this PC has talked to recently | None |
| SNMP on managed switches and routers (read-only) | LLDP/CDP neighbours, the MAC table per port, router ARP table | A few SNMP GET/GETBULK requests per device, the same as any monitoring system |
| Slow ping sweep (**off by default**) | Fills the ARP cache with hosts that are up | One ping at a time, with a pause between them |
| Reverse DNS (**off by default**) | Host names for labels | One DNS query per host, to your normal DNS server |

It never port scans, fingerprints operating systems, or sends SNMP writes. When it
follows LLDP/CDP neighbours to other switches, it only queries neighbours that are
network gear (switches, routers, access points) inside the subnets you configure,
never phones or PCs. There is also an opt-in setting that tries SNMP on every host it
finds. It's off by default because an SNMP sweep is exactly what IDS rules look for.
`init` finds the PC's address and gateway locally, without sending anything.
`update-oui` is the only command that talks to the internet.

## How it works out the connections

1. **LLDP/CDP** gives exact switch-to-switch, switch-to-router, phone and access point links.
2. **MAC tables between switches** link managed switches that don't run LLDP. If switch A
   sees B behind port 8, B sees A behind port 1, and nothing else known sits behind both,
   they're cabled A:8 to B:1.
3. **Endpoints** go on the switch port where their MAC is learned that isn't an uplink.
4. **Unmanaged switches** can't be asked anything, so they're inferred: an edge port with
   several endpoint MACs behind it gets an "unmanaged switch" node. Hosts behind a phone,
   an access point or a switch that couldn't be queried are attached to that device instead.
5. **VM hosts** look like unmanaged switches too (several MACs on one port), so the MACs
   are checked against the ranges hypervisors use (VMware, Hyper-V, Xen, VirtualBox,
   QEMU/KVM, Proxmox, Parallels). If all of them, or all but one, are virtual, the port
   gets a "VM host" with the VMs under it instead of an unmanaged switch.
6. Hosts seen only in an ARP cache, with no switch port, go under "Location unknown".

Inferred links are dashed on the diagram. Some limits:

- A VM host is only recognised if its VMs use their hypervisor's default MAC range. VMs
  given custom MACs, or VMs mixed with other physical devices behind a real unmanaged
  switch, still show as an unmanaged switch.
- With no managed switches, or no SNMP credentials, all it can show is which devices exist,
  not how they're cabled.
- Devices that haven't sent traffic recently may have aged out of the switches' MAC tables.
  Running it during working hours gives the fullest picture.

## Configuration

`netmapper init` writes a starter `config.toml`. [`config.example.toml`](config.example.toml)
explains every option. The main ones are:

```toml
[discovery]
subnets = ["192.168.1.0/24"]

[snmp]
seeds = ["192.168.1.1", "192.168.1.2"]   # core switch and router; others are found via LLDP/CDP

[[snmp.credentials]]
version = "2c"
community = "env:SNMP_COMMUNITY"          # read from an environment variable
```

Set the secret before running, for example `$env:SNMP_COMMUNITY = "..."` in PowerShell.
SNMPv3 (SHA/AES) is supported too. Several credential blocks can be listed, and each one
is tried in turn.

## Diagram

Each device gets an icon for its type (router, switch, access point, phone, computer, and
so on). To draw a frame round each site, floor or room, either turn on
`group_by_snmp_location` to use the location already set on your switches, or list the
groups yourself:

```toml
[[diagram.groups]]
name = "First floor"
members = ["ff-sw*", "192.168.20.0/24"]   # device names (wildcards), IPs or subnets
```

Devices you don't list join the group of the switch they plug into, so naming each floor's
switches is usually enough. Groups only change how the diagram is drawn; rerun
`netmapper topology` after editing them to redraw without touching the network.

## Re-running

Run it as often as you like, for example from Windows Task Scheduler. Each run keeps
timestamped copies in `runs/`, and the `*-latest` files are the newest. The HTML page and
the console list devices that are new, gone, or have moved to a different switch port
since the previous run. New devices are outlined in green on the diagram.

## Run from source

Needs Python 3.11 or newer.

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install .
netmapper discover --open
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

The SNMP parsing is tested against an in-memory fake agent. If `snmpsim` is installed
(`pip install snmpsim pysmi`), the real SNMP client is also tested against a simulated
switch over UDP.

To build the Windows executable: `pip install ".[build]"`, then run
`python packaging/build_exe.py`, which writes `dist/netmapper.exe`. After that,
`python packaging/smoke_test.py dist/netmapper.exe` runs it end to end.

CI (GitHub Actions) runs the tests on Windows and Linux. It then builds `netmapper.exe`
on Windows, smoke tests it there (including a run against a simulated SNMP switch), and
uploads it as the `netmapper-windows` artifact. Pushing a tag such as `v1.0.0` also
publishes a release with the .exe attached.
