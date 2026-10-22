"""Command line entry point.

    netmapper discover -c config.toml          collect data, then build the topology
    netmapper topology -c config.toml [RUN]    rebuild the topology from a saved run
    netmapper update-oui -c config.toml        download the MAC vendor list
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, load_config
from .discovery import load_run, run_discovery, save_run, save_topology
from .models import DiscoveryRun
from .oui import VendorLookup, download_oui
from .topology import build_topology, lookup_names, render_tree


def _topology(cfg: Config, run: DiscoveryRun, source: Path, show: bool) -> None:
    vendors = VendorLookup.load_if_present(cfg.oui_file)
    topo = build_topology(run, vendors, resolve=lookup_names if cfg.resolve_names else None,
                          source=source.name)
    path = save_topology(topo, cfg.output_directory, run.started_at)
    kinds: dict[str, int] = {}
    for n in topo.nodes:
        kinds[n.kind] = kinds.get(n.kind, 0) + 1
    summary = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
    print(f"Topology: {len(topo.nodes)} nodes, {len(topo.links)} links. {summary}".rstrip())
    for note in topo.notes:
        print(f"  note: {note}")
    print(f"Saved to {path}")
    if show and topo.nodes:
        print()
        print(render_tree(topo))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="netmapper", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"netmapper {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_config(p):
        p.add_argument("-c", "--config", default="config.toml", help="path to the config file")
        return p

    disc = with_config(sub.add_parser("discover", help="collect network data and build the topology"))
    disc.add_argument("--ping-sweep", action="store_true",
                      help="also run the slow ping sweep, even if the config turns it off")
    disc.add_argument("--no-snmp", action="store_true", help="skip SNMP and use the ARP cache only")
    disc.add_argument("--show", action="store_true", help="print the topology as an outline")

    topo = with_config(sub.add_parser("topology", help="rebuild the topology from a saved run"))
    topo.add_argument("run", nargs="?", help="discovery JSON (default: the latest run)")
    topo.add_argument("--show", action="store_true", help="print the topology as an outline")

    with_config(sub.add_parser("update-oui", help="download the IEEE MAC vendor list"))

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    # pysnmp is chatty at debug level and says nothing useful at info.
    logging.getLogger("pysnmp").setLevel(logging.WARNING)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.command == "update-oui":
        try:
            count = download_oui(cfg.oui_file)
        except (OSError, ValueError) as exc:
            print(f"could not download the OUI list: {exc}", file=sys.stderr)
            return 1
        print(f"Saved {count} vendors to {cfg.oui_file}")
        return 0

    if args.command == "topology":
        source = Path(args.run) if args.run else cfg.output_directory / "latest.json"
        if not source.exists():
            print(f"no discovery run at {source}; run 'netmapper discover' first", file=sys.stderr)
            return 2
        _topology(cfg, load_run(source), source, args.show)
        return 0

    if args.ping_sweep:
        cfg.ping_sweep = True
    if args.no_snmp:
        cfg.snmp_enabled = False
    run = run_discovery(cfg)
    path = save_run(run, cfg.output_directory)
    print(f"Found {len(run.hosts)} hosts and {len(run.devices)} managed devices.")
    for note in run.notes:
        print(f"  note: {note}")
    print(f"Saved to {path}")
    _topology(cfg, run, path, args.show)
    return 0


if __name__ == "__main__":
    sys.exit(main())
