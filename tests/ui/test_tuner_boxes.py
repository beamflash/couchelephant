"""Settings, Recording: every tuner box, and what each tuner is doing."""
import os

import pytest

from app import db, tuner, tunerbox
from tests import fake_tuner


@pytest.fixture
def boxes(page, two_boxes, monkeypatch):
    a, b = two_boxes
    a.live = ["5.1"]
    a.plex_holds = ["9.1"]
    tuner.hold(7, tunerbox.rows()[0]["id"], "5.1")
    monkeypatch.setattr(tuner, "describe_hold", lambda job: "Recording: Chiefs at Bills")
    db.set_setting("tuner_broadcast_ok", "0")
    page.goto("/settings")
    page.wait_for_selector("#setnav .nav-item")
    page.click('.nav-item[data-sec="recording"]')
    page.click('#settabs .tab:has-text("CouchElephant DVR")')
    page.wait_for_selector("#tunerlist .tbox")
    yield page
    tuner.release(7)


def test_settings_lists_both_boxes_and_each_tuner(boxes):
    page = boxes
    assert page.locator("#tunerlist .tbox").count() == 2
    text = page.locator("#tunerlist").inner_text()
    assert "Recording: Chiefs at Bills" in text
    assert "In use by another app" in text
    assert page.locator("#tunerbridge").is_visible()
    assert "Add them by address." in page.locator("#tunerbridge").inner_text()
    # Set by hand to look at the page.
    if os.environ.get("TUNER_SHOT"):
        page.locator("#tuneradd").scroll_into_view_if_needed()
        page.screenshot(path=os.environ["TUNER_SHOT"])


def test_add_by_address_then_turn_off_then_remove(boxes):
    page = boxes
    url, stop, _ = fake_tuner.start_box(device_id="FAKE000C", total=2)
    try:
        page.fill("#tuneraddr", url)
        page.click("#tuneradd")
        page.wait_for_function("document.querySelectorAll('#tunerlist .tbox').length === 3")
        third = page.locator("#tunerlist .tbox").nth(2)
        third.locator('button[data-act="toggle"]').click()
        page.wait_for_selector("#tunerlist .tbox.off")
        assert page.locator("#tunerlist .tbox.off button[data-act=toggle]").inner_text() == "Turn on"
        page.locator("#tunerlist .tbox.off button[data-act=remove]").click()
        page.wait_for_function("document.querySelectorAll('#tunerlist .tbox').length === 2")
    finally:
        stop()
