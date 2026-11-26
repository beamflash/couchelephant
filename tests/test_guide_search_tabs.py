"""Guide search by channel, favorite channels, and the guide tabs."""
import time

from app import db, filters


def _seed():
    now = int(time.time())
    with db.tx() as c:
        for vcn, call, title, net in [
                ("4.1", "WQBBDT", "4.1 WQBBDT (FOX)", "FOX"),
                ("14.1", "KQXXDT", "14.1 KQXXDT (CBS)", "CBS"),
                ("9.1", "WQDDDT", "9.1 WQDDDT (ABC)", "ABC")]:
            c.execute("INSERT INTO channels (vcn, call_sign, title, network) VALUES (?,?,?,?)",
                      (vcn, call, title, net))
        c.execute("INSERT INTO programs (guid, title, section, genres, teams, summary) "
                  "VALUES ('n1','Evening News','shows','[\"News\"]','[]','The day')")
        c.execute("INSERT INTO programs (guid, title, section, genres, teams, summary) "
                  "VALUES ('s1','Chiefs at Bills','sports','[\"Football\"]','[]','')")
        c.execute("INSERT INTO programs (guid, title, section, genres, teams, summary) "
                  "VALUES ('d1','Ridgemont','shows','[\"Drama\"]','[]','')")
        for aid, guid, vcn, call, title in [
                ("a1", "n1", "4.1", "WQBBDT", "4.1 WQBBDT (FOX)"),
                ("a2", "s1", "14.1", "KQXXDT", "14.1 KQXXDT (CBS)"),
                ("a3", "d1", "9.1", "WQDDDT", "9.1 WQDDDT (ABC)")]:
            c.execute("""INSERT INTO airings (id, program_guid, channel_vcn, channel_call_sign,
                                              channel_title, begins_at, ends_at)
                         VALUES (?,?,?,?,?,?,?)""",
                      (aid, guid, vcn, call, title, now - 600, now + 3000))
        c.execute("""INSERT INTO recordings (uid, kind, state, title, show_title, section,
                                             channel_vcn, begins_at, ends_at)
                     VALUES ('u1','recording','finished','Evening News','Local News','shows',
                             '4.1',?,?)""", (now - 90000, now - 88000))
        c.execute("""INSERT INTO recordings (uid, kind, state, title, show_title, section,
                                             channel_vcn, begins_at, ends_at)
                     VALUES ('u2','recording','finished','Other','Ridgemont','shows',
                             '9.1',?,?)""", (now - 90000, now - 88000))


def _ids(client, **q):
    d = client.get("/api/tv/search", params=q).json()
    return [a["id"] for a in d["airings"]], d


# ---------- search ----------

def test_search_finds_a_channel_by_network_callsign_and_number(client):
    _seed()
    assert _ids(client, q="fox")[0] == ["a1"]
    assert _ids(client, q="WQDD")[0] == ["a3"]
    # 4.1 is the channel, not the tail of 14.1
    assert _ids(client, q="4.1")[0] == ["a1"]
    assert _ids(client, q="14.1")[0] == ["a2"]


def test_web_search_finds_a_channel_too(client):
    _seed()
    page = client.get("/guide", params={"q": "cbs"}).text
    assert "Chiefs at Bills" in page and "Evening News" not in page


def test_search_still_finds_title_show_and_description(client):
    _seed()
    assert _ids(client, q="chiefs")[0] == ["a2"]
    assert _ids(client, q="the day")[0] == ["a1"]


def test_search_lists_matching_recordings_after_the_airings(client):
    _seed()
    ids, d = _ids(client, q="evening")
    assert ids == ["a1"]
    assert [r["title"] for r in d["recordings"]] == ["Local News"]


def test_web_search_shows_a_recordings_section(client):
    _seed()
    page = client.get("/guide", params={"q": "evening"}).text
    assert 'id="recresults"' in page and "Local News" in page
    assert 'id="recresults"' not in client.get("/guide", params={"q": "chiefs"}).text


def test_an_empty_search_finds_nothing(client):
    _seed()
    ids, d = _ids(client, q="  ")
    assert ids == [] and d["recordings"] == []


# ---------- favorites ----------

