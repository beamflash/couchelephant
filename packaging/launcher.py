"""Entry point for the frozen build.

PyInstaller runs its script as a top-level file, and app/__main__.py uses
relative imports, so this wraps it.
"""
import sys

from app.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
