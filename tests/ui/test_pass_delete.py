"""Deleting a season pass asks about its upcoming recordings, and the
bookings of a pass that is already gone can be managed from the same tab."""
import time

import pytest

from app import db, recorder
from tests.test_recorder import _airing


@pytest.fixture
def season(page):
    """A Murder, She Wrote season pass with three bookings ahead."""
    now = int(time.time())
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, series_title, series_guid, uid, recorder, "
                  "enabled, created_at) VALUES ('series', 'Murder, She Wrote', "
                  "'Murder, She Wrote', 'u-msw', 'couchelephant', 1, 0)")
        pid = c.execute("SELECT id FROM passes").fetchone()["id"]
    for i in range(3):
        row = _airing(f"s{i}", f"gs{i}", "5.1", now + 7200 + i * 4000,
                      title=f"Episode {i}", show="Murder, She Wrote")
        with db.tx() as c:
            c.execute("UPDATE programs SET section = 'tv' WHERE guid = ?", (f"gs{i}",))
        recorder.book(row, "pass", pass_id=pid, pass_uid="u-msw")
    page.goto("/recordings")
    page.click('.subtab[data-sub="season"]')
    page.wait_for_selector(".passrow", timeout=15000)
    return page


def _left():
    return db.one("SELECT COUNT(*) c FROM recordings")["c"]


def test_removing_a_season_pass_asks_and_yes_cancels_the_recordings(season):
    season.click('.passrow [data-act="remove"]')
    box = season.locator("#passdel")
    box.wait_for(timeout=5000)
    text = box.inner_text()
    assert "Delete the Murder, She Wrote season pass?" in text
    assert "Also cancel the 3 upcoming recordings" in text
    assert "Keep them" in text and "Cancel" in text
    season.click('#passdel [data-ans="yes"]')
    season.wait_for_selector("#passlist .empty", timeout=15000)
    assert _left() == 0 and db.one("SELECT COUNT(*) c FROM passes")["c"] == 0


def test_keep_them_leaves_the_recordings_as_a_lost_pass(season):
    season.click('.passrow [data-act="remove"]')
    season.click('#passdel [data-ans="keep"]')
    season.wait_for_selector('.passrow [data-act="lostcancel"]', timeout=15000)
    row = season.locator(".passrow").first
    assert "Murder, She Wrote (pass deleted)" in row.inner_text()
    assert "3 booked" in row.inner_text()
    assert _left() == 3


def test_cancel_in_the_question_changes_nothing(season):
    season.click('.passrow [data-act="remove"]')
    season.click('#passdel [data-ans="cancel"]')
    assert season.locator("#passdel").count() == 0
    assert db.one("SELECT COUNT(*) c FROM passes")["c"] == 1 and _left() == 3


def test_cancel_all_on_a_lost_pass_cancels_its_bookings(season):
    season.click('.passrow [data-act="remove"]')
    season.click('#passdel [data-ans="keep"]')
    season.click('.passrow [data-act="lostcancel"]')
    season.wait_for_selector("#passlist .empty", timeout=15000)
    assert _left() == 0


def test_follow_again_makes_an_ordinary_pass_of_a_lost_one(season):
    season.click('.passrow [data-act="remove"]')
    season.click('#passdel [data-ans="keep"]')
    season.click('.passrow [data-act="lostfollow"]')
    season.wait_for_selector('.passrow [data-act="edit"]', timeout=15000)
    row = season.locator(".passrow").first
    assert "Murder, She Wrote" in row.inner_text()
    assert "(pass deleted)" not in row.inner_text()
    assert "3 booked" in row.inner_text()
