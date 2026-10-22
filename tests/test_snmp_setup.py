import sys
import tomllib

import pytest
from fake_snmp import FakeSnmp
from test_snmp_device import core_switch

from netmapper import secret_store
from netmapper.collectors.snmp_client import SnmpError
from netmapper.config import ConfigError, check_v3, config_from_dict, load_config
from netmapper.init import render_config
from netmapper.snmp_setup import Prompter, add_seed, diagnose, run_setup


class Script:
    """Feeds scripted answers to the wizard and records what it printed."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.output = []

    def next(self, prompt):
        self.output.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def prompter(self):
        return Prompter(ask=self.next, ask_secret=self.next, say=self.output.append)

    @property
    def text(self):
        return "\n".join(self.output)


class Agent:
    """A factory that accepts one credential and fails the rest like a real v3 agent."""

    def __init__(self, check):
        self.check = check
        self.tried = []

    def __call__(self, ip, cred):
        self.tried.append((ip, cred))
        error = self.check(cred)
        return FakeSnmp(core_switch()) if error is None else Failing(error)


class Failing(FakeSnmp):
    def __init__(self, error):
        super().__init__({})
        self.error = error

    def get(self, oid):
        raise SnmpError(self.error)


def _config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(render_config("192.168.7.23", "192.168.7.1"), encoding="utf-8")
    return path


def _v3_answers(store="plain"):
    return ["3", "netmapper", "", "sha", "auth-pass-1", "auth-pass-1", "aes", "n",
            "priv-pass-1", "priv-pass-1", "192.168.7.2", store]


def test_v3_setup_tests_and_saves(tmp_path):
    path = _config(tmp_path)
    good = lambda c: None if (c.auth_key, c.priv_key) == ("auth-pass-1", "priv-pass-1") \
        else "Wrong SNMP PDU digest"  # noqa: E731
    agent = Agent(good)
    script = Script(_v3_answers())
    assert run_setup(path, agent, script.prompter(), seeds=["192.168.7.1"]) == 0

    assert agent.tried[0][0] == "192.168.7.2"
    assert "Success" in script.text and "core-sw" in script.text
    cfg = load_config(path)
    (cred,) = cfg.snmp_credentials
    assert (cred.version, cred.username, cred.auth_protocol, cred.priv_protocol) == \
        ("3", "netmapper", "sha", "aes")
    assert cred.priv_key == "priv-pass-1"
    assert cfg.snmp_seeds == ["192.168.7.1", "192.168.7.2"]
    # Re-reading the file works, and the rest of it was left alone.
    assert "[topology]" in path.read_text(encoding="utf-8")


def test_env_storage_never_writes_the_secret(tmp_path, monkeypatch):
    path = _config(tmp_path)
    script = Script(_v3_answers(store="env"))
    assert run_setup(path, Agent(lambda c: None), script.prompter()) == 0
    text = path.read_text(encoding="utf-8")
    assert "auth-pass-1" not in text and "priv-pass-1" not in text
    assert 'auth_key = "env:SNMP_AUTH_NETMAPPER"' in text
    assert "setx SNMP_PRIV_NETMAPPER" in script.text
    assert "auth-pass-1" not in script.text
    monkeypatch.setenv("SNMP_AUTH_NETMAPPER", "auth-pass-1")
    monkeypatch.setenv("SNMP_PRIV_NETMAPPER", "priv-pass-1")
    assert load_config(path).snmp_credentials[0].priv_key == "priv-pass-1"


def test_failure_is_explained_and_not_saved_by_default(tmp_path):
    path = _config(tmp_path)
    before = path.read_text(encoding="utf-8")
    script = Script(_v3_answers()[:-1] + ["n"])  # "Save it anyway?" -> no
    agent = Agent(lambda c: "Unknown USM user")
    assert run_setup(path, agent, script.prompter()) == 1
    assert "doesn't know that username" in script.text
    assert path.read_text(encoding="utf-8") == before


def test_aes256_key_variant_is_tried_automatically(tmp_path):
    path = _config(tmp_path)
    answers = _v3_answers()
    answers[6] = "aes256"
    cisco = lambda c: None if c.priv_protocol == "aes256c" else \
        "Ciphering services not available or ciphertext is broken"  # noqa: E731
    script = Script(answers)
    assert run_setup(path, Agent(cisco), script.prompter()) == 0
    assert "works with aes256c" in script.text
    assert load_config(path).snmp_credentials[0].priv_protocol == "aes256c"


def test_short_passphrase_and_bad_choice_are_asked_again(tmp_path):
    path = _config(tmp_path)
    answers = ["4", "3", "", "netmapper", "authnopriv", "md5", "short", "longenough",
               "longenough", "-", "plain"]
    script = Script(answers)
    assert run_setup(path, Agent(lambda c: None), script.prompter()) == 0
    assert "Please type one of: 3, 2c" in script.text
    assert "A username is needed" in script.text
    assert "at least 8 characters" in script.text
    cred = load_config(path).snmp_credentials[0]
    assert (cred.auth_protocol, cred.priv_protocol) == ("md5", None)


def test_v2c_setup(tmp_path):
    path = _config(tmp_path)
    script = Script(["2c", "s3cret", "s3cret", "", "plain"])
    assert run_setup(path, Agent(lambda c: None), script.prompter(),
                     seeds=["192.168.7.1"]) == 0
    assert load_config(path).snmp_credentials[0].community == "s3cret"


def test_cancel_saves_nothing(tmp_path):
    path = _config(tmp_path)
    before = path.read_text(encoding="utf-8")
    assert run_setup(path, Agent(lambda c: None), Script(["3", "bob"]).prompter()) == 1
    assert path.read_text(encoding="utf-8") == before


def test_add_seed():
    assert add_seed('seeds = ["10.0.0.1"]\n', "10.0.0.2") == 'seeds = ["10.0.0.1", "10.0.0.2"]\n'
    assert add_seed("seeds = []\n", "10.0.0.2") == 'seeds = ["10.0.0.2"]\n'
    assert add_seed('seeds = ["10.0.0.2"]\n', "10.0.0.2") == 'seeds = ["10.0.0.2"]\n'
    assert add_seed('seeds = [\n  "10.0.0.1",\n]\n', "10.0.0.2") is None


def test_diagnose():
    assert "authentication passphrase" in diagnose("Wrong SNMP PDU digest")
    assert "encryption" in diagnose("Ciphering services not available or ciphertext is broken")
    assert "access list" in diagnose("No SNMP response received before timeout")


def test_v3_config_mistakes_are_caught_early():
    def v3(**kw):
        return config_from_dict({"snmp": {"credentials": [{"version": "3", "username": "u",
                                                           **kw}]}})

    with pytest.raises(ConfigError, match="at least 8"):
        v3(auth_protocol="sha", auth_key="short")
    with pytest.raises(ConfigError, match="needs authentication"):
        v3(priv_protocol="aes", priv_key="longenough")
    with pytest.raises(ConfigError, match="auth_protocol is not"):
        v3(auth_key="longenough")
    with pytest.raises(ConfigError, match="unsupported"):
        v3(auth_protocol="sha3", auth_key="longenough")
    cred = v3(auth_protocol="SHA-1", auth_key="longenough", priv_protocol="AES128",
              priv_key="longenough").snmp_credentials[0]
    assert (cred.auth_protocol, cred.priv_protocol) == ("sha", "aes")
    check_v3(cred)


def test_dpapi_secret_without_windows_is_a_clear_error():
    if secret_store.DPAPI_AVAILABLE:
        pytest.skip("Windows")
    with pytest.raises(ConfigError, match="Windows"):
        config_from_dict({"snmp": {"credentials": [{"version": "2c", "community": "dpapi:AAAA"}]}})


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows only")
def test_dpapi_round_trip(tmp_path):
    sealed = secret_store.dpapi_protect("correct horse battery")
    assert sealed.startswith("dpapi:") and "horse" not in sealed
    assert secret_store.dpapi_unprotect(sealed) == "correct horse battery"

    path = _config(tmp_path)
    script = Script(_v3_answers(store="encrypted"))
    assert run_setup(path, Agent(lambda c: None), script.prompter()) == 0
    text = path.read_text(encoding="utf-8")
    assert "auth-pass-1" not in text and 'auth_key = "dpapi:' in text
    assert load_config(path).snmp_credentials[0].auth_key == "auth-pass-1"
    tomllib.loads(text)
