"""Sports overtime: keep recording a game that runs long.

Only for recordings CouchElephant DVR makes, because only those can be moved
while they run. The video says nothing about whether a game is over, so a
scoreboard is asked instead: ESPN's public one, which is unofficial but has
been stable for years, and covers the NFL, NBA, WNBA, MLB, NHL, college
football and basketball, and the main soccer leagues.

- When a sports recording starts, its game is found on the scoreboard by the
  two teams and the start time. No match, and the recording keeps its padding
  as before: this can only make a recording longer, never shorter.
- From ten minutes before the guide's end, every two minutes: a game still
  being played moves the stop to ten minutes from now; a finished game stops
  five minutes after it ended, or at the padding's end if that is later.
- Never past `overtime_cap_minutes` after the guide's end.
- A recording that needs a tuner wins: overtime ends when another recording
  is waiting for one and none is free.
"""
import datetime
import json
import logging
import re
import time
import unicodedata
import zoneinfo

import httpx

from . import db, recorder, tuner
from .plex import Plex

log = logging.getLogger("couchelephant.overtime")

BASE = "https://site.api.espn.com/apis/site/v2/sports/"
# Asked this often while a game is near its end or past it.
POLL_SECONDS = 120
# Asked from this long before the guide's end.
WINDOW_SECONDS = 600
# Still playing: the stop moves to this long from now. Over: stops this long after.
STILL_PLAYING_SECONDS = 600
AFTER_FINAL_SECONDS = 300
# A game on the scoreboard and in the guide start within this much of each other.
START_SLACK_SECONDS = 4 * 3600
# One team named alike is enough when the game starts this close to the
# guide's time and no other game fits: a team plays one game at a time. The
# guide says "Texas-El Paso" where the scoreboard says "UTEP".
ONE_TEAM_SLACK_SECONDS = 30 * 60
# How long one answer from the scoreboard is reused.
CACHE_SECONDS = 90

# Each league: its scoreboard path, extra query, and the words in a guide
# title that name it.
LEAGUES = [
    ("football/nfl", "", ("nfl",)),
    ("football/college-football", "groups=80&limit=400", ("college football", "ncaa football",
                                                          "futbol americano universitario")),
    ("basketball/nba", "", ("nba",)),
    ("basketball/wnba", "", ("wnba",)),
    ("basketball/womens-college-basketball", "groups=50&limit=400",
     ("women's college basketball", "womens college basketball")),
    ("basketball/mens-college-basketball", "groups=50&limit=400",
     ("college basketball", "college hoops")),
    ("baseball/mlb", "", ("mlb", "baseball", "beisbol")),
    ("hockey/nhl", "", ("nhl", "hockey")),
    ("soccer/usa.1", "", ("mls",)),
    ("soccer/usa.nwsl", "", ("nwsl",)),
    ("soccer/usa.usl.1", "", ("usl",)),
    ("soccer/eng.1", "", ("premier league",)),
    ("soccer/esp.1", "", ("primera division", "laliga", "la liga")),
    ("soccer/ger.1", "", ("bundesliga",)),
    ("soccer/ita.1", "", ("serie a",)),
    ("soccer/fra.1", "", ("ligue 1",)),
    ("soccer/mex.1", "", ("liga mx", "futbol mexicano")),
]

_cache: dict[str, tuple[float, list]] = {}


def enabled() -> bool:
    return (db.get_setting("sports_overtime") or "1") == "1"


def cap_seconds() -> int:
    try:
        minutes = int(db.get_setting("overtime_cap_minutes") or 180)
    except ValueError:
        minutes = 180
    return max(0, min(360, minutes)) * 60


# ---------- names ----------

