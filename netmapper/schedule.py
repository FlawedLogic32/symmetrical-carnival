"""Register netmapper with Windows Task Scheduler so it maps the network on its own.

The task is written as Task Scheduler XML and registered with ``schtasks /Create /XML``,
which (unlike plain ``schtasks`` options) lets us set the working directory, skip runs
while the network is down, stop a run that hangs, and never start two runs at once.
"""

from __future__ import annotations

import getpass
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

IS_WINDOWS = sys.platform == "win32"
DEFAULT_TASK = "netmapper"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")


@dataclass
class Schedule:
    at: str = "08:00"  # HH:MM, local time
    every_hours: int | None = None  # repeat every N hours through the day
    weekdays_only: bool = False
    background: bool = False  # run whether or not anyone is logged on, with no window

    def describe(self) -> str:
        days = "on weekdays" if self.weekdays_only else "every day"
        if self.every_hours:
            return f"every {self.every_hours}h from {self.at}, {days}"
        return f"at {self.at} {days}"


def parse_time(value: str) -> str:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        raise ValueError(f"expected a time like 08:00, not {value!r}")
    return f"{int(match[1]):02d}:{match[2]}"


def command_line(config: Path) -> tuple[str, list[str]]:
    """The program and arguments the task runs: this .exe, or this Python."""
    args = ["discover", "--scheduled", "-c", str(config.resolve())]
    if getattr(sys, "frozen", False):
        return sys.executable, args
    return sys.executable, ["-m", "netmapper", *args]


def _quote(arg: str) -> str:
    return f'"{arg}"' if not arg or any(c in arg for c in ' \t"') else arg


def _current_user() -> str:
    domain = os.environ.get("USERDOMAIN")
    user = os.environ.get("USERNAME") or getpass.getuser()
    return f"{domain}\\{user}" if domain else user


def task_xml(schedule: Schedule, config: Path, user: str | None = None,
             today: datetime | None = None) -> str:
    program, args = command_line(config)
    user = user or _current_user()
    start = (today or datetime.now()).strftime("%Y-%m-%d") + f"T{schedule.at}:00"
    repeat = ""
    if schedule.every_hours:
        hour, minute = (int(x) for x in schedule.at.split(":"))
        left = 24 * 60 - (hour * 60 + minute)  # repeat until midnight
        repeat = (f"<Repetition><Interval>PT{schedule.every_hours}H</Interval>"
                  f"<Duration>PT{left // 60}H{left % 60}M</Duration><StopAtDurationEnd>false</StopAtDurationEnd>"
                  "</Repetition>")
    if schedule.weekdays_only:
        days = "".join(f"<{d} />" for d in WEEKDAYS)
        when = f"<ScheduleByWeek><DaysOfWeek>{days}</DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek>"
    else:
        when = "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
    # S4U runs without a password being stored and without a window, but has no desktop,
    # so Windows notifications can't be shown from it.
    logon = "S4U" if schedule.background else "InteractiveToken"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>netmapper: map the network quietly and alert on changes ({escape(schedule.describe())}).</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      {repeat}<StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      {when}
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>{logon}</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <Priority>7</Priority>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(_quote(program))}</Command>
      <Arguments>{escape(" ".join(_quote(a) for a in args))}</Arguments>
      <WorkingDirectory>{escape(str(config.resolve().parent))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def cron_line(schedule: Schedule, config: Path) -> str:
    """The nearest crontab equivalent, for running from source on Linux or macOS."""
    hour, minute = (int(x) for x in schedule.at.split(":"))
    hours = f"{hour}-23/{schedule.every_hours}" if schedule.every_hours else str(hour)
    days = "1-5" if schedule.weekdays_only else "*"
    program, args = command_line(config)
    return f"{minute} {hours} * * {days} " + " ".join(_quote(a) for a in [program, *args])


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks.exe", *args], capture_output=True, text=True, timeout=60)


def _check(result: subprocess.CompletedProcess) -> str:
    text = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(text or f"schtasks exited {result.returncode}")
    return text


def install(schedule: Schedule, config: Path, name: str = DEFAULT_TASK) -> str:
    xml = task_xml(schedule, config)
    # schtasks only reads task XML as UTF-16.
    fd, path = tempfile.mkstemp(suffix=".xml", prefix="netmapper-task-")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as f:
            f.write(xml)
        return _check(_schtasks("/Create", "/TN", name, "/XML", path, "/F"))
    finally:
        os.unlink(path)


def remove(name: str = DEFAULT_TASK) -> str:
    return _check(_schtasks("/Delete", "/TN", name, "/F"))


def status(name: str = DEFAULT_TASK) -> str:
    return _check(_schtasks("/Query", "/TN", name, "/V", "/FO", "LIST"))


def run_now(name: str = DEFAULT_TASK) -> str:
    return _check(_schtasks("/Run", "/TN", name))


# --- Running as the scheduled task ------------------------------------------------------

def hide_own_console() -> None:
    """Hide the console window Windows opens for the scheduled run.

    Only when the console is ours alone: run by hand from a terminal, the terminal's
    shell shares it, and hiding it would hide the user's terminal.
    """
    if not IS_WINDOWS:
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    hwnd = kernel32.GetConsoleWindow()
    if not hwnd:
        return
    pids = (ctypes.c_uint32 * 16)()
    count = kernel32.GetConsoleProcessList(pids, 16)
    ours = {os.getpid()}
    if getattr(sys, "frozen", False):
        ours.add(os.getppid())  # PyInstaller's one-file .exe is a launcher plus a child
    if 0 < count <= 16 and set(pids[:count]) <= ours:
        ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
