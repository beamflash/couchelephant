#!/usr/bin/env python3
"""The promo video on the download page (site/promo.mp4, promo.webm).

Two steps.

    scripts/test.sh --video          record the real web interface (in Docker)
    .venv/bin/python scripts/promo_video.py build    cards, captions, final cut

The recording uses the same invented guide, fake Plex and seeded recordings as
scripts/screenshots.py, so nothing on screen is anybody's television. Each
scene is its own clip in the output folder (guide.webm, sports.webm,
filter.webm, home.webm). The build step needs Playwright's Chromium and ffmpeg
on the host, and the Apple stills described at APPLE below.

A cursor and a click ripple are drawn into the page, because a screen recording
of clicks nobody can see is just a page that changes by itself.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

OUT = os.environ.get("PROMO_OUT", "/out")
W, H = 1072, 603

CURSOR_JS = """
(() => {
  const mk = () => {
    if (document.getElementById('__cur')) return;
    const s = document.createElement('style');
    s.textContent = `
      #__cur{position:fixed;left:0;top:0;width:26px;height:26px;z-index:2147483647;
        pointer-events:none;transform:translate(-100px,-100px);
        filter:drop-shadow(0 2px 3px rgba(0,0,0,.6))}
      .__rip{position:fixed;z-index:2147483646;pointer-events:none;width:14px;height:14px;
        margin:-7px 0 0 -7px;border-radius:50%;border:3px solid #f3a30b;
        background:rgba(243,163,11,.35);animation:__rip .6s ease-out forwards}
      @keyframes __rip{to{transform:scale(4.5);opacity:0}}`;
    document.head.appendChild(s);
    const c = document.createElement('div');
    c.id = '__cur';
    c.innerHTML = '<svg viewBox="0 0 24 24" width="26" height="26"><path d="M4 2l15 9-6.5 1.6L9 19z" fill="#fff" stroke="#111" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    document.body.appendChild(c);
    addEventListener('mousemove', e => {
      c.style.transform = `translate(${e.clientX - 4}px,${e.clientY - 2}px)`;
    }, true);
    addEventListener('mousedown', e => {
      const r = document.createElement('div');
      r.className = '__rip';
      r.style.left = e.clientX + 'px'; r.style.top = e.clientY + 'px';
      document.body.appendChild(r);
      setTimeout(() => r.remove(), 700);
    }, true);
  };
  if (document.body) mk(); else addEventListener('DOMContentLoaded', mk);
})();
"""


class Actor:
    """Moves a visible mouse the way a person would, then clicks."""

    def __init__(self, page):
        self.page = page
        self.x, self.y = W - 120, H - 80
        page.mouse.move(self.x, self.y)

    def glide(self, x, y, steps=28):
        self.page.mouse.move(x, y, steps=steps)
        self.x, self.y = x, y

    def click(self, selector, pause=500):
        loc = self.page.locator(selector).first
        loc.scroll_into_view_if_needed()
        box = loc.bounding_box()
        self.glide(box["x"] + min(box["width"] / 2, 120), box["y"] + box["height"] / 2)
        self.page.wait_for_timeout(250)
        self.page.mouse.click(self.x, self.y)
        self.page.wait_for_timeout(pause)

    def wheel(self, dx, dy, total, per=60, gap=22):
        done = 0
        while done < total:
            self.page.mouse.wheel(dx, dy)
            self.page.wait_for_timeout(gap)
            done += per


def clip(browser, base, name, work):
    ctx = browser.new_context(
        base_url=base, viewport={"width": W, "height": H}, timezone_id="UTC",
        color_scheme="dark", record_video_dir=work,
        record_video_size={"width": W, "height": H})
    ctx.add_init_script(CURSOR_JS)
    page = ctx.new_page()
    return ctx, page


def save(ctx, page, name, work):
    video = page.video
    ctx.close()
    shutil.move(video.path(), os.path.join(OUT, name + ".webm"))
    print(f"  {name}.webm")


def record():
    import screenshots as S  # sets the scratch environment on import
    from playwright.sync_api import sync_playwright

    from tests import fake_plex, isolation

    plex_url, stop_plex = fake_plex.start()
    os.environ["COUCHELEPHANT_TEST_PLEX"] = plex_url
    isolation.assert_isolated()
    base, server = S.start_app(plex_url)
    S.seed_recordings()
    os.makedirs(OUT, exist_ok=True)
    work = os.path.join(S.SCRATCH, "video")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])

        # ---- the guide: scroll, open a game, record it ----
        ctx, page = clip(browser, base, "guide", work)
        page.goto("/guide")
        page.wait_for_selector(".gprog", timeout=20000)
        page.wait_for_timeout(900)
        a = Actor(page)
        a.glide(500, 300)
        a.wheel(0, 1, 420)
        page.wait_for_timeout(500)
        a.wheel(0, -1, 420)
        page.wait_for_timeout(300)
        a.click('.gprog.live[title*="Chiefs at Buccaneers"]', pause=1500)
        page.wait_for_selector("#ovlbox .air", timeout=20000)
        page.wait_for_timeout(1400)
        page.click("#ovlx")
        page.wait_for_timeout(500)
        a.click('.gprog[title*="The Long Way Home"]', pause=900)
        page.wait_for_selector("#ovlbox .air [data-rec]", timeout=20000)
        a.click("#ovlbox .air [data-rec]", pause=1200)
        page.wait_for_selector("#optgo", timeout=20000)
        page.wait_for_timeout(1600)
        save(ctx, page, "guide", work)

        # ---- a sports pass: follow a team ----
        ctx, page = clip(browser, base, "sports", work)
        page.goto("/recordings")
        page.wait_for_selector(".agrow", timeout=20000)
        a = Actor(page)
        page.wait_for_timeout(500)
        a.click('.subtab[data-sub="sports"]', pause=900)
        page.wait_for_selector(".passrow", timeout=20000)
        a.click(".passrow .pmeta", pause=1500)
        a.click("#addrule", pause=900)
        page.wait_for_selector("#rlist > *", timeout=20000)
        page.wait_for_timeout(700)
        a.click("#rq", pause=300)
        page.keyboard.type("Chief", delay=130)
        page.wait_for_timeout(900)
        a.click('#rlist [data-team]', pause=1800)
        save(ctx, page, "sports", work)

        # ---- the smart filter builder ----
        ctx, page = clip(browser, base, "filter", work)
        page.goto("/recordings")
        page.wait_for_selector(".agrow", timeout=20000)
        a = Actor(page)
        page.wait_for_timeout(400)
        a.click('.subtab[data-sub="sports"]', pause=600)
        a.click("#addrule", pause=900)
        page.wait_for_selector("#rlist > *", timeout=20000)
        a.click('input[name=rsub][value="filter"]', pause=900)
        page.wait_for_selector("#sfroot .sfrow", timeout=20000)
        a.click("#sfroot .sffield", pause=300)
        page.select_option("#sfroot .sffield", "genre")
        page.wait_for_selector("select.sfval", timeout=20000)
        page.wait_for_timeout(500)
        page.select_option("select.sfval", "Football")
        page.wait_for_function(
            "() => /programme/.test(document.getElementById('sfcount').textContent)",
            timeout=25000)
        page.wait_for_timeout(2200)
        save(ctx, page, "filter", work)

        # ---- Home: a recording in progress, from the start or join live ----
        ctx, page = clip(browser, base, "home", work)
        page.goto("/home")
        page.wait_for_selector(".tvhead", timeout=20000)
        page.wait_for_selector('[data-at="start"]', timeout=20000)
        # Bigger, so the card reads at the size the video is shown. The player
        # is not recorded: against the demo's fake ffmpeg it has nothing to play,
        # and promo_build.sh cuts to the app's own player right after the click.
        page.add_style_tag(content="body{zoom:1.2}")
        page.evaluate("window.scrollTo(0, 190)")
        a = Actor(page)
        page.wait_for_timeout(1500)
        a.glide(700, 330)
        loc = page.locator('[data-at="live"]').first
        box = loc.bounding_box()
        a.glide(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.wait_for_timeout(700)
        a.click('[data-at="start"]', pause=150)
        save(ctx, page, "home", work)

        browser.close()

    server.should_exit = True
    stop_plex()
    shutil.rmtree(S.SCRATCH, ignore_errors=True)
    print("done")




# ---------------------------------------------------------------------------
# The cut. Everything below runs on the host, not in the container.
# ---------------------------------------------------------------------------

WORK = os.environ.get("PROMO_WORK", "/tmp/claude-1000/promo")
SITE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "site")
# APPLE: stills exported from the XCUITest runs in the funtimes VM with
#   xcrun xcresulttool export attachments --path <xcresult> --output-path <dir>
# ipad-guide.png (the "guide at start" shot) and ipad-player.png (the player's
# "controls 1" shot). Put them, or copies, in this folder.
APPLE = os.environ.get("PROMO_APPLE", os.path.join(WORK, "apple"))
# PLACEHOLDER for the owner's own Apple TV footage. The simulator can only play
# a test pattern for live video, so the Apple scene ("c_apple" in cards()) uses
# still screenshots of the iPad app, where the pattern is a small part of the
# picture. To use real TV footage, make c_apple a footage scene: add a row to
# SHOTS with the clip, and put "c_apple" back as "s_apple" in ORDER.

FPS = 30
XF = 0.4            # crossfade between scenes, seconds
BG = "#040c15"
FX, FY, FW, FH = 104, 96, 1072, 603   # where the footage frame sits

CSS = """
:root{--bg:#040c15;--panel:#0b1621;--panel2:#12202d;--line:#1d2c3a;--text:#f2ede6;
  --dim:#9aa3ab;--accent:#f3a30b}
*{box-sizing:border-box}
html,body{margin:0;width:1280px;height:720px;overflow:hidden}
body{background:radial-gradient(ellipse at 50% 38%,#0f2133 0%,#071220 48%,#040c15 100%);
  color:var(--text);font:600 30px/1.25 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center}
[data-a]{animation-fill-mode:both;animation-timing-function:cubic-bezier(.2,.8,.2,1)}
@keyframes up{from{opacity:0;transform:translateY(34px)}to{opacity:1;transform:none}}
@keyframes pop{from{opacity:0;transform:scale(.6)}to{opacity:1;transform:none}}
@keyframes fade{from{opacity:0}to{opacity:1}}
@keyframes drift{from{transform:scale(1)}to{transform:scale(1.06)}}
@keyframes grow{from{width:0}to{width:var(--w)}}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.25}}
.drift{animation:drift 8s linear both}
.big{font-size:86px;font-weight:800;letter-spacing:-1px}
.amber{color:var(--accent)}
.dim{color:var(--dim);font-weight:500}
.num{width:132px;height:132px;border-radius:50%;background:var(--accent);color:#1a1a1a;
  font-size:84px;font-weight:800;display:flex;align-items:center;justify-content:center}
