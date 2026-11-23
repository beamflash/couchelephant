"""Reporting a problem, in a real browser: the consent screen, the form, the
Report this problem button on an error, and what the dialog does when the
send does not work. Every step clicks the real buttons.
"""
import base64
import gzip
import json
from pathlib import Path

import pytest

from app import plextv, sealing
from tests import fake_firestore, fake_plextv
from tests.fake_firestore import STATE
from tests.ui.test_failed_bookings import _a_failed_chiefs_booking

VEC = json.loads((Path(__file__).parent.parent / "data" / "seal_vector.json").read_text())
PRIV = base64.b64decode(VEC["private"])


@pytest.fixture
def fs(monkeypatch):
    """A stand-in Firestore, and the server pointed at it with the test key."""
    url, stop = fake_firestore.start()
    STATE.reset()
    monkeypatch.setenv("COUCHELEPHANT_REPORT_PUBLIC_KEY", VEC["public"])
    monkeypatch.setenv("COUCHELEPHANT_REPORT_PROJECT", fake_firestore.PROJECT)
    monkeypatch.setenv("COUCHELEPHANT_REPORT_API_KEY", fake_firestore.API_KEY)
    monkeypatch.setenv("COUCHELEPHANT_REPORT_FIRESTORE_URL", url)
    yield STATE
    stop()


@pytest.fixture
def plexaccount(monkeypatch):
    fake_plextv.STATE.reset()
    url, stop = fake_plextv.start()
    monkeypatch.setattr(plextv, "BASE", url)
    yield fake_plextv.STATE
    stop()


def _opened(doc):
    raw = base64.b64decode(doc["sealed"]["bytesValue"])
    return json.loads(gzip.decompress(sealing.open_sealed(raw, PRIV)))


def _open_report_from_settings(page):
    page.goto("/settings")
    page.wait_for_selector("#setnav .nav-item")
    page.click('.nav-item[data-sec="about"]')
    page.click("#reportopen")
    page.wait_for_selector("#reportdlg.show")


def _agree(page):
    """First time, the agreement sits in the form itself: nothing to click, the
    one Send is the agreement. Kept so the tests read the same either way."""
    page.wait_for_selector("#reportmessage")


def test_settings_report_an_issue_opens_form_and_consent_first(page, fs):
    _open_report_from_settings(page)
    text = page.locator("#reportbox").inner_text()
    assert "Only the person who makes CouchElephant can read it" in text
    assert "sealed before it leaves, and stored sealed" in text
    assert page.locator("#reportconsent").is_visible()
    assert page.locator("#reportmessage").is_visible(), "one screen, consent and form"
    assert page.locator("#reportemail").is_visible()
    assert page.get_by_role("button", name="Send").count() == 1
    assert page.get_by_role("button", name="Never mind").is_visible()
    assert not fs.docs and not fs.requests, "opening sends nothing"


def test_never_mind_sends_nothing(page, fs):
    _open_report_from_settings(page)
    page.click("#reportno")
    assert not page.locator("#reportdlg").is_visible()
    assert not fs.docs and not fs.requests


def test_send_shows_sent_and_report_id_and_doc_arrives(page, fs):
    _open_report_from_settings(page)
    _agree(page)
    page.fill("#reportmessage", "the guide is odd")
    page.fill("#reportemail", "me@example.org")
    page.click("#reportsend")
    page.wait_for_selector("#reportid")
    assert "Sent. Thank you." in page.locator("#reportresult").inner_text()
    rid = page.locator("#reportid").inner_text().replace("Report id:", "").strip()
    assert len(fs.docs) == 1 and rid in fs.docs
    body = _opened(fs.docs[rid])
    assert body["message"] == "the guide is odd"
    assert body["trigger"]["kind"] == "support"


def test_second_send_skips_consent_and_prefills_remembered_email(page, fs):
    _open_report_from_settings(page)
    _agree(page)
    page.fill("#reportemail", "me@example.org")
    page.click("#reportsend")
    page.wait_for_selector("#reportid")
    page.click("#reportclose")
    page.click("#reportopen")
    page.wait_for_selector("#reportmessage")
    assert page.input_value("#reportemail") == "me@example.org"
    assert page.locator("#reportconsent").count() == 0
    assert "Only the person who makes" not in page.locator("#reportbox").inner_text()


