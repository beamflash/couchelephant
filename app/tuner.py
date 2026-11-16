"""The tuner. HDHomeRun only, and everything that knows that is in this file.

A channel is a plain HTTP stream at `<tuner>:5004/auto/v<number>`, so there is
no driver and nothing to install. Another tuner type is another module with
these same four functions.
"""
import httpx

from . import db


class TunerError(RuntimeError):
    """Something a person can read. Never a bare status code."""


class NoTunerFree(TunerError):
    """Every tuner is in use. Worth trying again; nothing is broken."""


def base() -> str | None:
    """The tuner's address, with no trailing slash, or None if unknown."""
    url = (db.get_setting("tuner_url") or "").strip().rstrip("/")
    if not url:
        # What Plex's own device list named, kept by `detect`. A typed address
        # always wins over it.
        url = (db.get_setting("tuner_detected") or "").strip().rstrip("/")
    if not url:
        return None
    return url if "://" in url else "http://" + url


def _get(path: str):
    b = base()
    if not b:
        raise TunerError("No tuner address is set. Add one in Settings, Recorder.")
    try:
        r = httpx.get(b + path, timeout=5)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise TunerError(f"The tuner at {b} did not answer: {e}") from e


def detect(plex) -> str | None:
    """The tuner Plex records from, so there is nothing to type.

    Plex lists its DVR devices with the address it reaches them on. The first
    one that answers like an HDHomeRun is kept. Nothing is kept on a guess.
    """
    for d in plex.grabber_devices():
        uri = (d.get("uri") or "").strip().rstrip("/")
        if not uri.startswith("http"):
            continue
        host = uri.split("://", 1)[1].split("/")[0].split(":")[0]
        try:
            r = httpx.get(f"http://{host}/discover.json", timeout=4)
            if r.status_code == 200 and r.json().get("LineupURL"):
                found = f"http://{host}"
                if db.get_setting("tuner_detected") != found:
                    db.set_setting("tuner_detected", found)
                return found
        except (httpx.HTTPError, ValueError):
            continue
    return None


def lineup() -> list[dict]:
    return _get("/lineup.json") or []


def channel(vcn: str) -> dict | None:
    for c in lineup():
        if str(c.get("GuideNumber")) == str(vcn):
            return c
    return None


def stream_url(vcn: str) -> str:
    """Where this channel streams from.

    The lineup's own URL when the tuner can be asked, because it is the tuner's
    word on the matter. Built by hand otherwise, so a recording does not fail
    on a lineup request that timed out.
    """
    try:
        c = channel(vcn)
    except TunerError:
        c = None
    if c:
        refuse = refusal(c)
        if refuse:
            raise TunerError(refuse)
        if c.get("URL"):
            return c["URL"]
    b = base()
    if not b:
        raise TunerError("No tuner address is set. Add one in Settings, Recorder.")
    host = b.split("://", 1)[1].split(":")[0]
    return f"http://{host}:5004/auto/v{vcn}"


def refusal(c: dict) -> str | None:
    """Why this channel cannot be recorded here, or None if it can."""
    if c.get("DRM"):
        return "This channel is encrypted and cannot be recorded."
    # ATSC 3.0. The sound is AC-4, which ffmpeg cannot decode, so the viewing
    # copy would be silent. Every such station has an ATSC 1.0 twin.
    if (c.get("AudioCodec") or "").upper() == "AC4":
        return ("This is an ATSC 3.0 channel, which CouchElephant cannot record "
                "yet. Record the same station on its ordinary channel number.")
    return None


def tuners() -> list[dict]:
    """One row per physical tuner. A busy one carries `VctNumber`."""
    return _get("/status.json") or []


def free() -> int:
    return sum(1 for t in tuners() if not t.get("VctNumber") and not t.get("Frequency"))


def total() -> int:
    return len(tuners())
