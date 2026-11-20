"""Sign in with Plex on the welcome page, through the browser."""
import pytest

from app import plextv
from tests import fake_plextv as fake


@pytest.fixture(scope="module")
def plextv_fake():
    fake.STATE.reset()
    url, stop = fake.start()
    old = plextv.BASE
    plextv.BASE = url
    yield fake.STATE
    plextv.BASE = old
    stop()


@pytest.fixture
def fresh(page, plextv_fake, plex_url):
    from app import db
    plextv_fake.reset()
    db.set_setting("plex_url", "")
    db.set_setting("plex_token", "")
    plextv_fake.load_resources([fake.server("Den", "cid-1", [(plex_url, True, False)])])
    page.goto("/welcome")
    page.wait_for_selector("#wsignin")
    return page


def test_the_welcome_page_offers_plex_first(fresh):
    assert fresh.get_by_role("button", name="Sign in with Plex").count() == 1
    assert fresh.get_by_text("Enter it yourself").count() == 1
    assert not fresh.locator("#wform").is_visible()
    fresh.get_by_text("Enter it yourself").click()
    assert fresh.locator("#wform").is_visible()


def test_signing_in_shows_a_code_and_lands_on_the_guide(fresh, plextv_fake):
    from app import db
    fresh.get_by_role("button", name="Sign in with Plex").click()
    fresh.wait_for_selector(".psi-code")
    assert fresh.locator(".psi-code").inner_text() == "ABCD"
    assert fresh.locator('a[href="https://plex.tv/link"]').count() == 1
    plextv_fake.approve(next(iter(plextv_fake.pins)))
    fresh.wait_for_url(lambda u: "/welcome" not in u, timeout=30000)
    assert fresh.locator("#ptnav").count() == 1
    assert db.get_setting("plex_token") == "srv-token-abc"


# ---- Settings, Plex ----

@pytest.fixture
def settings(page, plextv_fake, plex_url):
    plextv_fake.reset()
    page.goto("/settings")
    page.wait_for_selector("#settingsplexsignin .psi-go")
    return page


def _start(page, plextv_fake):
    page.get_by_role("button", name="Sign in with Plex").click()
    page.wait_for_selector(".psi-code")
    plextv_fake.approve(next(iter(plextv_fake.pins)))


def test_settings_sign_in_with_two_servers_picks_one_by_name(settings, plextv_fake, plex_url):
    from app import db
    plextv_fake.load_resources([
        fake.server("Den", "cid-1", [("http://127.0.0.1:1", True, False)], token="tok-den"),
        fake.server("Attic", "cid-2", [(plex_url, True, False)], token="tok-attic")])
    _start(settings, plextv_fake)
    settings.get_by_role("button", name="Attic").wait_for(timeout=15000)
    settings.get_by_role("button", name="Attic").click()
    settings.wait_for_selector(".psi-out .verdict.ok", timeout=15000)
    assert db.get_setting("plex_token") == "tok-attic"
    assert db.get_setting("plex_url") == plex_url


def test_settings_sign_in_shows_the_addresses_it_tried(settings, plextv_fake):
    plextv_fake.load_resources([fake.server("Den", "cid-1", [
        ("http://127.0.0.1:1", True, False), ("http://127.0.0.1:2", False, False)])])
    _start(settings, plextv_fake)
    settings.wait_for_selector(".psi-out .verdict.bad", timeout=30000)
    text = settings.locator(".psi-out").inner_text()
    assert "Tried: http://127.0.0.1:1, http://127.0.0.1:2" in text
