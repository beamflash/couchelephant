"""The frame around every page: header, tabs, theme, account menu."""


def test_the_guide_is_the_landing_page(page):
    page.goto("/")
    assert "CouchElephant" in page.title()
    assert page.locator(".pt-tab.active").inner_text().strip() == "Guide"


def test_the_tabs_are_the_expected_ones_and_they_fill_the_bar(page):
    """Guide and Schedule, always. Live TV joins them only where CouchElephant's
    own recorder can run, which the suite's scratch install is set up for."""
    page.goto("/")
    tabs = page.locator(".pt-tab")
    labels = [tabs.nth(i).inner_text().strip() for i in range(tabs.count())]
    assert labels == ["Guide", "Schedule", "Live TV"]
    nav = page.locator("#ptnav").bounding_box()
    total = sum(tabs.nth(i).bounding_box()["width"] for i in range(tabs.count()))
    # Centred and full width, the way Ludodex does it.
    assert total > nav["width"] * 0.9


def test_the_tabs_navigate(page):
    page.goto("/")
    page.click('.pt-tab[data-t="recordings"]')
    page.wait_for_url("**/recordings")
    assert page.locator(".pt-tab.active").inner_text().strip() == "Schedule"


def test_a_divider_separates_the_header_from_the_tabs(page):
    page.goto("/")
    # A border, not a spacer element, so it survives a theme change. It sits
    # on the header, between it and the tabs.
    w = page.eval_on_selector("header", "el => getComputedStyle(el).borderBottomWidth")
    colour = page.eval_on_selector("header", "el => getComputedStyle(el).borderBottomColor")
    assert w not in ("", "0px")
    bg = page.eval_on_selector("header", "el => getComputedStyle(el).backgroundColor")
    assert colour != bg, "a divider the same colour as the bar is not a divider"


def test_the_header_holds_sync_settings_and_account_in_that_order(page):
    page.goto("/")
    # By id, not by label. The sync button's label follows what the button
    # does, and with a problem open it reads the problem rather than syncing.
    order = page.eval_on_selector_all(
        "header .right button, header .right a",
        "els => els.map(e => e.id || e.getAttribute('aria-label'))")
    order = [o for o in order if o]
    assert order.index("syncbtn") < order.index("gearbtn")
    assert order.index("gearbtn") < order.index("pbtn")


def test_the_sync_time_is_a_tooltip_not_a_line_of_text(page):
    page.goto("/")
    tip = page.get_attribute("#syncbtn", "title")
    assert "sync" in tip.lower()
    assert "Never synced" in tip or "Last synced" in tip
    assert "synced" not in page.locator("header").inner_text().lower()


def test_the_account_menu_opens_and_says_sign_in_is_off(page):
    page.goto("/")
    assert not page.locator("#pmenu").is_visible()
    page.click("#pbtn")
    menu = page.locator("#pmenu")
    assert menu.is_visible()
    assert "Guest" in menu.inner_text()
    assert "Sign-in is off" in menu.inner_text()


def test_the_account_menu_closes_when_you_click_away(page):
    page.goto("/")
    page.click("#pbtn")
    page.click("main", position={"x": 5, "y": 5})
    assert not page.locator("#pmenu").is_visible()


def test_the_theme_toggle_flips_the_page_and_is_remembered(page):
    page.goto("/")
    assert page.eval_on_selector(
        "html", "el => el.getAttribute('data-theme')") in (None, "dark")
    dark = page.eval_on_selector("body", "el => getComputedStyle(el).backgroundColor")

    page.click("#pbtn")
    page.click("#themebtn")
    assert page.eval_on_selector("html", "el => el.getAttribute('data-theme')") == "light"
    light = page.eval_on_selector("body", "el => getComputedStyle(el).backgroundColor")
    assert light != dark, "a theme that does not change the background is not a theme"

    page.reload()
    assert page.eval_on_selector("html", "el => el.getAttribute('data-theme')") == "light"


def test_a_light_system_preference_is_honoured_on_a_first_visit(light_system):
    light_system.goto("/")
    assert light_system.eval_on_selector(
        "html", "el => el.getAttribute('data-theme')") == "light"


def test_the_theme_reaches_the_browser_chrome_too(page):
    page.goto("/")
    before = page.get_attribute("#themecolor", "content")
    page.click("#pbtn")
    page.click("#themebtn")
    assert page.get_attribute("#themecolor", "content") != before


def test_preview_mode_is_announced_in_the_header(page):
    from app import db
    db.set_setting("dry_run", "1")
    page.goto("/")
    assert "PREVIEW MODE" in page.locator("header").inner_text()


def test_an_install_without_the_recorder_has_no_live_tv_tab(page, monkeypatch):
    """Not opted in means not asked: no tab, and no Recorded by choice."""
    from app import recorder
    monkeypatch.setattr(recorder, "REC_DIR", "/nonexistent-ce-test")
    page.goto("/")
    labels = page.eval_on_selector_all(".pt-tab", "els => els.map(e => e.textContent.trim())")
    assert labels == ["Guide", "Schedule"]
    assert page.evaluate("window.CE_RECORDER.available") is False


def test_the_live_tv_page_draws_its_three_lists(page):
    page.goto("/live")
    page.wait_for_selector("#tvrecent .empty")
    heads = page.eval_on_selector_all(".tvhead", "els => els.map(e => e.textContent.trim())")
    assert heads == ["Live now", "Recent recordings", "On now"]
    assert page.locator("#tvplayer").is_hidden()
