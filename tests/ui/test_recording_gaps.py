"""A recording the tuner dropped out of says where, on its card."""
import json

from app import db
from tests.test_recording_art import _finished


def test_a_card_says_where_the_tuner_left_a_hole(page):
    rid = _finished("gap-1", state="finished")
    with db.tx() as c:
        c.execute("UPDATE recordings SET gaps = ? WHERE id = ?",
                  (json.dumps([{"at": 3850, "seconds": 12}]), rid))
    page.goto("/home")
    page.wait_for_selector("#tvrecent .tvcard")
    notes = page.locator("#tvrecent .gapnote")
    assert notes.count() == 1
    assert notes.first.inner_text() == "12 seconds missing at 1:04:10, the tuner stopped answering"