def _norm(s: str) -> str:
    """Lower case, no accents or punctuation, and a closing "St." spelled
    out: the guide says "Ohio St." where the scoreboard says "Ohio State"."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    words = re.sub(r"[^a-z0-9 ]+", " ", s.replace("&", " and ")).split()
    if len(words) > 1 and words[-1] == "st":
        words[-1] = "state"
    return " ".join(words)


def _names(team: dict) -> set[str]:
    t = team.get("team") or team
    out = {_norm(t.get(k) or "") for k in ("displayName", "shortDisplayName", "location")}
    if t.get("location") and t.get("name"):
        out.add(_norm(f"{t['location']} {t['name']}"))
    return {n for n in out if n}


def _same_team(guide: str, espn: set[str]) -> bool:
    g = _norm(guide)
    if not g:
        return False
    # "Air Force" is the location of "Air Force Falcons"; "Kansas City Chiefs"
    # is the display name. Either end of the name may be the shared part.
    return any(g == n or n.startswith(g + " ") or g.startswith(n + " ") or g.endswith(" " + n)
               for n in espn)


def guide_teams(rec) -> list[str]:
    """The two teams, from the guide's list, or else from the title."""
    p = db.one("SELECT teams FROM programs WHERE guid = ?", (rec["program_guid"],)) \
        if rec["program_guid"] else None
    try:
        names = [t.get("name") for t in json.loads(p["teams"] or "[]")] if p else []
    except (ValueError, TypeError):
        names = []
    names = [n for n in names if n]
    if len(names) >= 2:
        return names[:2]
    parts = re.split(r"\s+(?:at|vs\.?|v\.?|@|contra)\s+", rec["title"] or "", maxsplit=1,
                     flags=re.I)
    return [p.strip() for p in parts] if len(parts) == 2 else []


def leagues_for(rec) -> list[tuple[str, str]]:
    """The leagues the guide's words name, or every league when they name none."""
    text = _norm(f"{rec['show_title'] or ''} {rec['title'] or ''}")
    named = [(path, q) for path, q, words in LEAGUES
             if any(re.search(r"\b" + re.escape(_norm(w)) + r"\b", text) for w in words)]
    # "College Basketball" also matches inside "Women's College Basketball".
    if any(p == "basketball/womens-college-basketball" for p, _ in named):
        named = [(p, q) for p, q in named if p != "basketball/mens-college-basketball"]
    return named or [(path, q) for path, q, _ in LEAGUES]


# ---------- the scoreboard ----------

def _get(path: str, query: str, day: str) -> list:
    key = f"{path}?{query}&dates={day}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    url = f"{BASE}{path}/scoreboard?dates={day}" + (f"&{query}" if query else "")
    r = httpx.get(url, timeout=15, headers={"User-Agent": "CouchElephant"})
    r.raise_for_status()
    events = r.json().get("events") or []
    _cache[key] = (time.time(), events)
    return events


def _days(begins_at: int) -> list[str]:
    """The scoreboard's day is the American Eastern one. A late game can sit
    on the day before in the guide's own timezone."""
    et = datetime.datetime.fromtimestamp(begins_at, zoneinfo.ZoneInfo("America/New_York"))
    return [et.strftime("%Y%m%d"), (et - datetime.timedelta(days=1)).strftime("%Y%m%d")]


def _start(event) -> int:
    try:
        return int(datetime.datetime.fromisoformat(
            event["date"].replace("Z", "+00:00")).timestamp())
    except (KeyError, ValueError):
        return 0


def find(rec) -> tuple[str | None, str]:
    """The scoreboard's game for a recording: ('league path/event id', why)."""
    teams = guide_teams(rec)
    if len(teams) != 2:
        return None, "The guide does not name two teams."
    begins = rec["begins_at"] or rec["starts_at"]
    near = {}
    for path, query in leagues_for(rec):
        for day in _days(begins):
            for e in _get(path, query, day):
                if abs(_start(e) - begins) > START_SLACK_SECONDS:
                    continue
                comp = (e.get("competitions") or [{}])[0]
                sides = [_names(c) for c in comp.get("competitors") or []]
                if len(sides) != 2:
                    continue
                a, b = teams
                if (_same_team(a, sides[0]) and _same_team(b, sides[1])) or \
                        (_same_team(a, sides[1]) and _same_team(b, sides[0])):
                    return f"{path}/{e['id']}", e.get("name") or ""
                if abs(_start(e) - begins) <= ONE_TEAM_SLACK_SECONDS and \
                        any(_same_team(t, s) for t in teams for s in sides):
                    near[f"{path}/{e['id']}"] = e.get("name") or ""
    if len(near) == 1:
        ref, name = next(iter(near.items()))
        return ref, name + " (one team matched by name, and the start time)"
    return None, f"No game on the scoreboard for {teams[0]} and {teams[1]}."


def state(event_ref: str, begins_at: int) -> str | None:
    """'pre', 'in' or 'post' for a matched game, or None when it is not listed."""
    path, event_id = event_ref.rsplit("/", 1)
    query = next((q for p, q, _ in LEAGUES if p == path), "")
    for day in _days(begins_at):
        for e in _get(path, query, day):
            if str(e.get("id")) == event_id:
                st = (e.get("status") or {}).get("type") or {}
                return "post" if st.get("completed") else st.get("state")
    return None


