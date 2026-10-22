"""``netmapper snmp-setup``: add an SNMP login to config.toml by answering questions.

Aimed at SNMPv3, where there are five settings that all have to match the switch
exactly and a mistake normally just looks like a timeout. The wizard asks for each
one with sensible defaults, tests the result against one device with a single SNMP
request, explains what a failure most likely means, and stores the passphrases
encrypted (Windows) or in environment variables rather than as plain text.

Nothing is sent to the network except that one test request, and only if the user
names a device to test against.
"""

from __future__ import annotations

import getpass
import ipaddress
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from .collectors.snmp_client import SnmpError
from .collectors.snmp_device import SYS_NAME, SYS_OBJECT_ID, ClientFactory
from .config import (AES_KEY_VARIANTS, AUTH_PROTOCOLS, MIN_V3_KEY_LENGTH, PRIV_PROTOCOLS,
                     ConfigError, SnmpCredential, check_v3, config_from_dict)
from .secret_store import DPAPI_AVAILABLE, dpapi_protect
from .util import octets_to_text

AUTH_HELP = "sha is the usual choice; sha256 or stronger if the switch offers it"
PRIV_HELP = ("aes (AES-128) is the usual choice. For AES-192/256 on Cisco, use aes192c "
             "or aes256c")


@dataclass
class Prompter:
    """How the wizard talks to the user. Tests replace these with scripted answers."""

    ask: Callable[[str], str] = input
    ask_secret: Callable[[str], str] = getpass.getpass
    say: Callable[[str], None] = print


# What pysnmp's error text most likely means, for someone looking at a switch's config.
_DIAGNOSES = [
    ("unknown usm user", "The switch doesn't know that username. Usernames are "
     "case-sensitive; check it matches the SNMPv3 user configured on the switch."),
    ("wrong snmp pdu digest", "The username is right but the authentication passphrase or "
     "authentication protocol doesn't match the switch."),
    ("cipher", "Authentication worked but the encryption (privacy) passphrase or protocol "
     "doesn't match the switch."),
    ("decrypt", "Authentication worked but the encryption (privacy) passphrase or protocol "
     "doesn't match the switch."),
    ("unsupported", "The switch wants a different security level, for example encryption "
     "turned on (or off) for this user."),
    ("security level", "The switch wants a different security level, for example encryption "
     "turned on (or off) for this user."),
    ("time", "No answer. Check the address, that SNMP is enabled on the device, and that its "
     "SNMP access list allows this PC. With v2c a wrong community also looks like this, "
     "because devices ignore requests with the wrong community."),
]


def diagnose(error: str) -> str:
    low = error.lower()
    for needle, meaning in _DIAGNOSES:
        if needle in low:
            return meaning
    return f"The device refused the request ({error})."


def _is_privacy_error(error: str) -> bool:
    low = error.lower()
    return "cipher" in low or "decrypt" in low


def try_credential(ip: str, cred: SnmpCredential, factory: ClientFactory) -> tuple[bool, str]:
    """Send one GET (two if the first works, for the name). Returns (ok, message)."""
    client = factory(ip, cred)
    try:
        if client.get(SYS_OBJECT_ID) is None:
            return False, "The device answered but returned no system information."
        name = octets_to_text(client.get(SYS_NAME)) or ip
        return True, f"{ip} answered: it is '{name}'."
    except SnmpError as exc:
        return False, str(exc)
    finally:
        close = getattr(client, "close", None)
        if close:
            close()


def _choose(p: Prompter, prompt: str, options: list[str], default: str) -> str:
    while True:
        answer = p.ask(f"{prompt} [{'/'.join(options)}] (Enter for {default}): ").strip().lower()
        answer = {"sha1": "sha", "aes128": "aes"}.get(answer, answer)
        if not answer:
            return default
        if answer in options:
            return answer
        p.say(f"  Please type one of: {', '.join(options)}")


def _passphrase(p: Prompter, prompt: str, minimum: int = MIN_V3_KEY_LENGTH) -> str:
    while True:
        first = p.ask_secret(f"{prompt} (hidden): ")
        if len(first) < minimum:
            p.say(f"  It needs at least {minimum} characters.")
            continue
        if p.ask_secret("  Type it again to confirm (hidden): ") == first:
            return first
        p.say("  They didn't match. Let's try again.")