def test_favorites_add_list_remove(client):
    _seed()
    assert client.get("/api/tv/favorites").json()["channels"] == []
    assert client.post("/api/tv/favorites/add", data={"vcn": "4.1"}).json()["ok"]
    client.post("/api/tv/favorites/add", data={"vcn": "4.1"})   # twice is fine
    got = client.get("/api/tv/favorites").json()["channels"]
    assert [c["number"] for c in got] == ["4.1"]
    assert client.post("/api/tv/favorites/remove", data={"vcn": "4.1"}).json()["ok"]
    assert client.get("/api/tv/favorites").json()["channels"] == []


def test_a_channel_that_is_not_there_cannot_be_a_favorite(client):
    _seed()
    r = client.post("/api/tv/favorites/add", data={"vcn": "99.9"})
    assert r.status_code == 404


def test_the_favorite_token_filters_by_favorite_channel(client):
    _seed()
    client.post("/api/tv/favorites/add", data={"vcn": "9.1"})
    frags, args = filters.build(["favorite"], [])
    rows = db.query("SELECT a.id FROM airings a JOIN programs p ON p.guid = a.program_guid "
                    "WHERE 1=1 " + " ".join(frags), tuple(args))
    assert [r["id"] for r in rows] == ["a3"]


def test_favorites_survive_a_restart_of_the_schema(client):
    """The table comes from the schema, and a second init keeps its rows."""
    _seed()
    client.post("/api/tv/favorites/add", data={"vcn": "9.1"})
    db.init()
    assert [c["number"] for c in client.get("/api/tv/favorites").json()["channels"]] == ["9.1"]


# ---------- tabs ----------

def test_the_guide_for_a_television_takes_a_tab(client):
    _seed()
    def nums(tab):
        d = client.get("/api/tv/guide", params={"tab": tab}).json()
        return [c["channel"]["number"] for c in d["channels"]]
    assert sorted(nums("")) == ["14.1", "4.1", "9.1"]
    assert nums("news") == ["4.1"]
    assert nums("sports") == ["14.1"]
    assert nums("favorites") == []
    client.post("/api/tv/favorites/add", data={"vcn": "9.1"})
    assert nums("favorites") == ["9.1"]


def test_the_grid_takes_a_tab(client):
    _seed()
    client.post("/api/tv/favorites/add", data={"vcn": "9.1"})
    now = int(time.time())
    def grid(tab):
        d = client.get("/api/grid", params={"start": now - 900, "end": now + 3600,
                                            "tab": tab}).json()
        return [c["vcn"] for c in d["channels"]], [a["id"] for a in d["airings"]]
    assert grid("favorites") == (["9.1"], ["a3"])
    assert grid("news") == (["4.1"], ["a1"])
    assert grid("sports") == (["14.1"], ["a2"])
    assert len(grid("")[0]) == 3


def test_the_web_guide_has_the_four_tabs_and_an_empty_favorites_message(client):
    _seed()
    page = client.get("/guide").text
    for name in ("All", "Favorites", "News", "Sports"):
        assert f">{name}<" in page
    empty = client.get("/guide", params={"tab": "favorites"}).text
    assert "Star a channel" in empty
    client.post("/api/tv/favorites/add", data={"vcn": "9.1"})
    assert "Star a channel" not in client.get("/guide", params={"tab": "favorites"}).text


def test_the_web_search_respects_the_tab(client):
    _seed()
    page = client.get("/guide", params={"q": "e", "tab": "sports"}).text
    assert "Chiefs at Bills" in page and "<b>Evening News</b>" not in page


def test_the_web_grid_marks_favorites(client):
    _seed()
    client.post("/api/tv/favorites/add", data={"vcn": "4.1"})
    d = client.get("/api/grid", params={"start": 0, "end": 2**31 - 1}).json()
    stars = {c["vcn"]: c["favorite"] for c in d["channels"]}
    assert stars == {"4.1": True, "9.1": False, "14.1": False}


def test_favorites_travel_in_an_export(client):
    from app import portable
    _seed()
    client.post("/api/tv/favorites/add", data={"vcn": "9.1"})
    blob = portable.export_bytes()
    with db.tx() as c:
        c.execute("DELETE FROM favorite_channels")
    portable.import_bytes(blob, replace=True)
    assert [c["number"] for c in client.get("/api/tv/favorites").json()["channels"]] == ["9.1"]


