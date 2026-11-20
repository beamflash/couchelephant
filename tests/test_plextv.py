"""The plex.tv client, against a fake plex.tv."""
import logging

import pytest

from app import plextv
from app.version import VERSION
from tests import fake_plextv as fake


@pytest.fixture
def tv(monkeypatch):
    fake.STATE.reset()
    url, stop = fake.start()
    monkeypatch.setattr(plextv, "BASE", url)
    yield fake.STATE
    stop()


def test_create_pin_posts_weak_pin_with_headers(tv):
    from app import paths
    pin = plextv.create_pin()
    assert pin.code == "ABCD" and len(pin.code) == 4
    assert pin.id >= 1000 and pin.expires_at > 0
    method, path, query, headers = tv.requests[0]
    assert (method, path) == ("POST", "/api/v2/pins")
    assert query["strong"] == "false"
    assert headers["X-Plex-Product"] == "CouchElephant"
    assert headers["X-Plex-Client-Identifier"] == paths.install_id()
    assert headers["X-Plex-Version"] == VERSION
    assert headers["Accept"] == "application/json"


def test_check_pin_none_until_approved(tv):
    pin = plextv.create_pin()
    assert plextv.check_pin(pin.id) is None
    tv.approve(pin.id)
    assert plextv.check_pin(pin.id) == fake.TOKEN


def test_servers_filters_and_orders_connections(tv):
    tv.load_resources([
        fake.server("Den", "cid-1", [
            ("https://relay.plex.direct:443", False, True),
            ("https://remote.plex.direct:32400", False, False),
            ("http://192.168.1.5:32400", True, False)]),
        fake.server("Phone", "cid-2", [("http://1.2.3.4:1", True, False)],
                    provides="client,player"),
    ])
    got = plextv.servers(fake.TOKEN)
    assert [s.name for s in got] == ["Den"]
    s = got[0]
    assert s.client_id == "cid-1" and s.access_token == "srv-token-abc"
    assert [c.uri for c in s.connections] == [
        "http://192.168.1.5:32400",
        "https://remote.plex.direct:32400",
        "https://relay.plex.direct:443"]
    assert [(c.local, c.relay) for c in s.connections] == [
        (True, False), (False, False), (False, True)]
    _, _, query, headers = tv.requests[-1]
    assert headers["X-Plex-Token"] == fake.TOKEN
    assert query["includeHttps"] == "1" and query["includeRelay"] == "1"


def test_timeout_raises_page_ready_error(tv, monkeypatch):
    monkeypatch.setattr(plextv, "TIMEOUT", 0.3)
    tv.hang = 1
    with pytest.raises(plextv.PlexTvError) as e:
        plextv.create_pin()
    assert str(e.value) == "plex.tv did not answer."


def test_http_error_is_a_plextv_error_without_the_token(tv):
    tv.fail_status = 500
    with pytest.raises(plextv.PlexTvError) as e:
        plextv.servers(fake.TOKEN)
    assert fake.TOKEN not in str(e.value) and fake.TOKEN not in repr(e.value)


def test_token_never_logged(tv, caplog):
    caplog.set_level(logging.DEBUG)
    pin = plextv.create_pin()
    tv.approve(pin.id)
    plextv.check_pin(pin.id)
    tv.load_resources([fake.server("Den", "c", [("http://h:1", True, False)])])
    plextv.servers(fake.TOKEN)
    tv.fail_status = 500
    with pytest.raises(plextv.PlexTvError):
        plextv.servers(fake.TOKEN)
    assert fake.TOKEN not in caplog.text
    assert "srv-token-abc" not in caplog.text


def test_server_repr_hides_tokens(tv):
    tv.load_resources([fake.server("Den", "c", [("http://h:1", True, False)])])
    text = repr(plextv.servers(fake.TOKEN))
    assert fake.TOKEN not in text and "srv-token-abc" not in text


def test_wrong_shape_json_is_a_plextv_error(tv):
    pin = plextv.create_pin()
    tv.raw = ["not", "a", "dict"]
    with pytest.raises(plextv.PlexTvError):
        plextv.check_pin(pin.id)
    tv.raw = {"not": "a list"}
    with pytest.raises(plextv.PlexTvError):
        plextv.servers(fake.TOKEN)


def test_non_dict_resource_entries_are_skipped(tv):
    den = fake.server("Den", "c", [])
    den["connections"] = ["junk"]
    tv.load_resources(["junk", 3, den])
    assert [s.name for s in plextv.servers(fake.TOKEN)] == ["Den"]


def test_expired_pin_has_its_own_message(tv):
    pin = plextv.create_pin()
    tv.expire(pin.id)
    with pytest.raises(plextv.PlexTvError) as e:
        plextv.check_pin(pin.id)
    assert str(e.value) == "That code has expired. Get a new one."
    assert e.value.status == 404


def test_rejected_token_has_its_own_message(tv):
    with pytest.raises(plextv.PlexTvError) as e:
        plextv.servers("wrong-token")
    assert str(e.value) == "Plex did not accept the sign-in. Sign in again."
    assert e.value.status == 401
    assert "wrong-token" not in str(e.value)


def test_approve_unknown_pin_is_clear(tv):
    with pytest.raises(KeyError):
        tv.approve(999999)


# ---- final review ----

def test_connections_keep_address_port_and_protocol(tv):
    tv.load_resources([fake.server("Den", "c", [
        ("https://192-168-1-5.abc.plex.direct:32400", True, False, "192.168.1.5", 32400)])])
    c = plextv.servers(fake.TOKEN)[0].connections[0]
    assert (c.address, c.port, c.protocol) == ("192.168.1.5", 32400, "https")


def test_servers_know_whether_the_account_owns_them(tv):
    tv.load_resources([fake.server("Mine", "a", [("http://h:1", True, False)]),
                       fake.server("Friend", "b", [("http://h:2", True, False)], owned=False)])
    assert [(s.name, s.owned) for s in plextv.servers(fake.TOKEN)] == [
        ("Mine", True), ("Friend", False)]


def test_a_slowly_dribbled_answer_still_hits_the_total_deadline(tv, monkeypatch):
    import time
    monkeypatch.setattr(plextv, "TIMEOUT", 0.6)
    tv.drip = 4
    t0 = time.monotonic()
    with pytest.raises(plextv.PlexTvError) as e:
        plextv.create_pin()
    assert str(e.value) == "plex.tv did not answer."
    assert time.monotonic() - t0 < 1.5
