"""Smart Sports Pass.

Plex has a team pass already. It is broken for the case it exists for: when a
game airs live and is then repeated, Plex can schedule the repeat. Verified on a
live server, with the guide plainly flagging the live broadcast `premiere: 1`
and Plex choosing the unflagged rebroadcast anyway.

This does the one thing Plex gets wrong. Rather than register a recurring
subscription and hope, it reads the guide itself, picks the airing deliberately,
and creates a one-shot recording pinned to that exact channel and start time so
Plex has nothing left to choose between.

Selection order for a game:
  1. Drop DRM airings. Nothing can decrypt them.
  2. Prefer an airing flagged `premiere` (the live broadcast).
  3. Among equals, take the earliest start.
"""
import threading
import time

from . import db, expectations, language, recorder, smartfilter, teamcat
from .plex import Plex, PlexError

# Give a game this much slack before treating it as too late to schedule.
LEAD_SECONDS = 120


# Taken by a booking and by a pass being deleted, so a delete cannot land in the
# middle of a booking and leave one behind.
PASS_LOCK = threading.RLock()


def _now():
    return int(time.time())


_AIRING_SQL = """SELECT a.*, p.title, p.grandparent_title, p.rating_key, p.teams, p.summary,
                  p.section, c.network AS channel_network,
                  COALESCE(c.language_override, c.language) AS channel_language
           FROM airings a
           JOIN programs p ON p.guid = a.program_guid
           LEFT JOIN channels c ON c.vcn = a.channel_vcn
           WHERE a.begins_at BETWEEN ? AND ?"""


def _future(extra="", args=(), horizon_days=30, limit=None, program_guid=None):
    """Future airings with a WHERE fragment applied by SQLite, not by Python.

    The guide holds around twenty thousand future airings. Pulling them all in
    to keep a dozen is how a pass list of forty took a second to draw.
    """
    cutoff = _now() - LEAD_SECONDS
    until = _now() + horizon_days * 86400
    # Narrowing to one programme is done here rather than by the caller, so
    # `idx_airings_program` serves it instead of the whole window being
    # hydrated and then thrown away.
    if program_guid:
        extra = f"a.program_guid = ?{f' AND {extra}' if extra else ''}"
        args = (program_guid, *args)
    sql = _AIRING_SQL + (f" AND {extra}" if extra else "") + " ORDER BY a.begins_at"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return db.query(sql, (cutoff, until, *args))


def candidate_airings(team_id: int | None, horizon_days: int = 30,
                      team_name: str | None = None, program_guid: str | None = None,
                      limit: int | None = None) -> list:
    """Future airings of games featuring this team.

    Matched on the team's NAME. The id is a fallback and nothing more.

    A pass holds an id from the `teams` table, which is Plex's section-level
    team list. A programme holds its own `teams` array, numbered per programme.
    THEY ARE DIFFERENT NUMBERINGS, and comparing one to the other is a category
    error rather than a stale lookup.

    Measured on the live guide on 2026-09-10: of the 89 ids that appear in both,
    71 name a different team in each. The section list calls 253 "Nevada" while
    a programme calls it "Sporting Kansas City". Inside the programme arrays
    alone, id 343 was Kansas City Chiefs, Borussia Dortmund, Los Angeles Rams
    and San Diego State.

    This used to match on the id OR the name, and the id half is what turned a
    Kansas City Chiefs pass into one that also tried to record Bundesliga
    football, two college games and a Bears game: 142 refused bookings. The old
    reasoning was that a pass and a programme might sit either side of a
    renumbering, so both halves were needed. That is only sound if the two ids
    name the same thing, and they never did.

    The id moves as well: one refresh took the Chiefs from 236 to 245 on the
    same game with the same programme guid. The name is what survives that,
    which is why it is the only thing consulted when a pass has one.

    The name is compared through `teamcat.ident`, which folds case, accents and
    punctuation and nothing else. Deliberately not `teamcat.norm`: that also
    drops club words, which is right for *finding* a team in the catalogue and
    wrong for deciding what to record. It folds "Real Madrid" and "Atletico
    Madrid" both to "madrid", and does the same to Cincinnati and FC
    Cincinnati, and to four more pairs in the shipped catalogue.

    Spelling is kept in step at the other end: `sync.resolve_team_passes` adopts
    Plex's own spelling for the team when it repoints a pass, so a pass made
    from the catalogue stops carrying a name the guide has never used.
    """
    if not team_id and not team_name:
        return []
    key = teamcat.ident(team_name or "")
    if key:
        return _future("EXISTS (SELECT 1 FROM json_each(p.teams) t "
                       "WHERE tident(json_extract(t.value, '$.name')) = ?)",
                       (key,), horizon_days, limit=limit, program_guid=program_guid)
    # No name to go on. An old pass made before the name was stored has only
    # the id, and a wrong match is still better than a pass that matches
    # nothing at all and looks like a quiet week.
    return _future("EXISTS (SELECT 1 FROM json_each(p.teams) t "
                   "WHERE json_extract(t.value, '$.id') = ?)",
                   (int(team_id or 0),), horizon_days, limit=limit,
                   program_guid=program_guid)


def series_airings(series_guid: str, horizon_days: int = 30,
                   program_guid: str | None = None) -> list:
    """Future airings of one programme, wherever it turns up.

    Matched on the show rather than the episode, so a rule follows the series
    across every episode the guide holds.
    """
    if not series_guid:
        return []
    return _future("(a.program_guid = ? OR p.grandparent_title = ?)",
                   (series_guid, series_guid), horizon_days,
                   program_guid=program_guid)


def smart_airings(tree: dict, horizon_days: int = 30,
                  program_guid: str | None = None) -> list:
    """Future airings matching a smart filter.

    Compiled to SQL and asked of the database, rather than pulled into Python
    and sifted. A loose filter can match thousands of rows, and the whole point
    of the count in the panel is that it comes back before the user has given up.
    """
    frag, args = smartfilter.build(tree)
    return _future(f"({frag})", args, horizon_days, program_guid=program_guid)


def any_airing(horizon_days: int = 30) -> list:
    """One usable broadcast, whichever. A stand-in when nothing is chosen yet.

    Plex's recording settings belong to Plex, not to the programme: every
    template offers the same ones. So a pass can be shown them before it knows
    what it follows, which is the difference between an options panel and an
    empty box that says come back later.
    """
    return _future("COALESCE(a.drm, 0) = 0", (), horizon_days, limit=1)


