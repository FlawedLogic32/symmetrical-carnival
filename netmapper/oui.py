"""MAC address vendor lookup from the IEEE OUI list.

The list isn't bundled (it's several MB and changes often). ``netmapper update-oui``
downloads it once from the IEEE; without it, vendors are simply left blank.
"""

from __future__ import annotations

import csv
import logging
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

IEEE_OUI_URL = "https://standards-oui.ieee.org/oui/oui.csv"


class VendorLookup:
    def __init__(self, table: dict[str, str] | None = None):
        self._table = table or {}

    @classmethod
    def from_csv(cls, path: str | Path) -> VendorLookup:
        """Load the IEEE ``oui.csv`` (columns: Registry, Assignment, Organization Name, ...)."""
        table = {}
        with open(path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                prefix = (row.get("Assignment") or "").strip().lower()
                name = (row.get("Organization Name") or "").strip()
                if len(prefix) == 6 and name:
                    table[prefix] = name
        return cls(table)

    @classmethod
    def load_if_present(cls, path: str | Path) -> VendorLookup:
        path = Path(path)
        if not path.exists():
            log.info("no OUI file at %s; run 'netmapper update-oui' to show vendors", path)
            return cls()
        return cls.from_csv(path)

    def __len__(self) -> int:
        return len(self._table)

    def vendor(self, mac: str | None) -> str | None:
        if not mac:
            return None
        if int(mac[:2], 16) & 0x02:
            # Locally administered: phones and laptops randomise their MAC for privacy.
            return "Private (randomised) MAC"
        return self._table.get(mac.replace(":", "")[:6])


def download_oui(path: str | Path) -> int:
    """Fetch the IEEE OUI list to ``path``. Returns how many vendors it holds."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    req = urllib.request.Request(IEEE_OUI_URL, headers={"User-Agent": "netmapper"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
        out.write(resp.read())
    count = len(VendorLookup.from_csv(tmp))
    if count == 0:
        tmp.unlink()
        raise ValueError("downloaded OUI file had no entries")
    tmp.replace(path)
    return count
