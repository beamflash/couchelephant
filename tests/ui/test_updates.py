"""The update banner: shown to an administrator, with the exact words."""
from app import updates
from app.routes._shared import VERSION

NEWER = ".".join(VERSION.split(".")[:-1] + [str(int(VERSION.split(".")[-1]) + 1)])


def _offer(monkeypatch, click=False):
    rel = updates.Release(NEWER, "https://example.invalid/release", {})
    monkeypatch.setattr(updates, "available", lambda: rel)
    monkeypatch.setattr(updates, "can_click", lambda r: click)
    monkeypatch.setattr(updates, "how", lambda: "docker")


def test_the_banner_says_what_is_available(page, monkeypatch):
    _offer(monkeypatch)
    page.goto("/guide")
    page.wait_for_selector("#updatebar .ub-text")
    assert page.inner_text("#updatebar .ub-text") == f"Update available: {NEWER}"
    assert page.get_attribute("#updatebar a", "href") == "https://example.invalid/release"


def test_no_banner_when_up_to_date(page, monkeypatch):
    monkeypatch.setattr(updates, "available", lambda: None)
    page.goto("/guide")
    page.wait_for_load_state("networkidle")
    assert not page.is_visible("#updatebar")