def test_consent_is_per_browser_even_if_the_server_already_has_it(page, fs):
    from app import db
    db.set_setting("report_consent", "1")
    _open_report_from_settings(page)
    assert page.locator("#reportconsent").is_visible()
    assert "sealed before it leaves" in page.locator("#reportbox").inner_text()


def test_email_prefilled_from_plex_account(page, fs, plexaccount):
    plexaccount.email = "plexuser@example.org"
    plexaccount.valid_tokens.add("test-token")
    _open_report_from_settings(page)
    _agree(page)
    page.wait_for_function("document.getElementById('reportemail').value !== ''")
    assert page.input_value("#reportemail") == "plexuser@example.org"


def test_email_field_starts_empty_when_the_server_says_no(page, fs):
    page.route("**/api/report/email", lambda r: r.fulfill(status=403, body="{}"))
    _open_report_from_settings(page)
    _agree(page)
    assert page.input_value("#reportemail") == ""


def test_failed_booking_row_has_report_button_that_attaches_the_error(page, fs):
    _a_failed_chiefs_booking(page)
    page.goto("/recordings")
    page.wait_for_selector(".agrow.fail", timeout=15000)
    page.locator(".agrow.fail .reportbtn").first.click()
    page.wait_for_selector("#reportdlg.show")
    assert page.locator("#failretry").count() == 0, "the button did not also open the row"
    _agree(page)
    page.click("#reportsend")
    page.wait_for_selector("#reportid")
    doc = next(iter(fs.docs.values()))
    trig = _opened(doc)["trigger"]
    assert trig["kind"] == "error"
    assert "HTTP 400: Bad Request" in trig["error"]


def test_the_open_failure_panel_has_the_button_too(page, fs):
    _a_failed_chiefs_booking(page)
    page.goto("/recordings")
    page.wait_for_selector(".agrow.fail", timeout=15000)
    page.click(".agrow.fail .agtitle")
    page.wait_for_selector("#failretry")
    assert page.locator("#ovlbox .reportbtn").count() == 1


def test_guide_error_state_has_report_button(page, fs):
    page.route("**/partial/airings*", lambda r: r.fulfill(status=500, body="boom"))
    from app import db
    word = db.one("SELECT title FROM programs LIMIT 1")["title"].split()[0]
    page.goto("/?q=" + word)
    page.wait_for_selector("#allloaded .reportbtn", timeout=15000)
    page.click("#allloaded .reportbtn")
    page.wait_for_selector("#reportdlg.show")
    _agree(page)
    assert "HTTP 500" in page.locator(".reportsum").inner_text()


def test_no_button_on_setup_no_tuner_state(page, fs):
    from app import db
    db.set_setting("plex_url", "")
    db.set_setting("plex_token", "")
    page.goto("/welcome")
    page.wait_for_selector("#wsignin")
    assert page.locator("[data-report]").count() == 0


def test_send_failure_shows_saved_copy(page, fs):
    fs.fail_next(500)
    _open_report_from_settings(page)
    _agree(page)
    page.click("#reportsend")
    page.wait_for_selector("#reportresult")
    assert "Could not send. It is saved and will be sent later." in \
        page.locator("#reportresult").inner_text()
    assert page.locator("#reportid").count() == 1


def test_dialog_error_is_visible_when_server_down(page, fs):
    _open_report_from_settings(page)
    _agree(page)
    page.route("**/api/report/send", lambda r: r.abort())
    page.click("#reportsend")
    page.wait_for_function(
        "document.getElementById('reportmsg').textContent.includes('nothing was sent')")
    assert page.locator("#reportmsg.bad").is_visible()
    assert page.locator("#reportsend").is_enabled(), "it can be tried again"


def test_send_records_consent_on_the_server(page, fs):
    from app import db
    _open_report_from_settings(page)
    assert db.get_setting("report_consent") != "1"
    page.click("#reportsend")
    page.wait_for_selector("#reportid")
    assert db.get_setting("report_consent") == "1"