.row{display:flex;gap:18px;align-items:center;justify-content:center}
.chip{background:var(--panel2);border:1px solid var(--line);border-radius:14px;
  padding:14px 28px;font-size:30px;font-weight:700}
.chip.hot{background:var(--accent);color:#1a1a1a;border-color:var(--accent)}
.mono{font:600 25px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:#0b1621;
  border:1px solid var(--line);border-radius:12px;padding:16px 26px;color:#f2ede6}
.mono i{color:var(--accent);font-style:normal}
"""


def _merge_styles(html):
    """An element may carry an animation (au) and its own style. HTML keeps only
    the first style attribute, so fold them into one."""
    import re

    def fix(m):
        tag = m.group(0)
        styles = re.findall(r'\sstyle="([^"]*)"', tag)
        if len(styles) < 2:
            return tag
        tag = re.sub(r'\sstyle="[^"]*"', "", tag)
        joined = ";".join(x.rstrip(";") for x in styles)
        return tag[:-1] + f' style="{joined}">'
    return re.sub(r"<[a-zA-Z][^<>]*>", fix, html)


def page(body, extra_css=""):
    return (f"<!doctype html><meta charset=utf-8><style>{CSS}{extra_css}</style>"
            f"<body>{_merge_styles(body)}</body>")


def au(cls_delay):
    """style attribute for an animated element: name duration delay."""
    name, dur, delay = cls_delay
    return f'data-a style="animation-name:{name};animation-duration:{dur}s;animation-delay:{delay}s"'


def cards():
    """Every card: (file stem, seconds, html). Frames are drawn at fixed times,
    not recorded, so the timing is exact and nothing depends on the machine."""
    mark = "file://" + os.path.join(SITE, "mark.png")
    apple = "file://" + APPLE
    out = []

    out.append(("c_title", 3.0, page(f'''
      <img {au(("pop", .7, .1))} src="{mark}" style="width:300px;margin-bottom:14px">
      <div {au(("up", .7, .45))} class="big">Couch<span class="amber">Elephant</span></div>
      <div {au(("up", .7, .85))} style="font-size:44px;margin-top:22px">A CouchElephant never forgets.</div>
      <div {au(("up", .7, 1.25))} class="dim" style="font-size:30px;margin-top:14px">It turns your Plex DVR into a full-featured DVR.</div>
    ''')))

    out.append(("c_install", 2.4, page(f'''
      <div {au(("pop", .5, 0))} class="row"><div class="num">1</div>
        <div class="big" style="font-size:96px">Install</div></div>
      <div class="row" style="margin-top:46px">
        {"".join(f'<div {au(("pop", .45, .35 + i * .12))} class="chip">{n}</div>' for i, n in enumerate(["Windows", "macOS", "Linux", "Docker", "Unraid"]))}
      </div>
      <div {au(("up", .6, 1.15))} class="mono" style="margin-top:34px">curl -fsSL couchelephant.app/install.sh | <i>sudo sh</i></div>
    ''')))

    boxes = "".join(
        f'<div {au(("pop", .35, .95 + i * .22))} class="chip hot" style="width:84px;height:100px;'
        f'font-size:56px;padding:0;display:flex;align-items:center;justify-content:center">{c}</div>'
        for i, c in enumerate("7KQ2"))
    out.append(("c_signin", 2.4, page(f'''
      <div {au(("pop", .5, 0))} class="row"><div class="num">2</div>
        <div class="big" style="font-size:80px">Sign in with <span class="amber">Plex</span></div></div>
      <div {au(("up", .5, .5))} class="chip hot" style="margin-top:44px;font-size:34px;padding:18px 44px">Sign in with Plex</div>
      <div class="row" style="margin-top:36px;gap:14px">{boxes}</div>
      <div {au(("fade", .5, 1.9))} class="dim" style="margin-top:20px;font-size:26px">Type the code at plex.tv/link</div>
    ''')))

    out.append(("c_watch", 2.8, page(f'''
      <div {au(("pop", .5, 0))} class="row"><div class="num">3</div>
        <div class="big" style="font-size:96px">Watch</div></div>
      <div {au(("up", .6, .4))} style="margin-top:30px;font-size:36px">Your apps find the server by themselves.</div>
      <div class="row" style="margin-top:34px">
        {"".join(f'<div {au(("pop", .45, .8 + i * .15))} class="chip">{n}</div>' for i, n in enumerate(["iPhone", "iPad", "Apple TV"]))}
      </div>
      <div {au(("up", .5, 1.5))} class="mono" style="margin-top:30px;font-size:26px">CouchElephant <i>found</i></div>
    ''')))

    # The buffer: the point of "watch while it records". A bar that fills as the
    # recording grows, a "start" mark at the left, "live" at the right edge.
    shot = f"{apple}/ipad-player.png"
    out.append(("c_player", 3.8, page(f'''
      <div style="position:absolute;left:0;top:0;width:1280px;height:84px;display:flex;align-items:center;justify-content:center;font-size:40px;font-weight:800">
        <span {au(("up", .5, .2))}>Start from the <span class="amber">beginning</span>, or join <span class="amber">live</span></span></div>
      <div {au(("pop", .7, .1))} style="position:absolute;left:96px;top:112px;width:470px;height:590px;border-radius:34px;
        border:9px solid #1b2733;background:#000;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.6)">
        <img src="{shot}" style="width:520px;margin:-80px 0 0 -25px;display:block"></div>
      <div style="position:absolute;left:640px;top:200px;width:540px;text-align:left">
        <div {au(("fade", .5, .5))} class="dim" style="font-size:24px">Chiefs at Buccaneers</div>
        <div {au(("fade", .5, .5))} style="font-size:30px;margin-bottom:26px">Recording now</div>
        <div style="position:relative;height:22px;border-radius:11px;background:var(--panel2);border:1px solid var(--line)">
          <div data-a style="--w:100%;position:absolute;left:0;top:0;bottom:0;border-radius:11px;
            background:linear-gradient(90deg,#8a5a05,var(--accent));animation-name:grow;animation-duration:3.2s;animation-delay:.5s;animation-timing-function:linear"></div></div>
        <div style="position:relative;height:150px">
          <div {au(("up", .5, 1.0))} style="position:absolute;left:0;top:18px"><div class="chip hot" style="font-size:25px;padding:10px 20px">Start from beginning</div>
            <div class="dim" style="font-size:22px;margin-top:8px">the whole game, from the first play</div></div>
          <div {au(("up", .5, 1.8))} style="position:absolute;right:0;top:18px;text-align:right"><div class="chip" style="font-size:25px;padding:10px 20px;border-color:var(--accent)">Join live</div>
            <div class="dim" style="font-size:22px;margin-top:8px">right where it is now</div></div>
        </div>
        <div {au(("fade", .5, 2.4))} class="dim" style="font-size:26px;margin-top:20px">It keeps recording while you watch.</div>
      </div>
    ''')))

    out.append(("c_apple", 3.6, page(f'''
      <div style="position:absolute;left:0;top:0;width:1280px;height:84px;display:flex;align-items:center;justify-content:center;font-size:40px;font-weight:800">
        <span {au(("up", .5, .2))}>Live TV on <span class="amber">Apple TV</span>, iPhone and iPad</span></div>
      <div {au(("up", .7, .15))} style="position:absolute;left:96px;top:128px;width:760px;height:410px;border-radius:26px;
        border:9px solid #1b2733;background:#000;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.6)">
        <img class="drift" src="{apple}/ipad-guide.png" style="width:760px;margin:-34px 0 0 -4px;display:block"></div>
      <div {au(("up", .7, .4))} style="position:absolute;left:910px;top:100px;width:274px;height:594px;border-radius:30px;
        border:9px solid #1b2733;background:#000;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.6)">
        <img src="{apple}/ipad-player.png" style="width:640px;margin:-212px 0 0 -183px;display:block"></div>
      <div class="row" style="position:absolute;left:96px;top:580px;width:760px;gap:14px">
        {"".join(f'<div {au(("pop", .4, 1.0 + i * .15))} class="chip" style="font-size:24px;padding:10px 22px">{n}</div>' for i, n in enumerate(["Guide over live TV", "Pause buffer", "Top Shelf"]))}
      </div>
    ''')))

    out.append(("c_extras", 3.0, page(f'''
      <div {au(("up", .5, 0))} class="big" style="font-size:64px">And <span class="amber">more</span></div>
      <div class="row" style="margin-top:44px;flex-wrap:wrap;max-width:1000px;gap:18px">
        {"".join(f'<div {au(("pop", .45, .35 + i * .16))} class="chip" style="font-size:30px">{n}</div>' for i, n in enumerate(["Skips the ads", "Season passes", "Series not in the guide yet", "Games not in the guide yet", "Pause buffer on live TV"]))}
      </div>
    ''')))

    out.append(("c_end", 3.4, page(f'''
      <img {au(("pop", .7, .1))} src="{mark}" style="width:240px;margin-bottom:10px">
      <div {au(("up", .7, .4))} class="big" style="font-size:66px">A CouchElephant<br><span class="amber">never forgets.</span></div>
      <div {au(("up", .7, .9))} class="chip hot" style="margin-top:34px;font-size:38px;padding:16px 40px">couchelephant.app</div>
    ''')))
    return out


def caption_html(text):
    marked = text.replace("*", "\0")
    parts = marked.split("\0")
    h = "".join(f'<span class="amber">{p}</span>' if i % 2 else p for i, p in enumerate(parts))
    return page(f'<div style="position:absolute;top:0;left:0;width:1280px;height:84px;display:flex;'
                f'align-items:center;justify-content:center;font-size:40px;font-weight:800"><div>{h}</div></div>',
                "html,body{background:transparent!important}")


MASK = page(f'''<div style="position:absolute;left:{FX}px;top:{FY}px;width:{FW}px;height:{FH}px;
  border-radius:18px;box-shadow:0 0 0 2px rgba(243,163,11,.45),0 0 0 2000px {BG}"></div>''',
            "html,body{background:transparent!important}")

# Footage scenes: (name, clip, start, length of source, speed, caption)
SHOTS = [
    ("s_guide", "guide", 0.8, 4.4, 1.0, "A guide that *stays put*"),
    ("s_live", "guide", 6.1, 4.3, 1.1, "Records the *live game*, not the repeat"),
    ("s_sports", "sports", 4.6, 7.6, 1.5, "Follow any of *1,300 teams*"),
    ("s_filter", "filter", 3.4, 8.6, 2.0, "Smart filters: *and, or, not*"),
    ("s_home", "home", 0.9, 3.6, 1.0, "*Watch* while it records"),
]
# The order of the final cut.
ORDER = ["c_title", "c_install", "c_signin", "c_watch", "s_guide", "s_live", "s_sports",
         "s_filter", "s_home", "c_player", "c_apple", "c_extras", "c_end"]


def sh(*cmd):
    import subprocess
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"{' '.join(cmd[:3])} failed:\n{r.stderr[-1500:]}")


def build():
    import time

    from playwright.sync_api import sync_playwright

    os.makedirs(WORK, exist_ok=True)
    scenes = os.path.join(WORK, "scenes")
    shutil.rmtree(scenes, ignore_errors=True)
    os.makedirs(scenes)
    lens = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox", "--allow-file-access-from-files"])
        pg = browser.new_page(viewport={"width": 1280, "height": 720})

        def load(html, name):
            f = os.path.join(scenes, name + ".html")
            with open(f, "w") as fh:
                fh.write(html)
            pg.goto("file://" + f)
            pg.wait_for_timeout(250)
            return f

        # Frames of a card, one screenshot per 1/30 s, animations set to a time.
        for name, secs, html in cards():
            load(html, name)
            d = os.path.join(scenes, name)
            os.makedirs(d)
            n = round(secs * FPS)
            pg.evaluate("document.getAnimations().forEach(a => a.pause())")
            for i in range(n):
                pg.evaluate(f"document.getAnimations().forEach(a => a.currentTime = {i * 1000 / FPS})")
                pg.screenshot(path=f"{d}/{i:04d}.png")
            sh("ffmpeg", "-v", "error", "-y", "-framerate", str(FPS), "-i", f"{d}/%04d.png",
               "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", f"{scenes}/{name}.mp4")
            shutil.rmtree(d)
            lens[name] = secs
            print("  card", name)

        # Frame mask and captions (transparent PNGs)
        load(MASK, "mask")
        pg.screenshot(path=f"{scenes}/mask.png", omit_background=True)
        for name, _c, _s, _l, _sp, cap in SHOTS:
            load(caption_html(cap), "cap_" + name)
            pg.screenshot(path=f"{scenes}/cap_{name}.png", omit_background=True)
        browser.close()

    for name, clipname, ss, ln, speed, _cap in SHOTS:
        dur = ln / speed
        lens[name] = dur
        clip_path = os.path.join(WORK, "web", clipname + ".webm")
        fade_out = max(0.0, dur - 0.45)
        slide = "-18*max(0,1-max(0,t-0.25)/0.35)"
        graph = (
            f"color=c={BG}:s=1280x720:r={FPS},format=yuv420p[b];"
            f"[0:v]setpts=(PTS-STARTPTS)/{speed},fps={FPS},scale={FW}:{FH},format=yuv420p[v];"
            f"[b][v]overlay={FX}:{FY}:shortest=1[a];"
            f"[1:v]format=rgba[m];[a][m]overlay=0:0[a2];"
            f"[2:v]format=rgba,fade=t=in:st=0.25:d=0.35:alpha=1,fade=t=out:st={fade_out}:d=0.3:alpha=1[c];"
            f"[a2][c]overlay=0:y='{slide}':eval=frame:shortest=1,format=yuv420p")
        sh("ffmpeg", "-v", "error", "-y", "-ss", str(ss), "-t", str(ln), "-i", clip_path,
           "-loop", "1", "-framerate", str(FPS), "-i", f"{scenes}/mask.png",
           "-loop", "1", "-framerate", str(FPS), "-i", f"{scenes}/cap_{name}.png",
           "-filter_complex", graph, "-t", f"{dur:.3f}", "-c:v", "libx264", "-crf", "12",
           "-pix_fmt", "yuv420p", f"{scenes}/{name}.mp4")
        print("  shot", name, f"{dur:.1f}s")

    # Crossfade chain. Alternating fade and slides keep it lively but calm.
    kinds = ["fade", "slideleft", "fade", "smoothleft", "fade", "fade", "slideleft", "fade",
             "smoothleft", "fade", "slideleft", "fade"]
    ins, chain = [], ""
    for n in ORDER:
        ins += ["-i", f"{scenes}/{n}.mp4"]
    last = "[0:v]"
    t = lens[ORDER[0]]
    for i in range(1, len(ORDER)):
        off = t - XF
        tag = f"[x{i}]"
        chain += f"{last}[{i}:v]xfade=transition={kinds[i - 1]}:duration={XF}:offset={off:.3f}{tag};"
        last = tag
        t = off + lens[ORDER[i]]
    chain = chain.rstrip(";")
    total = t
    master = os.path.join(WORK, "master.mp4")
    sh("ffmpeg", "-v", "error", "-y", *ins, "-filter_complex", chain, "-map", last,
       "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p", "-r", str(FPS), master)
    print(f"  master {total:.1f}s")

    # Final files. Silent: no audio stream at all.
    mp4 = os.path.join(SITE, "promo.mp4")
    sh("ffmpeg", "-v", "error", "-y", "-i", master, "-an", "-c:v", "libx264", "-preset", "slow",
       "-crf", os.environ.get("PROMO_CRF", "21"), "-profile:v", "high", "-pix_fmt", "yuv420p",
       "-movflags", "+faststart", mp4)
    webm = os.path.join(SITE, "promo.webm")
    sh("ffmpeg", "-v", "error", "-y", "-i", master, "-an", "-c:v", "libvpx-vp9", "-b:v", "0",
       "-crf", os.environ.get("PROMO_VP9_CRF", "33"), "-row-mt", "1", "-pix_fmt", "yuv420p", webm)
    poster_at = float(os.environ.get("PROMO_POSTER_AT", "21"))
    sh("ffmpeg", "-v", "error", "-y", "-ss", str(poster_at), "-i", master, "-frames:v", "1",
       "-q:v", "3", os.path.join(SITE, "promo-poster.jpg"))
    for f in ("promo.mp4", "promo.webm", "promo-poster.jpg"):
        print(f, os.path.getsize(os.path.join(SITE, f)))
    time.sleep(0)


if __name__ == "__main__":
    {"build": build}.get(sys.argv[1] if len(sys.argv) > 1 else "", record)()