def rule_airings(rule, horizon_days: int = 30, program_guid: str | None = None) -> list:
    """Everything a rule could record, before the source limit is applied.

    `program_guid` narrows the answer to one programme, in SQL. A caller that
    wants one game was otherwise served the pass's whole thirty-day window and
    threw all but one programme away, which is a full scan of the guide with a
    `json_each` per row for a result an index can find.
    """
    if rule["kind"] == "smart":
        return smart_airings(db.unjs(rule["filter"], {}) or {}, horizon_days,
                             program_guid=program_guid)
    if rule["kind"] == "series":
        return series_airings(rule["series_guid"] or rule["series_title"],
                              horizon_days, program_guid=program_guid)
    # The name goes with the id. A pass followed from the catalogue has only a
    # name until the team first plays, and after Plex renumbers, the name is
    # the half that is still true.
    return candidate_airings(rule["team_id"], horizon_days,
                             team_name=rule["team_name"], program_guid=program_guid)


def allowed_sources(rule) -> tuple[list[str], list[str]]:
    """The networks and channels a rule accepts. Empty means anywhere."""
    return (db.unjs(rule["networks"]) or [], db.unjs(rule["channels"]) or [])


def in_sources(row, networks: list[str], channels: list[str]) -> bool:
    """True when this broadcast comes from somewhere the rule accepts.

    The two lists are one allowlist, not two filters that both have to pass.
    Naming a network and a channel means "either of these", which is what a
    person means by "only ABC, CBS and channel 41.1".
    """
    if not networks and not channels:
        return True
    if row["channel_vcn"] in channels:
        return True
    try:
        net = row["channel_network"]
    except (IndexError, KeyError):
        # A row shape without the column. Cheaper to ask forgiveness than to
        # build and scan a list of column names for every airing.
        net = None
    return bool(net and net in networks)


def _language_rank(row, preferred: str) -> int:
    """0 in the preferred language, 1 when the channel's language is unknown,
    2 in another language."""
    lang = airing_language(row)
    return 0 if lang == preferred else (1 if lang is None else 2)


def choose_airing(airings: list, preferred: str | None = None) -> tuple[dict | None, str]:
    """Pick one broadcast of a game, and say why. Returns (row, reason).

    With `preferred`, the preferred language's channels are looked at first,
    then unknown ones, then the rest, and the usual rule (the live broadcast,
    else the earliest) applies inside that group. Another language is still
    chosen when it is the only one, so the game is never lost.
    """
    usable = [a for a in airings if not a["drm"]]
    if not usable:
        return None, "every airing is DRM encrypted and cannot be recorded"
    if preferred:
        # Language only breaks ties between broadcasts of the game itself. A
        # replay the next day in the preferred language is not the game.
        base = min(usable, key=lambda a: (not a["premiere"], a["begins_at"]))
        usable = [a for a in usable
                  if abs(a["begins_at"] - base["begins_at"]) <= LANGUAGE_WINDOW]
        best = min(_language_rank(a, preferred) for a in usable)
        usable = [a for a in usable if _language_rank(a, preferred) == best]
    premieres = [a for a in usable if a["premiere"]]
    if premieres:
        pick = min(premieres, key=lambda a: a["begins_at"])
        if len(usable) > 1:
            return pick, f"live broadcast (premiere) of {len(usable)} airings"
        return pick, "only airing, flagged premiere"
    pick = min(usable, key=lambda a: a["begins_at"])
    return pick, ("no airing is flagged premiere; took the earliest of "
                  f"{len(usable)}")


# Two channels airing one game kick off minutes apart (the live case measured
# 5 minutes). 90 minutes covers that with room, and stays short of a baseball
# doubleheader, where game 2 starts about three hours after game 1.
SAME_GAME_WINDOW = 90 * 60

# How far from the usual choice a channel's language may win it. Wider than
# SAME_GAME_WINDOW on purpose: that one decides whether two different listings
# are one game, and has to stay tight. This one decides whether an English
# airing a few hours after a live Spanish one (the same listing, shown again
# in the evening) is preferred over it. Six hours reaches that and still never
# reaches a replay the next day.
LANGUAGE_WINDOW = 6 * 3600


def _team_set(row) -> frozenset:
    """The team ids a programme names, or an empty set when it names fewer than two."""
    ids = set()
    for t in db.unjs(row["teams"], []) or []:
        if isinstance(t, dict) and t.get("id") is not None:
            ids.add(t["id"])
    return frozenset(ids) if len(ids) >= 2 else frozenset()


def group_by_game(rows: list) -> dict[str, list]:
    """Airings grouped by game. The key is the game's first programme guid.

    The ONE place that decides what a game is, so a later per-pass setting for
    simulcasts only has to bypass this. One programme guid is one game. Two
    guids are also one game when they name the same teams and kick off within
    SAME_GAME_WINDOW, because two networks list one game under two guids. Never
    matched on title: "at" and "vs." differ by network. A row with no teams
    groups by guid alone.

    One more case, for a pass that wants one airing per game. A network can show
    the game later than another does (Telemundo live at 19:00, NBC at 21:00
    under its own guid). Two guids of the same teams up to LANGUAGE_WINDOW apart
    are also one game WHEN the later guid airs in a language the game does not
    have yet (an unknown language counts as one of its own). Two guids in the same language stay
    two games however close, so an English doubleheader, or a Spanish one on
    Telemundo, keeps both games.
    """
    by_guid = {}
    for r in rows:
        by_guid.setdefault(r["program_guid"], []).append(r)
    games = {}
    open_game = {}       # team set -> (key, earliest kickoff) of the latest game
    firsts = sorted(by_guid, key=lambda g: (min(a["begins_at"] for a in by_guid[g]), g))
    for guid in firsts:
        airings = by_guid[guid]
        start = min(a["begins_at"] for a in airings)
        teams = _team_set(airings[0])
        mate = open_game.get(teams) if teams else None
        gap = start - mate[1] if mate else None
        if mate and (gap <= SAME_GAME_WINDOW or (
                gap <= LANGUAGE_WINDOW
                and not _guid_languages(airings) <= _guid_languages(games[mate[0]]))):
            games[mate[0]].extend(airings)
            continue
        games[guid] = list(airings)
        if teams:
            open_game[teams] = (guid, start)
    for g in games.values():
        g.sort(key=lambda a: (a["begins_at"], a["id"]))
    return games


