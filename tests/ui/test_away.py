"""Use away from home, from the Settings window."""
import pytest


@pytest.fixture
def signin_tab(page):
    page.goto("/settings")
    page.wait_for_selector('#setnav .nav-item[data-sec="accounts"]')
    page.click('.nav-item[data-sec="accounts"]')
    page.click('#settabs .tab[data-tab="signin"]')
    page.wait_for_selector("#awayon")
    return page


def test_the_switch_starts_off_and_asks_for_an_account_first(signin_tab):
    from app import db, exposure
    page = signin_tab
    assert not page.is_checked("#awayon")
    page.click("#awayon")
    page.wait_for_selector("#awaymsg:not([hidden])")
    assert "Make an account first" in page.inner_text("#awaymsg")
    assert not page.is_checked("#awayon")
    assert page.is_visible("#awayfix")
    assert not exposure.away_on()

    page.fill("#awayuser", "owner")
    page.fill("#awaypw", "a-good-password")
    page.click("#awayfixgo")
    page.wait_for_selector("#awaycheck:not([hidden])")
    assert page.is_checked("#awayon")
    assert db.get_setting("away_mode") == "1"
    text = page.inner_text("#awaycheck")
    assert "Five wrong passwords" in text
    assert ("Your router may send home devices through the internet address; "
            "they will be asked to sign in.") in text
    assert page.csp_violations == []


def test_the_switch_turns_off_again(signin_tab):
    from app import db
    page = signin_tab
    page.click("#awayon")
    page.fill("#awayuser", "owner")
    page.fill("#awaypw", "a-good-password")
    page.click("#awayfixgo")
    page.wait_for_selector("#awaycheck:not([hidden])")
    page.click("#awayon")
    page.wait_for_selector("#awaycheck", state="hidden")
    assert db.get_setting("away_mode") == "0"


def test_the_pages_work_under_the_security_policy(browser, base_url, synced):
    """Enforced here, not bypassed: a blocked script or style says so in the
    console, and any such message fails the test."""
    from tests.ui.conftest import _page
    ctx, page = _page(browser, base_url, viewport={"width": 1440, "height": 900},
                      bypass_csp=False)
    try:
        seen = []
        for path in ("/", "/guide", "/recordings", "/login", "/settings"):
            resp = page.goto(path)
            page.wait_for_load_state("networkidle")
            assert "default-src 'self'" in resp.headers["content-security-policy"]
            assert resp.headers.get("server") is None
        # Every settings section, and the pop-up window the gear opens.
        page.goto("/guide")
        page.wait_for_selector(".gprog", timeout=15000)
        page.click("#gearbtn")
        page.wait_for_selector("#setnav .nav-item")
        items = page.locator("#setnav .nav-item")
        for i in range(items.count()):
            items.nth(i).click()
            seen.append(i)
        assert len(seen) > 3
        assert page.csp_violations == []
    finally:
        ctx.close()
