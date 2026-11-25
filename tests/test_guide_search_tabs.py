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
    """The table comes from the schema and the migration list alike."""
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
