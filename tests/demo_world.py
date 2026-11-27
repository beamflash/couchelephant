"""The invented world the Apple tests and the public demo both run in.

Both run the app with everything it talks to replaced: the invented guide
(demo_guide) on a fake Plex, a fake HDHomeRun that sends a moving test pattern,
and recordings that were never made from anybody's television. The pieces that
both need live here, so the two entry points cannot drift apart:

  scripts/apple_test_server.py   the clicking tests' server (sign-in off)
  scripts/demo_server.py         the public demo for the App Store reviewer

Nothing here picks where files go. The caller sets the COUCHELEPHANT_* paths
before this is imported, as the test suite does.
"""
import hashlib
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app import adskip, auth, db, ratelimit, recorder
from tests import demo_guide, fake_plex, fake_tuner


def longest_on_now():
    """What is on now and runs on longest, so the recording made of it lasts
    through a whole test run."""
    now = int(time.time())
    return db.one("""SELECT a.*, p.title, p.grandparent_title, p.rating_key, p.teams
                     FROM airings a JOIN programs p ON p.guid = a.program_guid
                     WHERE a.begins_at <= ? AND a.ends_at > ? AND a.drm = 0
                     ORDER BY a.ends_at DESC LIMIT 1""", (now, now))


def add_news():
    """A half hour of news at the end of two channels' day. The demo's own news
    slot is long over by the time a test runs, and the News tab needs some."""
    with db.tx() as c:
        c.execute("""INSERT OR REPLACE INTO programs (guid, title, grandparent_title, summary,
                         type, section, genres, teams, year)
                     VALUES ('plex://episode/demo-news-late', 'Late News', 'Local News',
                             'The day in review.', 'episode', 'shows', '["News"]', '[]', 2026)""")
        for vcn in ("9.1", "41.1"):
            last = db.one("SELECT MAX(ends_at) e FROM airings WHERE channel_vcn = ?", (vcn,))["e"]
            call, title = demo_guide.CHANNELS[vcn]
            c.execute("""INSERT OR REPLACE INTO airings (id, program_guid, channel_vcn,
                             channel_call_sign, channel_title, begins_at, ends_at, drm)
                         VALUES (?, 'plex://episode/demo-news-late', ?, ?, ?, ?, ?, 0)""",
                      (f"demo-late-news-{vcn}", vcn, call, title, last, last + 1800))


def ticking(stop=None, each=None):
    """The recorder's own loop, without the ones that reach other servers.
    `each` is called before every tick (the demo's limits)."""
    while not (stop and stop.is_set()):
        try:
            if each:
                each()
            recorder.tick()
        except Exception as e:  # noqa: BLE001
            print("recorder:", e, flush=True)
        time.sleep(3)


UNDER_WAY_SOURCE = "demo-under-way"     # the demo's own rolling recording
UNDER_WAY_CHANNEL = "19.1"                # shows and films, never a game
UNDER_WAY_MAX_BYTES = 2 * 1024 ** 3       # what the demo's own may hold on disk
UNDER_WAY_GRACE = 2 * 3600                # a finished one is kept at least this long


def keep_one_under_way(in_use, now=None):
    """While someone is using the demo, something is always being recorded, so
    Home's Live now has a recording to open and watch while it records.

    It is the demo's own: one channel, whatever is on it, booked again as each
    programme ends. When nobody has used the demo for a while the one under way
    is ended early, as if its programme had ended, so an idle server records
    nothing and the small machine gets its CPU back. What it made stays (someone
    may be watching it, or a season pass may count it as its own) until the
    nightly reset, or until they pass a ceiling of their own (_trim_own). A
    visitor's limits never count it (app/demo.py)."""
    now = now or int(time.time())
    if not in_use:
        # Waiting for a tuner or recording, it ends now, as if its programme
        # had ended; the recorder finishes it on its next tick.
        with db.tx() as c:
            c.execute("UPDATE recordings SET stops_at = ? WHERE source = ? "
                      "AND state IN ('scheduled', 'recording') AND stops_at > ?",
                      (now, UNDER_WAY_SOURCE, now))
        return None
    _trim_own(now)
    if db.one("SELECT 1 FROM recordings WHERE source = ? AND state IN ('scheduled', 'recording') "
              "AND stops_at > ?", (UNDER_WAY_SOURCE, now)):
        return None
    airing = db.one("""SELECT a.*, p.title, p.grandparent_title, p.rating_key, p.teams
                       FROM airings a JOIN programs p ON p.guid = a.program_guid
                       WHERE a.channel_vcn = ? AND a.begins_at <= ? AND a.ends_at > ? + 60
                       AND a.drm = 0 ORDER BY a.begins_at DESC LIMIT 1""",
                    (UNDER_WAY_CHANNEL, now, now))
    if not airing:
        return None
    held = recorder.booked(airing)
    if (held and held["source"] == UNDER_WAY_SOURCE
            and held["state"] not in ("scheduled", "recording")
            and not held["watched_at"] and not held["position"]):
        # Ours, cut short when the demo went idle, nobody ever watched it, and
        # someone is back while the programme is still on: record it again
        # from here. One that was watched stays, and Live now waits for the
        # next programme.
        recorder.cancel(held["id"])
    elif held:
        return None
    return recorder.book(airing, source=UNDER_WAY_SOURCE, lead_minutes=0, trail_minutes=0)