def _guid_languages(airings: list) -> frozenset:
    """The channel languages one programme airs in. None stands for unknown."""
    return frozenset(airing_language(a) for a in airings)


def game_containing(rows: list, guid: str, anchor: int) -> list:
    """The game that holds this programme guid, seen the way a pass run sees it.

    `rows` is everything the pass could record. Every guid with an airing near
    `anchor` is taken with ALL of its airings, then grouped by `group_by_game`,
    so a stand-in or a repair chooses from the very same set the pass run does.
    """
    near = {a["program_guid"] for a in rows
            if abs(a["begins_at"] - anchor) <= LANGUAGE_WINDOW}
    for game in group_by_game([a for a in rows if a["program_guid"] in near]).values():
        if any(a["program_guid"] == guid for a in game):
            return game
    return []


def game_guids(airings: list) -> list[str]:
    """Every programme guid one game is listed under."""
    return sorted({a["program_guid"] for a in airings})


# How many airings of one game a pass books.
MODES = ("one", "preferred", "any")


def pass_mode(p) -> str:
    """The pass's channels-per-game choice. Anything unknown means one."""
    try:
        mode = p["channels_per_game"]
    except (IndexError, KeyError):
        mode = None
    return mode if mode in MODES else "one"


def preferred_language() -> str:
    return language.effective(None, None, db.get_setting("preferred_language"))


def airing_language(row) -> str | None:
    """The language of an airing's channel: the person's choice, else the
    detected one, else None. An unknown channel is NOT the preferred language
    when picking extra channels, since that would book every channel nobody
    could place. It is only fine for the single-airing fallback."""
    try:
        lang = row["channel_language"]
    except (IndexError, KeyError):
        lang = None
    return lang if lang in language.LANGUAGES else None


def choose_airings(airings: list, mode: str = "one",
                   preferred: str = "en") -> tuple[list[tuple[dict | None, str]], bool]:
    """The airings of ONE game to book, each with its reason.

    Returns (picks, per_channel). `per_channel` says whether "already booked"
    is asked of each channel on its own (modes that want several) or of the
    whole game (one airing per game, and the fallback below).

    one        exactly `choose_airing`, as before.
    preferred  one airing per channel whose language is the preferred one. When
               the game is on no such channel, ONE airing chosen as in `one`,
               so the game is never lost.
    any        one airing per channel.

    The extra channels are the ones airing the game WITH the first choice, inside
    SAME_GAME_WINDOW of it. A replay on another channel the next day is not
    another broadcast of the game, and would only cost a tuner and disk.
    """
    pick, why = choose_airing(airings, preferred)
    if mode not in ("preferred", "any") or not pick:
        return [(pick, why)], False
    live, _ = choose_airing(airings)
    # Only broadcasts of the game itself: a replay on another channel the next
    # day must not stand in for the live one.
    pool = [a for a in airings if not a["drm"]
            and abs(a["begins_at"] - live["begins_at"]) <= SAME_GAME_WINDOW]
    if mode == "preferred":
        pool = [a for a in pool if airing_language(a) == preferred]
        if not pool:
            name = language.name_of(preferred) or preferred
            return [(pick, f"{why}; no channel carries it in {name}")], False
    anchor, why = choose_airing(pool, preferred)
    near = [a for a in pool
            if abs(a["begins_at"] - anchor["begins_at"]) <= SAME_GAME_WINDOW]
    by_channel = {}
    for a in near:
        by_channel.setdefault(a["channel_vcn"], []).append(a)
    picks = []
    for vcn, rows in by_channel.items():
        # The anchor stands for its own channel, so the first choice is the
        # very airing `choose_airing` would have taken alone.
        one = anchor if vcn == anchor["channel_vcn"] else choose_airing(rows)[0]
        picks.append((one, why if one is anchor else
                      f"also on {vcn}, same game"))
    picks.sort(key=lambda t: (t[0]["begins_at"], str(t[0]["channel_vcn"])))
    if len(picks) > 1:
        picks = [(a, f"{r}; {len(picks)} channels carry this game") for a, r in picks]
    return picks, True


def plan(rows: list, networks: list[str], channels: list[str], mode: str,
         preferred: str) -> list:
    """Every airing a pass would book from these rows, without booking.

    The same choosing `_evaluate` does, for the preview. Games with no usable
    airing are left out.
    """
    out = []
    for airings in group_by_game(rows).values():
        allowed = [a for a in airings if in_sources(a, networks, channels)]
        if not allowed:
            continue
        picks, _ = choose_airings(allowed, mode, preferred)
        out.extend(a for a, _r in picks if a)
    return out


def peak_overlap(airings: list) -> int:
    """The most recordings running at one moment."""
    edges = []
    for a in airings:
        end = a["ends_at"] or (a["begins_at"] + 3600)
        edges.append((a["begins_at"], 1))
        edges.append((end, -1))
    edges.sort(key=lambda e: (e[0], e[1]))     # an end before a start at the same second
    now = peak = 0
    for _t, d in edges:
        now += d
        peak = max(peak, now)
    return peak


def last_read() -> int:
    """When Plex was last read back into our copy.

    A constant for the length of one pass run, so `run_passes` reads it once
    and hands it down rather than paying for it per game.
    """
    return db.one("SELECT COALESCE(MAX(started_at), 0) t FROM sync_log "
                  "WHERE ok = 1")["t"]


