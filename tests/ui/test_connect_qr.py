"""The QR code to connect an app, shown in the browser."""


def _drawn(page, selector):
    """The image loaded and has a size, so it is on screen and not broken."""
    page.wait_for_function(
        "s => { var i = document.querySelector(s);"
        " return i && i.complete && i.naturalWidth > 0; }", arg=selector, timeout=15000)
    return page.locator(selector).is_visible()


def test_settings_televisions_shows_the_qr_and_says_how_to_scan(page):
    page.goto("/settings")
    page.wait_for_selector("#setnav .nav-item")
    page.click('.nav-item[data-sec="recording"]')
    page.click('#settabs .tab[data-tab="televisions"]')
    sec = page.locator('section[data-tab="televisions"]')
    assert sec.is_visible()
    assert _drawn(page, "#connectqr")
    assert "Scan with your iPhone or iPad camera" in sec.inner_text()
    # The find-on-the-network switch sits beside it.
    assert sec.locator('input[name="announce_on"]').count() == 1


def test_the_welcome_page_shows_it_once_plex_is_set(page, plex_url):
    from app import db
    db.set_setting("plex_url", "")
    db.set_setting("plex_token", "")
    page.goto("/")
    page.wait_for_selector("#wsignin")
    assert not page.locator("#wconnect").is_visible()
    page.click("#wmanual")
    page.fill('input[name="plex_url"]', plex_url)
    page.fill('input[name="plex_token"]', "test-token")
    page.click("#wform button")
    page.wait_for_selector("#wconnect", state="visible", timeout=30000)
    assert _drawn(page, "#wqr")
    assert "Scan with your iPhone or iPad camera" in page.locator("#wconnect").inner_text()


def test_with_no_address_settings_says_so_where_the_qr_would_be(page, monkeypatch):
    from app import announce
    monkeypatch.setattr(announce, "_addresses", lambda: [])
    # The browser reaches the server as localhost, which a phone cannot use.
    page.goto("/settings")
    page.wait_for_selector("#setnav .nav-item")
    page.click('.nav-item[data-sec="recording"]')
    page.click('#settabs .tab[data-tab="televisions"]')
    sec = page.locator('section[data-tab="televisions"]')
    sec.get_by_text("no home network address the apps can use").wait_for(timeout=15000)
    assert not page.locator("#connectqr").is_visible()
