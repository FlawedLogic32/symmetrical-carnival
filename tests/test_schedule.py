import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest
from test_alerts import _discover

from netmapper import cli, schedule
from netmapper.models import Host

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def parse(xml: str):
    return ET.fromstring(xml.split("?>", 1)[1])


def test_task_xml_daily(tmp_path):
    config = tmp_path / "my net" / "config.toml"
    xml = schedule.task_xml(schedule.Schedule(at="07:30"), config, user="OFFICE\\tom",
                            today=datetime(2026, 10, 22))
    task = parse(xml)
    assert task.find(".//t:StartBoundary", NS).text == "2026-10-22T07:30:00"
    assert task.find(".//t:ScheduleByDay/t:DaysInterval", NS).text == "1"
    assert task.find(".//t:Repetition", NS) is None
    assert task.find(".//t:LogonType", NS).text == "InteractiveToken"
    assert task.find(".//t:UserId", NS).text == "OFFICE\\tom"
    assert task.find(".//t:MultipleInstancesPolicy", NS).text == "IgnoreNew"
    args = task.find(".//t:Arguments", NS).text
    assert "discover --scheduled -c" in args
    assert f'"{config.resolve()}"' in args  # quoted, because of the space
    assert task.find(".//t:WorkingDirectory", NS).text == str(config.resolve().parent)


def test_task_xml_hourly_weekdays_background(tmp_path):
    plan = schedule.Schedule(at="08:00", every_hours=2, weekdays_only=True, background=True)
    task = parse(schedule.task_xml(plan, tmp_path / "config.toml", user="tom"))
    assert task.find(".//t:Repetition/t:Interval", NS).text == "PT2H"
    assert task.find(".//t:Repetition/t:Duration", NS).text == "PT16H"
    days = [d.tag.split("}")[1] for d in task.find(".//t:DaysOfWeek", NS)]
    assert days == list(schedule.WEEKDAYS)
    assert task.find(".//t:LogonType", NS).text == "S4U"
    assert plan.describe() == "every 2h from 08:00, on weekdays"


def test_no_repetition_when_too_late_to_repeat(tmp_path):
    plan = schedule.Schedule(at="23:30", every_hours=1)
    task = parse(schedule.task_xml(plan, tmp_path / "config.toml", user="tom"))
    assert task.find(".//t:Repetition", NS) is None
    plan = schedule.Schedule(at="08:15", every_hours=1)
    task = parse(schedule.task_xml(plan, tmp_path / "config.toml", user="tom"))
    assert task.find(".//t:Repetition/t:Duration", NS).text == "PT15H45M"


def test_parse_time_and_cron(tmp_path):
    assert schedule.parse_time("7:05") == "07:05"
    for bad in ("25:00", "8", "08:60"):
        with pytest.raises(ValueError):
            schedule.parse_time(bad)
    line = schedule.cron_line(schedule.Schedule(at="08:15", every_hours=3, weekdays_only=True),
                              tmp_path / "config.toml")
    assert line.startswith("15 8-23/3 * * 1-5 ")
    assert line.endswith(f"discover --scheduled -c {tmp_path.resolve() / 'config.toml'}")


def test_schedule_command_installs_with_schtasks(tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text('[alerts]\ndesktop = true\n', encoding="utf-8")
    seen = {}

    def fake_schtasks(*args):
        seen["args"] = args
        if "/XML" in args:
            seen["xml"] = Path(args[args.index("/XML") + 1]).read_text(encoding="utf-16")
        return mock.Mock(returncode=0, stdout="SUCCESS: created", stderr="")

    with mock.patch.object(schedule, "IS_WINDOWS", True), \
            mock.patch.object(cli.alerting, "IS_WINDOWS", True), \
            mock.patch.object(schedule, "_schtasks", side_effect=fake_schtasks):
        assert cli.main(["schedule", "-c", str(config), "--at", "9:00", "--weekdays"]) == 0
        out = capsys.readouterr().out
        assert "maps the network at 09:00 on weekdays" in out
        assert "go to: desktop" in out
        assert seen["args"][:3] == ("/Create", "/TN", "netmapper")
        assert "<StartBoundary>" in seen["xml"] and "T09:00:00" in seen["xml"]

        assert cli.main(["schedule", "-c", str(config), "--remove"]) == 0
        assert seen["args"] == ("/Delete", "/TN", "netmapper", "/F")


def test_schedule_command_elsewhere(tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    with mock.patch.object(schedule, "IS_WINDOWS", False):
        assert cli.main(["schedule", "-c", str(config)]) == 1
        assert "cron" in capsys.readouterr().out
    assert cli.main(["schedule", "-c", str(config), "--print-xml"]) == 0
    assert "<Task " in capsys.readouterr().out
    assert cli.main(["schedule", "-c", str(config), "--every-hours", "30"]) == 2


def test_scheduled_run_logs_to_file(tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text('[discovery]\nsubnets = ["192.168.1.0/24"]\n[snmp]\nenabled = false\n'
                      '[alerts]\ndesktop = false\n', encoding="utf-8")
    import sys
    stdout, stderr = sys.stdout, sys.stderr
    try:
        assert _discover(config, [Host("192.168.1.20", "a4:5e:60:01:01:01", ["arp-cache"])],
                         "--scheduled") == 0
    finally:
        log_file = sys.stdout
        sys.stdout, sys.stderr = stdout, stderr
        log_file.close()
    log = (tmp_path / "runs" / "netmapper.log").read_text(encoding="utf-8")
    assert "=== scheduled run" in log and "Topology:" in log


def test_scheduled_run_records_config_errors(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[alerts]\non = ["bogus"]\n', encoding="utf-8")
    assert cli.main(["discover", "--scheduled", "-c", str(config)]) == 2
    assert "unknown events" in (tmp_path / "netmapper.log").read_text(encoding="utf-8")