def already_handled(program_guid, read_at: int | None = None,
                    channel: str | None = None) -> str | None:
    """Why this game needs no booking, or None.

    THE LIVE STATE, NEVER THE LOG. This used to answer yes to any programme
    that had ever been written down as scheduled, which is a different
    question and the wrong one. A booking can be lost after it is made: Plex
    drops a subscription on its own, the user cancels one, or the guide
    re-times the broadcast and the pinned recording stops existing. In every
    one of those the log still said scheduled, so the pass never looked at the
    game again and nothing recorded it. A stale record read as consent, which
    is the same shape as a failed lookup read as permission.

    Matching Plex's grabs by title alone treated any programme that keeps its
    title, a daily news or quiz show, as already covered for ever. A programme
    guid names one episode and so one game, which is why it is what is asked
    about here: the rebroadcast of a game shares its programme, and recording
    either of them is recording the game.

    `program_guid` may be a list: a game listed under several guids is handled
    when ANY of them is.

    `channel` narrows the question to one channel, for a pass that wants the
    game on several: a booked NBC airing must not stop the Telemundo one.
    """
    guids = [program_guid] if isinstance(program_guid, str) else list(program_guid)
    if not guids:
        return None
    marks = ",".join("?" * len(guids))
    # Our own booking, and then whether it is real. `our_grabs` alone is an
    # intention; Plex listing the subscription is the fact. The second half of
    # the OR is the moment between booking one and reading Plex back: the row
    # exists, `plex_subscriptions` is only refreshed by a sync and cannot see
    # it yet, and without this two runs in a row would book the game twice.
    if read_at is None:
        read_at = last_read()
    if any(recorder.booked(program_guid=g, channel=channel) for g in guids):
        return "CouchElephant is recording it"
    on = " AND o.channel_vcn = ?" if channel else ""
    on_grab = " AND g.channel_vcn = ?" if channel else ""
    extra = (channel,) if channel else ()
    mine = db.one(
        """SELECT 1 FROM our_grabs o
             LEFT JOIN plex_subscriptions s ON s.key = o.subscription
            WHERE o.program_guid IN (""" + marks + """)
              AND (s.key IS NOT NULL OR o.created_at >= ?)""" + on + """ LIMIT 1""",
        (*guids, read_at, *extra))
    if mine:
        return "already booked by a pass"
    hit = db.one(
        """SELECT g.status FROM plex_grabs g
           JOIN airings a ON a.channel_vcn = g.channel_vcn AND a.begins_at = g.begins_at
           WHERE a.program_guid IN (""" + marks + """)
             AND g.status IN ('scheduled','inprogress','complete')""" + on_grab + """
           LIMIT 1""", (*guids, *extra))
    if hit:
        return f"Plex already has it ({hit['status']})"
    return None


