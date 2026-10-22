"""Command line entry point: ``netmapper discover -c config.toml``."""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .config import ConfigError, load_config
from .discovery import run_discovery, save_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="netmapper", description=__doc__)
    parser.add_argument("--version", action="version", version=f"netmapper {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    disc = sub.add_parser("discover", help="collect network data and save it as JSON")
    disc.add_argument("-c", "--config", default="config.toml", help="path to the config file")
    disc.add_argument("--ping-sweep", action="store_true",
                      help="also run the slow ping sweep, even if the config turns it off")
    disc.add_argument("--no-snmp", action="store_true", help="skip SNMP and use the ARP cache only")

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