def test_escape_backdrop_and_never_mind_do_nothing_while_sending(page, fs):
    release = []
    def slow(route):
        release.append(route)           # held until the test lets it go
    page.route("**/api/report/send", slow)
    _open_report_from_settings(page)
    page.click("#reportsend")
    page.wait_for_function("document.getElementById('reportmsg').textContent === 'Sending...'")
    page.keyboard.press("Escape")
    page.mouse.click(5, 5)
    page.click("#reportno")
    assert page.locator("#reportdlg").is_visible()
    release[0].continue_()
    page.wait_for_selector("#reportid")


def test_a_proxy_error_page_is_not_called_unreachable(page, fs):
    _open_report_from_settings(page)
    page.route("**/api/report/send", lambda r: r.fulfill(
        status=502, content_type="text/html", body="<html>Bad Gateway</html>"))
    page.click("#reportsend")
    page.wait_for_function(
        "document.getElementById('reportmsg').textContent.includes('answered with an error')")
    text = page.locator("#reportmsg").inner_text()
    assert "so nothing was sent" in text and "Could not reach" not in text


NASTY = "it's \"bad\" <img src=x onerror=window.__x=1> & more &amp; <b>x</b>"


def _assert_nothing_ran(page):
    assert page.evaluate("typeof window.__x") == "undefined"
    assert page.locator('img[src="x"]').count() == 0
    assert page.locator("#reportbox b").count() == 0


def test_a_failed_booking_error_with_markup_round_trips_and_runs_nothing(page, fs):
    from app import db
    _a_failed_chiefs_booking(page)
    with db.tx() as c:
        c.execute("UPDATE pass_actions SET reason = ? WHERE action = 'failed'", (NASTY,))
    page.goto("/recordings")
    page.wait_for_selector(".agrow.fail", timeout=15000)
    page.locator(".agrow.fail .reportbtn").first.click()
    page.wait_for_selector("#reportdlg.show")
    summary = page.locator(".reportsum").inner_text()
    assert NASTY in summary, summary
    _assert_nothing_ran(page)
    page.click("#reportsend")
    page.wait_for_selector("#reportid")
    assert NASTY in _opened(next(iter(fs.docs.values())))["trigger"]["error"]
    _assert_nothing_ran(page)


def test_the_shared_button_escapes_whatever_it_is_given(page, fs):
    """The guide's error state, the panels and the banners all build their
    button with CEReport.button, so the attribute escaping is tested here."""
    page.route("**/partial/airings*", lambda r: r.fulfill(status=500, body="boom"))
    from app import db
    word = db.one("SELECT title FROM programs LIMIT 1")["title"].split()[0]
    page.goto("/?q=" + word)
    page.wait_for_selector("#allloaded .reportbtn", timeout=15000)
    page.evaluate("""(t) => {
        const d = document.createElement('div'); d.id = 'xss';
        d.innerHTML = CEReport.button({error: t, screen: 'guide'});
        document.body.appendChild(d); }""", NASTY)
    page.click("#xss .reportbtn")
    page.wait_for_selector("#reportdlg.show")
    assert NASTY in page.locator(".reportsum").inner_text()
    _assert_nothing_ran(page)


def test_about_table_does_not_overlap_on_a_phone(phone, fs):
    phone.goto("/settings")
    phone.wait_for_selector("#setnav .nav-item")
    phone.click('.nav-item[data-sec="about"]')
    phone.wait_for_selector("#reportopen")
    rows = phone.eval_on_selector_all(
        'section[data-sec="about"] table.kv tr',
        "els => els.map(r => { const t = [...r.children].map(c => c.getBoundingClientRect());"
        " return {n: t.length, a: t[0], b: t[1]}; })")
    assert len(rows) >= 5
    for r in rows:
        # label sits above its value, so the two boxes never share a line
        assert r["a"]["bottom"] <= r["b"]["top"] + 1, r
        assert r["a"]["width"] > 80, "label is not squeezed"


def test_mobile_layout_dialog_fits(phone, fs):
    _open_report_from_settings(phone)
    _agree(phone)
    box = phone.locator("#reportbox").bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= 390.5
    assert phone.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert phone.evaluate(
        "(() => { const b = document.getElementById('reportbox');"
        " return b.scrollWidth <= b.clientWidth + 1; })()")
    assert phone.locator("#reportsend").is_visible()