def _log(pass_id, row, action, reason, dry_run, subscription=None):
    with db.tx() as c:
        c.execute(
            """INSERT INTO pass_actions (pass_id, program_guid, airing_id, program_title,
                                         channel_vcn, begins_at, action, reason,
                                         plex_subscription, dry_run, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (pass_id, row["program_guid"] if row else None,
             row["id"] if row else None,
             row["title"] if row else None,
             row["channel_vcn"] if row else None,
             row["begins_at"] if row else None,
             action, reason, subscription, 1 if dry_run else 0, _now()),
        )


def templates(plex: Plex, row) -> list[dict]:
    """Every recording option Plex offers for this programme, in its order."""
    out = []
    for t in plex.template(row["rating_key"]):
        out.extend(t.get("MediaSubscription") or [])
    return out


# Plex's own subscription types. 4 is one broadcast; 2 is a series or a
# league; 15 is a team. Anything that is not 4 recurs.
ONE_SHOT_TYPE = 4


def is_one_shot(template: dict) -> bool:
    """Whether this template books one broadcast rather than a rule.

    Read from Plex's `type`, not from its title. The title is localized: a
    German server offers "Diese Sendung", not "This Episode", and matching the
    English word made the whole app pick the wrong template for anyone whose
    Plex is not in English. The title is only a fallback for a server old
    enough not to send a type at all.
    """
    kind = template.get("type")
    if kind not in (None, ""):
        try:
            return int(kind) == ONE_SHOT_TYPE
        except (TypeError, ValueError):
            pass
    return (template.get("title") or "").lower().startswith("this ")


def single_template(options: list[dict]) -> dict | None:
    """The one-shot option: the one that books this broadcast and no other."""
    for s in options:
        if is_one_shot(s):
            return s
    return None


def offered_prefs(template: dict, prefs: dict) -> dict:
    """Only the settings this template actually offers.

    Plex refuses a whole booking if one setting is not on the template's own
    list, and it refuses it with a bare HTML 400 page, in no measurable time,
    with nothing in its log. Read against the client that is a network fault,
    not a bad request, which is exactly how it went unnoticed.

    That is not hypothetical. A team pass made before the settings panel
    started hiding them kept `onlyNewAirings` in its stored settings. A pass
    always books the one-shot template, and the one-shot template does not
    offer that setting, so every booking failed from 2026-08-22 to 2026-09-10:
    364 attempts, two per hour, and no Chiefs game recorded.

    A template that declares no settings is an unknown server, not a server
    that allows none, so nothing is dropped in that case.
    """
    ids = {s.get("id") for s in (template.get("Setting") or []) if s.get("id")}
    if not ids:
        return dict(prefs)
    return {k: v for k, v in prefs.items() if k in ids}


def _schedule(plex, row, target_section, source="pass", template=None, prefs=None,
              pass_id=None, expectation_id=None):
    """Create a recording for this broadcast.

    With no template given this is the pinned one-shot the passes rely on.
    The overlay passes its own template and settings, so the user gets the
    same choices Plex itself offers.
    """
    chosen = template
    if chosen is None:
        chosen = single_template(templates(plex, row))
        if not chosen:
            raise PlexError("Plex offered no single-event recording option")

    # Drop anything this template will not take, before the pins go on. Plex
    # answers 400 to the whole request otherwise, and says nothing about why.
    prefs = offered_prefs(chosen, dict(prefs or {}))
    # These three are not the user's to change on a pass booking. The pin is
    # the whole mechanism: without it Plex picks the airing itself, which is
    # the bug this app exists to fix. They go on AFTER the filter, so a server
    # that declares no settings at all still gets a pinned booking.
    prefs.update({
        "oneShot": "1",
        "lineupChannel": row["channel_identifier"] or "",
        "startTimeslot": str(row["begins_at"]),
    })
    key = plex.create_recording(
        chosen["parameters"],
        target_section or chosen.get("targetLibrarySectionID") or 2,
        int(chosen.get("type") or 4),
        prefs,
    )
    # Plex answers 200 and hands back a key, then sometimes drops the
    # subscription by itself. Reporting a recording it did not keep is worse
    # than failing, so check before claiming anything.
    # `is False` on purpose. None means the check could not be made, and a
    # network blip must not be reported as "Plex discarded your recording".
    if key and plex.subscription_exists(key) is False:
        raise PlexError(
            "Plex accepted the recording and then discarded it. That usually "
            "means it already has this episode, or the airing is a repeat and "
            "the rule is set to new airings only.")
    if not key:
        # Older path: no key in the reply, so look it up.
        try:
            key = plex.find_subscription(row["program_guid"], row["begins_at"], tries=3)
        except Exception:
            key = None
    remember(row, source, key, pass_id, expectation_id)
    return chosen.get("targetLibrarySectionID")


def pass_recorder(p) -> str:
    """Who records what this pass books. Anything but our name means Plex."""
    mine = p["recorder"] if "recorder" in p.keys() else None
    return "couchelephant" if mine == "couchelephant" else "plex"


def padding(prefs: dict) -> tuple[int | None, int | None]:
    """Plex's two padding settings as (lead, trail) minutes, None where unset.

    A pass keeps Plex's own settings. Our recorder honours the two that mean
    the same thing to it, so "ten minutes after" is not lost by the choice of
    recorder.
    """
    def minutes(key):
        try:
            return max(0, int(prefs.get(key)))
        except (TypeError, ValueError):
            return None
    return minutes("startOffsetMinutes"), minutes("endOffsetMinutes")


def _book(plex, p, pick, expectation_id=None):
    """Book one broadcast for a pass, with the recorder the pass names.

    A pass that names our recorder on an install where it cannot run fails
    here, in words. It does not fall back to Plex: the user chose, and a
    silent switch would hide that the recorder is off.
    """
    prefs = dict(db.unjs(p["prefs"]) or {})
    if pass_recorder(p) != "couchelephant":
        _schedule(plex, pick, None, "pass", prefs=prefs, pass_id=p["id"],
                  expectation_id=expectation_id)
        return
    why = recorder.why_unavailable()
    if why:
        raise RuntimeError(why)
    lead, trail = padding(prefs)
    recorder.book(pick, "pass", pass_id=p["id"], pass_uid=p["uid"],
                  trail_minutes=trail, lead_minutes=lead)


def rehome(p, plex) -> int:
    """Hand a pass's future bookings to the recorder the pass now names.

    Changing who records a pass means the games it has already booked, not
    only the ones it books next. Each booking the other recorder holds, and
    has not started, is cancelled there. Nothing is booked here: the caller
    refreshes what Plex has and runs the passes, which book each game again
    with the right recorder, by the pass's own rules as they stand now.

    Returns how many bookings were cancelled to be made again.
    """
    now, moved = _now(), 0
    if pass_recorder(p) == "couchelephant":
        for g in db.query("SELECT * FROM our_grabs WHERE pass_id = ? AND begins_at > ?",
                          (p["id"], now + LEAD_SECONDS)):
            # Only a key we stored. One looked up by game and start can be a
            # recurring rule, and deleting that removes the whole rule.
            if not g["subscription"]:
                continue
            plex.delete_subscription(g["subscription"])
            forget(g["airing_id"])
            moved += 1
    else:
        for r in db.query("SELECT id FROM recordings WHERE pass_id = ? AND kind = 'recording' "
                          "AND state = 'scheduled' AND starts_at > ?", (p["id"], now)):
            recorder.cancel(r["id"])
            moved += 1
    return moved


def _gone_pass(col_id: str, col_uid: str, alias: str) -> str:
    """SQL: this row names a pass that no longer exists."""
    return (f"{alias}.{col_id} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM passes x "
            f"WHERE x.id = {alias}.{col_id} OR (x.uid IS NOT NULL AND x.uid = {alias}.{col_uid}))")


def _upcoming(pass_id: int):
    """What of this pass has not started: (our recordings, Plex bookings we
    can cancel, Plex bookings with no stored key, which only Plex can drop)."""
    now = _now()
    recs = db.query("SELECT * FROM recordings WHERE pass_id = ? AND kind = 'recording' "
                    "AND state = 'scheduled' AND starts_at > ?", (pass_id, now))
    grabs = db.query("SELECT * FROM our_grabs WHERE pass_id = ? AND begins_at > ?",
                     (pass_id, now + LEAD_SECONDS))
    keyed = [g for g in grabs if g["subscription"]]
    return recs, keyed, [g for g in grabs if not g["subscription"]]


def keyless(pass_id: int) -> int:
    """Plex bookings of this pass we cannot cancel, for lack of a stored key."""
    return len(_upcoming(pass_id)[2])


def count_upcoming(pass_id: int) -> int:
    """How many bookings of this pass a delete would cancel. Recording now,
    finished and in Plex ones are never cancelled with a pass."""
    recs, keyed, _ = _upcoming(pass_id)
    return len(recs) + len(keyed)


class CancelFailed(Exception):
    """Cancelling stopped part way. `done` is how many were cancelled, and
    `where` is "plex" or "recorder", whichever failed."""
    def __init__(self, message, done=0, where="plex"):
        super().__init__(message)
        self.done = done
        self.where = where


def cancel_upcoming(pass_id: int) -> int:
    """Cancel what this pass booked and has not started. Answers how many.

    Plex is asked first whether it is there at all, so a down Plex changes
    nothing. If it fails part way, CancelFailed says how many had gone. A
    Plex booking with no stored key is left alone and linked: only Plex can
    drop it, and `keyless` says how many. Works after the pass is gone too,
    which is what cancelling a lost pass's bookings is.
    """
    recs, grabs, _ = _upcoming(pass_id)
    return _cancel(recs, grabs)


def _cancel(recs, grabs) -> int:
    """Cancel these bookings, the one way every cleanup does it. Answers how
    many went. `grabs` need a stored key. Raises CancelFailed."""
    done, failure = 0, None
    if grabs:
        try:
            with Plex(db.get_setting("plex_url"), db.get_setting("plex_token")) as plex:
                try:
                    plex.server_info()
                    for g in grabs:
                        # Asked again: it may be gone or about to start.
                        now = db.one("SELECT * FROM our_grabs WHERE airing_id = ?",
                                     (g["airing_id"],))
                        if (not now or not now["subscription"]
                                or now["begins_at"] <= _now() + LEAD_SECONDS):
                            continue
                        plex.delete_subscription(now["subscription"])
                        forget(g["airing_id"])
                        done += 1
                except Exception as e:  # noqa: BLE001
                    failure = CancelFailed(str(e), done, "plex")
                try:
                    from . import sync  # sync imports this module
                    sync.sync_recordings(plex)
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            failure = failure or CancelFailed(str(e), done, "plex")
        if failure:
            raise failure
    for r in recs:
        try:
            if recorder.cancel_if_waiting(r["id"]):
                done += 1
        except Exception as e:  # noqa: BLE001
            raise CancelFailed(str(e), done, "recorder") from e
    return done


def _pass_bookings(pass_id: int) -> list[dict]:
    """What this pass booked by itself and has not started, in the shape
    `group_by_game` reads. Bookings made by hand carry another source and are
    never listed, so nothing below can touch them."""
    now = _now()
    out = []
    for g in db.query(
            "SELECT o.*, p.teams FROM our_grabs o LEFT JOIN programs p ON p.guid = o.program_guid "
            "WHERE o.pass_id = ? AND o.source = 'pass' AND o.begins_at > ?",
            (pass_id, now + LEAD_SECONDS)):
        out.append({"id": g["airing_id"], "program_guid": g["program_guid"],
                    "teams": g["teams"], "begins_at": g["begins_at"],
                    "channel_vcn": g["channel_vcn"], "grab": g, "rec": None})
    for r in db.query(
            "SELECT r.*, p.teams FROM recordings r LEFT JOIN programs p ON p.guid = r.program_guid "
            "WHERE r.pass_id = ? AND r.source = 'pass' AND r.kind = 'recording' "
            "AND r.state = 'scheduled' AND r.starts_at > ?", (pass_id, now)):
        out.append({"id": r["airing_id"], "program_guid": r["program_guid"],
                    "teams": r["teams"], "begins_at": r["begins_at"],
                    "channel_vcn": r["channel_vcn"], "grab": None, "rec": r})
    return out


def _booking_rank(b: dict, preferred: str) -> int:
    row = db.one("SELECT COALESCE(language_override, language) AS l FROM channels "
                 "WHERE vcn = ?", (b["channel_vcn"],))
    lang = row["l"] if row and row["l"] in language.LANGUAGES else None
    return 0 if lang == preferred else (1 if lang is None else 2)


def _keeper(bookings: list[dict], preferred: str | None = None) -> dict:
    """Which booking of one game stays: the airing `choose_airing` would pick
    from the booked ones (preferred language first, then the live broadcast,
    else the earliest). When the guide no longer holds them, the best-ranked
    and then earliest booked stays, because it is the one a reader of the log
    saw first. Always the same answer for the same bookings."""
    ids = [b["id"] for b in bookings if b["id"]]
    found = db.query(_AIRING_SQL + f" AND a.id IN ({','.join('?' * len(ids))})",
                     (0, 2**62, *ids)) if ids else []
    pick, _ = choose_airing(found, preferred) if found else (None, "")
    if pick:
        for b in bookings:
            if b["id"] == pick["id"]:
                return b
    return min(bookings, key=lambda b: (_booking_rank(b, preferred) if preferred else 0,
                                        b["begins_at"], str(b["id"])))


def _extras(mode: str, bookings: list[dict], preferred: str) -> tuple[list[dict], str]:
    """The bookings of one game that the pass's mode says to cancel, and why."""
    if mode == "any":
        return _doubles(bookings, [], preferred), "this channel already records this game"
    if mode == "one":
        keep = _keeper(bookings, preferred)
        return ([b for b in bookings if b is not keep],
                "another channel already records this game")
    # preferred: the channels that are not in the preferred language go, and
    # at least one booking always stays.
    langs = {}
    for b in bookings:
        row = db.one("SELECT COALESCE(language_override, language) AS l FROM channels "
                     "WHERE vcn = ?", (b["channel_vcn"],))
        langs[id(b)] = row["l"] if row else None
    wrong = [b for b in bookings if langs[id(b)] != preferred]
    if len(wrong) == len(bookings):
        keep = _keeper(bookings, preferred)
        wrong = [b for b in bookings if b is not keep]
        return wrong, "another channel carries this game in your preferred language"
    return (wrong + _doubles(bookings, wrong, preferred),
            "another channel carries this game in your preferred language")


def _doubles(bookings: list[dict], skip: list[dict], preferred: str) -> list[dict]:
    """More than one booking of one game on one channel: all but the keeper."""
    by_channel = {}
    for b in bookings:
        if not any(b is x for x in skip):
            by_channel.setdefault(b["channel_vcn"], []).append(b)
    out = []
    for rows in by_channel.values():
        if len(rows) > 1:
            keep = _keeper(rows, preferred)
            out.extend(b for b in rows if b is not keep)
    return out


def dedupe_games(dry: bool = False) -> list[dict]:
    """Cancel the extra bookings a pass made of one game on several channels.

    Older versions booked every guid of a game. Per pass, upcoming bookings
    the pass made itself are grouped as `group_by_game` groups them, and what
    goes depends on the pass's channels-per-game choice (`_extras`): one
    booking stays for "one", the ones outside the preferred language go for
    "preferred", and nothing goes for "any". Cancelled the way a pass delete
    cancels, in Plex too. Safe to run every time: with nothing extra it finds
    nothing. A Plex booking with no stored key cannot be cancelled and stays.
    """
    results = []
    preferred = preferred_language()
    for p in db.query("SELECT id, channels_per_game FROM passes"):
        mode = pass_mode(p)
        games = group_by_game(_pass_bookings(p["id"]))
        for bookings in games.values():
            if len(bookings) < 2:
                continue
            extra, why = _extras(mode, bookings, preferred)
            for b in extra:
                if b["grab"] is not None and not b["grab"]["subscription"]:
                    continue
                row = (db.one("SELECT a.*, pr.title FROM airings a JOIN programs pr "
                              "ON pr.guid = a.program_guid WHERE a.id = ?", (b["id"],))
                       or {"id": b["id"], "program_guid": b["program_guid"], "title": None,
                           "channel_vcn": None, "begins_at": b["begins_at"]})
                if dry:
                    _log(p["id"], row, "would cancel", why, True)
                    results.append({"action": "would cancel", "reason": why})
                    continue
                try:
                    with PASS_LOCK:
                        _cancel([b["rec"]] if b["rec"] else [],
                                [b["grab"]] if b["grab"] else [])
                    _log(p["id"], row, "cancelled", why, False)
                    results.append({"action": "cancelled", "reason": why})
                except Exception as e:  # noqa: BLE001
                    msg = f"{type(e).__name__}: {e}"
                    _log(p["id"], row, "failed", "could not cancel the extra booking: " + msg, False)
                    results.append({"action": "failed", "reason": msg})
    return results


def orphans() -> list[dict]:
    """Bookings of passes that were deleted, one entry per lost pass.

    Each has the show it was following, how many are still ahead, and whether
    a pass can be made again from what the bookings hold: only a series whose
    name they carry. A team pass cannot be rebuilt from its games.
    """
    groups: dict = {}

    def grp(pid):
        return groups.setdefault(pid, {"lost_id": pid, "title": "", "shown": "", "sports": False,
                                       "recorder": "plex", "n": 0})
    for r in db.query(
            "SELECT r.* FROM recordings r WHERE r.kind = 'recording' "
            "AND r.state = 'scheduled' AND r.starts_at > " + str(_now()) + " AND "
            + _gone_pass("pass_id", "pass_uid", "r")
            + " ORDER BY r.begins_at"):
        g = grp(r["pass_id"])
        g["n"] += 1
        g["recorder"] = "couchelephant"
        g["title"] = g["title"] or r["show_title"] or ""
        g["shown"] = g["shown"] or r["show_title"] or r["title"] or ""
        g["sports"] = g["sports"] or r["section"] == "sports"
    for o in db.query(
            "SELECT o.*, p.grandparent_title AS show, p.section AS section "
            "FROM our_grabs o LEFT JOIN programs p ON p.guid = o.program_guid "
            "WHERE o.begins_at > ? AND " + _gone_pass("pass_id", "pass_uid", "o")
            + " ORDER BY o.begins_at", (_now() + LEAD_SECONDS,)):
        g = grp(o["pass_id"])
        g["n"] += 1
        g["title"] = g["title"] or o["show"] or ""
        g["shown"] = g["shown"] or o["show"] or o["title"] or ""
        g["sports"] = g["sports"] or o["section"] == "sports"
    out = []
    for g in groups.values():
        out.append({"lost_id": g["lost_id"], "title": g["title"] or g["shown"] or "Unknown",
                    "upcoming": g["n"], "sports": g["sports"], "recorder": g["recorder"],
                    "can_follow": bool(g["title"]) and not g["sports"]})
    out.sort(key=lambda o: o["title"].lower())
    return out


def change_recorder(pass_id: int) -> tuple[int, list[dict]]:
    """After a pass's recorder was changed: move its bookings and book again.

    Plex's schedule is read again between the two halves. Without that, the
    games just cancelled in Plex still look booked there and are skipped.
    """
    p = db.one("SELECT * FROM passes WHERE id = ?", (pass_id,))
    if not p:
        return 0, []
    from . import sync  # sync imports this module
    with Plex(db.get_setting("plex_url"), db.get_setting("plex_token")) as plex:
        moved = rehome(p, plex)
        sync.sync_recordings(plex)
    done = run_passes()
    if moved and pass_recorder(p) != "couchelephant":
        with Plex(db.get_setting("plex_url"), db.get_setting("plex_token")) as plex:
            sync.sync_recordings(plex)
    return moved, done


def remember(row, source: str, subscription: str | None = None,
             pass_id: int | None = None, expectation_id: int | None = None) -> None:
    """Record that this airing was scheduled by us, and by what.

    The pass is recorded by uid as well as by id, because `id` is an
    autoincrement and means nothing on another machine.

    `expectation_id` marks a stand-in booking, and is written as given rather
    than kept: booking the same airing again for its own sake, once the guide
    names it, is what ends the stand-in.
    """
    with db.tx() as c:
        c.execute(
            """INSERT INTO our_grabs (airing_id, program_guid, title, channel_vcn,
                                      begins_at, source, subscription, pass_id,
                                      pass_uid, expectation_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,
                       (SELECT uid FROM passes WHERE id = ?),?,?)
               ON CONFLICT(airing_id) DO UPDATE SET source=excluded.source,
                 subscription=COALESCE(excluded.subscription, our_grabs.subscription),
                 pass_id=COALESCE(excluded.pass_id, our_grabs.pass_id),
                 pass_uid=COALESCE(excluded.pass_uid, our_grabs.pass_uid),
                 expectation_id=excluded.expectation_id,
                 created_at=excluded.created_at""",
            (row["id"], row["program_guid"], row["title"], row["channel_vcn"],
             row["begins_at"], source, subscription, pass_id, pass_id,
             expectation_id, _now()))


def forget(airing_id) -> None:
    """Drop our record of an airing, after the recording has been cancelled."""
    with db.tx() as c:
        c.execute("DELETE FROM our_grabs WHERE airing_id = ?", (airing_id,))


def choose_stand_in(item, networks, channels) -> tuple[dict | None, str]:
    """The generic slot to book for a game the guide has not named, and why.

    Returns (row, reason), or (None, reason) when no slot can be trusted. Two
    generic slots at the same kickoff on different channels are two different
    games, and nothing yet says which is this one, so neither is booked. The
    live broadcast wins over a repeat, as it does for a named game.
    """
    rows = [r for r in expectations.stand_in_candidates(item)
            if in_sources(r, networks, channels)
            and r["begins_at"] > _now() - LEAD_SECONDS]
    if not rows:
        return None, "no generic slot in the guide at that kickoff"
    live = [r for r in rows if r["premiere"]] or rows
    where = sorted({r["channel_vcn"] for r in live})
    if len(where) > 1:
        return None, (f"the guide has generic slots on {', '.join(where)} at that "
                      f"kickoff, and nothing says which one is this game")
    pick = min(live, key=lambda r: r["begins_at"])
    return pick, (f"stand-in for {item['subtitle'] or item['title']}: the guide "
                  f"lists this slot as \"{pick['title']}\" and has not named "
                  f"the teams yet")


def stand_in_held(item, pick, read_at) -> str | None:
    """Why this stand-in needs no booking, or None.

    Asked of the SLOT, never of the programme. A generic listing is not a
    name for a game, and a guide that reused one guid for every bare "NFL
    Football" would otherwise read one booked slot as all of them booked.
    """
    if recorder.booked(pick):
        return "CouchElephant is recording it"
    mine = db.one(
        """SELECT 1 FROM our_grabs o
             LEFT JOIN plex_subscriptions s ON s.key = o.subscription
            WHERE (o.expectation_id = ? OR o.airing_id = ?)
              AND (s.key IS NOT NULL OR o.created_at >= ?) LIMIT 1""",
        (item["id"], pick["id"], read_at))
    if mine:
        return "already booked by a pass"
    hit = db.one(
        """SELECT status FROM plex_grabs
            WHERE channel_vcn = ? AND begins_at = ?
              AND status IN ('scheduled','inprogress','complete') LIMIT 1""",
        (pick["channel_vcn"], pick["begins_at"]))
    if hit:
        return f"Plex already has it ({hit['status']})"
    return None


def rule_label(rule) -> str:
    keys = rule.keys()
    if rule["kind"] == "smart":
        named = rule["label"] if "label" in keys else None
        if named:
            return named
        return smartfilter.describe(db.unjs(rule["filter"], {}) or {})
    return rule["team_name"] or rule["series_title"] or "rule"


def run_passes(force_dry_run: bool | None = None) -> list[dict]:
    """Evaluate every enabled pass. Returns a list of decision dicts."""
    dry = db.get_setting("dry_run") == "1" if force_dry_run is None else force_dry_run
    results = []
    # First, tidy what older versions double booked. Idempotent.
    dedupe_games(dry)
    rows = db.query("SELECT * FROM passes WHERE enabled = 1")
    if not rows:
        return results

    plex = None
    if not dry:
        plex = Plex(db.get_setting("plex_url"), db.get_setting("plex_token"))
    try:
        _evaluate(rows, plex, dry, results)
    finally:
        if plex is not None:
            plex.close()
    return results


def _handle_pick(p, plex, dry, results, read_at, label, airings, pick, reason, channel):
    """Book one chosen airing of a game, or say why not. `channel` is set when
    "already booked" is asked of that channel alone."""
    blocked = already_handled(game_guids(airings), read_at, channel)
    if blocked:
        results.append({"pass": label, "game": pick["title"],
                        "action": "skipped", "reason": blocked,
                        "channel": pick["channel_vcn"], "begins_at": pick["begins_at"]})
        return
    if dry:
        _log(p["id"], pick, "would schedule", reason, True)
        results.append({"pass": label, "game": pick["title"],
                        "action": "would schedule", "reason": reason,
                        "channel": pick["channel_vcn"], "begins_at": pick["begins_at"]})
        return
    try:
        with PASS_LOCK:
            # Deleted or paused since this run began: book nothing.
            live = db.one("SELECT 1 FROM passes WHERE id = ? AND enabled = 1",
                          (p["id"],))
            if not live:
                return
            _book(plex, p, pick)
        _log(p["id"], pick, "scheduled", reason, False)
        results.append({"pass": label, "game": pick["title"],
                        "action": "scheduled", "reason": reason,
                        "channel": pick["channel_vcn"], "begins_at": pick["begins_at"]})
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        _log(p["id"], pick, "failed", msg, False)
        results.append({"pass": label, "game": pick["title"],
                        "action": "failed", "reason": msg,
                        "channel": pick["channel_vcn"], "begins_at": pick["begins_at"]})


def _evaluate(rows, plex, dry, results):
    # Read once for the whole run. It cannot change while this is running, and
    # it was being asked once per game per pass.
    read_at = last_read()
    preferred = preferred_language()
    for p in rows:
        label = rule_label(p)
        networks, channels = allowed_sources(p)
        try:
            games = group_by_game(rule_airings(p))
        except smartfilter.FilterError as e:
            # One unusable filter must not stop every other pass running.
            _log(p["id"], None, "failed", f"the filter cannot be read: {e}", dry)
            results.append({"pass": label, "game": "", "action": "failed",
                            "reason": f"the filter cannot be read: {e}"})
            continue
        for airings in games.values():
            # The source limit is applied before the choice, not after, so the
            # rule picks the best airing among the ones it is allowed to use
            # rather than picking first and then finding it disallowed.
            allowed = [a for a in airings if in_sources(a, networks, channels)]
            if not allowed:
                where = " or ".join(networks + channels)
                reason = f"no airing is on {where}"
                _log(p["id"], airings[0], "skipped", reason, dry)
                results.append({"pass": label, "game": airings[0]["title"],
                                "action": "skipped", "reason": reason})
                continue
            picks, per_channel = choose_airings(allowed, pass_mode(p), preferred)
            if not picks[0][0]:
                reason = picks[0][1]
                if networks or channels:
                    reason += f", limited to {' or '.join(networks + channels)}"
                _log(p["id"], allowed[0], "skipped", reason, dry)
                results.append({"pass": label, "game": allowed[0]["title"],
                                "action": "skipped", "reason": reason})
                continue
            for pick, reason in picks:
                if networks or channels:
                    reason += f", limited to {' or '.join(networks + channels)}"
                _handle_pick(p, plex, dry, results, read_at, label, airings, pick,
                             reason, pick["channel_vcn"] if per_channel else None)
        if p["kind"] == "team":
            _stand_ins(p, plex, dry, results, read_at, label, networks, channels)


def _stand_ins(p, plex, dry, results, read_at, label, networks, channels):
    """Book the generic slot for each game the guide has not named yet.

    A team pass matches games by their teams, so a game the guide lists as a
    bare "NFL Football" is invisible to the loop above. The league's schedule
    says when it kicks off, and `choose_stand_in` finds the slot. Once the
    guide names the game, `sync.check_bookings` moves the booking onto the
    named listing, so Plex shows the real title too.

    An ambiguous slot is logged and skipped, never guessed. Only games inside
    the same thirty days `_future` looks at, so a stand-in is never booked
    further out than a named game would be.
    """
    until = _now() + 30 * 86400
    for item in expectations.waiting(p["id"]):
        if not item["expected_at"] or item["expected_at"] > until:
            continue
        pick, reason = choose_stand_in(item, networks, channels)
        if not pick:
            continue          # the ordinary case: the guide simply is not there yet
        game = item["subtitle"] or item["title"]
        blocked = stand_in_held(item, pick, read_at)
        if blocked:
            results.append({"pass": label, "game": game, "action": "skipped",
                            "reason": blocked, "channel": pick["channel_vcn"],
                            "begins_at": pick["begins_at"]})
            continue
        if dry:
            _log(p["id"], pick, "would schedule", reason, True)
            results.append({"pass": label, "game": game, "action": "would schedule",
                            "reason": reason, "channel": pick["channel_vcn"],
                            "begins_at": pick["begins_at"]})
            continue
        try:
            _book(plex, p, pick, expectation_id=item["id"])
            _log(p["id"], pick, "scheduled", reason, False)
            results.append({"pass": label, "game": game, "action": "scheduled",
                            "reason": reason, "channel": pick["channel_vcn"],
                            "begins_at": pick["begins_at"]})
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            _log(p["id"], pick, "failed", msg, False)
            results.append({"pass": label, "game": game, "action": "failed",
                            "reason": msg, "channel": pick["channel_vcn"],
                            "begins_at": pick["begins_at"]})
