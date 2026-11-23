"""Preferred language at setup and in Settings, channel languages, and the
channels-per-game choice on a pass. Every step is a click."""
import re

import pytest

from tests.test_passes import _bills_game
from tests.ui.conftest import _page


@pytest.fixture
def fresh_with(browser, base_url, synced):
    """A not-yet-configured install, opened in a browser of the given language."""
    from app import db
    made = []

    def open_as(locale):
        db.set_setting("plex_url", "")
        db.set_setting("plex_token", "")
        db.set_setting("preferred_language", "en")
        ctx, p = _page(browser, base_url, viewport={"width": 1440, "height": 900},
                       locale=locale)
        made.append(ctx)
        p.goto("/")
        p.wait_for_selector("#wsignin")
        return p

    yield open_as
    for c in made:
        c.close()


def test_setup_preselects_the_browsers_language(fresh_with):
    p = fresh_with("es-MX")
    assert p.locator("#wl").input_value() == "es"
    assert "PREFERRED LANGUAGE FOR TV" in p.locator('label[for="wl"]').inner_text().upper()


def test_setup_falls_back_to_english_for_a_language_it_does_not_offer(fresh_with):
    p = fresh_with("ja-JP")
    assert p.locator("#wl").input_value() == "en"


def test_setup_lists_the_languages_in_their_own_words(fresh_with):
    p = fresh_with("en-US")
    names = p.eval_on_selector_all("#wl option", "els => els.map(e => e.textContent.trim())")
    assert names == ["English", "Español", "Français", "Português", "Deutsch", "Italiano"]


def test_the_language_picked_at_setup_is_saved(fresh_with, plex_url):
    from app import db
    p = fresh_with("en-US")
    p.select_option("#wl", "pt")
    p.click("#wmanual")
    p.wait_for_selector("#wform", state="visible")
    p.fill('input[name="plex_url"]', plex_url)
    p.fill('input[name="plex_token"]', "test-token")
    p.click("#wform button")
    p.wait_for_selector("#wconnect", state="visible", timeout=30000)
    assert db.get_setting("preferred_language") == "pt"


@pytest.fixture
def settings(page):
    page.goto("/settings")
    page.wait_for_selector("#setnav .nav-item")
    return page


def test_the_preferred_language_is_changed_in_settings(settings):
    from app import db
    settings.click('.nav-item[data-sec="plex"]')
    settings.click('#settabs .tab[data-tab="guide"]')
    settings.select_option("#preflang", "es")
    settings.click('section[data-tab="guide"] button.primary')
    settings.wait_for_function(
        "() => document.querySelector('section[data-tab=\"guide\"] .saved.on')")
    assert db.get_setting("preferred_language") == "es"


def test_a_channels_language_is_overridden_from_its_row(settings):
    from app import db
    settings.click('.nav-item[data-sec="channels"]')
    settings.click('#settabs .tab[data-tab="language"]')
    row = settings.locator('.lgrow[data-vcn="41.1"]')
    assert "Not detected" in row.inner_text()
    row.locator(".lgsel").select_option("es")
    settings.wait_for_function(
        "() => /Saved/.test(document.querySelector('.lgrow[data-vcn=\"41.1\"] .chsay').textContent)")
    assert db.one("SELECT language_override o FROM channels WHERE vcn = '41.1'")["o"] == "es"
    row.locator(".lgsel").select_option("")
    settings.wait_for_function(
        "() => document.querySelector('.lgrow[data-vcn=\"41.1\"] .lgsel').value === ''")
    settings.wait_for_timeout(300)
    assert db.one("SELECT language_override o FROM channels WHERE vcn = '41.1'")["o"] is None


def test_the_channel_language_list_can_be_searched(settings):
    settings.click('.nav-item[data-sec="channels"]')
    settings.click('#settabs .tab[data-tab="language"]')
    settings.fill("#lgq", "41.1")
    visible = settings.eval_on_selector_all(
        "#lglist .lgrow", "els => els.filter(e => !e.hidden).map(e => e.dataset.vcn)")
    assert visible == ["41.1"]


