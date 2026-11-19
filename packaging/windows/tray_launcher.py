"""Entry point for the frozen tray program: CouchElephantTray.exe [--port N]."""
import argparse

from app import tray

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=None)
    tray.run(ap.parse_args().port)
