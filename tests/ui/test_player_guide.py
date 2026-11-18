"""The guide over live TV: down opens it over the picture, a channel in it
changes channel, Escape closes it and leaves the channel playing."""
import json

import pytest

LIVE = {"id": "77", "kind": "live", "title": "News at Ten", "subtitle": "",
        "channel": {"number": "4.1", "name": "KQBB", "logo": ""},
        "playlist": "/nowhere/live.m3u8", "complete": False, "seconds": 60,
        "position": 0, "pause_limit": 300}


@pytest.fixture
def live(page):
    """Live TV playing on 4.1, with every call to the server about live TV
    noted rather than tuning a tuner."""
    calls = []

    def answer(route):
        req = route.request
        calls.append((req.method, req.url.split("/api/tv/live/", 1)[1]))
        if req.url.endswith("/stop") or req.url.endswith("/keepalive"):
            return route.fulfill(json={"ok": True})
        new = dict(LIVE, id="78", channel={"number": "5.1", "name": "KQCC", "logo": ""})
        return route.fulfill(json={"ok": True, "recording": new})

    page.route("**/api/tv/live/**", answer)
    page.route("**/api/tv/recordings/78", lambda r: r.fulfill(
        json={"ok": True, "recording": dict(LIVE, id="78", playable=True)}))
    page.goto("/home")
    page.wait_for_function("window.CE && CE.Player")
    page.evaluate(f"CE.Player.play({json.dumps(LIVE)}, 'live')")
    page.wait_for_selector(".tvplayer:not([hidden])")
    page.calls = calls
    return page


def _frame(page):
    page.wait_for_selector("#tvpguidebox iframe")
    return page.frame_locator("#tvpguidebox iframe")


def test_down_opens_the_guide_over_live_tv_and_escape_closes_it(live):
    assert live.locator("#tvpguide").is_visible()
    live.keyboard.press("ArrowDown")
    assert live.locator("#tvpguidebox").is_visible()
    grid = _frame(live)
    grid.locator(".gprog").first.wait_for(timeout=15000)
    # Only the grid: the page's own header and filters are not in the frame.
    assert not grid.locator(".topbar").is_visible()
    assert grid.locator("#nowline.gnow").count() == 1
    grid.locator("body").press("Escape")
    assert not live.locator("#tvpguidebox").is_visible()
    # The channel plays on.
    assert live.locator(".tvplayer").is_visible()
    assert not any(u.endswith("/stop") for _, u in live.calls)


def test_a_channel_in_the_guide_changes_channel(live):
    live.locator("#tvpguide").click()
    grid = _frame(live)
    grid.locator(".gch.watch").first.wait_for(timeout=15000)
    grid.locator(".gch.watch", has_text="5.1").first.click()
    live.wait_for_function("document.getElementById('tvpguidebox').hidden")
    # The old channel's tuner goes back before the new one is tuned.
    urls = [u for _, u in live.calls]
    assert urls.index("77/stop") < urls.index("5.1")


def test_the_guide_opens_over_a_recording_still_being_made(page):
    """Watching a channel that is recording plays the recording."""
    page.goto("/home")
    page.wait_for_function("window.CE && CE.Player")
    page.evaluate(f"CE.Player.play({json.dumps(dict(LIVE, kind='recording'))}, 'live')")
    page.wait_for_selector(".tvplayer:not([hidden])")
    assert page.locator("#tvpguide").is_visible()


def test_the_guide_is_not_for_a_finished_recording(page):
    page.goto("/home")
    page.wait_for_function("window.CE && CE.Player")
    page.evaluate(f"CE.Player.play({json.dumps(dict(LIVE, kind='recording', complete=True))}, 'start')")
    page.wait_for_selector(".tvplayer:not([hidden])")
    assert not page.locator("#tvpguide").is_visible()
    page.keyboard.press("ArrowDown")
    assert not page.locator("#tvpguidebox").is_visible()