# ---- the pass form ----

@pytest.fixture
def add_team(page):
    _bills_game()
    page.goto("/recordings")
    page.click("#addrule")
    page.wait_for_selector("input[name=rmode]")
    page.check('input[name=rmode][value="smart"]')
    page.check('input[name=rsub][value="team"]')
    page.wait_for_selector("#rlist > *", timeout=15000)
    page.click("#rlist >> text=Kansas City Chiefs")
    page.wait_for_function(
        "() => /Kansas City Chiefs/.test(document.getElementById('rpick').textContent)",
        timeout=15000)
    return page


def test_the_channels_per_game_choice_defaults_to_one_airing(add_team):
    sel = add_team.locator("#cpg")
    assert sel.input_value() == "one"
    labels = add_team.eval_on_selector_all(
        "#cpg option", "els => els.map(e => e.textContent.trim())")
    assert labels[0] == "One airing per game"
    assert labels[1].startswith("All airings in my preferred language")
    assert labels[2] == "All airings, any language"
    assert add_team.locator("#cpgnote").is_hidden()


def test_picking_all_airings_says_what_it_costs_before_saving(add_team):
    add_team.select_option("#cpg", "any")
    add_team.wait_for_function(
        "() => /Over the next 30 days/.test(document.getElementById('cpgnote').textContent)",
        timeout=15000)
    text = add_team.locator("#cpgnote").inner_text()
    m = re.search(r"books (\d+) recordings instead of (\d+)\. Up to (\d+) at the same time",
                  text)
    assert m, text
    assert int(m.group(1)) > int(m.group(2))
    assert int(m.group(3)) >= 2


def test_going_back_to_one_airing_clears_the_line(add_team):
    add_team.select_option("#cpg", "preferred")
    add_team.wait_for_function(
        "() => /Over the next 30 days/.test(document.getElementById('cpgnote').textContent)",
        timeout=15000)
    add_team.select_option("#cpg", "one")
    assert add_team.locator("#cpgnote").is_hidden()


def test_the_choice_made_is_the_choice_saved(add_team):
    from app import db
    add_team.select_option("#cpg", "preferred")
    add_team.wait_for_function(
        "() => /Over the next 30 days/.test(document.getElementById('cpgnote').textContent)",
        timeout=15000)
    add_team.click("#rgo")
    add_team.wait_for_function(
        "() => /Done/.test(document.getElementById('rgo').textContent)", timeout=30000)
    assert db.one("SELECT channels_per_game c FROM passes")["c"] == "preferred"


def test_a_series_pass_has_no_channels_per_game(page):
    page.goto("/recordings")
    page.click("#addrule")
    page.wait_for_selector("input[name=rmode]")
    page.check('input[name=rmode][value="series"]')
    assert page.locator("#cpg").count() == 0


def test_editing_a_pass_shows_and_changes_its_choice(page):
    from app import db
    from tests.test_passes import _mode_pass
    _bills_game()
    pid = _mode_pass("any")
    page.goto("/recordings")
    page.click('.subtab[data-sub="sports"]')
    page.click(f'.passrow[data-id="{pid}"] [data-act="edit"]')
    page.wait_for_selector("#cpg", timeout=15000)
    # The language names arrive after the panel and redraw the options; wait
    # for that, or the pick can land on the element being replaced.
    page.wait_for_function(
        "() => /\\(English\\)/.test(document.getElementById('cpg').textContent)",
        timeout=15000)
    assert page.locator("#cpg").input_value() == "any"
    page.select_option("#cpg", "one")
    page.click("#rgo")
    page.wait_for_function(
        "() => /Done/.test(document.getElementById('rgo').textContent)", timeout=30000)
    assert db.one("SELECT channels_per_game c FROM passes WHERE id = ?", (pid,))["c"] == "one"