# ---------- the loop ----------

def tick(now: int | None = None) -> None:
    """Every minute, from its own loop: the scoreboard may be slow to answer,
    and the recorder must never wait for it."""
    if not enabled() or not recorder.available():
        return
    now = now or int(time.time())
    for rec in db.query("SELECT * FROM recordings WHERE kind = 'recording' "
                        "AND section = 'sports' AND state = 'recording'"):
        try:
            _one(rec, now)
        except (httpx.HTTPError, ValueError) as e:
            # The padding still holds. Said once a game, not every minute.
            if rec["ot_state"] != "unreachable":
                log.warning("overtime: the scoreboard did not answer for recording %s: %s",
                            rec["id"], e)
                recorder._set(rec["id"], ot_state="unreachable" if not rec["ot_event"]
                              else rec["ot_state"], ot_checked_at=now)


def _one(rec, now):
    if not rec["ot_event"]:
        if rec["ot_state"] == "nomatch":
            return
        if rec["ot_state"] == "unreachable" and now - (rec["ot_checked_at"] or 0) < POLL_SECONDS:
            return
        ref, said = find(rec)
        if ref:
            log.info("overtime: recording %s is %s (%s)", rec["id"], said, ref)
            recorder._set(rec["id"], ot_event=ref, ot_state="matched", ot_base=rec["stops_at"],
                          ot_checked_at=0)
        else:
            log.info("overtime: recording %s: %s", rec["id"], said)
            recorder._set(rec["id"], ot_state="nomatch", ot_note=said)
        return
    if rec["ot_state"] == "post":
        return
    base = rec["ot_base"] or rec["stops_at"]
    if now > base and _tuner_wanted(rec, now):
        recorder._set(rec["id"], stops_at=now, ot_state="post",
                      ot_note="Overtime ended early: another recording needed the tuner.")
        log.info("overtime: recording %s stopped for a recording that needs its tuner", rec["id"])
        return
    if now < rec["ends_at"] - WINDOW_SECONDS or now - (rec["ot_checked_at"] or 0) < POLL_SECONDS:
        return
    st = state(rec["ot_event"], rec["begins_at"])
    cols = {"ot_checked_at": now}
    limit = max(base, rec["ends_at"] + cap_seconds())
    if st in ("pre", "in"):
        want = min(now + STILL_PLAYING_SECONDS, limit)
        if want > rec["stops_at"]:
            cols["stops_at"] = want
            cols["ot_note"] = (f"The game ran long: recorded "
                               f"{max(0, (want - base) // 60)} minutes past the padding.")
        cols["ot_state"] = "in"
    elif st == "post":
        # Only an extension of ours is taken back; the padding stays.
        cols["stops_at"] = max(base, min(rec["stops_at"], now + AFTER_FINAL_SECONDS))
        if cols["stops_at"] > base:
            cols["ot_note"] = (f"The game ran long: recorded "
                               f"{(cols['stops_at'] - base) // 60} minutes past the padding.")
        cols["ot_state"] = "post"
    recorder._set(rec["id"], **cols)


def _tuner_wanted(rec, now) -> bool:
    """Another recording is due now and waiting, and no tuner is free."""
    waiting = db.one("SELECT id FROM recordings WHERE kind = 'recording' AND state = 'scheduled' "
                     "AND starts_at <= ? AND stops_at > ? AND id != ?", (now, now, rec["id"]))
    if not waiting and not _plex_starting(now):
        return False
    try:
        return tuner.free() == 0
    except tuner.TunerError:
        return False


def _plex_starting(now) -> bool:
    """Plex's own DVR has a recording starting now or within a few minutes.
    Asked only while a game is in overtime."""
    try:
        with Plex(db.get_setting("plex_url"), db.get_setting("plex_token")) as plex:
            ops = plex.scheduled()
    except Exception:
        return False
    for op in ops:
        if (op.get("status") or "") in ("complete", "inprogress", "error"):
            continue
        media = ((op.get("Video") or op.get("Metadata") or {}).get("Media") or {})
        if isinstance(media, list):
            media = media[0] if media else {}
        try:
            begins, ends = int(media.get("beginsAt") or 0), int(media.get("endsAt") or 0)
        except ValueError:
            continue
        if begins - 300 <= now < ends:
            return True
    return False
