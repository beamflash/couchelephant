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
