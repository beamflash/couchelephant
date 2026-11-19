"""The download page at couchelephant.app (site/index.html).

It reads the latest release from the GitHub API in the browser, so the tests
open the file directly and answer that one request with a fixture.
"""
import json
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[2] / "site" / "index.html").as_uri()
BASE = "https://github.com/datbird/couchelephant/releases/download/v9.9.9/"

NAMES = [
    "CouchElephant-Setup-9.9.9.exe",
    "CouchElephant-9.9.9-macos-arm64.dmg",
    "CouchElephant-9.9.9-macos-x64.dmg",
    "couchelephant_9.9.9_amd64.deb",
    "couchelephant_9.9.9_arm64.deb",
    "couchelephant-9.9.9-1.x86_64.rpm",
    "couchelephant-9.9.9-1.aarch64.rpm",
    "couchelephant-9.9.9-linux-x64.tar.gz",
    "couchelephant-9.9.9-linux-arm64.tar.gz",
    "SHA256SUMS",
]

FULL = {"tag_name": "v9.9.9",
        "assets": [{"name": n, "browser_download_url": BASE + n} for n in NAMES]}
OLD = {"tag_name": "v1.5.0", "assets": []}

WIN = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/130 Safari/537.36"
LINUX = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
PHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1"


def open_site(browser, ua, release=FULL, arch=None, status=200):
    ctx = browser.new_context(user_agent=ua)
    if arch:
        # Chrome reports the processor through this hint; the user agent cannot.
        ctx.add_init_script(
            "Object.defineProperty(navigator,'userAgentData',{value:{"
            f"getHighEntropyValues:()=>Promise.resolve({{architecture:{json.dumps(arch)}}})}}}})")
    ctx.route("https://api.github.com/**", lambda r: r.fulfill(
        status=status, content_type="application/json", body=json.dumps(release)))
    page = ctx.new_page()
    page.goto(PAGE)
    return ctx, page


def hero(page):
    page.wait_for_selector("#hero-button:not([hidden])")
    b = page.locator("#hero-button")
    return b.inner_text(), b.get_attribute("href")


def test_windows(browser):
    ctx, page = open_site(browser, WIN)
    text, href = hero(page)
    assert "Windows" in text
    assert href == BASE + "CouchElephant-Setup-9.9.9.exe"
    ctx.close()


def test_mac_apple_silicon(browser):
    ctx, page = open_site(browser, MAC, arch="arm")
    text, href = hero(page)
    assert "macOS" in text
    assert href == BASE + "CouchElephant-9.9.9-macos-arm64.dmg"
    assert "right-click CouchElephant and choose Open." in page.locator("#hero-note").inner_text()
    ctx.close()


def test_mac_intel(browser):
    ctx, page = open_site(browser, MAC, arch="x86")
    text, href = hero(page)
    assert "macOS" in text
    assert href == BASE + "CouchElephant-9.9.9-macos-x64.dmg"
    ctx.close()


def test_linux(browser):
    ctx, page = open_site(browser, LINUX)
    text, href = hero(page)
    assert "Linux" in text
    assert href == BASE + "couchelephant_9.9.9_amd64.deb"
    others = page.locator("#others-list a").evaluate_all("els => els.map(e => e.href)")
    assert BASE + "couchelephant-9.9.9-1.x86_64.rpm" in others
    assert BASE + "CouchElephant-Setup-9.9.9.exe" in others
    ctx.close()


def test_no_installer_yet_leads_with_docker(browser):
    ctx, page = open_site(browser, WIN, release=OLD)
    page.wait_for_selector("#hero-missing:not([hidden])")
    assert ("The download for Windows arrives with the next release."
            in page.locator("#hero-missing").inner_text())
    assert page.locator("#hero-button").is_hidden()
    # Docker sits right after the hero, ahead of the Linux one-liner.
    ids = page.locator("main > section").evaluate_all("els => els.map(e => e.id)")
    assert ids.index("docker") == ids.index("hero") + 1
    ctx.close()


def test_hostile_download_urls_are_never_linked(browser):
    evil = {"tag_name": "v9.9.9", "assets": [
        {"name": "CouchElephant-Setup-9.9.9.exe", "browser_download_url": "javascript:alert(1)"},
        {"name": "CouchElephant-9.9.9-macos-arm64.dmg",
         "browser_download_url": "https://example.invalid/CouchElephant-9.9.9-macos-arm64.dmg"}]}
    ctx, page = open_site(browser, WIN, release=evil)
    page.wait_for_selector("#hero-missing:not([hidden])")
    assert ("The download for Windows arrives with the next release."
            in page.locator("#hero-missing").inner_text())
    assert page.locator("#hero-button").is_hidden()
    assert page.locator("#others").is_hidden()
    ctx.close()


def test_feed_failure_links_the_releases_page(browser):
    ctx, page = open_site(browser, WIN, status=500)
    text, href = hero(page)
    assert href == "https://github.com/datbird/couchelephant/releases/latest"
    ctx.close()


def test_no_script_points_at_every_download(browser):
    ctx = browser.new_context(user_agent=WIN, java_script_enabled=False)
    page = ctx.new_page()
    page.goto(PAGE)
    link = page.locator("noscript a[href='https://github.com/datbird/couchelephant/releases/latest']")
    assert link.count() == 1
    assert page.locator("#hero-button").is_hidden()
    assert "right-click CouchElephant and choose Open." in page.locator("noscript").inner_text()
    assert "curl -fsSL https://couchelephant.app/install.sh" in page.content()
    ctx.close()
