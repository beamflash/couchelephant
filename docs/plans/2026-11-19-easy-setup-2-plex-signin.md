# Easy setup, part 2: Sign in with Plex - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The owner connects CouchElephant to Plex with one button and a plex.tv/link code, and the recorder's library folders are suggested from Plex for the owner to confirm.

**Architecture:** A new `app/plextv.py` talks to plex.tv (PIN, resources). Three routes under `/settings/plex/signin` drive it from the welcome page and Settings, Plex. The chosen server is saved through the same test-then-save path `welcome_save` uses today. A new suggestion route reads library locations from Plex and checks each is writable; the recorder settings form shows them filled in.

**Tech Stack:** Python 3.12, FastAPI, httpx (existing), Jinja2 templates, Playwright tests.

**Spec:** `docs/specs/2026-11-19-easy-setup.md` (part 2, and the "Library folders are suggested, never assumed" call).

## Global Constraints

- The token is stored where it is today (setting `plex_token`), never logged, never sent back to a page.
- "Enter it yourself" (address and token) stays available on the welcome page and in Settings, Plex.
- plex.tv is called with `X-Plex-Product: CouchElephant`, `X-Plex-Client-Identifier: <paths.install_id()>`, `X-Plex-Version: <VERSION>`, `Accept: application/json`. No other identifying data.
- Nothing is written into a Plex library folder until the owner confirms the suggested folders.
- No emoji or em dashes; "TV", "Movies", "series".

## Review Focus

- **The owner never approves the code** (closes the tab): polling stops after the PIN expires (plex.tv says so, about 15 minutes) and the page offers a new code. Test in Task 2.
- **The account has two Plex servers, or one with no DVR**: the owner picks by name; a server with no DVR says "Add a tuner to Plex first", as `_test_plex` does now. Test in Task 2.
- **Plex's local address does not work from where CouchElephant runs** (Docker bridge, another subnet): the next connection Plex reports is tried, and if none answer the error names the addresses tried. Test in Task 2.
- **A Plex library folder path from a different machine** (Plex on a NAS, CouchElephant on a PC): it shows as not writable here, with the reason, and nothing is saved for it. Test in Task 3.
- **plex.tv down or slow**: the button says plex.tv did not answer and offers "Enter it yourself"; nothing hangs longer than 10 seconds. Test in Task 1.

---

### Task 1: plex.tv client

**Files:**
- Create: `app/plextv.py`, `tests/fake_plextv.py`
- Test: `tests/test_plextv.py`

**Interfaces:**
- Consumes: `paths.install_id()` (part 1, Task 1; if part 1 is not done yet, add `install_id()` to `app/paths.py` as specified there).
- Produces: `plextv.BASE` (from env `COUCHELEPHANT_PLEXTV_URL`, default `https://plex.tv`); `create_pin() -> Pin` (`Pin(id: int, code: str, expires_at: int)`); `check_pin(pin_id: int) -> str | None` (the token once approved); `servers(token: str) -> list[PlexServer]` (`PlexServer(name: str, client_id: str, access_token: str, connections: list[Connection])`, `Connection(uri: str, local: bool, relay: bool)`); `PlexTvError(Exception)` with a message fit for the page.

- [ ] **Step 1: Failing tests** against `fake_plextv.py` (a small Starlette app served with `httpx.MockTransport`): `create_pin` posts to `/api/v2/pins` with `strong=false` and the headers above, and returns the four-character code; `check_pin` returns None while `authToken` is null and the token after; `servers` returns only resources whose `provides` includes `server`, local connections first, relay connections last; a 10 s timeout raises `PlexTvError("plex.tv did not answer.")`; no test output or log line contains the token (assert with `caplog`).
- [ ] **Step 2: Run, expect failure.** `pytest tests/test_plextv.py -q`
- [ ] **Step 3: Implement** with httpx, timeout 10 s.
- [ ] **Step 4: Run, expect pass.**
- [ ] **Step 5: Commit** `git commit -m "plex.tv client: PIN and servers"`

### Task 2: Sign in with Plex on the welcome page and in Settings

**Files:**
- Modify: `app/routes/account.py` (welcome), `app/routes/settings.py` (new routes beside `settings_save`), `app/templates/welcome.html`, `app/templates/_settings.html`
- Test: `tests/test_plex_signin.py`, `tests/ui/test_plex_signin.py`