def test_favorite_is_a_tab_and_not_a_facet(client):
    _seed()
    client.post("/api/tv/favorites/add", data={"vcn": "9.1"})
    kinds = [r["id"] for sec in client.get("/api/facets").json() for r in sec["rows"]]
    assert "favorite" not in kinds
    # the token still filters
    frags, args = filters.build(["favorite"], [])
    assert frags


def test_search_recordings_follow_the_tab(client):
    _seed()
    with db.tx() as c:
        c.execute("""INSERT INTO recordings (uid, kind, state, title, show_title, section,
                                             channel_vcn, begins_at, ends_at)
                     VALUES ('u3','recording','finished','Evening Game','Evening Game','sports',
                             '14.1',1000,2000)""")
    page = client.get("/guide", params={"q": "evening", "tab": "sports"}).text
    assert "Evening Game" in page and "Local News" not in page
    page = client.get("/guide", params={"q": "evening", "tab": "favorites"}).text
    assert 'id="recresults"' not in page
    client.post("/api/tv/favorites/add", data={"vcn": "4.1"})
    page = client.get("/guide", params={"q": "evening", "tab": "favorites"}).text
    assert "Local News" in page and "Evening Game" not in page


def test_a_recording_result_links_to_the_recording(client):
    _seed()
    rid = db.one("SELECT id FROM recordings WHERE uid = 'u1'")["id"]
    page = client.get("/guide", params={"q": "evening"}).text
    assert f'href="/recordings?rec={rid}"' in page


def _news_channels(now):
    """5.1 and 7.1 have news now, 2.1 only in four hours."""
    with db.tx() as c:
        for vcn in ("2.1", "5.1", "7.1"):
            c.execute("INSERT OR REPLACE INTO channels (vcn, call_sign, title) VALUES (?,?,?)",
                      (vcn, "C" + vcn, vcn + " C" + vcn))
        c.execute("INSERT OR REPLACE INTO programs (guid, title, section, genres, teams) "
                  "VALUES ('nn','Late News','shows','[\"News\"]','[]')")
        for vcn, b, e in [("5.1", now - 600, now + 1200), ("7.1", now - 600, now + 1200),
                          ("2.1", now + 4 * 3600, now + 5 * 3600)]:
            c.execute("""INSERT INTO airings (id, program_guid, channel_vcn, channel_call_sign,
                                              channel_title, begins_at, ends_at)
                         VALUES (?,?,?,?,?,?,?)""", ("n" + vcn, "nn", vcn, "C" + vcn, vcn, b, e))


def _grid(client, **q):
    d = client.get("/api/grid", params=q).json()
    return [c["vcn"] for c in d["channels"]], sorted(a["id"] for a in d["airings"]), d


def test_a_tab_picks_its_channels_once_for_the_whole_guide(client):
    """Whatever window is asked for, the channel set and its order are the
    same, so paging channels never skips or repeats one."""
    now = int(time.time())
    _news_channels(now)
    first = {"start": now - 900, "end": now + 3600, "tab": "news", "since": now - 900}
    all_vcns, _, _ = _grid(client, **first, chlimit=10)
    assert all_vcns == ["2.1", "5.1", "7.1"], "2.1 is in the set though it is on in four hours"
    a, _, d1 = _grid(client, **first, chlimit=2, choffset=0)
    b, _, d2 = _grid(client, **first, chlimit=2, choffset=2)
    assert a == ["2.1", "5.1"] and d1["more_channels"] is True
    assert b == ["7.1"] and d2["more_channels"] is False
    # the same pages over a later window give the same channels
    later = dict(first, start=now + 3600, end=now + 6 * 3600)
    a2, _, _ = _grid(client, **later, chlimit=2, choffset=0)
    assert a2 == a


def test_more_time_on_a_tab_returns_only_the_rows_already_on_screen(client):
    now = int(time.time())
    _news_channels(now)
    got, airings, _ = _grid(client, start=now + 3600, end=now + 6 * 3600, tab="news",
                            since=now - 900, vcns="5.1,7.1", chlimit=2)
    assert got == ["5.1", "7.1"], "2.1 is not on screen, so it is not sent"
    assert airings == [], "and nothing is on those two later"
    got, airings, _ = _grid(client, start=now + 3600, end=now + 6 * 3600, tab="news",
                            since=now - 900, vcns="2.1,5.1,7.1", chlimit=3)
    assert got == ["2.1", "5.1", "7.1"] and airings == ["n2.1"]


