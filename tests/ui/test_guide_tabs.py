"""The guide's tabs, the star on a channel, and search from the page."""
from app import db


def _channels(page):
    return page.locator(".gch").count()


def test_the_four_tabs_are_there_and_all_is_chosen(guide):
    tabs = guide.locator("#gtabs a")
    assert [tabs.nth(i).inner_text() for i in range(tabs.count())] == \
        ["All", "Favorites", "News", "Sports"]
    assert guide.locator("#tab-all.on").count() == 1


def test_an_empty_favorites_tab_says_how_to_add_one(page):
    page.goto("/guide")
    page.click("#tab-favorites")
    page.wait_for_selector("#nofavorites")
    assert "Star a channel" in page.locator("#nofavorites").inner_text()
    assert page.locator(".gch").count() == 0


def test_starring_a_channel_puts_it_on_the_favorites_tab_and_unstarring_removes_it(guide):
    all_channels = _channels(guide)
    star = guide.locator(".gch .gstar").first
    star.click()
    guide.wait_for_selector(".gch .gstar.on")
    assert db.one("SELECT COUNT(*) n FROM favorite_channels")["n"] == 1

    guide.click("#tab-favorites")
    guide.wait_for_selector(".gprog")
    assert _channels(guide) == 1 < all_channels

    guide.locator(".gch .gstar.on").click()
    guide.wait_for_selector("#nofavorites")
    assert db.one("SELECT COUNT(*) n FROM favorite_channels")["n"] == 0


def test_the_sports_tab_keeps_only_channels_with_sport(guide):
    guide.click("#tab-sports")
    guide.wait_for_selector(".gprog")
    assert guide.locator("#tab-sports.on").count() == 1
    titles = [guide.locator(".gprog").nth(i).get_attribute("title") or ""
              for i in range(guide.locator(".gprog").count())]
    assert titles and all("Chiefs" in t or "Bills" in t or "\n" in t for t in titles)


def test_searching_for_a_channel_finds_what_is_on_it(page):
    row = db.one("SELECT call_sign FROM channels WHERE call_sign != '' LIMIT 1")
    page.goto("/guide")
    page.wait_for_selector(".gprog")
    page.click("#sbtoggle")
    page.fill("#sbinput", row["call_sign"])
    page.keyboard.press("Enter")
    page.wait_for_selector("#grid tbody tr")
    assert page.locator("#gtabs").count() == 1, "the tabs stay on a search"