def _trim_own(now):
    """The demo's own recordings have a ceiling of their own: an app left open
    keeps the demo in use for days, and the nightly reset waits for it. Past
    UNDER_WAY_MAX_BYTES the oldest finished one goes, if it ended at least
    UNDER_WAY_GRACE ago, so nothing is deleted under someone who just watched it."""
    from app import demo
    rows = db.query("SELECT id, uid, state, COALESCE(ended_at, stops_at) ended FROM recordings "
                    "WHERE source = ? ORDER BY id", (UNDER_WAY_SOURCE,))
    total = sum(demo._folder_bytes(recorder.rec_dir(r["uid"])) for r in rows)
    for r in rows:
        if total <= UNDER_WAY_MAX_BYTES:
            break
        if r["state"] in ("scheduled", "recording") or (r["ended"] or 0) > now - UNDER_WAY_GRACE:
            continue
        size = demo._folder_bytes(recorder.rec_dir(r["uid"]))
        recorder.cancel(r["id"])
        total -= size


def seed_pass(title, n=3, lost=0, section="tv", after=7200):
    """A season pass with n bookings of ours ahead, from airings later in the
    demo week. With lost=1 the pass is deleted straight away, which leaves the
    bookings behind as the owner found them."""
    now = int(time.time())
    rows = db.query(
        """SELECT a.*, p.title, p.grandparent_title FROM airings a
           JOIN programs p ON p.guid = a.program_guid
           WHERE a.begins_at > ? AND a.drm = 0 AND a.channel_vcn = ?
           ORDER BY a.begins_at""", (now + after, next(iter(demo_guide.CHANNELS))))
    uid = f"seed-{title}-{now}"
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, series_title, series_guid, uid, recorder, "
                  "enabled, created_at) VALUES ('series', ?, ?, ?, 'couchelephant', 1, ?)",
                  (title, title, uid, now))
        pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    made = []
    for row in rows:
        if len(made) >= n:
            break
        if recorder.booked(row):
            continue
        rid = recorder.book(row, "pass", pass_id=pid, pass_uid=uid)
        with db.tx() as c:
            c.execute("UPDATE recordings SET show_title = ?, section = ? WHERE id = ?",
                      (title, section, rid))
        made.append(rid)
    if lost:
        with db.tx() as c:
            c.execute("DELETE FROM passes WHERE id = ?", (pid,))
    return {"pass_id": pid, "booked": len(made)}


# ---------- the fakes ----------

def start_fakes(tuners=None):
    """Start the fake Plex and the fake tuner on localhost. Returns their
    addresses. `tuners` says how many tuners the box has, which is also the
    most streams that can run at once."""
    plex_url, _ = fake_plex.start()
    fake_tuner.LINEUP[:] = [(vcn, call) for vcn, (call, _) in demo_guide.CHANNELS.items()]
    tuner_url, _ = fake_tuner.start()
    if tuners:
        fake_tuner.STATE.total = tuners
    return plex_url, tuner_url


def reanchor(anchor=None):
    """Lay the invented guide out again from the top of this hour. The fake
    Plex reads its lists at every request, so swapping them is enough. A
    long-running demo does this at every reset, so the guide never runs out."""
    anchor = anchor or int(time.time()) // 3600 * 3600
    fake_plex.LIVE_AT = anchor
    (fake_plex.SPORTS_ITEMS, fake_plex.SHOW_ITEMS,
     fake_plex.MOVIE_ITEMS, fake_plex.TEAM_TAGS) = demo_guide.build(anchor, fake_plex._media)
    return anchor


# ---------- our own channel logos ----------