def test_search_and_the_apps_take_the_tab_too(client):
    _seed()
    d = client.get("/api/tv/search", params={"q": "e", "tab": "sports"}).json()
    assert [a["title"] for a in d["airings"]] == ["Chiefs at Bills"]
    assert d["recordings"] == []
    d = client.get("/api/tv/search", params={"q": "evening", "tab": "news"}).json()
    assert d["airings"] and all(a["title"] == "Evening News" for a in d["airings"])


def test_the_tab_links_keep_the_day_and_the_filters(client):
    _seed()
    page = client.get("/guide", params={"day": "2026-11-27", "f": "hd", "x": "drm"}).text
    link = page.split('id="tab-news"')[0].rsplit("<a ", 1)[1]
    assert "day=2026-11-27" in link and "f=hd" in link and "x=drm" in link and "tab=news" in link


def test_search_matches_a_channel_name_through_the_channel_list_first(client):
    """The channel is found in the channel table, then the airings are asked
    for by number, so a channel with no airing is simply not listed."""
    _seed()
    ids, _ = _ids(client, q="abc")
    assert ids == ["a3"]


def test_a_form_post_decodes_an_escaped_plus_and_ampersand(client):
    """The apps escape every value, so "%2B" must arrive as "+" and "%26" as "&"."""
    with db.tx() as c:
        c.execute("INSERT INTO channels (vcn, call_sign, title) VALUES ('1+2&3', 'X', 'X')")
    r = client.post("/api/tv/favorites/add", content="vcn=1%2B2%263",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200, r.text
    assert [c["number"] for c in r.json()["channels"]] == ["1+2&3"]


def test_channels_sharing_a_number_page_in_a_fixed_order(client):
    """Two channels with the same number would swap places between pages
    without a tiebreaker."""
    now = int(time.time())
    with db.tx() as c:
        for vcn in ("5", "5.0", "5.00", "6"):
            c.execute("INSERT INTO channels (vcn, call_sign, title) VALUES (?,?,?)",
                      (vcn, "C" + vcn, vcn))
    pages = []
    for off in range(4):
        got, _, _ = _grid(client, start=now, end=now + 3600, chlimit=1, choffset=off)
        pages += got
    assert pages == ["5", "5.0", "5.00", "6"]


def test_the_grid_refuses_an_oversized_request(client):
    now = int(time.time())
    q = {"start": now, "end": now + 3600}
    assert client.get("/api/grid", params=dict(q, chlimit=501)).status_code == 400
    many = ",".join(str(n) for n in range(501))
    assert client.get("/api/grid", params=dict(q, vcns=many)).status_code == 400
    assert client.get("/api/grid", params=dict(q, vcns="1" * 21)).status_code == 400
    assert client.get("/api/grid", params=dict(q, chlimit=500)).status_code == 200
    assert client.get("/api/grid", params=dict(q, vcns="1" * 20)).status_code == 200


def test_a_tab_leaves_out_a_channel_whose_only_match_is_days_away(client):
    now = int(time.time())
    _news_channels(now)
    with db.tx() as c:
        c.execute("""INSERT INTO airings (id, program_guid, channel_vcn, channel_call_sign,
                                          channel_title, begins_at, ends_at)
                     VALUES ('far','nn','9.1','C','9.1',?,?)""",
                  (now + 3 * 86400, now + 3 * 86400 + 1800))
        c.execute("INSERT INTO channels (vcn, call_sign, title) VALUES ('9.1','C','9.1')")
    got, _, _ = _grid(client, start=now - 900, end=now + 3600, tab="news",
                      since=now - 900, chlimit=10)
    assert got == ["2.1", "5.1", "7.1"]


def test_the_tab_links_keep_the_channel(client):
    _seed()
    page = client.get("/guide", params={"channel": "4.1"}).text
    assert "tab=news&amp;channel=4.1" in page or "channel=4.1&amp;tab=news" in page \
        or "tab=news&channel=4.1" in page
