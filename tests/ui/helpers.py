"""Shared steps for the browser tests."""


def open_plex_settings(page):
    """The Plex settings section starts closed. Click its header, as a person would."""
    page.wait_for_selector("#sec-plex > summary", timeout=20000)
    if page.locator("#sec-plex").get_attribute("open") is None:
        page.click("#sec-plex > summary")
