"""Build a single-file netmapper executable with PyInstaller.

    pip install ".[build]"
    python packaging/build_exe.py

The result is dist/netmapper.exe on Windows (dist/netmapper elsewhere).
"""

import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent.parent

PyInstaller.__main__.run([
    str(ROOT / "packaging" / "netmapper_entry.py"),
    "--name", "netmapper",
    "--onefile",
    "--console",
    "--paths", str(ROOT),
    "--collect-submodules", "netmapper",
    "--noconfirm",
    "--clean",
    "--distpath", str(ROOT / "dist"),
    "--workpath", str(ROOT / "build"),
    "--specpath", str(ROOT / "build"),
    # pysnmp loads its MIB modules from files at run time, so bundle the whole package.
    "--collect-all", "pysnmp",
    "--collect-submodules", "cryptography",
    *sys.argv[1:],
])
