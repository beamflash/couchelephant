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
        for path in ("/", "/guide", "/home", "/recordings", "/login", "/settings"):
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


def test_the_player_and_its_guide_work_under_the_security_policy(browser, base_url, synced):
    """The live player, opened from Home, with the guide framed over it."""
    import json

    from tests.ui.conftest import _page
    from tests.ui.test_player_guide import LIVE
    ctx, page = _page(browser, base_url, viewport={"width": 1440, "height": 900},
                      bypass_csp=False)
    try:
        page.route("**/api/tv/live/**", lambda r: r.fulfill(json={"ok": True}))
        page.goto("/home")
        page.wait_for_load_state("networkidle")
        page.evaluate("(p) => CE.Player.play(p, 'live')", json.loads(json.dumps(LIVE)))
        page.wait_for_selector(".tvplayer:not([hidden])")
        page.keyboard.press("ArrowDown")
        page.wait_for_selector("#tvpguidebox iframe")
        page.frame_locator("#tvpguidebox iframe").locator(".gprog").first.wait_for(
            timeout=15000)
        assert page.csp_violations == []
    finally:
        ctx.close()


def test_a_domain_for_a_real_certificate(signin_tab, monkeypatch):
    from app import acme_tls, db

    def cannot(name):
        raise acme_tls._Say("Let's Encrypt could not reach this server on port 443.")
    monkeypatch.setattr(acme_tls, "_order", cannot)
    page = signin_tab
    assert page.is_hidden("#awaydomainbox")
    page.click("#awayon")
    page.fill("#awayuser", "owner")
    page.fill("#awaypw", "a-good-password")
    page.click("#awayfixgo")
    page.wait_for_selector("#awaydomainbox", state="visible")

    page.fill("#awaydomain", "not a name")
    page.click("#awaydomaingo")
    page.wait_for_selector("#awaydomainsay:not([hidden])")
    assert "not a valid domain" in page.inner_text("#awaydomainsay")

    page.fill("#awaydomain", "tv.example.com")
    page.click("#awaydomaingo")
    page.wait_for_function(
        "document.querySelector('#awaydomainsay').textContent.indexOf('could not reach') > -1",
        timeout=15000)
    assert page.is_visible("#awaydomainretry")
    assert db.get_setting("away_domain") == "tv.example.com"
    assert "Traceback" not in page.inner_text("#awaydomainsay")
    page.wait_for_function(
        "document.querySelector('#awaycheck').textContent.indexOf('Encryption') > -1")
    assert "could not reach" in page.inner_text("#awaycheck")

    page.fill("#awaydomain", "")
    page.click("#awaydomaingo")
    page.wait_for_selector("#awaydomainsay", state="hidden")
    assert db.get_setting("away_domain") == ""
    assert page.csp_violations == []


def test_trusted_proxies_are_saved_and_wide_ones_refused(signin_tab):
    from app import db
    page = signin_tab
    page.fill("#awayproxies", "0.0.0.0/0")
    page.click("#awayproxiesgo")
    page.wait_for_selector("#awayproxiessay:not([hidden])")
    assert "too wide" in page.inner_text("#awayproxiessay").lower()
    assert not db.get_setting("trusted_proxies")

    page.fill("#awayproxies", "192.0.2.10, 10.0.0.0/8")
    page.click("#awayproxiesgo")
    page.wait_for_function(
        "document.querySelector('#awayproxiessay').textContent.indexOf('Saved') >= 0")
    assert db.get_setting("trusted_proxies") == "192.0.2.10, 10.0.0.0/8"
    page.reload()
    page.wait_for_selector('#setnav .nav-item[data-sec="accounts"]')
    page.click('.nav-item[data-sec="accounts"]')
    page.click('#settabs .tab[data-tab="signin"]')
    assert page.input_value("#awayproxies") == "192.0.2.10, 10.0.0.0/8"
    assert page.csp_violations == []
