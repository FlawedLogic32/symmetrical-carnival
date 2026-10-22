import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import pytest
from fake_snmp import FakeSnmp
from test_snmp_device import core_switch

from netmapper import alerts, cli
from netmapper.config import AlertsConfig, ConfigError, EmailAlert, WebhookAlert, config_from_dict
from netmapper.diff import TopologyDiff
from netmapper.models import Host
from netmapper.topology import Node


class Hook:
    """A local webhook receiver that records each JSON body posted to it."""

    def __init__(self):
        received = self.received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def hook():
    h = Hook()
    yield h
    h.close()


def node(label, ip=None):
    return Node(id=label, kind="host", label=label, ip=ip, mac="a4:5e:60:01:01:01")


def test_alert_config(monkeypatch):
    monkeypatch.setenv("HOOK", "https://example.invalid/x")
    cfg = config_from_dict({"alerts": {
        "on": ["new", "gone"], "desktop": False,
        "email": {"smtp_host": "mail.example.com", "to": "it@example.com",
                  "security": "ssl"},
        "webhook": {"url": "env:HOOK", "format": "teams"}}})
    assert cfg.alerts.on == {"new", "gone"} and not cfg.alerts.desktop
    assert cfg.alerts.email.smtp_port == 465 and cfg.alerts.email.to == ["it@example.com"]
    assert cfg.alerts.webhook.url == "https://example.invalid/x"
    assert config_from_dict({}).alerts.on == {"new"}
    assert config_from_dict({}).alerts.email is None


def test_bad_alert_config():
    with pytest.raises(ConfigError, match="unknown events"):
        config_from_dict({"alerts": {"on": ["new", "rebooted"]}})
    with pytest.raises(ConfigError, match="'to'"):
        config_from_dict({"alerts": {"email": {"smtp_host": "mail"}}})
    with pytest.raises(ConfigError, match="https"):
        config_from_dict({"alerts": {"webhook": {"url": "ftp://x"}}})


def test_build_alert_only_for_chosen_events():
    diff = TopologyDiff(added=[node("192.168.1.77", "192.168.1.77")], removed=[node("old-pc")])
    assert alerts.build_alert(TopologyDiff(), {"new"}) is None
    assert alerts.build_alert(TopologyDiff(removed=[node("old-pc")]), {"new"}) is None
    alert = alerts.build_alert(diff, {"new", "gone"})
    assert alert.title == "netmapper: 1 new device, 1 device gone"
    assert alert.lines[0].startswith("New: 192.168.1.77")
    assert alert.new[0]["ip"] == "192.168.1.77"

    many = alerts.Alert("t", lines=[str(i) for i in range(30)])
    assert many.body().splitlines()[-1] == "...and 5 more"


def test_webhook_formats(hook):
    alert = alerts.Alert("netmapper: 1 new device", lines=["New: x"], new=[{"label": "x"}])
    for fmt in ("json", "slack", "discord", "teams"):
        alerts.send_webhook(alert, WebhookAlert(url=hook.url, format=fmt))
    generic, slack, discord, teams = hook.received
    assert generic["new"] == [{"label": "x"}] and "New: x" in generic["text"]
    assert slack["text"].startswith("*netmapper")
    assert "New: x" in discord["content"]
    card = teams["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard" and card["body"][0]["text"] == alert.title


def test_email():
    cfg = EmailAlert(smtp_host="mail.example.com", to=["a@example.com", "b@example.com"],
                     sender="netmapper@example.com", username="u", password="p")
    with mock.patch("smtplib.SMTP") as smtp:
        alerts.send_email(alerts.Alert("netmapper: 1 new device", ["New: x"]), cfg)
    server = smtp.return_value
    server.starttls.assert_called_once()
    server.login.assert_called_once_with("u", "p")
    msg = server.send_message.call_args[0][0]
    assert msg["To"] == "a@example.com, b@example.com"
    assert "New: x" in msg.get_content()


def test_desktop_notification_passes_text_safely():
    alert = alerts.Alert("netmapper: 1 new device", ["New: <evil> 'quote'"])
    done = mock.Mock(returncode=0, stdout="", stderr="")
    with mock.patch.object(alerts, "IS_WINDOWS", True), \
            mock.patch("subprocess.run", return_value=done) as run:
        alerts.send_desktop(alert)
    cmd, kwargs = run.call_args[0][0], run.call_args[1]
    assert cmd[0] == "powershell.exe" and "-EncodedCommand" not in cmd
    assert kwargs["input"] == alerts._TOAST_SCRIPT  # the text never goes into the script
    assert kwargs["env"]["NETMAPPER_TOAST_BODY"] == "New: <evil> 'quote'"


def test_one_failing_channel_does_not_stop_the_others(hook):
    cfg = AlertsConfig(desktop=False, webhook=WebhookAlert(url=hook.url),
                       email=EmailAlert(smtp_host="mail", to=["a@b"], sender="n@b"))
    with mock.patch("smtplib.SMTP", side_effect=OSError("connection refused")):
        results = alerts.send_alert(alerts.Alert("t", ["x"]), cfg)
    assert results == {"email": "connection refused", "webhook": None}
    assert len(hook.received) == 1


def _config(tmp_path, hook_url):
    config = tmp_path / "config.toml"
    config.write_text(
        '[discovery]\nsubnets = ["192.168.1.0/24"]\n'
        '[snmp]\nseeds = ["192.168.1.2"]\n'
        '[[snmp.credentials]]\nversion = "2c"\ncommunity = "public"\n'
        f'[alerts]\ndesktop = false\n[alerts.webhook]\nurl = "{hook_url}"\n', encoding="utf-8")
    return config


def _discover(config, hosts, *extra):
    def factory(cfg):
        return lambda ip, cred: FakeSnmp(core_switch(neighbor_ip="10.9.9.9"))

    with mock.patch("netmapper.discovery._default_factory", factory), \
            mock.patch("netmapper.discovery.local.read_arp_cache", return_value=hosts):
        return cli.main(["discover", "-c", str(config), *extra])


def test_discover_alerts_on_new_devices_only(tmp_path, hook, capsys):
    config = _config(tmp_path, hook.url)
    first = [Host("192.168.1.20", "a4:5e:60:01:01:01", ["arp-cache"])]
    second = first + [Host("192.168.1.77", "a4:5e:60:09:09:09", ["arp-cache"])]

    assert _discover(config, first) == 0
    assert hook.received == []  # first run: nothing to compare against
    assert _discover(config, first) == 0
    assert hook.received == []  # nothing changed
    assert _discover(config, second, "--no-alerts") == 0
    assert hook.received == []
    assert _discover(config, second + [Host("192.168.1.78", "a4:5e:60:09:09:0a", ["arp-cache"])]) == 0
    assert len(hook.received) == 1
    assert hook.received[0]["title"] == "netmapper: 1 new device"
    assert hook.received[0]["new"][0]["ip"] == "192.168.1.78"
    assert "Alert (webhook): sent" in capsys.readouterr().out
    assert _discover(config, first) == 0  # devices gone: not alerted by default
    assert len(hook.received) == 1


def test_test_alert_command(tmp_path, hook):
    config = _config(tmp_path, hook.url)
    assert cli.main(["test-alert", "-c", str(config)]) == 0
    assert hook.received[0]["title"] == "netmapper: test alert"
