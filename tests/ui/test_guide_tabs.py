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
    star = guide.locator(".gchcol .gstar").first
    star.click()
    guide.wait_for_selector(".gchcol .gstar.on")
    assert db.one("SELECT COUNT(*) n FROM favorite_channels")["n"] == 1

    guide.click("#tab-favorites")
    guide.wait_for_selector(".gprog")
    assert _channels(guide) == 1 < all_channels

    guide.locator(".gchcol .gstar.on").click()
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


def test_the_star_is_not_inside_the_channel_button(guide):
    assert guide.locator(".gch .gstar").count() == 0
    box = guide.locator(".gchcol .gstar").first.bounding_box()
    assert box["width"] >= 24 and box["height"] >= 24


def test_a_set_star_is_fully_visible(guide):
    guide.locator(".gchcol .gstar").first.click()
    guide.wait_for_selector(".gstar.on")
    assert guide.eval_on_selector(".gstar.on", "e => getComputedStyle(e).opacity") == "1"


def test_enter_on_the_star_toggles_it_and_does_not_start_playback(browser, base_url, synced):
    from tests.ui.conftest import _page
    ctx, page = _page(browser, base_url, viewport={"width": 1440, "height": 900})
    try:
        page.goto("/guide")
        page.wait_for_selector(".gprog")
        assert page.locator(".gch.watch").count() >= 1, "the label is a watch button here"
        # Replace tuning with a note of what was asked, so a test never tunes.
        page.evaluate("window.__watched = null; CE.Player.watch = function (c) { window.__watched = c; }")
        star = page.locator(".gchcol .gstar").first
        star.focus()
        page.keyboard.press("Enter")
        page.wait_for_selector(".gstar.on")
        assert page.evaluate("window.__watched") is None
        # Enter on the label itself still watches.
        page.locator(".gch.watch").first.focus()
        page.keyboard.press("Enter")
        assert page.evaluate("window.__watched") is not None
    finally:
        ctx.close()


def test_the_tabs_mark_the_current_page_without_tab_roles(guide):
    assert guide.locator("#gtabs [role=tab]").count() == 0
    assert guide.locator('#gtabs a[aria-current="page"]').inner_text() == "All"


def test_a_search_result_recording_opens_on_the_recordings_page(page):
    from app import db
    with db.tx() as c:
        c.execute("""INSERT INTO recordings (uid, kind, state, title, show_title, section,
                                             channel_vcn, begins_at, ends_at)
                     VALUES ('uzz','recording','finished','Zebra Hour','Zebra Hour','shows',
                             '41.1',1000,2000)""")
        rid = c.execute("SELECT id FROM recordings WHERE uid = 'uzz'").fetchone()[0]
    page.goto("/guide?q=zebra")
    page.click('#recresults + .card a')
    page.wait_for_selector(f'[data-rec="{rid}"].hit')


def test_scrolling_a_tab_for_more_time_keeps_every_row_once(page):
    from app import db
    with db.tx() as c:
        for r in db.query("SELECT vcn FROM channels"):
            c.execute("INSERT OR IGNORE INTO favorite_channels (vcn, added_at) VALUES (?, 1)",
                      (r["vcn"],))
    page.goto("/guide?tab=favorites")
    page.wait_for_selector(".gprog")
    rows = page.locator(".growr").count()
    ticks = page.locator(".gtick").count()
    assert rows >= 2
    for _ in range(3):
        page.evaluate("var w = document.getElementById('gw'); w.scrollLeft = w.scrollWidth")
        page.wait_for_timeout(700)
    assert page.locator(".gtick").count() > ticks, "more time was loaded"
    vcns = page.eval_on_selector_all(".growr", "els => els.map(e => e.dataset.vcn)")
    assert len(vcns) == rows == len(set(vcns)), "no row repeated, none lost"
    assert page.locator(".growr .glane:empty").count() < rows


def test_scrolling_a_tab_for_more_channels_keeps_every_row_once(page):
    import time

    from app import db
    now = int(time.time())
    with db.tx() as c:
        c.execute("INSERT INTO programs (guid, title, section, genres, teams) "
                  "VALUES ('nz','Zed News','shows','[\"News\"]','[]')")
        for i in range(14):
            vcn = f"{70 + i}.1"
            c.execute("INSERT INTO channels (vcn, call_sign, title) VALUES (?,?,?)",
                      (vcn, f"Z{i}", f"{vcn} Z{i}"))
            c.execute("""INSERT INTO airings (id, program_guid, channel_vcn, channel_call_sign,
                                              channel_title, begins_at, ends_at)
                         VALUES (?,?,?,?,?,?,?)""",
                      (f"nz{i}", "nz", vcn, f"Z{i}", vcn, now - 600, now + 7200))
    page.goto("/guide?tab=news")
    page.wait_for_selector(".gprog")
    for _ in range(6):
        page.evaluate("var w = document.getElementById('gw'); w.scrollTop = w.scrollHeight")
        page.wait_for_timeout(500)
    vcns = page.eval_on_selector_all(".growr", "els => els.map(e => e.dataset.vcn)")
    assert len(vcns) == 14 == len(set(vcns))
    assert vcns == sorted(vcns, key=float), "in channel order"


def test_a_tab_channel_whose_match_is_past_the_first_chunk_shows_once(page, synced):
    """Page down through more channels than one page holds, then right past the
    first four hours: the channel with its only match late must appear once,
    with its programme, and none repeated or empty."""
    import time
    now = int(time.time()) // 1800 * 1800
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO programs (guid, title, section, genres, teams) "
                  "VALUES ('nt','Tab News','shows','[\"News\"]','[]')")
        for i in range(25):
            vcn = f"80.{i}"
            c.execute("INSERT INTO channels (vcn, call_sign, title) VALUES (?,?,?)",
                      (vcn, f"T{i}", vcn))
            c.execute("""INSERT INTO airings (id, program_guid, channel_vcn, channel_call_sign,
                         channel_title, begins_at, ends_at) VALUES (?,?,?,?,?,?,?)""",
                      (f"tn{i}", "nt", vcn, f"T{i}", vcn, now, now + 1800))
        c.execute("INSERT INTO channels (vcn, call_sign, title) VALUES ('99.9','LATE','99.9')")
        c.execute("""INSERT INTO airings (id, program_guid, channel_vcn, channel_call_sign,
                     channel_title, begins_at, ends_at) VALUES ('tlate','nt','99.9','LATE','99.9',?,?)""",
                  (now + 6 * 3600, now + 7 * 3600))
    page.goto("/guide?tab=news")
    page.wait_for_selector(".gprog")
    for _ in range(40):
        page.evaluate("var w = document.getElementById('gw'); w.scrollTop = w.scrollHeight;")
        page.wait_for_timeout(250)
        if page.locator('.growr[data-vcn="99.9"]').count():
            break
    assert page.locator('.growr[data-vcn="99.9"]').count() == 1
    for _ in range(40):
        page.evaluate("var w = document.getElementById('gw'); w.scrollLeft = w.scrollWidth;")
        page.wait_for_timeout(250)
        if page.locator('.gprog[data-vcn="99.9"]').count():
            break
    assert page.locator('.gprog[data-vcn="99.9"]').count() == 1
    assert page.locator('.growr[data-vcn="99.9"]').count() == 1
    seen = page.eval_on_selector_all(".growr", "els => els.map(e => e.dataset.vcn)")
    assert len(seen) == len(set(seen)), "no channel row is drawn twice"
