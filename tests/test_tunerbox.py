"""The table of HDHomeRun boxes: the old settings moved in, typed rows kept."""
from app import db, tunerbox
from tests import fake_tuner


def test_old_typed_address_becomes_one_typed_row(tuner_url):
    db.set_setting("tuner_url", tuner_url)
    tunerbox.migrate_settings()
    tunerbox.migrate_settings()
    rows = tunerbox.rows()
    assert len(rows) == 1
    assert rows[0]["found_by"] == "typed"
    assert rows[0]["device_id"] == "FAKE0001"
    assert rows[0]["tuner_count"] == 4


def test_old_found_address_becomes_a_plex_row(tuner_url):
    db.set_setting("tuner_detected", tuner_url)
    tunerbox.migrate_settings()
    rows = tunerbox.rows()
    assert [r["found_by"] for r in rows] == ["plex"]


def test_an_offline_typed_address_keeps_a_row_without_a_device_id():
    db.set_setting("tuner_url", "http://192.0.2.1")
    tunerbox.migrate_settings()
    rows = tunerbox.rows()
    assert len(rows) == 1
    assert rows[0]["device_id"] is None
    assert rows[0]["error"]


def test_typed_addresses_are_normalised_once():
    first = tunerbox.add_typed("192.0.2.5:8080/")
    second = tunerbox.add_typed("http://192.0.2.5:8080")
    assert first == second
    rows = tunerbox.rows()
    assert len(rows) == 1
    assert rows[0]["address"] == "http://192.0.2.5:8080"


def test_normalise_gives_scheme_host_and_port_only():
    assert tunerbox.normalise(" 192.0.2.5 ") == "http://192.0.2.5"
    assert tunerbox.normalise("HTTP://192.0.2.5:80/lineup.json") == "http://192.0.2.5:80"


def test_discovery_never_changes_a_typed_row(tuner_url):
    typed = tunerbox.add_typed(tuner_url)
    body = {"DeviceID": "FAKE0001", "TunerCount": 4, "FriendlyName": "Renamed"}
    got = tunerbox.add_found("http://192.0.2.77", "network", body)
    assert got == typed
    rows = tunerbox.rows()
    assert len(rows) == 1
    assert rows[0]["address"] == tuner_url
    assert rows[0]["found_by"] == "typed"


def test_an_emulator_reusing_a_device_id_with_another_count_stays_separate():
    a = tunerbox.add_found("http://192.0.2.10", "plex",
                           {"DeviceID": "SAME", "TunerCount": 4})
    b = tunerbox.add_found("http://192.0.2.11", "network",
                           {"DeviceID": "SAME", "TunerCount": 2})
    assert a != b
    assert len(tunerbox.rows()) == 2


def test_the_same_box_found_twice_is_one_row():
    a = tunerbox.add_found("http://192.0.2.10", "plex",
                           {"DeviceID": "ABC", "TunerCount": 4})
    b = tunerbox.add_found("http://box.example", "network",
                           {"DeviceID": "ABC", "TunerCount": 4})
    assert a == b
    assert tunerbox.rows()[0]["address"] == "http://192.0.2.10"


def test_only_typed_rows_can_be_removed():
    found = tunerbox.add_found("http://192.0.2.10", "plex",
                               {"DeviceID": "ABC", "TunerCount": 4})
    typed = tunerbox.add_typed("http://192.0.2.20")
    assert tunerbox.remove(found) is False
    assert tunerbox.remove(typed) is True
    assert [r["id"] for r in tunerbox.rows()] == [found]


def test_a_box_can_be_turned_off_and_on():
    box = tunerbox.add_typed("http://192.0.2.20")
    tunerbox.set_enabled(box, False)
    assert tunerbox.rows(enabled_only=True) == []
    tunerbox.set_enabled(box, True)
    assert len(tunerbox.rows(enabled_only=True)) == 1


def test_typed_rows_come_first():
    a = tunerbox.add_found("http://192.0.2.10", "plex", {"DeviceID": "A", "TunerCount": 2})
    b = tunerbox.add_typed("http://192.0.2.20")
    assert [r["id"] for r in tunerbox.rows()] == [b, a]


def test_the_fake_box_answers_discover_json(tuner_url):
    import httpx
    d = httpx.get(tuner_url + "/discover.json").json()
    assert d["DeviceID"] == "FAKE0001" and d["TunerCount"] == 4
    assert fake_tuner.STATE.url == tuner_url


def _raw(address, found_by):
    with db.tx() as c:
        return c.execute("INSERT INTO tuner_boxes (address, found_by, enabled, added_at) "
                         "VALUES (?, ?, 1, 0)", (address, found_by)).lastrowid


INFO = {"DeviceID": "DUP00001", "TunerCount": 4, "FriendlyName": "Box"}


def test_two_rows_that_turn_out_to_be_one_box_become_one_and_the_typed_stays():
    found = _raw("http://192.0.2.7", "plex")
    typed = _raw("http://box.example:80", "typed")
    tunerbox._refresh(found, INFO)
    assert len(tunerbox._rows()) == 2
    tunerbox._refresh(typed, INFO)
    assert [r["id"] for r in tunerbox._rows()] == [typed]


def test_two_found_rows_of_one_box_keep_the_lower_id():
    one = _raw("http://192.0.2.8", "network")
    two = _raw("http://192.0.2.9", "plex")
    tunerbox._refresh(two, INFO)
    tunerbox._refresh(one, INFO)
    assert [r["id"] for r in tunerbox._rows()] == [one]


def test_a_box_with_a_different_tuner_count_is_not_merged():
    one = _raw("http://192.0.2.8", "network")
    two = _raw("http://192.0.2.9", "plex")
    tunerbox._refresh(one, INFO)
    tunerbox._refresh(two, dict(INFO, TunerCount=2))
    assert len(tunerbox._rows()) == 2


def test_two_quick_adds_of_one_address_make_one_row(monkeypatch):
    import threading
    import time

    def slow_probe(address):
        time.sleep(0.3)
        return None, "no answer"
    monkeypatch.setattr(tunerbox, "_probe", slow_probe)
    ids = []
    ts = [threading.Thread(target=lambda: ids.append(tunerbox.add_typed("192.0.2.30")))
          for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(tunerbox._rows()) == 1
    assert ids[0] == ids[1]


def test_a_removed_boxs_id_is_never_given_to_another():
    first = tunerbox.add_typed("192.0.2.2")
    assert tunerbox.remove(first)
    assert tunerbox.add_typed("192.0.2.3") > first


def test_a_database_cut_off_mid_rebuild_heals_on_init():
    # The old code renamed the table and died before copying: the rows sit in
    # tuner_boxes_old and a fresh empty tuner_boxes was made beside them.
    conn = db.connect()
    conn.execute("INSERT INTO tuner_boxes (id, address, found_by) "
                 "VALUES (7, 'http://192.0.2.7', 'typed')")
    conn.execute("ALTER TABLE tuner_boxes RENAME TO tuner_boxes_old")
    conn.executescript(db.SCHEMA)
    conn.commit()
    db.init()
    assert [r["id"] for r in tunerbox.rows()] == [7]
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'tuner_boxes_old'").fetchone()
