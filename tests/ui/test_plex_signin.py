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