def make_logos():
    """A logo for every channel, drawn here: a coloured plate with the channel
    number. No network's mark is used anywhere. The files go where the sync
    keeps logos, and the rows say they are fresh so nothing tries to fetch."""
    from PIL import Image, ImageDraw, ImageFont
    logos = db.query("SELECT vcn, thumb_url FROM channels")
    os.makedirs(os.environ["COUCHELEPHANT_LOGOS"], exist_ok=True)
    big = ImageFont.load_default(size=84)
    small = ImageFont.load_default(size=30)
    for row in logos:
        vcn = row["vcn"]
        hue = (sum(map(ord, vcn)) * 47) % 360
        plate = Image.new("HSV", (1, 1), (hue * 255 // 360, 190, 200)).convert("RGB").getpixel((0, 0))
        img = Image.new("RGBA", (400, 225), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((8, 8, 392, 217), radius=36, fill=plate)
        d.text((200, 100), vcn.split(".")[0], font=big, fill="white", anchor="mm")
        d.text((200, 177), "DEMO TV", font=small, fill="white", anchor="mm")
        path = os.path.join(os.environ["COUCHELEPHANT_LOGOS"], f"{vcn}.png")
        img.save(path)
        with db.tx() as c:
            c.execute("UPDATE channels SET logo_path = ?, logo_source = thumb_url, "
                      "logo_fetched_at = ?, logo_attempts = 0 WHERE vcn = ?",
                      (path, int(time.time()), vcn))


# ---------- finished recordings ----------

# key, show, title, kind, channel, minutes, where it was left (seconds),
# ad breaks (seconds), hue, season, episode, section
RECORDINGS = [
    ("long-ad", "Lanternfall", "The Long Way Home", "episode", "9.1", 20, 55,
     [(70, 130), (400, 460), (800, 860)], 0, 1, 4, "shows"),
    ("resume", "Bend in the Willow", "Bend in the Willow", "movie", "62.1", 12, 300,
     [], 60, None, None, "movies"),
    ("kitchen", "Pepperkettle", "A Study in Salt", "episode", "5.1", 6, 0,
     [], 120, 2, 1, "shows"),
    ("auditions", "Brightside Auditions", "Round One", "episode", "41.1", 8, 0,
     [(120, 180)], 180, 1, 1, "shows"),
    ("game", "Prairie League Football", "Gulls at Otters", "episode", "4.1", 8, 0,
     [(150, 210)], 240, None, None, "sports"),
    ("ice", "Deep Lantern", "Under the Ice", "episode", "19.1", 5, 0,
     [], 300, 1, 2, "shows"),
]


def media_scale():
    """1 in the real demo; the unit test shrinks every recording with it."""
    return float(os.environ.get("DEMO_MEDIA_SCALE", "1"))


def _uid(key):
    return hashlib.md5(f"couchelephant-demo-{key}".encode()).hexdigest()


def _make_one(media_dir, spec):
    key, minutes, hue = spec[0], spec[5], spec[8]
    out = os.path.join(media_dir, key)
    if os.path.exists(os.path.join(out, "index.m3u8")):
        return
    tmp = out + ".part"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    seconds = max(8, int(minutes * 60 * media_scale()))
    # A small picture and a low bitrate: the demo's own disk and the reviewer's
    # connection are both modest. Keyframes every segment, as the recorder makes.
    subprocess.run(
        [recorder.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24",
         "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000",
         "-t", str(seconds), "-vf", f"hue=h={hue}", "-af", "volume=0.15",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p",
         "-force_key_frames", f"expr:gte(t,n_forced*{recorder.SEGMENT_SECONDS})",
         "-c:a", "aac", "-ac", "2", "-b:a", "64k",
         "-f", "hls", "-hls_time", str(recorder.SEGMENT_SECONDS),
         "-hls_playlist_type", "vod", "-hls_flags", "independent_segments",
         "-hls_segment_filename", os.path.join(tmp, "seg%05d.ts"),
         os.path.join(tmp, "index.m3u8")], check=True)
    shutil.rmtree(out, ignore_errors=True)
    os.rename(tmp, out)


def ensure_media(media_dir):
    """Make the viewing copies once. They are plain HLS, kept apart from the
    recordings folder so a reset only has to put them back."""
    os.makedirs(media_dir, exist_ok=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda s: _make_one(media_dir, s), RECORDINGS))


def _link_tree(src, dst):
    """Hard links where the disk allows, copies where it does not."""
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        try:
            os.link(os.path.join(src, name), os.path.join(dst, name))
        except OSError:
            shutil.copy2(os.path.join(src, name), os.path.join(dst, name))


def seed_recordings(media_dir):
    """The finished recordings, with their ad breaks and where each was left."""
    scale = media_scale()
    now = int(time.time())
    for i, (key, show, title, ptype, vcn, minutes, pos, breaks, _h, season, ep,
            section) in enumerate(RECORDINGS):
        uid = _uid(key)
        _link_tree(os.path.join(media_dir, key), recorder.view_dir(uid))
        seconds = max(8, int(minutes * 60 * scale))
        began = now - 86400 * (i + 1) - 3600
        pic = demo_guide.art_for(show or title)
        with db.tx() as c:
            cur = c.execute(
                """INSERT INTO recordings (uid, kind, state, title, show_title, ptype,
                       season, episode, section, channel_vcn, channel_title, begins_at,
                       ends_at, started_at, ended_at, source, position, ads_final,
                       created_at, updated_at, art, thumb)
                   VALUES (?, 'recording', 'finished', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           'manual', ?, 1, ?, ?, ?, ?)""",
                (uid, title, show, ptype, season, ep, section, vcn,
                 demo_guide.CHANNELS[vcn][1], began, began + seconds, began,
                 began + seconds, pos * scale, began, began, pic.get("art"), pic.get("thumb")))
            for a, b in breaks:
                c.execute("INSERT INTO ad_breaks (recording_id, start_s, end_s, final) "
                          "VALUES (?, ?, ?, 1)", (cur.lastrowid, a * scale, b * scale))


def seed_schedule():
    """Things set to record: one-off bookings well ahead, and a season pass.
    They are the recorder's own bookings, so the Scheduled list, the Calendar
    and the passes screens all have something in them."""
    now = int(time.time())
    for hours in (14, 26):
        row = db.one("""SELECT a.*, p.title, p.grandparent_title, p.rating_key, p.teams
                        FROM airings a JOIN programs p ON p.guid = a.program_guid
                        WHERE a.begins_at > ? AND a.drm = 0 AND p.section = 'shows'
                        AND p.type = 'episode' AND a.channel_vcn IN ('5.1', '9.1')
                        ORDER BY a.begins_at LIMIT 1""", (now + hours * 3600,))
        if row:
            recorder.book(row)
    # Several nights of one show. They start well after the reviewer is likely
    # to be looking, so no tuner is held while they sit through the guide.
    seed_pass("Marrowbrook", n=3, after=12 * 3600)


# ---------- reset ----------

def _tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]


# What a reset keeps: the settings are rewritten rather than emptied (a moment
# with none would read as "no sign-in"), and a reviewer who has been let in
# stays let in. Only content is reset.
KEEP_TABLES = ("settings", "tv_devices")
# The settings a visitor can change from the apps or the web page.
VISITOR_SETTINGS = ("report_consent",)


def wipe():
    """Back to nothing but who is signed in: every running stream stopped, every
    row of content and every recording gone. Accounts, sessions and the devices
    that were let in stay. The guide comes back with the next sync, the rest
    with the seeds."""
    recorder.shutdown()
    adskip.shutdown()
    # The connection is this thread's own and stays open.
    conn = db.connect()
    conn.execute("PRAGMA foreign_keys=OFF")
    for table in _tables(conn):
        if table not in KEEP_TABLES:
            conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM tv_devices WHERE approved_at IS NULL")
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    auth._con().execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))
    auth._con().commit()
    rec = recorder.REC_DIR
    if os.path.isdir(rec):
        for name in os.listdir(rec):
            shutil.rmtree(os.path.join(rec, name), ignore_errors=True)
    ratelimit.reset()
    recorder._live_paused.clear()


