"""netmapper: map a network quietly and draw it.

    netmapper init                    create config.toml for the network this PC is on
    netmapper discover --open         collect data, build the topology, draw the diagram
    netmapper topology [RUN.json]     rebuild topology and diagram from a saved run
    netmapper update-oui              download the MAC vendor list (needs internet)
    netmapper schedule --at 08:00     map the network every day with Task Scheduler
    netmapper test-alert              send a sample alert through each configured channel

Run with no arguments (for example by double-clicking netmapper.exe) to create the
config if needed, map the network and open the diagram.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

from . import __version__
from . import alerts as alerting
from . import schedule as scheduling
from .config import Config, ConfigError, load_config
from .diagram import to_drawio, to_html
from .diff import diff_topologies
from .discovery import _stamp, load_run, run_discovery, save_run, save_topology
from .init import write_starter_config
from .models import DiscoveryRun
from .oui import VendorLookup, download_oui
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
                  open_html: bool = False, alert: bool = False) -> Path:
    """Topology, diff against the previous one, then the draw.io and HTML diagrams.
    With ``alert``, also sends the configured alerts if anything changed."""
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
    if alert:
        _send_alerts(cfg, alerting.build_alert(diff, cfg.alerts.on, html.resolve()))
    if open_html and not os.environ.get("NETMAPPER_NO_BROWSER"):
        webbrowser.open(html.resolve().as_uri())
    return html


def _send_alerts(cfg: Config, alert: alerting.Alert | None) -> bool:
    if alert is None:
        return True
    names = alerting.channels(cfg.alerts)
    if not names:
        return True
    ok = True
    for name, error in alerting.send_alert(alert, cfg.alerts).items():
        print(f"Alert ({name}): {'sent' if error is None else 'failed: ' + error}")
        ok = ok and error is None
    return ok


def _discover(cfg: Config, ping_sweep=False, no_snmp=False, show=False, open_html=False,
              alert=True) -> int:
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
    build_outputs(cfg, run, path, show, open_html, alert=alert)
    return 0


def _log_to_file(cfg: Config) -> None:
    """A scheduled run has no one watching the console, so keep its output in a log."""
    cfg.output_directory.mkdir(parents=True, exist_ok=True)
    log = open(cfg.output_directory / "netmapper.log", "a", encoding="utf-8", buffering=1)
    console = sys.stderr
    sys.stdout = sys.stderr = log
    root = logging.getLogger()
    for handler in root.handlers:
        # Only the console handler basicConfig made, not handlers someone else added.
        if type(handler) is logging.StreamHandler and handler.stream is console:
            handler.setStream(log)
    print(f"\n=== scheduled run {datetime.now().isoformat(timespec='seconds')} ===")


def _schedule(cfg: Config, args) -> int:
    config = Path(args.config)
    if args.remove or args.status or args.run_now:
        action = (scheduling.remove if args.remove else
                  scheduling.status if args.status else scheduling.run_now)
        if not scheduling.IS_WINDOWS:
            print("Task Scheduler is only on Windows.", file=sys.stderr)
            return 1
        try:
            print(action(args.name))
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            print(f"schtasks failed: {exc}", file=sys.stderr)
            return 1
        return 0

    try:
        plan = scheduling.Schedule(at=scheduling.parse_time(args.at),
                                   every_hours=args.every_hours,
                                   weekdays_only=args.weekdays, background=args.background)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.every_hours is not None and not 1 <= args.every_hours <= 23:
        print("--every-hours must be between 1 and 23", file=sys.stderr)
        return 2
    if args.print_xml:
        print(scheduling.task_xml(plan, config))
        return 0
    if not scheduling.IS_WINDOWS:
        print("Task Scheduler is only on Windows. The cron equivalent is:")
        print(f"  {scheduling.cron_line(plan, config)}")
        return 1
    try:
        print(scheduling.install(plan, config, args.name))
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"could not create the scheduled task: {exc}", file=sys.stderr)
        if plan.background:
            print("Running in the background (--background) may need an administrator "
                  "terminal.", file=sys.stderr)
        return 1
    print(f"Scheduled task '{args.name}' maps the network {plan.describe()}.")
    print(f"Output and a log (netmapper.log) go to {cfg.output_directory.resolve()}")
    names = alerting.channels(cfg.alerts)
    if plan.background and "desktop" in names:
        names.remove("desktop")
        print("Background runs can't show Windows notifications; use email or a webhook.")
    if names:
        print(f"Alerts on {', '.join(sorted(cfg.alerts.on))} devices go to: {', '.join(names)}")
    else:
        print("No alerts are configured; add an [alerts] section to the config for them.")
    print("Secrets read with env:NAME must be set permanently (setx NAME value) for the task "
          "to see them.")
    return 0


def _test_alert(cfg: Config) -> int:
    names = alerting.channels(cfg.alerts)
    if not names:
        print("No alert channels are configured. See [alerts] in config.example.toml.")
        return 1
    alert = alerting.Alert(
        title="netmapper: test alert",
        lines=["This is a test. Real alerts list new, gone or moved devices here."],
        diagram=(cfg.output_directory / "network-latest.html").resolve())
    return 0 if _send_alerts(cfg, alert) else 1


def _load(path: str) -> Config | None:
    try:
        return load_config(path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return None


def _double_click() -> int:
    """No arguments: set up if needed, map the network, open the diagram."""
    config = Path("config.toml")
    if not config.exists():
        print(write_starter_config(config))
        print()
    cfg = _load(str(config))
    if cfg is None:
        return 2
    return _discover(cfg, open_html=True)


def _pause_if_double_clicked() -> None:
    # A console window opened by double-clicking closes as soon as we exit.
    if getattr(sys, "frozen", False) and sys.stdin and sys.stdin.isatty():
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

    disc = with_config(sub.add_parser("discover", help="map the network and draw the diagram"))
    disc.add_argument("--ping-sweep", action="store_true",
                      help="also run the slow ping sweep, even if the config turns it off")
    disc.add_argument("--no-snmp", action="store_true", help="skip SNMP and use the ARP cache only")
    disc.add_argument("--show", action="store_true", help="print the topology as an outline")
    disc.add_argument("--open", action="store_true", help="open the diagram in the browser")
    disc.add_argument("--no-alerts", action="store_true", help="don't send alerts for this run")
    disc.add_argument("--scheduled", action="store_true",
                      help="used by the scheduled task: no window, output goes to netmapper.log")

    topo = with_config(sub.add_parser("topology",
                                      help="rebuild topology and diagram from a saved run"))
    topo.add_argument("run", nargs="?", help="discovery JSON (default: the latest run)")
    topo.add_argument("--show", action="store_true", help="print the topology as an outline")
    topo.add_argument("--open", action="store_true", help="open the diagram in the browser")

    with_config(sub.add_parser("update-oui", help="download the IEEE MAC vendor list"))

    sched = with_config(sub.add_parser(
        "schedule", help="run discovery on a schedule with Windows Task Scheduler",
        description="Create (or replace) a Task Scheduler task that runs "
                    "'netmapper discover' with this config. Run it again to change the time."))
    sched.add_argument("--at", default="08:00", help="time of day, HH:MM (default 08:00)")
    sched.add_argument("--every-hours", type=int, metavar="N",
                       help="repeat every N hours from --at until midnight")
    sched.add_argument("--weekdays", action="store_true", help="Monday to Friday only")
    sched.add_argument("--background", action="store_true",
                       help="run even when nobody is logged on, with no window "
                            "(no desktop notifications; may need an admin terminal)")
    sched.add_argument("--name", default=scheduling.DEFAULT_TASK, help="task name")
    sched.add_argument("--print-xml", action="store_true",
                       help="print the task definition instead of creating it")
    what = sched.add_mutually_exclusive_group()
    what.add_argument("--remove", action="store_true", help="delete the scheduled task")
    what.add_argument("--status", action="store_true", help="show the task and its last result")
    what.add_argument("--run-now", action="store_true", help="start the scheduled task now")

    with_config(sub.add_parser("test-alert",
                               help="send a sample alert through each configured channel"))

    args = parser.parse_args(argv)

    if args.command == "init":
        try:
            print(write_starter_config(Path(args.config)))
        except FileExistsError as exc:
            print(f"{exc}; not overwriting it", file=sys.stderr)
            return 1
        return 0

    scheduled = args.command == "discover" and args.scheduled
    if scheduled:
        scheduling.hide_own_console()
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        if scheduled:  # nobody sees stderr, so leave the reason next to the config
            with open(Path(args.config).parent / "netmapper.log", "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat(timespec='seconds')} config error: {exc}\n")
        return 2
    if scheduled:
        _log_to_file(cfg)

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

    if args.command == "schedule":
        return _schedule(cfg, args)

    if args.command == "test-alert":
        return _test_alert(cfg)

    try:
        return _discover(cfg, args.ping_sweep, args.no_snmp, args.show,
                         args.open and not scheduled, alert=not args.no_alerts)
    except Exception:
        if not scheduled:
            raise
        logging.exception("the scheduled run failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
