"""A recording Plex has not been given yet says why, and can be retried now."""
import time

from app import db, recorder
from tests.test_recorder import _airing

WHY = "CouchElephant cannot write to the folder /library/tv/NFL Football (2002)."


def test_a_stuck_handover_says_why_and_retry_now_asks_again(page):
    now = int(time.time())
    rec_id = recorder.book(_airing("ui-retry", "g-ui-retry", "5.1", now - 7200))
    recorder._set(rec_id, state="finished", ended_at=now - 3600, error=WHY,
                  tried_at=now)
    page.goto("/recordings")
    page.click('.subtab[data-sub="recorded"]')
    page.wait_for_selector("[data-retry]")
    text = page.locator("#reclist").inner_text()
    assert WHY in text and "tries again by itself" in text
    page.click("[data-retry]")
    # The click gave the handover back to the worker at once: it ran, found
    # nothing set up, and put its own reason on the row.
    deadline = time.time() + 10
    while time.time() < deadline and recorder.get(rec_id)["error"] == WHY:
        time.sleep(0.1)
    assert recorder.get(rec_id)["error"] != WHY
    db.set_setting("recorder_shows_folder", "")


def test_a_failed_retry_request_never_leaves_the_button_stuck(page):
    now = int(time.time())
    rec_id = recorder.book(_airing("ui-retry2", "g-ui-retry2", "5.1", now - 7200))
    recorder._set(rec_id, state="finished", ended_at=now - 3600, error=WHY,
                  tried_at=now)
    page.goto("/recordings")
    page.click('.subtab[data-sub="recorded"]')
    page.wait_for_selector("[data-retry]")
    page.route("**/retry", lambda route: route.abort())
    page.click("[data-retry]")
    page.wait_for_function(
        "document.querySelector('[data-retry]').textContent === 'Retry now'")
    assert "Could not reach CouchElephant" in page.locator(".retrysay").inner_text()
    page.unroute("**/retry")
    page.route("**/retry", lambda route: route.fulfill(
        status=500, content_type="text/plain", body="boom"))
    page.click("[data-retry]")
    page.wait_for_function("document.querySelector('.retrysay').textContent.includes('HTTP 500')")
    assert page.locator("[data-retry]").inner_text() == "Retry now"


def test_a_held_handover_is_shown_as_a_problem_not_a_success(page):
    now = int(time.time())
    rec_id = recorder.book(_airing("ui-retry3", "g-ui-retry3", "5.1", now - 7200))
    recorder._set(rec_id, state="finished", ended_at=now - 3600, error=WHY, tried_at=now)
    page.goto("/recordings")
    page.click('.subtab[data-sub="recorded"]')
    page.wait_for_selector("[data-retry]")
    page.route("**/retry", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"ok": true, "outcome": "held", "message": "No Plex library is chosen."}'))
    page.click("[data-retry]")
    page.wait_for_selector(".retrysay.bad")
    assert "No Plex library" in page.locator(".retrysay").inner_text()