def _yes(p: Prompter, prompt: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = p.ask(f"{prompt} [{hint}]: ").strip().lower()
    return default if not answer else answer.startswith("y")


def ask_credential(p: Prompter) -> SnmpCredential:
    version = _choose(p, "SNMP version", ["3", "2c"], "3")
    if version == "2c":
        community = _passphrase(p, "Read-only community string", minimum=1)
        return SnmpCredential("2c", community=community)

    while not (username := p.ask("SNMPv3 username: ").strip()):
        p.say("  A username is needed.")
    p.say("Security level: authPriv (signed and encrypted) is what most switches expect.")
    level = _choose(p, "Security level", ["authpriv", "authnopriv", "noauthnopriv"], "authpriv")
    cred = SnmpCredential("3", username=username)
    if level != "noauthnopriv":
        p.say(f"  {AUTH_HELP}")
        cred.auth_protocol = _choose(p, "Authentication protocol", AUTH_PROTOCOLS, "sha")
        cred.auth_key = _passphrase(p, "Authentication passphrase")
    if level == "authpriv":
        p.say(f"  {PRIV_HELP}")
        cred.priv_protocol = _choose(p, "Encryption protocol", PRIV_PROTOCOLS, "aes")
        if _yes(p, "Is the encryption passphrase the same as the authentication one?", False):
            cred.priv_key = cred.auth_key
        else:
            cred.priv_key = _passphrase(p, "Encryption passphrase")
    check_v3(cred)
    return cred


def test_with_retries(p: Prompter, ip: str, cred: SnmpCredential,
                      factory: ClientFactory) -> tuple[SnmpCredential, bool]:
    p.say(f"Testing against {ip} with one SNMP request...")
    ok, message = try_credential(ip, cred, factory)
    if not ok and _is_privacy_error(message) and cred.priv_protocol in AES_KEY_VARIANTS:
        # Vendors disagree on how AES-192/256 keys are derived; try the other way once.
        other = replace(cred, priv_protocol=AES_KEY_VARIANTS[cred.priv_protocol])
        ok2, message2 = try_credential(ip, other, factory)
        if ok2:
            p.say(f"  It works with {other.priv_protocol} rather than {cred.priv_protocol}, "
                  "so that's what will be saved.")
            cred, ok, message = other, ok2, message2
    if ok:
        p.say(f"  Success. {message}")
    else:
        p.say(f"  It didn't work. {diagnose(message)}")
    return cred, ok


# --- writing the config --------------------------------------------------------------

def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _env_name(base: str, username: str | None) -> str:
    suffix = re.sub(r"[^A-Za-z0-9]+", "_", username or "").strip("_").upper()
    return f"{base}_{suffix}" if suffix else base


def store_secrets(p: Prompter, cred: SnmpCredential) -> tuple[dict[str, str], list[str]]:
    """Ask how to store the secrets. Returns (config values, env variables to set)."""
    secrets = {k: v for k, v in (("community", cred.community), ("auth_key", cred.auth_key),
                                 ("priv_key", cred.priv_key)) if v}
    options = ["env", "plain"]
    if DPAPI_AVAILABLE:
        options.insert(0, "encrypted")
        p.say("How should the passphrases be kept?\n"
              "  encrypted: in config.toml, encrypted so only you, on this PC, can use them\n"
              "  env:       in environment variables you set yourself\n"
              "  plain:     as plain text in config.toml (not recommended)")
    else:
        p.say("How should the passphrases be kept?\n"
              "  env:   in environment variables you set yourself\n"
              "  plain: as plain text in config.toml (not recommended)")
    how = _choose(p, "Store passphrases", options, options[0])

    values: dict[str, str] = {}
    env: list[str] = []
    bases = {"community": "SNMP_COMMUNITY", "auth_key": "SNMP_AUTH", "priv_key": "SNMP_PRIV"}
    for key, secret in secrets.items():
        if how == "encrypted":
            values[key] = dpapi_protect(secret)
        elif how == "env":
            if key == "priv_key" and cred.priv_key == cred.auth_key:
                values[key] = values["auth_key"]
                continue
            name = _env_name(bases[key], cred.username)
            values[key] = f"env:{name}"
            env.append(name)
        else:
            values[key] = secret
    return values, env


def credential_block(cred: SnmpCredential, values: dict[str, str]) -> str:
    lines = ["", "[[snmp.credentials]]"]
    if cred.version == "3":
        lines.append(f'# Added by "netmapper snmp-setup" for SNMPv3 user {cred.username}')
    else:
        lines.append('# Added by "netmapper snmp-setup"')
    lines.append(f"version = {_toml_str(cred.version)}")
    if cred.version == "3":
        lines.append(f"username = {_toml_str(cred.username or '')}")
        if cred.auth_protocol:
            lines.append(f"auth_protocol = {_toml_str(cred.auth_protocol)}")
            lines.append(f"auth_key = {_toml_str(values['auth_key'])}")
        if cred.priv_protocol:
            lines.append(f"priv_protocol = {_toml_str(cred.priv_protocol)}")
            lines.append(f"priv_key = {_toml_str(values['priv_key'])}")
    else:
        lines.append(f"community = {_toml_str(values['community'])}")
    return "\n".join(lines) + "\n"


_SEEDS_LINE = re.compile(r"^(\s*seeds\s*=\s*\[)([^\]\n]*)(\]\s*(?:#.*)?)$", re.M)


def add_seed(text: str, ip: str) -> str | None:
    """Add ``ip`` to a one-line ``seeds = [...]`` list. None if it can't be done safely."""
    matches = list(_SEEDS_LINE.finditer(text))
    if len(matches) != 1:
        return None
    m = matches[0]
    items = [s.strip() for s in m.group(2).split(",") if s.strip()]
    if _toml_str(ip) in items:
        return text
    items.append(_toml_str(ip))
    return text[: m.start()] + m.group(1) + ", ".join(items) + m.group(3) + text[m.end():]


def _validated(text: str, base_dir: Path) -> str:
    """Parse the new config before writing it, so a mistake never breaks the file."""
    try:
        config_from_dict(tomllib.loads(text), base_dir=base_dir)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"the updated config would not be valid TOML: {exc}") from None
    return text


