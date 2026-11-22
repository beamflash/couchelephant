"""SQLite storage. One file, WAL, no ORM.

The guide is a cache: it is re-fetched whole and upserted, so any row can be
rebuilt from Plex at any time. The tables that matter are `passes` and
`pass_actions`, because those are ours and cannot be recovered from Plex.
"""
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager

from . import paths, teamcat

DB_PATH = paths.db_path()
_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS channels (
    vcn        TEXT PRIMARY KEY,
    call_sign  TEXT,
    title      TEXT,
    identifier TEXT,
    thumb_url  TEXT,
    logo_path  TEXT,
    logo_source TEXT,
    logo_fetched_at INTEGER,
    logo_attempts INTEGER DEFAULT 0,
    updated_at INTEGER
);

-- One row per programme. Sports games are episodes of a parent like
-- "NFL Football", so grandparent_title is the league and title is the matchup.
CREATE TABLE IF NOT EXISTS programs (
    guid                  TEXT PRIMARY KEY,
    rating_key            TEXT,
    title                 TEXT,
    grandparent_title     TEXT,
    summary               TEXT,
    type                  TEXT,
    section               TEXT,
    genres                TEXT,
    teams                 TEXT,
    thumb                 TEXT,
    art                   TEXT,
    originally_available  TEXT,
    year                  INTEGER,
    content_rating        TEXT,
    duration              INTEGER,
    -- When we last asked Plex for this row's teams, whatever the answer. A
    -- sports programme that has none is most of them, and without this the
    -- row qualifies for enrichment again on every sync, forever.
    teams_tried_at        INTEGER,
    updated_at            INTEGER
);

-- One row per BROADCAST. The same game appears here several times, once per
-- channel and time. This distinction is the whole point of the project.
CREATE TABLE IF NOT EXISTS airings (
    id                 TEXT PRIMARY KEY,
    program_guid       TEXT NOT NULL,
    channel_vcn        TEXT,
    channel_call_sign  TEXT,
    channel_identifier TEXT,
    channel_title      TEXT,
    begins_at          INTEGER,
    ends_at            INTEGER,
    premiere           INTEGER DEFAULT 0,
    resolution         TEXT,
    drm                INTEGER DEFAULT 0,
    updated_at         INTEGER,
    FOREIGN KEY (program_guid) REFERENCES programs(guid)
);
CREATE INDEX IF NOT EXISTS idx_airings_begins ON airings(begins_at);
CREATE INDEX IF NOT EXISTS idx_airings_program ON airings(program_guid);
CREATE INDEX IF NOT EXISTS idx_airings_channel ON airings(channel_vcn, begins_at);

CREATE TABLE IF NOT EXISTS teams (
    id         INTEGER PRIMARY KEY,
    name       TEXT,
    -- Which league it plays in, and when it was last seen in the guide.
    -- Teams are kept once seen rather than deleted when they stop playing,
    -- so the list you pick from grows over a season instead of shrinking to
    -- whoever is on this week.
    league     TEXT,
    in_guide   INTEGER DEFAULT 0,
    last_seen  INTEGER,
    updated_at INTEGER
);