**Interfaces:**
- Consumes: Task 1; `_test_plex(url, token)` in `app/routes/settings.py:254`.
- Produces: `POST /settings/plex/signin` -> `{"ok": true, "pin": int, "code": str, "link": "https://plex.tv/link"}`; `GET /settings/plex/signin/{pin}` -> `{"ok": true, "state": "waiting"}` or `{"ok": true, "state": "servers", "servers": [{"name": str, "id": str}]}` or `{"ok": false, "state": "expired"}`; `POST /settings/plex/signin/{pin}/use` with form `server` -> `{"ok": true, "detail": str, "address": str}` or `{"ok": false, "detail": str, "tried": [str]}`. The token waiting between steps is held in memory keyed by pin, for 20 minutes, never in a response.

- [ ] **Step 1: Failing API tests**: the whole flow against the fake plex.tv and the existing `fake_plex` (a DVR server): start, poll waiting, approve in the fake, poll lists one server, use saves `plex_url` (the first connection that passes `_test_plex`) and `plex_token`, sets `dry_run` to 1 and starts a sync as `welcome_save` does; two servers listed by name; a server with no DVR gives the `_test_plex` message; all connections failing gives `tried` with each address; an expired pin gives `state: expired`; these routes are administrator-only with sign-in on (they sit under `/settings`) and open on first run (the `_first_run` middleware already lets `/settings` through).
- [ ] **Step 2: Failing Playwright test**: on a fresh install the welcome page shows one button "Sign in with Plex" and a link "Enter it yourself"; pressing it shows the four-letter code and a link to plex.tv/link; the fake approves; the page lands on the guide.
- [ ] **Step 3: Run, expect failure.**
- [ ] **Step 4: Implement** the routes and pages. The welcome page keeps the time zone choice, filled from the browser's zone.
- [ ] **Step 5: Run both suites, expect pass.**
- [ ] **Step 6: Commit** `git commit -m "Sign in with Plex"`

### Task 3: Library folders suggested from Plex

**Files:**
- Modify: `app/routes/settings.py` (beside `recorder_status`), `app/handover.py:26,49-55,161-163`, `app/templates/_settings.html` (recorder section)
- Test: `tests/test_library_suggest.py`, `tests/ui/test_recorder_settings.py`

**Interfaces:**
- Consumes: `paths.library_dir()` (None outside a container), `plex.library_sections()` (existing, `handover.py:130`).
- Produces: `GET /api/recorder/library-suggest` -> `{"ok": true, "suggestions": [{"kind": "shows" | "movies", "section_id": str, "section": str, "path": str, "writable": bool, "why": str}]}`; handover accepts an absolute `recorder_shows_folder` / `recorder_movies_folder` when `paths.library_dir()` is None.

- [ ] **Step 1: Failing tests**: with the fake Plex reporting a TV section at a temp folder and a Movies section at a missing path, the route returns the TV one `writable: true` and the Movies one `writable: false, why: "That folder does not exist on this computer."`; the check leaves no file behind; inside a container the paths are shown relative to `/library` as today; handover moves a finished recording into an absolute folder when `library_dir()` is None and refuses a relative one.
- [ ] **Step 2: Failing Playwright test**: Settings, Recording shows the suggestions filled in with "Use these folders"; nothing is saved until it is pressed; pressing it posts the existing `/settings/recorder` form with the folders and section ids.
- [ ] **Step 3: Run, expect failure.**
- [ ] **Step 4: Implement.** The writable check makes and removes `tempfile.mkstemp(dir=path, prefix=".couchelephant-check-")`.
- [ ] **Step 5: Run both suites, expect pass.**
- [ ] **Step 6: Commit** `git commit -m "Recorder library folders suggested from Plex"`

### Task 4: Docs and changelog

**Files:**
- Modify: `docs/INSTALL.md` ("Point it at Plex" leads with Sign in with Plex; the token section moves under "Enter it yourself"), `CHANGELOG.md`, `docs/API.md` (the four routes)

- [ ] **Step 1: Write them.** Bump the patch version in `app/routes/_shared.py`.
- [ ] **Step 2: Run** `pytest -q tests/test_conventions.py` (it checks docs and copy rules). Expected: pass.
- [ ] **Step 3: Commit** `git commit -m "Docs: Sign in with Plex"`
