# Entry point for the PyInstaller build (PyInstaller needs a script, not a module).
import sys

from netmapper.cli import main

if __name__ == "__main__":
    sys.exit(main())
