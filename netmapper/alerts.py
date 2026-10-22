"""Tell someone when the network changes: a Windows notification, an email or a webhook.

Alerts go out after a run only when the diff against the previous run has something the
config asks about (by default, new devices). The first run never alerts, because there is
nothing to compare against. A channel that fails is logged and skipped, so a broken mail
server never stops the map from being drawn.

Email and webhooks are the only traffic netmapper sends off the local network, and only
when they are configured. Desktop notifications stay on the PC.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

from .config import AlertsConfig, EmailAlert, WebhookAlert
from .diff import TopologyDiff, _describe

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
MAX_LINES = 25  # longer lists are cut, with "and N more"


@dataclass
class Alert:
    title: str
    lines: list[str] = field(default_factory=list)
    diagram: Path | None = None
    new: list[dict] = field(default_factory=list)
    gone: list[dict] = field(default_factory=list)
    moved: list[dict] = field(default_factory=list)

    def body(self, limit: int = MAX_LINES, with_diagram: bool = True) -> str:
        lines = self.lines[:limit]
        if len(self.lines) > limit:
            lines.append(f"...and {len(self.lines) - limit} more")
        if self.diagram and with_diagram:
            lines += ["", f"Diagram: {self.diagram}"]
        return "\n".join(lines)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _node(n) -> dict:
    return {"label": n.label, "kind": n.kind, "ip": n.ip, "mac": n.mac, "vendor": n.vendor}


def build_alert(diff: TopologyDiff, events: set[str], diagram: Path | None = None) -> Alert | None:
    """The alert for this diff, or None when nothing the config cares about changed."""
    added = diff.added if "new" in events else []
    removed = diff.removed if "gone" in events else []
    moved = diff.moved if "moved" in events else []
    if not (added or removed or moved):
        return None
    parts = []
    if added:
        parts.append(f"{_plural(len(added), 'new device')}")
    if removed:
        parts.append(f"{_plural(len(removed), 'device')} gone")
    if moved:
        parts.append(f"{_plural(len(moved), 'device')} moved")
    lines = [f"New: {_describe(n)}" for n in added]
    lines += [f"Gone: {_describe(n)}" for n in removed]
    lines += [f"Moved: {_describe(n)} from {was} to {now}" for n, was, now in moved]
    return Alert(
        title="netmapper: " + ", ".join(parts),
        lines=lines,
        diagram=diagram,
        new=[_node(n) for n in added],
        gone=[_node(n) for n in removed],
        moved=[dict(_node(n), was=was, now=now) for n, was, now in moved],
    )


# --- Windows notification -------------------------------------------------------------

# Shows a toast through the Windows Runtime, borrowing PowerShell's app id so no
# registration is needed. The text arrives in environment variables, never pasted into
# the script, and the script is piped in on stdin rather than as an encoded command,
# which endpoint protection rightly treats as suspicious.
_TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$title = [Security.SecurityElement]::Escape($env:NETMAPPER_TOAST_TITLE)
$body = [Security.SecurityElement]::Escape($env:NETMAPPER_TOAST_BODY)
$launch = [Security.SecurityElement]::Escape($env:NETMAPPER_TOAST_LAUNCH)
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast activationType='protocol' launch='$launch'><visual><binding template='ToastGeneric'><text>$title</text><text>$body</text></binding></visual></toast>")
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show(
    [Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def send_desktop(alert: Alert) -> None:
    if not IS_WINDOWS:
        raise RuntimeError("desktop notifications are only supported on Windows")
    env = dict(os.environ,
               NETMAPPER_TOAST_TITLE=alert.title,
               NETMAPPER_TOAST_BODY=alert.body(limit=3, with_diagram=False),
               NETMAPPER_TOAST_LAUNCH=alert.diagram.resolve().as_uri() if alert.diagram else "")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", "-"],
        input=_TOAST_SCRIPT, env=env, capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise RuntimeError(detail[0] if detail else f"powershell exited {result.returncode}")


# --- Email ----------------------------------------------------------------------------

def send_email(alert: Alert, cfg: EmailAlert, timeout: float = 30) -> None:
    msg = EmailMessage()
    msg["Subject"] = alert.title
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(cfg.to)
    msg.set_content(alert.body(limit=500) + "\n\nSent by netmapper.\n")
    if cfg.security == "ssl":
        server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=timeout,
                                  context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=timeout)
    with server:
        if cfg.security == "starttls":
            server.starttls(context=ssl.create_default_context())
        if cfg.username and cfg.password:
            server.login(cfg.username, cfg.password)
        server.send_message(msg)


# --- Webhook --------------------------------------------------------------------------

def webhook_payload(alert: Alert, fmt: str) -> dict:
    text = alert.body()
    if fmt == "slack":
        return {"text": f"*{alert.title}*\n{text}"}
    if fmt == "discord":
        return {"content": f"**{alert.title}**\n{text}"[:2000]}
    if fmt == "teams":
        # An Adaptive Card, which Teams "Workflows" incoming webhooks expect.
        blocks = [{"type": "TextBlock", "text": alert.title, "weight": "Bolder",
                   "size": "Medium", "wrap": True}]
        blocks += [{"type": "TextBlock", "text": line, "wrap": True, "spacing": "None"}
                   for line in text.splitlines() if line]
        return {"type": "message", "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard", "version": "1.4", "body": blocks}}]}
    return {"text": f"{alert.title}\n{text}", "title": alert.title,
            "new": alert.new, "gone": alert.gone, "moved": alert.moved,
            "diagram": str(alert.diagram) if alert.diagram else None}


def send_webhook(alert: Alert, cfg: WebhookAlert, timeout: float = 30) -> None:
    data = json.dumps(webhook_payload(alert, cfg.format)).encode("utf-8")
    req = urllib.request.Request(cfg.url, data=data, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": "netmapper"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"HTTP {resp.status}")


# --- Sending --------------------------------------------------------------------------

def channels(cfg: AlertsConfig) -> list[str]:
    out = []
    if cfg.desktop and IS_WINDOWS:
        out.append("desktop")
    if cfg.email:
        out.append("email")
    if cfg.webhook:
        out.append("webhook")
    return out


def send_alert(alert: Alert, cfg: AlertsConfig) -> dict[str, str | None]:
    """Send through every configured channel. Returns {channel: error or None}."""
    senders = {
        "desktop": lambda: send_desktop(alert),
        "email": lambda: send_email(alert, cfg.email),
        "webhook": lambda: send_webhook(alert, cfg.webhook),
    }
    results: dict[str, str | None] = {}
    for name in channels(cfg):
        try:
            senders[name]()
            results[name] = None
        except Exception as exc:  # noqa: BLE001 - one broken channel mustn't stop the rest
            log.warning("could not send the %s alert: %s", name, exc)
            results[name] = str(exc) or type(exc).__name__
    return results
