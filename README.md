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
   the PC is on, offers to set up SNMP credentials, maps the network and opens the diagram.
   Without credentials it uses only the PC's ARP cache.
3. For the full picture, run `.\netmapper.exe snmp-setup` to add read-only SNMP credentials
   for your managed switches (see [SNMP credentials](#snmp-credentials)), and run it again.

From a terminal you get more control:

```powershell
.\netmapper.exe init                 # write config.toml (won't overwrite)
.\netmapper.exe snmp-setup           # add SNMPv3 or v2c credentials, tested and stored safely
.\netmapper.exe discover --open      # map, draw, open the diagram
.\netmapper.exe discover --show      # also print the topology as an outline
.\netmapper.exe topology --open      # redraw from the last run without touching the network
.\netmapper.exe update-oui           # one-off: download the MAC vendor list from the IEEE
.\netmapper.exe schedule             # map every day at 08:00 (alerts are opt-in)
```

If a build isn't code-signed, Windows SmartScreen may warn the first time. Choose
"More info", then "Run anyway", or run it from source as below. Each release's notes say
whether its .exe is signed, and `SHA256SUMS.txt` lets you check the download.

## How it finds things

| Source | What it gives | Traffic it creates |
| --- | --- | --- |
| Local ARP cache (`arp -a`) | IP and MAC of hosts this PC has talked to recently | None |
| SNMP on managed switches and routers (read-only) | LLDP/CDP neighbours, the MAC table per port, router ARP table (old and new IP-MIB tables) | A few SNMP GET/GETBULK requests per device, the same as any monitoring system |
| DHCP lease files (**off until configured**) | IP, MAC and name of every device holding a lease, including quiet ones | None: it reads a file you exported |
| Windows DHCP server (**off until configured**) | The same, read live from the server | One management call per server, the same as opening the DHCP console |
| Listening for announcements (**off by default**) | IPs (and often names) of printers, TVs, phones, IoT and PCs that announce themselves over mDNS, SSDP or LLMNR | None: it only receives |
| Slow ping sweep (**off by default**) | Fills the ARP cache with hosts that are up | One ping at a time, with a pause between them |
| Reverse DNS (**off by default**) | Host names for labels | One DNS query per host, to your normal DNS server |

It never port scans, fingerprints operating systems, or sends SNMP writes. When it
follows LLDP/CDP neighbours to other switches, it only queries neighbours that are
network gear (switches, routers, access points) inside the subnets you configure,
never phones or PCs. There is also an opt-in setting that tries SNMP on every host it
finds. It's off by default because an SNMP sweep is exactly what IDS rules look for.
`init` finds the PC's address and gateway locally, without sending anything.
`update-oui` is the only command that talks to the internet, apart from email or
webhook alerts if you set them up.

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
  Running it during working hours gives the fullest picture. DHCP leases and listening
  (below) catch many of the quiet ones, though without a switch port they go under
  "Location unknown".

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
Several credential blocks can be listed, and each one is tried in turn.

### SNMP credentials

`netmapper snmp-setup` adds a credential block for you. It asks for the SNMP version
(v3 is the default), the username, security level, authentication and encryption
protocols and passphrases, then tests them against one switch with a single SNMP request.
If the test fails it says what most likely doesn't match (unknown user, wrong
authentication passphrase, wrong encryption passphrase, wrong security level, or no answer
at all), and saves nothing unless you ask it to. For AES-192/256 it tries both ways
vendors derive the key and keeps the one that works. The switch it tested against is
added to the SNMP seeds.

It can keep the passphrases:

- **encrypted** in `config.toml` (Windows, the default). They are stored as `dpapi:...`,
  encrypted with Windows' Data Protection API, so only the same Windows user on the same
  PC can use them. Scheduled runs under that user work; copying the file elsewhere doesn't
  give the passphrases away.
- in **environment variables**. It prints the `setx` commands to run, without the secret.
- as **plain text** in `config.toml`, if you must.

Supported SNMPv3 protocols: authentication `md5`, `sha`, `sha224`, `sha256`, `sha384`,
`sha512`; encryption `des`, `3des`, `aes`, `aes192`, `aes256`, and `aes192c`/`aes256c`
(the Cisco key derivation, named as in Net-SNMP).

### Finding quiet devices

Devices that are idle, asleep or behind a switch netmapper can't read are easy to miss.
Two extra sources help, without scanning:

```toml
[discovery]
listen_seconds = 60          # listen for mDNS/SSDP/LLMNR announcements; sends nothing

[dhcp]
lease_files = ["leases.csv"] # exported lease lists: dnsmasq, dhcpd.leases or CSV
windows_servers = ["dc01"]   # read leases live from Windows DHCP servers
```

- **DHCP lease files** can come from most routers (dnsmasq format, e.g. OpenWrt or Pi-hole),
  pfSense or Linux (`dhcpd.leases`), Kea, or Windows Server
  (`Get-DhcpServerv4Scope | Get-DhcpServerv4Lease | Export-Csv leases.csv`, or "Export
  List" in the DHCP console). Only active leases are used, and their host names label the
  diagram.
- **Windows DHCP servers** are read with `Get-DhcpServerv4Lease`. This needs the DhcpServer
  PowerShell module (part of RSAT) and an account in the server's "DHCP Users" group.
- **Listening** joins the mDNS, SSDP and LLMNR multicast groups and notes who speaks,
  while the rest of discovery runs. It never sends a query. Windows Firewall may ask
  whether netmapper can receive on the network the first time; allow it on private
  networks, or nothing will be heard.

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

## Scheduled runs and alerts

Each run keeps timestamped copies in `runs/`, and the `*-latest` files are the newest. The
HTML page and the console list devices that are new, gone, or have moved to a different
switch port since the previous run. New devices are outlined in green on the diagram.

To map the network on its own, register it with Windows Task Scheduler:

```powershell
.\netmapper.exe schedule                          # every day at 08:00
.\netmapper.exe schedule --at 09:00 --every-hours 2 --weekdays   # 09:00 to midnight, Mon-Fri
.\netmapper.exe schedule --status                 # when it last ran, and the result
.\netmapper.exe schedule --run-now                # try it straight away
.\netmapper.exe schedule --remove
```

Running `schedule` again replaces the task. It runs with the config you pass (`-c`, default
`config.toml` in the current folder) and writes its output to `runs/netmapper.log`. Runs
during working hours give the fullest picture, because idle devices drop out of the
switches' MAC tables. A run is skipped if the network is down or the previous one hasn't
finished.

The task runs as you, while you're logged on. A console window flashes up briefly and
hides itself. `--background` runs it whether or not anyone is logged on, with no window at
all, but then Windows notifications can't be shown, and creating it may need an
administrator terminal.

When a run finds a new device, netmapper can tell you. Alerts are all off by default;
turn on the ones you want in `[alerts]` (see [`config.example.toml`](config.example.toml)):

- **Windows notification** on this PC (`desktop = true`).
- **Email** through your mail server.
- **Webhook** to Teams, Slack, Discord, or anything that accepts JSON.

`on = ["new", "gone", "moved"]` alerts on the other changes too. `netmapper test-alert`
sends a sample through every configured channel. Email and webhooks are the only things
netmapper sends off the network, and only when you configure them. Secrets written as
`env:NAME` must be set permanently for the scheduled task to see them, for example
`setx NETMAPPER_WEBHOOK "https://..."` (then open a new terminal).

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
publishes a release with the .exe attached. The .exe is code-signed when signing secrets
are set up; see [docs/code-signing.md](docs/code-signing.md).
