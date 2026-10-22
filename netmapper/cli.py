"""netmapper: map a network quietly and draw it.

    netmapper init                    create config.toml for the network this PC is on
    netmapper snmp-setup              add SNMP (v3 or v2c) credentials, step by step
    netmapper discover --open         collect data, build the topology, draw the diagram
    netmapper topology [RUN.json]     rebuild topology and diagram from a saved run
    netmapper update-oui              download the MAC vendor list (needs internet)

Run with no arguments (for example by double-clicking netmapper.exe) to create the
config if needed, map the network and open the diagram.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tomllib
import webbrowser
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, load_config
from .diagram import to_drawio, to_html
from .diff import diff_topologies
from .discovery import _stamp, load_run, run_discovery, save_run, save_topology
from .init import write_starter_config
from .models import DiscoveryRun
from .oui import VendorLookup, download_oui
from .snmp_setup import run_setup
from .topology import Topology, build_topology, lookup_names, render_tree


def _previous_topology(cfg: Config) -> Topology | None:
    path = cfg.output_directory / "topology-latest.json"
    try:
        return Topology.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def _write_text(directory: Path, name: str, latest: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for target in (directory / name, directory / latest):
        target.write_text(text, encoding="utf-8")
    return directory / name


def build_outputs(cfg: Config, run: DiscoveryRun, source: Path, show: bool = False,
                  open_html: bool = False) -> Path:
    """Topology, diff against the previous one, then the draw.io and HTML diagrams."""
    previous = _previous_topology(cfg)
    vendors = VendorLookup.load_if_present(cfg.oui_file)
    topo = build_topology(run, vendors, resolve=lookup_names if cfg.resolve_names else None,
                          source=source.name)
    diff = diff_topologies(previous, topo)
    save_topology(topo, cfg.output_directory, run.started_at)

    stamp = _stamp(run.started_at)
    drawio = _write_text(cfg.output_directory, f"network-{stamp}.drawio", "network-latest.drawio",
                         to_drawio(topo, new_ids={n.id for n in diff.added}))
    html = _write_text(cfg.output_directory, f"network-{stamp}.html", "network-latest.html",
                       to_html(topo, diff, first_run=previous is None))

    kinds: dict[str, int] = {}
    for n in topo.nodes:
        kinds[n.kind] = kinds.get(n.kind, 0) + 1
    summary = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
    print(f"Topology: {len(topo.nodes)} devices, {len(topo.links)} links. {summary}".rstrip())
    for note in topo.notes:
        print(f"  note: {note}")
    if previous is None:
        print("First run: nothing to compare against yet.")
    elif diff.empty:
        print("No changes since the last run.")
    else:
        print("Changes since the last run:")
        for line in diff.lines():
            print(f"  {line}")
    print(f"Diagram: {html.resolve()}")
    print(f"Editable diagram (draw.io): {drawio.resolve()}")
    if show and topo.nodes:
        print()
        print(render_tree(topo))
    if open_html and not os.environ.get("NETMAPPER_NO_BROWSER"):
        webbrowser.open(html.resolve().as_uri())
    return html


def _discover(cfg: Config, ping_sweep=False, no_snmp=False, show=False, open_html=False) -> int:
    if ping_sweep:
        cfg.ping_sweep = True
    if no_snmp:
        cfg.snmp_enabled = False
    run = run_discovery(cfg)
    path = save_run(run, cfg.output_directory)
    print(f"Found {len(run.hosts)} hosts and {len(run.devices)} managed devices.")
    for note in run.notes:
        print(f"  note: {note}")
    print(f"Raw data: {path}")
    build_outputs(cfg, run, path, show, open_html)
    return 0


def _load(path: str) -> Config | None:
    try:
        return load_config(path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return None


def _snmp_setup(path: Path) -> int:
    """Run the credential wizard. Reads the raw file, so it works even when the
    current config can't load yet (for example an env: secret that isn't set)."""
    from .collectors.snmp_client import PySnmpClient

    try:
        snmp = tomllib.loads(path.read_text(encoding="utf-8")).get("snmp", {})
    except (OSError, tomllib.TOMLDecodeError):
        snmp = {}

    def factory(ip, cred):
        return PySnmpClient(ip, cred, timeout=float(snmp.get("timeout_seconds", 2.0)),
                            retries=int(snmp.get("retries", 1)), port=int(snmp.get("port", 161)))

    return run_setup(path, factory, seeds=[str(s) for s in snmp.get("seeds", [])])


def _interactive() -> bool:
    return bool(sys.stdin and sys.stdin.isatty())


def _double_click() -> int:
    """No arguments: set up if needed, map the network, open the diagram."""
    config = Path("config.toml")
    if not config.exists():
        print(write_starter_config(config))
        print()
        if _interactive():
            answer = input("Do you have SNMP login details for your managed switches? "
                           "Set them up now? [y/N]: ").strip().lower()
            if answer.startswith("y"):
                _snmp_setup(config)
            else:
                print("OK. You can add them later with 'netmapper snmp-setup'.")
            print()
    cfg = _load(str(config))
    if cfg is None:
        return 2
    return _discover(cfg, open_html=True)


def _pause_if_double_clicked() -> None:
    # A console window opened by double-clicking closes as soon as we exit.
    if getattr(sys, "frozen", False) and _interactive():
        try:
            input("\nPress Enter to close.")
        except EOFError:
            pass


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.DEBUG if "-v" in argv or "--verbose" in argv
                        else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    # pysnmp is chatty at debug level and says nothing useful at info.
    logging.getLogger("pysnmp").setLevel(logging.WARNING)

    if not argv:
        try:
            return _double_click()
        finally:
            _pause_if_double_clicked()

    parser = argparse.ArgumentParser(prog="netmapper", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"netmapper {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_config(p):
        p.add_argument("-c", "--config", default="config.toml", help="path to the config file")
        return p

    init = sub.add_parser("init", help="create a starter config.toml for this network")
    init.add_argument("-c", "--config", default="config.toml", help="where to write it")

    with_config(sub.add_parser("snmp-setup",
                               help="add SNMP credentials step by step, test and save them"))

    disc = with_config(sub.add_parser("discover", help="map the network and draw the diagram"))
    disc.add_argument("--ping-sweep", action="store_true",
                      help="also run the slow ping sweep, even if the config turns it off")
    disc.add_argument("--no-snmp", action="store_true", help="skip SNMP and use the ARP cache only")
    disc.add_argument("--show", action="store_true", help="print the topology as an outline")
    disc.add_argument("--open", action="store_true", help="open the diagram in the browser")

    topo = with_config(sub.add_parser("topology",
                                      help="rebuild topology and diagram from a saved run"))
    topo.add_argument("run", nargs="?", help="discovery JSON (default: the latest run)")
    topo.add_argument("--show", action="store_true", help="print the topology as an outline")
    topo.add_argument("--open", action="store_true", help="open the diagram in the browser")

    with_config(sub.add_parser("update-oui", help="download the IEEE MAC vendor list"))

    args = parser.parse_args(argv)

    if args.command == "init":
        try:
            print(write_starter_config(Path(args.config)))
        except FileExistsError as exc:
            print(f"{exc}; not overwriting it", file=sys.stderr)
            return 1
        return 0

    if args.command == "snmp-setup":
        return _snmp_setup(Path(args.config))

    cfg = _load(args.config)
    if cfg is None:
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
        build_outputs(cfg, load_run(source), source, args.show, args.open)
        return 0

    return _discover(cfg, args.ping_sweep, args.no_snmp, args.show, args.open)


if __name__ == "__main__":
    sys.exit(main())