_reset_lock = threading.Lock()


def reset(settings, media_dir, review_user=None, review_password=None):
    """The demo as it was on its first day, for its content. `settings` is what
    the demo sets (the fake servers' addresses and the like). One reset at a
    time. While it runs the demo answers 503 to everything but /healthz."""
    from app import demo
    from app import sync as guide_sync
    with _reset_lock:
        demo.RESETTING.set()
        try:
            wipe()
            anchor = reanchor()
            # Settings are never emptied, and only what a visitor can set is
            # dropped: install_id (what the apps know the server by) and the
            # rest stay as they are. What the demo writes is written again below.
            with db.tx() as c:
                keep = ",".join("?" * len(VISITOR_SETTINGS))
                c.execute(f"DELETE FROM settings WHERE key IN ({keep})", VISITOR_SETTINGS)
            for k, v in settings.items():
                db.set_setting(k, v)
            ok, detail = guide_sync.full_sync()
            if not ok:
                raise RuntimeError(f"the demo guide would not sync: {detail}")
            make_logos()
            ensure_media(media_dir)
            seed_recordings(media_dir)
            seed_schedule()
            # Everything up to here is the demo's own; a visitor's comes after.
            db.set_setting(demo.SEED_KEY, db.one("SELECT COALESCE(MAX(id), 0) m FROM recordings")["m"])
            if review_user:
                if auth.user_exists(review_user):
                    uid = auth._con().execute(
                        "SELECT id FROM users WHERE username=? COLLATE NOCASE",
                        (review_user,)).fetchone()[0]
                    auth.set_password(uid, review_password)
                else:
                    auth.create_user(review_user, review_password, role="user")
            return anchor
        finally:
            demo.RESETTING.clear()