def run_setup(config_path: Path, factory: ClientFactory, prompter: Prompter | None = None,
              seeds: list[str] | None = None) -> int:
    p = prompter or Prompter()
    if not config_path.exists():
        p.say(f"{config_path} doesn't exist yet. Run 'netmapper init' first.")
        return 2
    p.say("Add an SNMP login so netmapper can read your managed switches and routers.\n"
          "Read-only access is all it needs. Nothing is sent to the network until the test\n"
          "at the end, which is a single SNMP request to a device you choose.\n")
    try:
        cred = ask_credential(p)
        default_ip = (seeds or [""])[0]
        hint = f"Enter for {default_ip}, or '-' to skip" if default_ip else "Enter to skip"
        while True:
            ip = p.ask(f"Test it against which switch or router? ({hint}): ").strip()
            if ip == "-" or (not ip and not default_ip):
                ip = ""
                break
            ip = ip or default_ip
            try:
                ipaddress.IPv4Address(ip)
                break
            except ValueError:
                p.say("  Please enter an IPv4 address, like 192.168.1.2.")
        if ip:
            cred, ok = test_with_retries(p, ip, cred, factory)
            if not ok and not _yes(p, "Save it anyway?", False):
                p.say("Nothing was saved. Run 'netmapper snmp-setup' again to retry.")
                return 1
        values, env = store_secrets(p, cred)
    except (EOFError, KeyboardInterrupt):
        p.say("\nCancelled; nothing was saved.")
        return 1

    text = config_path.read_text(encoding="utf-8")
    new_text = text.rstrip("\n") + "\n" + credential_block(cred, values)
    seed_note = ""
    if ip:
        with_seed = add_seed(new_text, ip)
        if with_seed is None:
            seed_note = f"Add {ip} to 'seeds' under [snmp] so it's read on every run."
        elif with_seed != new_text:
            new_text, seed_note = with_seed, f"Added {ip} to the SNMP seeds."
    try:
        if env:
            # The env variables aren't set yet, so check the file with stand-ins.
            check = new_text
            for name in env:
                check = check.replace(f'"env:{name}"', '"placeholder-secret"')
            _validated(check, config_path.parent)
        else:
            _validated(new_text, config_path.parent)
    except ConfigError as exc:
        p.say(f"Not saved: {exc}")
        return 1
    config_path.write_text(new_text, encoding="utf-8")
    p.say(f"\nSaved to {config_path}. {seed_note}".rstrip())
    if env:
        p.say("Now set the passphrases as environment variables. In PowerShell, run these\n"
              "(they last for your Windows user, including scheduled runs), then open a new\n"
              "window:")
        for name in env:
            p.say(f'  setx {name} "your-passphrase-here"')
    p.say("Then run 'netmapper discover --open' to map the network.")
    return 0
