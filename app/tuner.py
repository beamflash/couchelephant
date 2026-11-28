"""The tuner side of the app. HDHomeRun only, and everything that knows that is
here and in `tunerbox.py`.

A channel is a plain HTTP stream at `<box>:5004/auto/v<number>` (or the URL the
box's own lineup gives), so there is no driver and nothing to install. Several
boxes may be set; every function here reads them through `allowed_boxes`, the
one place that can limit how many may be used.
"""
import threading
import time

from . import tunerbox

NO_ADDRESS = "No tuner address is set. Add one in Settings, Recording, CouchElephant DVR."


class TunerError(RuntimeError):
    """Something a person can read. Never a bare status code."""


class NoTunerFree(TunerError):
    """Every tuner is in use. Worth trying again; nothing is broken."""


# CouchElephant's own streams: job id -> (box id, channel). The recorder says
# when one opens and closes, so a busy tuner can be told ours from Plex's.
_holds: dict[int, tuple[int, str]] = {}
_hold_lock = threading.Lock()

# Set by the recorder: a job id to the words for its tuner ("Recording: ...").
describe_hold = None


def allowed_boxes():
    """The boxes that may be used. Every enabled box today; the only place a
    limit on the count belongs."""
    return tunerbox.rows(enabled_only=True)


def hold(job_id: int, box_id: int, vcn: str) -> None:
    with _hold_lock:
        _holds[job_id] = (box_id, str(vcn))


def release(job_id: int) -> None:
    with _hold_lock:
        _holds.pop(job_id, None)


def _held_on(box_id: int) -> list[tuple[int, str]]:
    with _hold_lock:
        return [(j, v) for j, (b, v) in _holds.items() if b == box_id]


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


def _hand_built(box, vcn: str) -> str:
    """Where a channel streams from when the lineup cannot be asked, so a
    recording does not fail on a lineup request that timed out."""
    hostport = box["address"].split("://", 1)[1]
    host, _, port = hostport.partition(":")
    if port and port != "80":
        # Not a real HDHomeRun (Threadfin, an emulator): it streams on the
        # port it answers on.
        return f"{box['address']}/auto/v{vcn}"
    return f"http://{host}:5004/auto/v{vcn}"


def _carry(boxes, vcn: str):
    """The boxes that carry a channel: ([(box, stale, url)], refusals)."""
    lineups = {b["id"]: tunerbox.lineup(b) for b in boxes}
    if not any(rows for rows, _ in lineups.values()):
        # Nobody could say. Every box is tried at the usual address, so a box
        # with no lineup records as it always did.
        return [(b, False, _hand_built(b, vcn)) for b in boxes], []
    carry, refusals = [], []
    for b in boxes:
        rows, stale = lineups[b["id"]]
        row = next((r for r in rows if str(r.get("GuideNumber")) == str(vcn)), None)
        if not row:
            continue
        why = refusal(row)
        if why:
            refusals.append(why)
            continue
        carry.append((b, stale, row.get("URL") or _hand_built(b, vcn)))
    return carry, refusals


def _answered(box) -> bool:
    """The box spoke within the hour and its lineup is not stale. The same
    rule total() uses."""
    return bool(box["last_seen"] and box["last_seen"] >= time.time() - 3600
                and not tunerbox.lineup_is_stale(box))


def _free_on(box, st) -> int:
    if st is not None:
        return sum(1 for t in st if not t.get("VctNumber") and not t.get("Frequency"))
    # No /status.json. Guess from the count only for a box that is there.
    if not _answered(box):
        return 0
    return max(0, _count(box, st) - len(_held_on(box["id"])))


def _count(box, st=None) -> int:
    """How many tuners a box has: what it says, else what its status lists."""
    if box["tuner_count"]:
        return box["tuner_count"]
    st = st if st is not None else tunerbox.status(box)
    return len(st or [])


def pick(vcn: str) -> list:
    """The boxes to try for a channel, best first, as (box, stream URL).

    Only boxes that carry it. The most free tuners first; a box that does not
    report its tuners (some emulators) counts what we hold on it and goes after
    the ones that did; a box whose lineup is stale goes last of all.
    """
    boxes = allowed_boxes()
    if not boxes:
        raise TunerError(NO_ADDRESS)
    carry, refusals = _carry(boxes, vcn)
    if not carry:
        raise TunerError(refusals[0] if refusals else f"No tuner carries channel {vcn}.")
    sts = tunerbox.statuses([b for b, _, _ in carry])
    keyed = []
    for i, (b, stale, url) in enumerate(carry):
        st = sts[b["id"]]
        keyed.append(((stale, st is None, -_free_on(b, st), len(_held_on(b["id"])), i), b, url))
    keyed.sort(key=lambda k: k[0])
    return [(b, url) for _, b, url in keyed]


def check(vcn: str) -> None:
    """Raises if the channel cannot be recorded or watched, in words."""
    pick(vcn)


def free(vcn: str | None = None) -> int:
    """Free tuners across every box, or across the boxes that carry a channel."""
    boxes = allowed_boxes()
    if not boxes:
        raise TunerError(NO_ADDRESS)
    if vcn is not None:
        boxes = [b for b, _, _ in _carry(boxes, vcn)[0]]
    sts = tunerbox.statuses(boxes)
    return sum(_free_on(b, sts[b["id"]]) for b in boxes)


def total() -> int:
    """Tuners across every enabled box that answered within the last hour."""
    cutoff = time.time() - 3600
    return sum(_count(b) for b in allowed_boxes()
               if b["last_seen"] and b["last_seen"] >= cutoff)


def refused_channels() -> set[str]:
    """Channels every box that carries them refuses (encrypted, ATSC 3.0)."""
    carried, refused = set(), set()
    for b in allowed_boxes():
        for c in tunerbox.lineup(b)[0]:
            vcn = str(c.get("GuideNumber"))
            if refusal(c):
                refused.add(vcn)
            else:
                carried.add(vcn)
    return refused - carried


def box_tuners(box) -> list[dict]:
    """One line per tuner of a box: free, ours (with what for) or in use by
    another app. Ours is told by what CouchElephant holds on this box."""
    st = tunerbox.status(box)
    mine = _held_on(box["id"])
    out = []

    def ours(vcn):
        for i, (job, v) in enumerate(mine):
            if v == str(vcn):
                mine.pop(i)
                text = describe_hold(job) if describe_hold else None
                return {"state": "ours", "label": text or f"Live: {v}"}
        return None

    if st is None:
        # No /status.json: all we know is what we hold.
        for job, v in mine:
            text = describe_hold(job) if describe_hold else None
            out.append({"state": "ours", "label": text or f"Live: {v}"})
        out += [{"state": "free", "label": "Free"} for _ in range(max(0, _count(box) - len(mine)))]
        return out
    for t in st:
        vcn = t.get("VctNumber")
        if not vcn and not t.get("Frequency"):
            out.append({"state": "free", "label": "Free"})
        else:
            out.append((vcn and ours(vcn)) or
                       {"state": "other", "label": "In use by another app"})
    return out