-- Mirror of what Plex itself has scheduled, so the UI can show recurring
-- ("All new episodes of X") alongside one-off recordings.
CREATE TABLE IF NOT EXISTS plex_subscriptions (
    key             TEXT PRIMARY KEY,
    title           TEXT,
    type            TEXT,
    target_section  TEXT,
    settings        TEXT,
    created_at      INTEGER,
    updated_at      INTEGER,
    owned_by_us     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS plex_grabs (
    id            TEXT PRIMARY KEY,
    subscription  TEXT,
    status        TEXT,
    title         TEXT,
    parent_title  TEXT,
    channel_vcn   TEXT,
    begins_at     INTEGER,
    ends_at       INTEGER,
    updated_at    INTEGER
);
-- `plex_grabs` holds the WHOLE DVR schedule, not only ours, and three hot
-- questions read it: has this booking a recording, does another booking cover
-- this game, and is Plex already recording this programme. Each was a full
-- scan, the last one once per airing of the programme.
CREATE INDEX IF NOT EXISTS idx_grabs_sub ON plex_grabs(subscription);
CREATE INDEX IF NOT EXISTS idx_grabs_slot ON plex_grabs(channel_vcn, begins_at);

-- Ours. A pass says "follow this team"; the scheduler turns it into pinned
-- one-shot recordings on the airing we choose.
CREATE TABLE IF NOT EXISTS passes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL DEFAULT 'team',
    team_id      INTEGER,
    team_name    TEXT,
    -- A series rule follows one programme rather than a team.
    series_guid  TEXT,
    series_title TEXT,
    -- Two allowlists, JSON lists. Empty or null on both means anywhere. Set
    -- either and an airing has to match one of them, which is how "only ABC,
    -- CBS and FOX" is said. Plex has no equivalent: its rules take one channel.
    networks     TEXT,
    channels     TEXT,
    -- A smart pass carries a condition tree instead of a team or a series.
    filter       TEXT,
    label        TEXT,
    -- Stable across machines, unlike `id`. This is what an export, a backing
    -- store or a restore uses to say "the same pass".
    uid          TEXT,
    enabled      INTEGER DEFAULT 1,
    -- Resolved once from the team name and kept, because the ids do not
    -- change and the free tier is rate limited.
    sportsdb_team_id   TEXT,
    sportsdb_league_id TEXT,
    -- When this pass was last asked about, whatever came back. Dated here and
    -- not inferred from the rows, because an attempt that found nothing
    -- produces no rows and would otherwise repeat on every single sync.
    sportsdb_asked_at  INTEGER,
    -- The name that was resolved, so a pass renamed under us is looked up
    -- again instead of keeping another team's ids.
    sportsdb_asked_for TEXT,
    -- Whether there was a key at the time. Adding one should show the season
    -- now rather than tomorrow.
    sportsdb_asked_with TEXT,
    -- The same three facts for a SERIES pass at TVmaze. No key column, because
    -- TVmaze needs no key, so nothing about the answer can change with one.
    tvmaze_show_id  TEXT,
    tvmaze_asked_at INTEGER,
    tvmaze_asked_for TEXT,
    created_at   INTEGER
);

-- Audit trail. Every decision is written here, including the ones we skipped
-- and why, so the UI can explain itself instead of being a black box.
CREATE TABLE IF NOT EXISTS pass_actions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    pass_id           INTEGER,
    program_guid      TEXT,
    airing_id         TEXT,
    program_title     TEXT,
    channel_vcn       TEXT,
    begins_at         INTEGER,
    action            TEXT,
    reason            TEXT,
    plex_subscription TEXT,
    dry_run           INTEGER DEFAULT 0,
    created_at        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_actions_pass ON pass_actions(pass_id, created_at DESC);

-- Recordings CouchElephant created. Plex has no field saying who asked, so
-- the distinction has to be kept here.
CREATE TABLE IF NOT EXISTS our_grabs (
    airing_id    TEXT PRIMARY KEY,
    program_guid TEXT,
    title        TEXT,
    channel_vcn  TEXT,
    begins_at    INTEGER,
    source       TEXT,
    subscription TEXT,
    pass_uid     TEXT,
    created_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ourgrabs_guid ON our_grabs(program_guid);

CREATE TABLE IF NOT EXISTS sync_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at INTEGER,
    ended_at   INTEGER,
    ok         INTEGER,
    detail     TEXT
);

-- Something wrong that the person running this needs to know about, almost
-- always something Plex is or is not doing. A notice is raised by a check and
-- cleared by the same check passing again. It is never dismissed: a health
-- problem you can click away is a health problem you forget about.
--
-- `code` is the primary key, so a condition that keeps failing stays one row
-- with a growing age rather than a pile of duplicates. `first_seen` is the
-- answer to "how long has this been broken", which is the question you ask
-- when you find out four days late.
-- Something the user has asked for that the guide does not carry yet: a series
-- announced for next spring, a game the league has scheduled but no broadcaster
-- has claimed. It is an intention, never a booking. Only a guide airing can be
-- recorded, because only the guide knows the channel.
--
-- Deliberately not rows in `programs` and `airings`. Those are read by every
-- query in the app, and mixing invented rows into them would mean auditing all
-- of those queries, forever, for a flag they could forget.
--
-- UNIQUE is per pass, not global. Two passes waiting on the same game is two
-- passes, not a duplicate.
CREATE TABLE IF NOT EXISTS expectations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pass_id       INTEGER NOT NULL,
    source        TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    title         TEXT NOT NULL,
    subtitle      TEXT,
    network       TEXT,
    expected_at   INTEGER,
    -- How much of `expected_at` is real: time, day, month or year. A league
    -- gives a kickoff. An announcement often gives only a month. Rendering
    -- more precision than this says is inventing a fact.
    precision     TEXT NOT NULL,
    matched_guid  TEXT,
    matched_at    INTEGER,
    missed_at     INTEGER,
    -- When a sync FIRST saw the guide reach past this date, and what Plex's
    -- own guide refresh stamp was at that moment. Plex lists a game before it
    -- tags it with its teams, and a team expectation matches on teams, so the
    -- first look is always too early. These two turn "the guide got there"
    -- into "the guide got there, a day passed, and Plex had another go".
    covered_at    INTEGER,
    covered_epg_at INTEGER,
    updated_at    INTEGER,
    UNIQUE (source, source_id, pass_id)
);

