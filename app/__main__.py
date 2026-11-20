"""`python -m app`: run the server. The installers freeze this entry point."""
import argparse
import threading
import time
import webbrowser

import uvicorn

from . import db, oneport, paths

OPEN_DELAY = 2          # seconds, so the page is not asked for before it exists
_server: oneport.Server | None = None


def first_run() -> bool:
    """True until Plex is set, the same test the welcome page uses."""
    db.init()
    return not (db.get_setting("plex_url") and db.get_setting("plex_token"))


def stop() -> None:
    if _server is not None:
        _server.should_exit = True


def _open_later(port: int) -> None:
    time.sleep(OPEN_DELAY)
    webbrowser.open(f"http://localhost:{port}/welcome")


def make_config(port: int) -> uvicorn.Config:
    # proxy_headers off: app.exposure alone decides whom to believe.
    return uvicorn.Config("app.web:app", host="0.0.0.0", port=port,
                          server_header=False, proxy_headers=False)


def main(argv: list[str]) -> int:
    global _server
    ap = argparse.ArgumentParser(prog="python -m app")
    ap.add_argument("--port", type=int, default=paths.port())
    ap.add_argument("--open", dest="open", action="store_true",
                    help="open the setup page in a browser on first run")
    ap.add_argument("--no-open", dest="open", action="store_false")
    ap.set_defaults(open=False)
    args = ap.parse_args(argv)

    paths.data_dir()                      # make the folder before anything uses it
    if args.open and first_run():
        threading.Thread(target=_open_later, args=(args.port,), daemon=True).start()
    _server = oneport.Server(make_config(args.port))
    oneport.serve(_server.config, _server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