-- A broadcast CouchElephant records itself, rather than asking Plex to. One
-- row from the moment it is booked until long after Plex has the file, because
-- the list of what is live and what was recorded is read from here.
--
-- `kind` is 'recording' or 'live'. A live row is somebody watching a channel
-- now: it uses the same capture and the same viewing copy, and is deleted with
-- its files the moment nobody is watching.
--
-- begins_at and ends_at are the broadcast. starts_at and stops_at are when the
-- tuner is opened and closed, which is the broadcast plus its padding.
CREATE TABLE IF NOT EXISTS recordings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Names the folder on disk, and the recording to a client.
    uid           TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'recording',
    -- scheduled, recording, finished, moving, in_plex, failed
    state         TEXT NOT NULL DEFAULT 'scheduled',
    error         TEXT,
    airing_id     TEXT,
    program_guid  TEXT,
    title         TEXT,
    show_title    TEXT,
    -- Kept here at booking, because the guide forgets a programme once it has
    -- aired and the handover names the file after it has.
    ptype         TEXT,
    season        INTEGER,
    episode       INTEGER,
    year          INTEGER,
    -- The guide's section for it, which is how a list tells sport from the rest.
    section       TEXT,
    channel_vcn   TEXT,
    channel_title TEXT,
    begins_at     INTEGER,
    ends_at       INTEGER,
    starts_at     INTEGER,
    stops_at      INTEGER,
    started_at    INTEGER,
    ended_at      INTEGER,
    bytes         INTEGER DEFAULT 0,
    source        TEXT,
    pass_id       INTEGER,
    pass_uid      TEXT,
    -- Where the player was, in seconds, and when a player last said it was
    -- here. The second one is what "nobody is watching" is measured from.
    position      REAL DEFAULT 0,
    watched_at    INTEGER,
    -- Set by the handover: where the file went, and what Plex calls it.
    plex_path     TEXT,
    plex_rating_key TEXT,
    -- The resume position Plex was last given, so a later change is sent too.
    plex_position REAL,
    -- Ad breaks: 1 once found from the whole file, -1 when there was no file
    -- left to read; failures and the last try, so a bad file is not read forever.
    ads_final     INTEGER DEFAULT 0,
    ads_fails     INTEGER DEFAULT 0,
    ads_tried_at  INTEGER,
    -- Sports overtime (app/overtime.py): the scoreboard's game, what it last
    -- said, the stop time before any overtime, when it was asked, and a
    -- sentence for people.
    ot_event      TEXT,
    ot_state      TEXT,
    ot_base       INTEGER,
    ot_checked_at INTEGER,
    ot_note       TEXT,
    moved_at      INTEGER,
    -- When the handover was last tried, so a failing one is not tried every
    -- five seconds.
    tried_at      INTEGER,
    created_at    INTEGER,
    updated_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_recordings_state ON recordings(state, starts_at);

-- Where the ads are in a recording, in seconds from the start of its file.
-- `final` rows come from the whole file; the others from a recording still
-- being made, and are replaced as it goes. See app/adskip.py.
CREATE TABLE IF NOT EXISTS ad_breaks (
    recording_id INTEGER NOT NULL,
    start_s      REAL NOT NULL,
    end_s        REAL NOT NULL,
    final        INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ad_breaks ON ad_breaks(recording_id, start_s);
CREATE INDEX IF NOT EXISTS idx_recordings_airing ON recordings(airing_id);

-- A television client that has been let in. With sign-in on, the app on the
-- television cannot type a password, so it shows a code and an administrator
-- types the code into Settings. Keys are stored hashed, like a session.
CREATE TABLE IF NOT EXISTS tv_devices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT,
    code        TEXT,
    poll_hash   TEXT,
    key_hash    TEXT,
    approved_at INTEGER,
    created_at  INTEGER,
    last_seen   INTEGER,
    -- What this one television may do, set per television in Settings.
    allow_delete INTEGER DEFAULT 0,
    -- With sign-in on, whether it may change Settings. Off, any one may.
    allow_settings INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS notices (
    code        TEXT PRIMARY KEY,
    severity    TEXT,
    title       TEXT,
    detail      TEXT,
    hint        TEXT,
    first_seen  INTEGER,
    last_seen   INTEGER,
    resolved_at INTEGER,
    -- Only a `tip` may ever set this. A tip is a suggestion, and asking twice
    -- is nagging. A health problem is never dismissible: one you can click
    -- away is one you forget about. `health.dismiss` is where that is kept.
    dismissed_at INTEGER
);

-- Where alerts go. A list rather than a fixed pair of settings keys, because
-- routing is the point: faults to one channel, recordings to another, a phone
-- separately. Each row carries its own event selection, so adding a channel
-- never changes what an existing one receives.
CREATE TABLE IF NOT EXISTS destinations (
    id           INTEGER PRIMARY KEY,
    -- Stable across machines, unlike `id`, and unlike `name` which the user may
    -- reuse. Rule 2 in docs/DATA.md: a key must mean the same row on any
    -- machine, which is why passes carry one too.
    uid          TEXT,
    name         TEXT NOT NULL,
    -- 'discord', 'discord_bot', 'telegram' or 'notifiarr'. The transport is
    -- chosen from this
    -- and nowhere else, so a fourth platform is one new function in notify.py.
    kind         TEXT NOT NULL,
    -- Discord's whole integration is this URL, and it is a bearer credential:
    -- anyone holding it can post to the channel. Masked in the UI, never logged.
    webhook      TEXT,
    -- The secret for the kinds that use one: Telegram's bot token, or the
    -- Notifiarr global API key. Masked, never logged.
    token        TEXT,
    -- Where the message lands, for the kinds that need it named: Telegram's
    -- chat, or the Discord channel id Notifiarr posts into. Not a secret.
    -- Telegram's is found by getUpdates rather than hunted for by hand.
    chat_id      TEXT,
    -- Comma-separated event codes. Empty means this destination is configured
    -- but has been asked to send nothing, which is not the same as disabled.
    events       TEXT NOT NULL DEFAULT '',
    -- How long a fault may stay open before it is mentioned again. 0 silences
    -- reminders without silencing the open and clear messages.
    remind_hours INTEGER NOT NULL DEFAULT 24,
    enabled      INTEGER NOT NULL DEFAULT 1,
    -- What happened last time. This app has no logger, and a webhook that has
    -- been revoked fails silently forever otherwise. The UI shows it, which is
    -- the only place a person would look.
    last_ok_at   INTEGER,
    last_error   TEXT,
    created_at   INTEGER,
    updated_at   INTEGER
);

-- What each destination has already been told. This one table is the whole
-- repeat-suppression mechanism, and it holds one rule: a row exists means this
-- destination has heard about this thing.
--
-- A fault keeps its row for as long as it is open, and the row's last_sent_at
-- is what a reminder is measured from. A one-shot activity event writes its row
-- once and never reminds, and the row existing is what stops the next hourly
-- sync announcing the same booking all over again.
--
-- It is per destination and not global on purpose. Two destinations may hold
-- different reminder intervals, and one added tomorrow must not be handed a
-- backlog of everything that opened last week.
CREATE TABLE IF NOT EXISTS notify_state (
    destination_id INTEGER NOT NULL,
    event          TEXT NOT NULL,
    -- The thing this is about: a notice code, a grab id, an airing id.
    key            TEXT NOT NULL,
    opened_at      INTEGER,
    last_sent_at   INTEGER,
    PRIMARY KEY (destination_id, event, key)
);

-- Issue reports that could not be sent yet. The blob is already sealed to the
-- owner's key, so nothing readable sits here.
CREATE TABLE IF NOT EXISTS report_queue (
    report_id   TEXT PRIMARY KEY,
    blob        BLOB NOT NULL,
    source      TEXT,
    app_version TEXT,
    created     INTEGER
);

-- When each report left, for the per-install daily limit.
CREATE TABLE IF NOT EXISTS report_sent (
    ts INTEGER
);
"""

DEFAULTS = {
    "report_consent": "",
    "plex_url": "",
    "plex_token": "",
    "timezone": "UTC",
    "sync_minutes": "60",
    # Start in preview. Nothing is written to Plex until this is turned off,
    # so the first run can be inspected before it is trusted.
    "dry_run": "1",
    "epg_provider": "",
    "sports_section": "",
    "shows_section": "",
    "movies_section": "",
    # Both optional. TVmaze needs no key, so following a series works with
    # neither of these set.
    "sportsdb_key": "",
    "tmdb_key": "",
    # The own recorder. Off in effect until a /recordings folder is mounted,
    # and Plex stays the recorder for anything that does not say otherwise.
    "tuner_url": "",
    "tuner_detected": "",
    # Who makes a new recording: 'auto', 'plex' or 'couchelephant'. Auto is
    # CouchElephant on an install where its recorder can run, and Plex on one
    # where it cannot, so turning the recorder on is what makes it the default.
    "recorder_default": "auto",
    "recorder_encoder": "auto",
    "recorder_lead_minutes": "1",
    # How long live TV may stay paused. The tuner is held that long for a player
    # that has gone quiet, and a player paused that long carries on playing.
    "live_pause_minutes": "30",
    # Ad breaks found by comskip, for a Skip button in CouchElephant's own
    # players. One while a recording is still being made, one after it ends.
    "adskip_live": "1",
    "adskip_final": "1",
    # Keep recording a game that runs long, up to this many minutes past the
    # guide's end. Asks a scoreboard; see app/overtime.py.
    "sports_overtime": "1",
    "overtime_cap_minutes": "180",
    "recorder_trail_minutes": "3",
    # The handover. The folder names are inside the /library mount, and the
    # section is the Plex library that folder belongs to. Empty means the
    # recording is held here and not handed over.
    "recorder_shows_folder": "",
    "recorder_shows_section": "",
    "recorder_movies_folder": "",
    "recorder_movies_section": "",
    # Days a viewing copy is kept after Plex has the recording.
    "recorder_keep_days": "2",
}


def _ulower(v):
    return v.lower() if isinstance(v, str) else v


def connect() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        # SQLite's own lower() and COLLATE NOCASE only fold A-Z, so a search
        # for "muller" never matched "MÜLLER" and "ÉTÉ" never matched "été".
        # Python's str.lower() folds the whole of Unicode.
        conn.create_function("ulower", 1, _ulower, deterministic=True)
        # Team identity, folded the same way in SQL as in Python. Plex renumbers
        # its team ids on every guide refresh, so the name is the stable half
        # and a query has to be able to compare it. `ident` and not `norm`:
        # `norm` drops club words and would fold Real Madrid into Atletico
        # Madrid. See `teamcat.ident`.
        conn.create_function("tident", 1, teamcat.ident, deterministic=True)
        _local.conn = conn
    return _local.conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not
# add them to a database that already exists, so they are applied by hand.
MIGRATIONS = [
    ("tv_devices", "allow_delete", "INTEGER DEFAULT 0"),
    ("tv_devices", "allow_settings", "INTEGER DEFAULT 0"),
    ("recordings", "section", "TEXT"),
    # The season and episode number, which the guide sends and nothing kept.
    # Our own recorder names a file with them, the way Plex names its own.
    ("programs", "season", "INTEGER"),
    ("programs", "episode", "INTEGER"),
    # Who makes the recordings this pass books: 'plex' or 'couchelephant'.
    # Empty on a pass made before the choice existed, which means Plex.
    ("passes", "recorder", "TEXT"),
    ("channels", "thumb_url", "TEXT"),
    ("channels", "logo_path", "TEXT"),
    # What we actually fetched, so a changed upstream URL triggers a re-fetch.
    ("channels", "logo_source", "TEXT"),
    ("channels", "logo_fetched_at", "INTEGER"),
    ("channels", "logo_attempts", "INTEGER DEFAULT 0"),
    # The Plex subscription a recording of ours created, so it can be cancelled
    # again from the same place it was scheduled.
    ("our_grabs", "subscription", "TEXT"),
    # The network a channel carries, pulled out of the guide's channel title.
    ("channels", "network", "TEXT"),
    # A rule can be limited to a set of networks, which Plex cannot express:
    # its own rules take one channel or none.
    ("passes", "networks", "TEXT"),
    ("passes", "channels", "TEXT"),
    # A logo the user supplied, which wins over whatever the guide offers. Kept
    # in its own column so a re-fetch of the guide's own art cannot touch it.
    ("channels", "custom_logo", "TEXT"),
    ("channels", "custom_logo_at", "INTEGER"),
    # Which rule booked this, so the schedule can say why a thing records.
    ("our_grabs", "pass_id", "INTEGER"),
    # Plex's own recording settings, kept on the pass and applied to every
    # broadcast it books, so padding and quality are not lost by using a pass.
    ("passes", "prefs", "TEXT"),
    # What a Plex rule follows. Its own title is the generic template name,
    # "All Episodes", which says nothing about which programme.
    ("plex_subscriptions", "target", "TEXT"),
    ("passes", "series_guid", "TEXT"),
    ("passes", "series_title", "TEXT"),
    # The parental rating and the running time, so a smart filter can ask about
    # them. Both come from the guide listing and are empty until the next sync.
    ("programs", "content_rating", "TEXT"),
    ("programs", "duration", "INTEGER"),
    # A smart pass keeps its condition tree here, as JSON. Plex cannot express
    # one, so this kind of pass is always CouchElephant's to run.
    ("passes", "filter", "TEXT"),
    ("passes", "label", "TEXT"),
    # Teams are now remembered rather than dropped when they stop playing.
    ("teams", "league", "TEXT"),
    # A suggestion the user has waved off. Health problems can never set it;
    # `health.dismiss` refuses any severity but `tip`.
    ("notices", "dismissed_at", "INTEGER"),
    # Where this team sits at TheSportsDB. Resolved once from the name.
    ("passes", "sportsdb_team_id", "TEXT"),
    ("passes", "sportsdb_league_id", "TEXT"),
    ("passes", "sportsdb_asked_at", "INTEGER"),
    ("passes", "sportsdb_asked_for", "TEXT"),
    ("passes", "sportsdb_asked_with", "TEXT"),
    # Where this show sits at TVmaze, and when it was last asked about. A
    # series pass used to get its expectations once, at creation, and never
    # again, so it never learned about an episode announced later.
    ("passes", "tvmaze_show_id", "TEXT"),
    ("passes", "tvmaze_asked_at", "INTEGER"),
    ("passes", "tvmaze_asked_for", "TEXT"),
    ("teams", "in_guide", "INTEGER DEFAULT 0"),
    ("teams", "last_seen", "INTEGER"),
    # A pass's `id` is an autoincrement, which means a different number on
    # every install. Anything that leaves this machine, an export, a backing
    # store, a snapshot restored elsewhere, needs a name for a pass that two
    # machines agree on. See [[dbstore]].
    ("passes", "uid", "TEXT"),
    ("our_grabs", "pass_uid", "TEXT"),
    # A "we asked and Plex had none" note, so an untagged sports programme is
    # not re-fetched every hour for as long as it stays in the guide.
    ("programs", "teams_tried_at", "INTEGER"),
    # The grace clock on a miss. Without these the notice fires the instant the
    # guide covers a date, which is before Plex has tagged the programme, so
    # every game warned once on its way in.
    ("expectations", "covered_at", "INTEGER"),
    ("expectations", "covered_epg_at", "INTEGER"),
    # What Plex's own guide looked like at the moment of this sync: when Plex
    # last refreshed it, and how far ahead it reached. Two numbers per sync is
    # all it takes to see a guide stop moving, which is otherwise invisible
    # until a recording you expected never happens.
    ("sync_log", "epg_refreshed_at", "INTEGER"),
    ("sync_log", "guide_ends_at", "INTEGER"),
    # Destinations gained a uid for the same reason passes have one: an export
    # taken here has to name the same row when it is restored somewhere else.
    ("destinations", "uid", "TEXT"),
    # A STAND-IN booking: a generic "NFL Football" slot booked because the
    # league's schedule puts this game there before the guide names it. The
    # expectation carries the matchup, which is what the schedule shows in
    # place of the guide's generic title. See `expectations.stand_in`.
    ("our_grabs", "expectation_id", "INTEGER"),
    # The programme's description, kept like its name: the guide forgets it.
    ("recordings", "summary", "TEXT"),
    # The resume position Plex last took. CE keeps playing a recording after
    # the handover, and each change is sent to Plex when the player stops.
    ("recordings", "plex_position", "REAL"),
    ("recordings", "ads_final", "INTEGER DEFAULT 0"),
    ("recordings", "ads_fails", "INTEGER DEFAULT 0"),
    ("recordings", "ads_tried_at", "INTEGER"),
    ("recordings", "ot_event", "TEXT"),
    ("recordings", "ot_state", "TEXT"),
    ("recordings", "ot_base", "INTEGER"),
    ("recordings", "ot_checked_at", "INTEGER"),
    ("recordings", "ot_note", "TEXT"),
]


def _migrate(conn):
    for table, column, decl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init() -> None:
    conn = connect()
    conn.executescript(SCHEMA)
    _migrate(conn)
    for k, v in DEFAULTS.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    _backfill_uids(conn)
    _strip_recurring_only_prefs(conn)
    _recorder_default_to_auto(conn)
    _reslot_airing_ids(conn)
    _timmyd_to_webhook(conn)
    # After the migration and the backfill, not inside SCHEMA: on an existing
    # install the column does not exist until _migrate has run.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS passes_uid ON passes(uid)")
    conn.commit()


def _reslot_airing_ids(conn):
    """Re-key existing bookings onto the slot-minted airing id.

    An airing id used to be minted from Plex's own `Media.id`, which a guide
    refresh changes for a broadcast that has not moved. It is now minted from
    the programme, the channel and the start time, so the same broadcast keeps
    the same id.

    Bookings made before that change hold the old form, and everything that
    reads `our_grabs.airing_id` would miss them: the guide's "Being recorded"
    filter, cancelling by hand, and the panel a schedule row opens. The row
    already carries the three parts, so the new id is computed rather than
    looked up, and a booking whose broadcast is long gone is re-keyed just as
    correctly as a live one.

    `UPDATE OR REPLACE` because the id is the primary key: two rows for one
    broadcast can only be duplicates of each other, and keeping one is right.

    `notify_state` goes first, and it has to. Its whole rule is that a row
    exists means this destination has been told, and a `pass_booked` row is
    keyed on the airing id. Re-key the booking without re-keying its state and
    the row no longer matches, so the next dispatch reads a booking nobody has
    heard about and announces it again. That happened on a live install: an
    upgrade re-keyed two bookings and both were announced a second time, hours
    after they were made, to a channel that had already been told.

    Only `pass_booked` is keyed on an airing. A recording started or finished is
    keyed on Plex's own grab id, a failed sync on the sync_log id, and a fault
    on its own code, none of which this touches.

    `pass_actions` is deliberately left alone. It is history, and the schedule
    already resolves a failure's airing against the guide as it stands rather
    than trusting the id it recorded.
    """
    slot = "program_guid || '#' || channel_vcn || '@' || begins_at"
    usable = ("program_guid IS NOT NULL AND channel_vcn IS NOT NULL "
              "AND begins_at IS NOT NULL")
    conn.execute(f"""
        UPDATE OR REPLACE notify_state
           SET key = (SELECT {slot} FROM our_grabs o
                       WHERE o.airing_id = notify_state.key AND {usable})
         WHERE event = 'pass_booked'
           AND EXISTS (SELECT 1 FROM our_grabs o
                        WHERE o.airing_id = notify_state.key AND {usable}
                          AND o.airing_id <> {slot})""")
    conn.execute(f"""UPDATE OR REPLACE our_grabs SET airing_id = {slot}
                      WHERE {usable} AND airing_id <> {slot}""")


# Settings only a recurring Plex rule can honour. A pass always books the
# one-shot template, which does not offer them, and Plex refuses the whole
# booking rather than ignoring one it does not know.
RECURRING_ONLY_PREFS = ("onlyNewAirings",
                        "autoDeletionItemPolicyUnwatchedLibrary",
                        "autoDeletionItemPolicyWatchedLibrary")


def _recorder_default_to_auto(conn):
    """Once: an install seeded with 'plex' as the default moves to 'auto'.

    1.5.5 wrote 'plex' into every install as the seed value, so a stored
    'plex' there is not a choice anybody made. From here on the default is
    CouchElephant wherever its recorder is on. Done once and marked, so a
    person who then picks Plex keeps Plex.
    """
    if conn.execute("SELECT 1 FROM settings WHERE key = 'recorder_default_auto'").fetchone():
        return
    conn.execute("UPDATE settings SET value = 'auto' "
                 "WHERE key = 'recorder_default' AND value = 'plex'")
    conn.execute("INSERT OR IGNORE INTO settings (key, value) "
                 "VALUES ('recorder_default_auto', '1')")


def _strip_recurring_only_prefs(conn):
    """Clean settings a pass should never have kept.

    A pass made before the settings panel hid these stored them, and every
    booking it then made was refused by Plex with a bare 400. Bookings survive
    that now, because `passes.offered_prefs` filters at the point of sending,
    but the stored value is still a lie about what the pass does, so it goes.
    """
    for r in conn.execute(
            "SELECT id, prefs FROM passes WHERE prefs IS NOT NULL").fetchall():
        try:
            cfg = json.loads(r["prefs"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(cfg, dict):
            continue
        kept = {k: v for k, v in cfg.items() if k not in RECURRING_ONLY_PREFS}
        if len(kept) != len(cfg):
            conn.execute("UPDATE passes SET prefs = ? WHERE id = ?",
                         (json.dumps(kept), r["id"]))


def _timmyd_to_webhook(conn):
    """Once: the old relay destination becomes a generic webhook.

    The relay kind stored a base address and always posted to `<base>/notify`.
    The webhook kind stores the whole URL, so the old address gets `/notify`
    added and the token stays. Nothing matches `timmyd` afterwards, so a second
    run does nothing.
    """
    for r in conn.execute(
            "SELECT id, webhook FROM destinations WHERE kind = 'timmyd'").fetchall():
        base = (r["webhook"] or "").strip().rstrip("/")
        conn.execute("UPDATE destinations SET kind = 'webhook', webhook = ? "
                     "WHERE id = ?", (base + "/notify" if base else None, r["id"]))


def _backfill_uids(conn):
    """Give every existing pass a uid, and every grab the uid of its pass.

    A pass made before this column existed has none, so anything that names a
    pass across machines would silently skip it.
    """
    rows = conn.execute("SELECT id FROM passes WHERE uid IS NULL OR uid = ''").fetchall()
    for r in rows:
        conn.execute("UPDATE passes SET uid = ? WHERE id = ?",
                     (uuid.uuid4().hex, r["id"]))
    conn.execute(
        "UPDATE our_grabs SET pass_uid = (SELECT uid FROM passes WHERE passes.id = "
        "our_grabs.pass_id) WHERE pass_uid IS NULL AND pass_id IS NOT NULL")
    for r in conn.execute(
            "SELECT id FROM destinations WHERE uid IS NULL OR uid = ''").fetchall():
        conn.execute("UPDATE destinations SET uid = ? WHERE id = ?",
                     (uuid.uuid4().hex, r["id"]))


def get_setting(key: str, default: str | None = None) -> str | None:
    row = connect().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else (default if default is not None else DEFAULTS.get(key))


def all_settings() -> dict[str, str]:
    rows = connect().execute("SELECT key, value FROM settings").fetchall()
    out = dict(DEFAULTS)
    out.update({r["key"]: r["value"] for r in rows})
    return out


def set_setting(key: str, value) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def query(sql: str, params=()) -> list[sqlite3.Row]:
    return connect().execute(sql, params).fetchall()


def one(sql: str, params=()) -> sqlite3.Row | None:
    return connect().execute(sql, params).fetchone()


def js(value) -> str:
    return json.dumps(value, separators=(",", ":"))


def unjs(value, default=None):
    try:
        return json.loads(value) if value else (default if default is not None else [])
    except Exception:
        return default if default is not None else []
