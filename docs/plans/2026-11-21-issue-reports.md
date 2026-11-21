# Issue Reports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A person can send a sealed, scrubbed problem report from the web app, the iPhone, iPad and Apple TV apps, and the server, and only the owner can read it.

**Architecture:** Every sender builds one JSON report, gzips it, seals it to the owner's public key (libsodium `crypto_box_seal`), and writes the sealed blob to a create-only Cloud Firestore collection over REST. Web reports go through the server; Apple apps send straight to Firestore with an App Check token; a failed send waits in a sealed queue. A reader tool in the private Apple repo pulls with admin credentials, opens with the private key, and writes readable folders.

**Tech Stack:** Python 3 / FastAPI / PyNaCl (new), Jinja templates and plain JS, pytest + Playwright, Firebase Firestore REST + Security Rules + App Check (App Attest), Swift / SwiftUI, swift-sodium (SPM), FirebaseAppCheck (SPM), XCUITest run in the macOS VM.

**Spec:** `/home/tbird/gitrepos/couchelephant/docs/specs/2026-11-21-issue-reports.md`

## Global Constraints

- No emoji and no em dash anywhere (code, comments, docs, copy, commit messages).
- User-facing copy says "TV", "Movies" and "series". Never "Show" or "Films". The button is exactly **Report this problem**; the settings row is exactly **Report an issue**.
- Sent-confirmation copy is exactly `Sent. Thank you.` followed by the report id. Failure copy is exactly `Could not send. It is saved and will be sent later.`
- Public repo (`/home/tbird/gitrepos/couchelephant`) must contain no host names, LAN addresses or people's names, and never the private key or any admin key. The Firebase project id and web API key are public by design and may be committed.
- Patch version bumps only. Phase A ships as 1.5.33. Phase B bumps the Apple apps' patch version.
- No Claude attribution in any commit message or tag.
- Commits name their paths: `git add <new files>` then `git commit <paths> -m "..."`. Never `git add -A`, never `git commit -a`.
- Phase A and the docs live in the public repo. Phase B and C live in `/home/tbird/gitrepos/couchelephant/clients/apple`, a separate private nested git repo (gitignored by the public one). Commit there with `git -C clients/apple`.
- Free Firebase (Spark) only. Any paid feature needs the owner's yes first.
- Sealed box: X25519 + XSalsa20-Poly1305, libsodium `crypto_box_seal`. Sealed document size cap 900,000 bytes. Queue max 20, oldest dropped. Send limit 10 reports per day per install, enforced on the sending side. Log window: last 15 minutes or 2,000 lines, whichever is smaller.
- Nothing is sent without consent. The person's own message is sent as typed; the consent text says so.
- Apple builds and tests run in the macOS build VM, never locally. Every Apple verification step is "run XCUITest `<Class/testName>` via the VM runner" (`ios-run`, see skill apple-mac-deploy and `clients/apple/README.md`). No local `xcodebuild`.
- Python tests: `.venv/bin/python -m pytest <path> -v` from the repo root. Browser tests: `scripts/test.sh --ui -k <name>`. Lint: `.venv/bin/ruff check app tests`.

## Review Focus

- The server does not answer, or answers slowly, while an app builds its report: the report still sends with the app's part and a masked "server did not answer" line, and building never blocks longer than 5 seconds. Tests in tasks 4, 11 and 15.
- A report over the 900,000 byte cap after sealing: logs are truncated from the oldest end, the report is never dropped silently, and the trigger and message always survive. Test in task 4.
- A secret in a place no setting names: a Plex token in a URL query string in a log line, a `Bearer` header echoed in an error, a PEM block in an exception text, a password in a `user:pass@host` URL. Corpus test in task 3, reused in tasks 11 and 14.
- Double tap on Send, or a retry racing the queue flush: the same report id must never create two documents (the Firestore document id is the report id; a 409 on create counts as sent). Tests in tasks 5 and 13.
- Email field input: blank, spaces, a very long string, no `@`, non-ASCII. Blank is allowed, junk is refused with a plain message, the field is capped at 254 characters. Tests in tasks 7 and 12.

## File Structure

Public repo (Phase A):

- `app/sealing.py`: sealed box wrapper (PyNaCl).
- `app/reportconfig.py`: public key, key id, Firebase project id and API key, with env overrides for tests.
- `app/logring.py`: in-memory ring buffer log handler.
- `app/reportscrub.py`: allow lists, deny patterns, salted hashing, address masking.
- `app/reportbuild.py`: builds the server part and a whole report.
- `app/reportsend.py`: Firestore REST sender, queue, rate limit, install id.
- `app/routes/report.py`: HTTP endpoints for the web app and paired devices.
- `app/static/js/report.js`, `app/templates/_report.html`: consent screen, form, shared button.
- `tests/fake_firestore.py`: a fake Firestore REST endpoint for tests (also used by `scripts/apple_test_server.py`).
- `tests/data/seal_vector.json`, `tests/data/scrub_corpus.json`: cross-language fixtures.
- `firestore.rules`, `tests/firestore_rules/`: security rules and their test.

Private repo (Phase B, C), all under `clients/apple/`:

- `Shared/Report/Seal.swift`, `Scrub.swift`, `AppLog.swift`, `ReportBuilder.swift`, `ReportQueue.swift`, `ReportSender.swift`, `ReportView.swift`, `ReportButton.swift`, `CrashMark.swift`.
- `tools/ce_reports.py`: the reader.
- Tests in `UITests/Shared/`, `UITests/Phone/`, `UITests/TV/`.

---

## Task 0: Firebase project, rules, App Check, keys (LEAD ONLY, not for an implementer)

The lead does this by hand, before task 5 is merged. No implementer touches it.

**Files:** none in code except filling `app/reportconfig.py` after task 5 exists.

- [ ] **Step 1: Create the Firebase project** on the owner's signed-in Google account, Spark (free) plan. Create Cloud Firestore in the default database, production mode.
- [ ] **Step 2: Two projects (decided by the lead).** App Check enforcement on Firestore is per project, so one project cannot serve attested and unattested senders. Make two free projects with the same rules: `couchelephant-reports` for the Apple apps, App Check enforced, and `couchelephant-reports-open` for the web app and server, no App Check. `app/reportconfig.py` names the open project; the Apple config names the other. The reader pulls from both.
- [ ] **Step 3: Generate the key pair** on the build machine with `app.sealing.generate_keypair()` (after task 1). Store the private key in Vaultwarden as `couchelephant-reports-private-<keyid>` (`vw` CLI), then delete the local copy and shell history of it. Never write it into any repo.
- [ ] **Step 4: Deploy `firestore.rules`** (from task 9) with the Firebase CLI. Register the Apple apps in App Check with the App Attest provider (bundle ids of the iPhone/iPad and TV targets).
- [ ] **Step 5: Create a service account** for the reader (Cloud Datastore User role), store its JSON in Vaultwarden, not in any repo.
- [ ] **Step 6: Fill `app/reportconfig.py`** with the public key (base64), key id, project id and web API key. Commit that file in the public repo with message `Reports: public key and Firebase project`.
- [ ] **Step 7: Verify** by running task 5's live-send check once (marked `-m live`, skipped by default) and confirming the document exists in the console and cannot be read back with the web API key (expect 403).

---

# Phase A: server and web (public repo, one patch release, 1.5.33)

## Task 1: Sealing module

**Files:**
- Create: `app/sealing.py`, `tests/test_sealing.py`, `tests/data/seal_vector.json`
- Modify: `requirements.txt` (add `PyNaCl==1.5.0`), `packaging/couchelephant.spec` (import `nacl` so frozen builds carry it, like `zeroconf`), `tests/test_frozen_imports.py` (add `nacl`), `THIRD_PARTY.md` (PyNaCl, Apache-2.0, bundles libsodium, ISC)

**Interfaces:**
- Produces: `generate_keypair() -> tuple[bytes, bytes]` (private, public, 32 bytes each); `key_id(public: bytes) -> str` (first 8 hex chars of sha256 of the public key); `seal(plaintext: bytes, public: bytes) -> bytes`; `open_sealed(blob: bytes, private: bytes) -> bytes` (raises `SealError`); `class SealError(Exception)`.

- [ ] **Step 1: Write failing tests** in `tests/test_sealing.py`: `test_round_trip`, `test_wrong_key_fails_with_sealerror`, `test_two_seals_of_same_text_differ` (anonymous box uses an ephemeral key), `test_tampered_blob_fails`, `test_key_id_is_stable_8_hex`, and `test_vector_file_opens` which loads `tests/data/seal_vector.json` (`{"private": b64, "public": b64, "plaintext": b64, "sealed": b64}`) and asserts `open_sealed` returns the plaintext. The vector's private key is a throwaway made only for this file; the file says so in a `"note"` key.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_sealing.py -v`. Expected: FAIL, no module.
- [ ] **Step 3: Implement** `app/sealing.py` using `nacl.public.SealedBox`. Install `.venv/bin/pip install PyNaCl==1.5.0`. Generate the vector once with a one-off script (not committed) and write the JSON.
- [ ] **Step 4: Run** the tests, `tests/test_frozen_imports.py` and `.venv/bin/ruff check app tests`. Expected: PASS.
- [ ] **Step 5: Commit**: `git add app/sealing.py tests/test_sealing.py tests/data/seal_vector.json` then `git commit app/sealing.py tests/test_sealing.py tests/data/seal_vector.json requirements.txt packaging/couchelephant.spec tests/test_frozen_imports.py THIRD_PARTY.md -m "Reports: sealed box for the owner's key"`.

## Task 2: In-memory log ring buffer

**Files:**
- Create: `app/logring.py`, `tests/test_logring.py`
- Modify: `app/web.py` near its `log = logging.getLogger("couchelephant")` (line 38) to call `logring.install()` at import.

**Interfaces:**
- Produces: `install() -> None` (idempotent; attaches a handler to the `couchelephant` logger and to `uvicorn.error`, level INFO); `recent(max_lines: int = 2000, max_age: int = 900, now: float | None = None) -> list[str]` (each line `"<HH:MM:SS> <LEVEL> <logger>: <message>"`, oldest first); `clear() -> None` (tests). Buffer holds at most 5,000 records.

- [ ] **Step 1: Write failing tests**: `test_recent_returns_logged_lines_in_order`, `test_old_lines_age_out` (pass `now=` 20 minutes later, expect none), `test_line_cap` (log 3,000, `recent()` returns 2,000, the newest), `test_install_twice_logs_once`, `test_exception_text_included` (a `log.exception` line carries the traceback text).
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_logring.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** with a `collections.deque(maxlen=5000)` and a `logging.Handler`, guarded by a lock. Lines are raw here; scrubbing happens in task 3, never skipped by callers.
- [ ] **Step 4: Run** tests and ruff. Expected: PASS.
- [ ] **Step 5: Commit** `git add app/logring.py tests/test_logring.py` and `git commit app/logring.py tests/test_logring.py app/web.py -m "Reports: keep the recent log in memory"`.

## Task 3: Scrubbing (allow list, deny patterns, salted hashing)

**Files:**
- Create: `app/reportscrub.py`, `tests/test_reportscrub.py`, `tests/data/scrub_corpus.json`

**Interfaces:**
- Produces:
  - `class Scrubber`; `Scrubber(salt: bytes | None = None)` makes a fresh random 16-byte salt when none given (a new Scrubber per report).
  - `Scrubber.host(name: str) -> str` returns `host-` plus first 10 hex of sha256(salt + name).
  - `Scrubber.address(url_or_hostport: str) -> str`: private range or loopback keeps its shape (`http://private-lan:32400`, `https://private-lan`), a public address or a hostname becomes `<scheme>://host-xxxxxxxxxx:<port>`; a `user:pass@` part is dropped.
  - `Scrubber.text(s: str) -> str`: applies the deny patterns and masks addresses inside free text (log lines, error text).
  - `SETTINGS_ALLOW: tuple[str, ...]` and `ADDRESS_SETTINGS: tuple[str, ...]`; `Scrubber.settings(all_settings: dict[str, str]) -> dict[str, str]` returns only allow-listed keys, with address keys masked.
- Allow list contents (explicit, reviewed in code review): `timezone, sync_minutes, dry_run, epg_provider, sports_section, shows_section, movies_section, recorder_default, recorder_encoder, recorder_lead_minutes, live_pause_minutes, adskip_live, adskip_final, sports_overtime, overtime_cap_minutes, recorder_trail_minutes, recorder_shows_folder, recorder_shows_section, recorder_movies_folder, recorder_movies_section, recorder_keep_days`. Address keys, masked: `plex_url, tuner_url`. Never present: `plex_token, tmdb_key, sportsdb_key`, anything not listed. Add a source comment that a new setting is invisible to reports until added here on purpose.
- Deny patterns (applied to every string): `X-Plex-Token=...` and `token=...` style query values; `Authorization: Bearer ...`; JWT shape `eyJ...\..*\..*`; PEM blocks `-----BEGIN ... -----END`; `(password|passwd|secret|api[_-]?key|token|key|auth)\s*[=:]\s*\S+`; any 20+ character run of `[A-Za-z0-9_-]` that mixes letters and digits (the Plex token shape, also catches device keys and ACME keys); 64 hex runs. Replacement text `[removed]`.

- [ ] **Step 1: Write `tests/data/scrub_corpus.json`**: a list of `{"name": ..., "input": ..., "secret": ...}` where `secret` is the substring that must not survive. Cover: Plex token in a URL query, in a header line, bare in a log line; a device key; a Bearer header; a JWT; a PEM TLS private key block; an ACME account key JSON; `https://user:hunter2@example.net/x`; `password=hunter2`; a setting dump line. Add a `"must_survive"` list of normal strings (a show title "Murder, She Wrote", a team name "Green Bay Packers", a pass name, a version `1.5.33`, a clock `12:30:45`, a short id) that the patterns must not damage.
- [ ] **Step 2: Write failing tests** in `tests/test_reportscrub.py`: `test_corpus_no_secret_survives` (parametrized over the file; assert `secret not in Scrubber().text(input)`), `test_corpus_normal_text_survives`, `test_settings_only_allow_list` (feed a dict with `plex_token="SECRET"`, `tmdb_key`, a made-up `new_setting`; assert none appear, and a value never contains `SECRET` anywhere in `json.dumps`), `test_every_default_setting_is_classified` (every key in `db.DEFAULTS` is in `SETTINGS_ALLOW`, `ADDRESS_SETTINGS` or an explicit `NEVER_SENT` tuple, so adding a setting forces a decision), `test_private_address_keeps_shape` (`http://192.168.1.20:32400` -> `http://private-lan:32400`), `test_public_address_hashed` (`https://example.net:8443` contains `host-` and `:8443`, not `example.net`), `test_salt_differs_per_scrubber` (same host hashes differently), `test_same_scrubber_hash_is_stable`, `test_userinfo_dropped`.
- [ ] **Step 3: Run** `.venv/bin/python -m pytest tests/test_reportscrub.py -v`. Expected: FAIL.
- [ ] **Step 4: Implement** `app/reportscrub.py`. Use `ipaddress` for the private check (10/8, 172.16/12, 192.168/16, 169.254/16, loopback, `fc00::/7`).
- [ ] **Step 5: Run** the tests and ruff. Expected: PASS. If a `must_survive` string is eaten, narrow the long-run pattern; do not delete the case.
- [ ] **Step 6: Commit** `git add app/reportscrub.py tests/test_reportscrub.py tests/data/scrub_corpus.json` and `git commit` those three paths `-m "Reports: scrub secrets and mask addresses before sealing"`.

## Task 4: Report builder (server part and whole report)

**Files:**
- Create: `app/reportbuild.py`, `tests/test_reportbuild.py`

**Interfaces:**
- Consumes: `Scrubber` (task 3), `logring.recent` (task 2), `db.all_settings`, `db.one`/`db.connect` for the sync log, pass table and bookings (read `app/routes/passes.py` and `app/recorder.py` for the table and column names), `recorder.why_unavailable`, `health.open_notices`, `paths.recordings_dir`, `app.version.VERSION`, `shutil.disk_usage`.
- Produces:
  - `server_part(scrub: Scrubber) -> dict`: keys `version, uptime_seconds, os, container (bool), python, log (list[str], scrubbed), settings, tuner (url masked, detected), recorder ({available, why_unavailable, encoder}), passes (list of {kind, title, enabled}, titles are programme or team names and allowed), schedule_failures (last 10 failed recordings: title, time, error scrubbed), notices (codes), disk ({free_bytes, total_bytes} of the recordings folder)`.
  - `build_report(*, source: str, app: dict, trigger: dict, app_log: list[str], server: dict | None, server_note: str | None, message: str, email: str, scrub: Scrubber, report_id: str | None = None, now: int | None = None) -> dict`: returns the report dict (`v: 1`, `reportId` = 32 hex random when not given, `created`, `source` in `web|ios|ipados|tvos|server`, `app`, `trigger`, `appLog`, `server`, `serverNote`, `message`, `email`). The message and email are not scrubbed. Everything else goes through `scrub.text`.
  - `pack(report: dict, cap: int = 900_000, public: bytes | None = None) -> bytes`: gzip(JSON), shrink while sealed size is over `cap` by dropping the oldest log lines (app log and server log in turn), then seal. Never drops trigger or message. Raises `ReportTooLarge` only if trigger plus message alone exceed the cap.
  - `source` validation: `ValueError` on anything else.

- [ ] **Step 1: Write failing tests** in `tests/test_reportbuild.py` using the `client` fixture and the fake plex: `test_server_part_has_expected_keys`, `test_server_part_never_contains_known_secrets` (set `plex_token="PLEXSECRET123456789012"`, `tmdb_key`, `sportsdb_key`, create a device key via `tv_devices`, log a line containing the token, then `json.dumps(server_part(Scrubber()))` has none of them and no `plex_url` host), `test_settings_come_only_from_allow_list`, `test_disk_space_reported`, `test_build_report_message_and_email_verbatim` (message with the text `password=hunter2` survives untouched in `message`), `test_build_report_scrubs_app_log` (a log line with a token is `[removed]`), `test_report_id_is_32_hex_and_random`, `test_bad_source_refused`, `test_pack_round_trips_with_private_key` (use the task 1 vector private key as the `public`/private pair), `test_pack_truncates_old_log_when_over_cap` (3 MB of logs, sealed size under 900,000, trigger and message intact, newest log line kept), `test_pack_never_drops_message`.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_reportbuild.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `app/reportbuild.py`. The `source` for server-made reports is `server` or `web` as the caller says.
- [ ] **Step 4: Run** tests and ruff. Expected: PASS.
- [ ] **Step 5: Commit** `git add app/reportbuild.py tests/test_reportbuild.py`, `git commit` both `-m "Reports: build a scrubbed report from the server's state"`.

## Task 5: Firestore sender, queue, rate limit, key id, fake Firestore

**Files:**
- Create: `app/reportconfig.py`, `app/reportsend.py`, `tests/fake_firestore.py`, `tests/test_reportsend.py`
- Modify: `app/db.py` (schema string and the `MIGRATIONS` list per `docs/ARCHITECTURE.md` "Migrations": tables `report_queue(report_id TEXT PRIMARY KEY, blob BLOB NOT NULL, source TEXT, app_version TEXT, created INTEGER)` and `report_sent(ts INTEGER)`), `tests/conftest.py` (wipe is automatic for new tables; add nothing unless needed), `app/paths.py` untouched.

**Interfaces:**
- Produces:
  - `reportconfig.config() -> ReportConfig | None` with fields `public_key: bytes, key_id: str, project: str, api_key: str, base_url: str`. Reads constants in the module (empty until task 0 step 6); env overrides `COUCHELEPHANT_REPORT_PUBLIC_KEY`, `..._PROJECT`, `..._API_KEY`, `..._FIRESTORE_URL` (defaults to `https://firestore.googleapis.com`). Returns `None` when not configured.
  - `reportsend.install_id() -> str`: 32 hex, made on first call and stored in setting `report_install_id`.
  - `reportsend.submit(report: dict, source: str) -> SendResult` where `SendResult = {ok: bool, sent: bool, queued: bool, report_id: str, reason: str | None}`: packs (task 4), enforces the rate limit, tries to send, on failure queues.
  - `reportsend.flush_queue() -> int` (count sent), called at the start of `submit` and by a 15-minute thread started from `app/web.py` only when the queue is non-empty.
  - `reportsend.queue_size() -> int`.
  - Firestore body: `POST {base}/v1/projects/{project}/databases/(default)/documents/reports?documentId={reportId}&key={api_key}` with fields `reportId` (string), `sealed` (bytesValue, base64), `size` (integer), `created` (timestamp), `source`, `appVersion`, `keyId`, `installId`. HTTP 200 is sent; 409 (id exists) counts as sent; 4xx other than 409 or 429 drops the report as junk with a log line; 5xx, 429 and network errors queue. Rate limit: at most 10 sends per rolling 24 hours (`report_sent`), over the limit returns `ok=False, reason="limit"` and does not queue.
  - Queue rule: when 20 are waiting, adding another deletes the oldest.
- The fake: `tests/fake_firestore.py` exposes `start() -> (url, stop)` and `STATE` with `.docs` (dict by id), `.fail_next(status, n=1)`, `.reset()`; it checks the path shape and that exactly the documented fields are present and rejects reads (GET returns 403), so tests mirror the rules.

- [ ] **Step 1: Write failing tests** in `tests/test_reportsend.py` (monkeypatch the env config to the fake): `test_submit_sends_one_document_with_expected_fields_and_no_cleartext` (fields are exactly the documented eight; no part of message, email or log text appears anywhere in the request body), `test_document_id_is_report_id`, `test_same_report_twice_is_one_document` (second gets 409 and is treated as sent), `test_failure_queues_and_flush_sends_later`, `test_queue_keeps_20_drops_oldest`, `test_rate_limit_10_per_day_then_refused_not_queued`, `test_rate_limit_resets_after_a_day` (patch time), `test_junk_400_is_dropped_not_queued`, `test_not_configured_queues_nothing_and_says_so`, `test_install_id_is_stable_and_not_derived_from_anything`, `test_queue_blob_on_disk_is_sealed` (read the raw column; the message text is absent and `open_sealed` with the vector key opens it).
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_reportsend.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the three modules with `httpx` (already a dependency), 8 second timeout. Add the live check test marked `@pytest.mark.live` (register the marker in `pytest.ini`), skipped unless `COUCHELEPHANT_REPORT_LIVE=1`.
- [ ] **Step 4: Run** tests and ruff. Expected: PASS.
- [ ] **Step 5: Commit** `git add app/reportconfig.py app/reportsend.py tests/fake_firestore.py tests/test_reportsend.py`, `git commit` those plus `app/db.py pytest.ini -m "Reports: send to Firestore, queue sealed, limit per install"`.

## Task 6: Server-part and email endpoints, and who may call them

**Files:**
- Create: `app/routes/report.py`, `tests/test_report_routes.py`
- Modify: `app/plextv.py` (add `account_email(token: str) -> str | None`, calling plex.tv `GET /api/v2/user` through `_call`; returns only the email string, never the token), `app/web.py` (include the router the way the other routers are included; add `/api/report` to `_ADMIN_PREFIX`; in `_device_may` allow `/api/report/server` and `/api/report/email` so a paired device with its key passes the gate), `tests/fake_plextv.py` (answer `/api/v2/user` with `{"email": "..."}`).

**Interfaces:**
- Consumes: `reportbuild.server_part`, `Scrubber`, `plextv.account_email`, `db.get_setting("plex_token")`, `tv.device_for`, `_shared.current_user`, `auth.mode`.
- Produces:
  - `GET /api/report/server` returns `{"ok": true, "server": <server_part>}`; same 5-second budget.
  - `GET /api/report/email` returns `{"ok": true, "email": "<address or empty>"}`. Empty when there is no Plex sign-in or plex.tv does not answer; never an error status for that.
  - Access: sign-in off, anyone; sign-in on, an administrator session or a paired device key. A non-admin signed-in person gets 403 (`administrators only`), no key gets 401.
- Name the existing helpers in the task: admin check is `user.get("role") == "admin"` as in `app/web.py` `_auth_gate` and `routes/tv_settings.may_change`; device check is `routes/tv.device_for`.

- [ ] **Step 1: Write failing tests** in `tests/test_report_routes.py` (see `tests/test_auth.py` and `tests/test_tv_api.py` for sign-in on and device pairing setup): `test_server_part_open_when_signin_off`, `test_server_part_admin_session_ok`, `test_server_part_non_admin_403`, `test_server_part_paired_device_key_ok`, `test_server_part_no_key_401`, `test_server_part_body_has_no_token` (plex token set, assert absent in response text), `test_email_returns_plex_account_email`, `test_email_empty_without_plex_signin`, `test_email_empty_when_plextv_down`, `test_email_response_never_contains_token`.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_report_routes.py -v`. Expected: FAIL (404).
- [ ] **Step 3: Implement** the route module and the `account_email` function; wire the gate. 
- [ ] **Step 4: Run** the new tests plus `tests/test_auth.py tests/test_tv_api.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** new paths added with `git add`, then `git commit app/routes/report.py tests/test_report_routes.py app/plextv.py app/web.py tests/fake_plextv.py -m "Reports: the server's part and the account email, for admins and paired devices"`.

## Task 7: Send and consent endpoints for the web

**Files:**
- Modify: `app/routes/report.py`, `tests/test_report_routes.py`

**Interfaces:**
- Consumes: `reportbuild.build_report`, `reportsend.submit`, `reportsend.queue_size`, `db.get_setting/set_setting` (new setting `report_consent` default `""`, add to `db.DEFAULTS`, and to the `NEVER_SENT` tuple in `reportscrub`).
- Produces:
  - `GET /api/report/consent` returns `{"ok": true, "consented": bool, "queued": int}`.
  - `POST /api/report/consent` (form `agree=1`) records consent, returns `{"ok": true}`.
  - `POST /api/report/send` (JSON `{trigger: {kind: "error"|"support"|"crash", error?: str, screen?: str}, message: str, email: str, client: {ua?: str, screen_size?: str}, app_log?: list[str]}`) builds the report with source `web`, includes the server part and the browser's console lines if given, and calls `submit`. Returns `{"ok": true, "sent": bool, "queued": bool, "reportId": str, "message": "Sent. Thank you."` or the failure copy `}` . 403 `{"ok": false, "error": "consent"}` before consent. Validation: email blank or matching `^[^@\s]{1,64}@[^@\s]{1,190}$` and at most 254 characters, else 400 with `"That email address does not look right."`; message capped at 20,000 characters, longer returns 400. Any signed-in person may send (not admin only); do not add `/api/report/send` or `/consent` to `_ADMIN_PREFIX`; the sign-in-on, signed-out case is blocked by the gate already.
  - The web app has no crash trigger; `kind: "crash"` is accepted only for symmetry with the other sources and tested as accepted.

- [ ] **Step 1: Write failing tests**: `test_send_refused_before_consent`, `test_consent_then_send_creates_one_sealed_doc` (fake Firestore; the doc opens with the vector private key and its JSON has the message verbatim and `source == "web"`), `test_send_includes_server_part`, `test_send_failure_queues_and_says_saved_copy` (exact failure copy), `test_non_admin_signed_in_person_may_send`, `test_email_validation_cases` (blank ok; `junk` refused; 300 chars refused; `a@b.co` ok), `test_message_over_cap_refused`, `test_double_post_same_second_does_not_duplicate` (two rapid sends with distinct report ids make two docs; resubmitting one report id through the queue makes one), `test_rate_limit_message_is_plain` (`ok: false`, `error` says too many reports today, try tomorrow).
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_report_routes.py -v`. Expected: new tests FAIL.
- [ ] **Step 3: Implement** the endpoints and the `report_consent` default.
- [ ] **Step 4: Run** `tests/test_report_routes.py tests/test_reportscrub.py -v`. Expected: PASS (the classification test includes the new setting).
- [ ] **Step 5: Commit** `git commit app/routes/report.py tests/test_report_routes.py app/db.py app/reportscrub.py -m "Reports: consent and send from the web app"`.

## Task 8: Web consent screen, support form and the shared Report button

**Files:**
- Create: `app/static/js/report.js`, `app/templates/_report.html`, `tests/ui/test_report.py`
- Modify: `app/templates/base.html` (include `_report.html` once and load `report.js`; and the `.banner bad` last-sync-failed at line 225 gets the button), `app/templates/_settings.html` (row **Report an issue** in the existing Settings layout; the failing-device line at 163 and the failed-sync row at 292 get the button), `app/templates/recordings.html` (shared button at the error displays: `say(..., 'bad')` at lines 1175 and 1196, the failed-recording rows around 184 and 338, the `empty` error panels at 507 and 1326, the preview error 863, and replace `alert(j.error)` at 1357 with the same message display plus the button), `app/templates/guide.html` (the `.catch` at 178 shows an error state with the button), `app/static/css/app.css`, `docs/images` untouched.

**Interfaces:**
- Produces (JS, in `report.js`, global `CEReport`): `CEReport.button(trigger: {error: string, screen: string}) -> string` returns the HTML of a small `<button type="button" class="reportbtn" data-report='...'>Report this problem</button>` with the trigger JSON-escaped in the attribute; `CEReport.open(trigger?)` opens the dialog; one delegated click handler on `document` handles every `[data-report]` and `[data-report-open]` so dynamically built HTML works with no per-site wiring. Browser console errors are kept by `window.onerror` and `console.error` wrappers in a 100-line array sent as `app_log`.
- Dialog (`_report.html`): first time, the consent text (what a report contains, who reads it: "Only the person who makes CouchElephant can read it. It is sealed before it leaves, and stored sealed.") with **Send** and **Never mind**; every time, a one-or-two-line summary, a message field (optional), an email field prefilled from `GET /api/report/email`, then remembered in `localStorage` key `ce.report.email`, and **Send** / **Never mind**. After send: `Sent. Thank you.` plus `Report id: <id>`. On failure: the exact saved-and-later copy. The dialog's own errors are shown in the dialog, never silent (anti-goal).
- Wording: "TV", "Movies", "series" only.
- Not given the button: expected non-errors such as "no tuner found yet" in setup (`welcome.html`, `settings` tuner scan states), per spec call 7. List the skipped places in a comment in `report.js`.

- [ ] **Step 1: Write failing Playwright tests** in `tests/ui/test_report.py` (follow `tests/ui/test_notices.py` and its `page` fixture; fake Firestore from task 5 started in a fixture and env pointed at it): `test_settings_report_an_issue_opens_form_and_consent_first`, `test_never_mind_sends_nothing` (fake has no docs), `test_send_shows_sent_and_report_id_and_doc_arrives`, `test_second_send_skips_consent_and_prefills_remembered_email`, `test_email_prefilled_from_plex_account` (fake plextv), `test_failed_booking_row_has_report_button_that_attaches_the_error` (make a failed recording as `tests/ui/test_failed_bookings.py` does, click the button, open the doc with the vector key, `trigger.error` equals the error text), `test_guide_error_state_has_report_button` (force `/partial/...` failure via route interception), `test_no_button_on_setup_no_tuner_state`, `test_send_failure_shows_saved_copy` (fake returns 500), `test_dialog_error_is_visible_when_server_down` (intercept `/api/report/send` to abort; the dialog shows a message, not silence), `test_mobile_layout_dialog_fits` (iPhone viewport, no horizontal scroll). Every test clicks the real buttons.
- [ ] **Step 2: Run** `scripts/test.sh --ui -k report`. Expected: FAIL.
- [ ] **Step 3: Implement** `report.js`, `_report.html`, the template edits and css in the app's existing style (look at `.banner`, `.pill`, `.note.said.bad` and the modal pattern already used for the pass-delete dialog).
- [ ] **Step 4: Run** `scripts/test.sh --ui -k "report or failed_bookings or notices or settings"`. Expected: PASS and the neighbours still pass.
- [ ] **Step 5: Commit** `git add app/static/js/report.js app/templates/_report.html tests/ui/test_report.py` and `git commit` those plus the modified templates and css `-m "Reports: Report this problem on every error, and Report an issue in Settings"`.

## Task 9: Firestore security rules and their test

**Files:**
- Create: `firestore.rules`, `tests/firestore_rules/package.json`, `tests/firestore_rules/rules.test.js`, `tests/test_firestore_rules.py` (runs the node test through `firebase emulators:exec` when `firebase` and `node` exist, else `pytest.skip` with a reason)
- Modify: `docs/DEVELOPING.md` (how to run it)

**Interfaces:** none consumed. Rules: `match /reports/{id}`; `allow create` only, when the document keys are exactly `reportId, sealed, size, created, source, appVersion, keyId, installId`, types are correct (`sealed` is bytes, `size` an int equal to `sealed.size()`, `size <= 900000`, `source in ['web','ios','ipados','tvos','server']`, strings capped at 64, `reportId` equals the document id and is 32 hex, `created` is a timestamp within a day of `request.time`); `allow read, update, delete, list: if false`; everything else denied. Put the outcome of task 0 step 2 in a header comment, and the App Check condition if rules can express it.

- [ ] **Step 1: Write failing rules tests** (`@firebase/rules-unit-testing`): valid create allowed; read, list, update, delete denied; extra field denied; missing field denied; `sealed` as string denied; 900,001 bytes denied; bad `source` denied; id and `reportId` mismatch denied; create under another collection denied; created far in the past denied.
- [ ] **Step 2: Run** `tests/firestore_rules` via `firebase emulators:exec --only firestore "npm test"` if available. Expected: FAIL before the rules exist. If the emulator cannot be installed here, say so in the commit body and let the lead run it as part of task 0 step 4; the pytest wrapper skips.
- [ ] **Step 3: Write `firestore.rules`.**
- [ ] **Step 4: Re-run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `git add firestore.rules tests/firestore_rules tests/test_firestore_rules.py` and `git commit` those plus `docs/DEVELOPING.md -m "Reports: create-only Firestore rules, with a test"`.

## Task 10: Apple test server speaks the fake Firestore, docs, release 1.5.33

**Files:**
- Modify: `scripts/apple_test_server.py` and `scripts/apple_test_server.sh` (start `tests/fake_firestore.py` on port 8792 and point the server at it with `COUCHELEPHANT_REPORT_FIRESTORE_URL`; add `GET /test/reports` returning the stored docs for XCUITests to inspect and `POST /test/reports/reset`; the fake also accepts the App Check header `X-Firebase-AppCheck` and records it), `docs/API.md` (new "Reports" section: the four endpoints, their access rule, the request and response shapes, the Firestore document shape), `docs/ARCHITECTURE.md` (new "Issue reports" section: sealed box, path per source, queue, rate limit, key id, what is scrubbed and the allow list rule, where the rules live), `docs/INSTALL.md` only if it lists settings (add one line that reports are optional and consent based), `CHANGELOG.md` (1.5.33 entry, "Added": Report this problem, Report an issue, sealed), `app/version.py` (`VERSION = "1.5.33"`), `site/` untouched.
- Test: `tests/test_conventions.py` (existing; run it, it may check docs and version), `tests/test_api.py`.

- [ ] **Step 1: Write a failing test** in `tests/test_report_routes.py`: `test_docs_name_every_report_route` (every path registered in `app/routes/report.py` appears in `docs/API.md`; see `tests/test_conventions.py` for the existing pattern and reuse it if it already does this).
- [ ] **Step 2: Run** it. Expected: FAIL.
- [ ] **Step 3: Update** docs, changelog, version, the test server. Grep the new docs and code for host names, LAN addresses and people's names, and for em dashes.
- [ ] **Step 4: Run the full suite**: `scripts/test.sh --unit` then `scripts/test.sh --ui`. Expected: all pass.
- [ ] **Step 5: Commit** `git commit scripts/apple_test_server.py scripts/apple_test_server.sh docs/API.md docs/ARCHITECTURE.md CHANGELOG.md app/version.py tests/test_report_routes.py -m "1.5.33: issue reports from the web app and the server, sealed for the owner"`. Tagging, push and deploy follow `docs/RELEASING.md` and are the lead's step, not the implementer's.

---

# Phase B: Apple apps (private repo `clients/apple`, next TestFlight builds)

Phase B needs Phase A merged and deployed to a test server (the Apple apps call `/api/report/server` and `/api/report/email`). Older servers answer 404: the app then sends its own part only and shows the email field empty.

## Task 11: Swift sealed box and the cross-language vector

**Files:**
- Create: `Shared/Report/Seal.swift`, `UITests/Shared/SealUnitTests.swift`, `UITests/Shared/seal_vector.json` (copy of the public repo's `tests/data/seal_vector.json`; the copy script is noted in `README.md`)
- Modify: `CouchElephant.xcodeproj/project.pbxproj` (add the `swift-sodium` Swift package, `https://github.com/jedisct1/swift-sodium`, pinned to an exact version at least two weeks old, to the Phone and TV targets, and the vector file to the test target's resources). CryptoKit has no XSalsa20, so a libsodium-backed package is the way to match PyNaCl byte for byte.

**Interfaces:**
- Produces: `enum Seal { static func seal(_ plaintext: Data, to publicKey: Data) throws -> Data; static func keyID(of publicKey: Data) -> String }` (same 8-hex sha256 rule as `app/sealing.py`); `Seal.open(_:privateKey:publicKey:)` exists for tests only (`#if DEBUG`).

- [ ] **Step 1: Write failing tests**: `testOpensTheVectorSealedByPython` (open `seal_vector.json`'s `sealed` with its keys, equals `plaintext`), `testSealedByUsOpensWithTheVectorPrivateKey` (seal with the vector public key and open with `open`), `testKeyIDMatchesPythonForTheVector` (vector file carries `"keyId"`; add it to the public vector in task 1's file by a one-line follow-up edit committed with this task's public-repo companion, see step 5), `testTwoSealsDiffer`.
- [ ] **Step 2: Run** the `SealUnitTests` class via the VM runner (`ios-run`, per `clients/apple/README.md` and skill apple-mac-deploy). Expected: FAIL, no `Seal`.
- [ ] **Step 3: Implement** `Seal.swift` over `Sodium().box.seal(message:recipientPublicKey:)`. 
- [ ] **Step 4: Run** `SealUnitTests` via the VM runner. Expected: PASS.
- [ ] **Step 5: Commit** in the private repo: `git -C clients/apple add Shared/Report/Seal.swift UITests/Shared/SealUnitTests.swift UITests/Shared/seal_vector.json` then `git -C clients/apple commit Shared/Report/Seal.swift UITests/Shared/SealUnitTests.swift UITests/Shared/seal_vector.json CouchElephant.xcodeproj/project.pbxproj -m "Reports: sealed box compatible with the server's"`. If `keyId` had to be added to the public vector, add it in the public repo in the same sitting and commit that file alone.

## Task 12: Swift scrubber and report builder, app log

**Files:**
- Create: `Shared/Report/Scrub.swift`, `Shared/Report/AppLog.swift`, `Shared/Report/ReportBuilder.swift`, `UITests/Shared/ReportBuilderUnitTests.swift`, `UITests/Shared/scrub_corpus.json` (copy of the public `tests/data/scrub_corpus.json`)
- Modify: `Shared/PlayerView.swift`, `Shared/Server.swift`, `Shared/Hub.swift` (their `Logger` calls also write to `AppLog` through one helper `AppLog.note(_ level:, _ category:, _ message:)`; replace only call sites that already log, add no new logging noise).

**Interfaces:**
- Produces:
  - `final class AppLog { static let shared; func note(...); func recent(maxLines: Int = 2000, maxAge: TimeInterval = 900) -> [String] }` a 5,000 line ring buffer, in memory only.
  - `struct Scrubber { init(salt: Data = random 16 bytes); func host(_:) -> String; func address(_:) -> String; func text(_:) -> String }` same behaviour and same output shapes as task 3; the app has no settings allow list because it sends no settings.
  - `struct ReportBuilder { static func build(source: Source, trigger: Trigger, message: String, email: String, server: [String: Any]?, serverNote: String?) -> Data }` returning gzipped JSON of the same schema as `build_report` (`v 1`, `reportId`, `created`, `source`, `app` with version, build, OS version and device model, `trigger`, `appLog`, `server`, `serverNote`, `message`, `email`), with `enum Source: String { case ios, ipados, tvos }` and `enum Trigger { case error(text: String, screen: String), support, crash(details: String) }`. A `packed(cap: 900_000)` step trims the oldest log lines like `pack` in task 4.

- [ ] **Step 1: Write failing tests**: `testCorpusNoSecretSurvives`, `testCorpusNormalTextSurvives` (same file as Python, same assertions), `testAddressMaskingMatchesPythonShapes`, `testSaltDiffersPerReport`, `testMessageAndEmailVerbatim`, `testOversizeTrimsOldestLogKeepsTriggerAndMessage`, `testServerDidNotAnswerBuildsAppPartOnlyWithMaskedAddress`, `testReportIDIs32Hex`, `testAppLogKeepsLastFifteenMinutes`.
- [ ] **Step 2: Run** `ReportBuilderUnitTests` via the VM runner. Expected: FAIL.
- [ ] **Step 3: Implement.** The deny patterns are ported one for one from `app/reportscrub.py`; when either side changes a pattern, the shared corpus must be updated and both suites rerun (note this in a comment in both files).
- [ ] **Step 4: Run** `ReportBuilderUnitTests` via the VM runner. Expected: PASS.
- [ ] **Step 5: Commit** in the private repo with explicit paths and message `Reports: build and scrub a report in the app`.

## Task 13: Sealed queue, sender, App Check, rate limit

**Files:**
- Create: `Shared/Report/ReportQueue.swift`, `Shared/Report/ReportSender.swift`, `UITests/Shared/ReportSenderUnitTests.swift`
- Modify: `CouchElephant.xcodeproj/project.pbxproj` (add `https://github.com/firebase/firebase-ios-sdk`, product `FirebaseAppCheck` only, pinned exact; App Attest entitlement `com.apple.developer.devicecheck.appattest-environment` in the Phone and TV entitlements files), `Shared/CouchElephantApp.swift` (configure App Check at launch: `FirebaseApp.configure()` with the public web config plist `Shared/Report/FirebaseConfig.plist` (project id, API key, app id) and `AppAttestProvider`; in DEBUG with launch argument `-reportFirestore <url>` use the debug provider and the fake base URL instead).

**Interfaces:**
- Consumes: `Seal`, `ReportBuilder`, the public key and key id constants in `Shared/Report/ReportKeys.swift` (public key base64, key id; filled by the lead in task 0 step 6).
- Produces: `actor ReportSender { static let shared; func submit(_ packed: Data, reportID: String, source: String, appVersion: String) async -> SendResult }` with `enum SendResult { case sent(id: String), queued(id: String), limited, failed(String) }`; `ReportQueue.count: Int`; `ReportSender.flush() async`, called at launch and when the app becomes active. Same Firestore URL, body, status handling, 20-item queue and 10-per-day rule as `app/reportsend.py` (task 5). Queue files hold only already sealed bytes, in Application Support with file protection complete, one file per report id. The install id is a random UUID hex in UserDefaults on first send. Header `X-Firebase-AppCheck` carries the token; no token available means queue, not send.

- [ ] **Step 1: Write failing tests** (a `URLProtocol` stub): `testSendPostsDocumentWithEightFieldsAndAppCheckHeader`, `testNoCleartextInRequestBody`, `testConflictCountsAsSent`, `testNetworkFailureQueuesSealedFile`, `testQueueFileOpensOnlyWithPrivateKey`, `testQueueKeepsTwentyDropsOldest`, `testTenPerDayThenLimited`, `testFlushSendsQueuedOnce`, `testNoAppCheckTokenQueues`, `testJunk400IsDropped`.
- [ ] **Step 2: Run** `ReportSenderUnitTests` via the VM runner. Expected: FAIL.
- [ ] **Step 3: Implement.** Mirror task 5 and name differences from it in comments only where they exist.
- [ ] **Step 4: Run** `ReportSenderUnitTests` via the VM runner. Expected: PASS.
- [ ] **Step 5: Commit** in the private repo `Reports: send sealed, queue sealed, App Check`.

## Task 14: Report screen, shared button, Settings row, remembered email

**Files:**
- Create: `Shared/Report/ReportView.swift`, `Shared/Report/ReportButton.swift`
- Modify: `Shared/SettingsView.swift` (a row **Report an issue**, under the existing sections; line 64 `SettingsView`), `Shared/Hub.swift` (new `@Published var reporting: ReportContext?` where `struct ReportContext { var error: String; var screen: String }`, and `func report(_ error: String, screen: String)`), `Phone/RootView.swift` (the `hub.telling` alert at line 40 gets a second button **Report this problem** that sets `hub.reporting`; add the `.sheet(item: $hub.reporting)`), `TV/RootView.swift` (same at line 44, as a full-screen cover), `Shared/PlayerView.swift` (its alert at line 1434 and the playback failure state at line 245), `Shared/RecordAllView.swift` (the server's answer panel at line 99), `Shared/ScheduleView.swift` (failure text at lines 561 and 649), `Phone/HomeView.swift` line 192 and `TV/Cards.swift` line 98 (a failed recording's error), `Shared/Server.swift` (a not-reachable state at line 214 sets a report context).

**Interfaces:**
- Produces: `struct ReportButton: View { init(error: String, screen: String) }` the one shared component (title **Report this problem**, uses `hub.report`); `struct ReportView: View` (the sheet): first-time consent screen (same words as the web, setting `reportConsent` in UserDefaults), then summary line, a message `TextField` (multi-line on iPhone and iPad; on tvOS the standard `TextField`, which brings the system keyboard with the Siri remote dictation button), the email `TextField` prefilled from `GET /api/report/email` (via `Server.send`), remembered in Keychain name `reportEmail` with `Keychain.write` / `Keychain.read` (see `Shared/Server.swift:90`), **Send** / **Never mind**, then `Sent. Thank you.` with the report id or the exact failure copy. Email validation as in task 7. Accessibility identifiers: `report.button`, `report.message`, `report.email`, `report.send`, `report.cancel`, `report.consent.send`, `report.sent`, `report.saved`.
- Expected non-errors such as "no tuner found yet" in `SetupView` do not get the button.
- Server part: the sheet calls `GET /api/report/server` with a 5 second timeout; failure or 404 builds the app part only with `serverNote`.

- [ ] **Step 1: Write failing XCUITests** (this task's tests are written in task 16 as the platform suites; write them first there and run them red here, see below). Add one unit test now: `testEmailValidation` in `ReportBuilderUnitTests` (blank ok, `junk` refused, 300 chars refused).
- [ ] **Step 2: Run** `ReportBuilderUnitTests/testEmailValidation` via the VM runner. Expected: FAIL.
- [ ] **Step 3: Implement** the views and the call sites. Follow the existing look (`Shared/Theme.swift`).
- [ ] **Step 4: Run** `ReportBuilderUnitTests` via the VM runner. Expected: PASS. Then run the Phone and TV XCUITests from task 16 steps 2 and 4.
- [ ] **Step 5: Commit** in the private repo `Reports: Report this problem on errors, and Report an issue in Settings`.

## Task 15: Crash running mark, MetricKit, and the Settings switch

**Files:**
- Create: `Shared/Report/CrashMark.swift`
- Modify: `Shared/CouchElephantApp.swift` (set the mark at launch, clear on scene phase `.background` and on clean exit, offer on next launch), `Shared/SettingsView.swift` (switch **Offer a report after a crash**, default on, UserDefaults `reportCrashPrompt`)

**Interfaces:**
- Produces: `enum CrashMark { static func start(); static func clean(); static var crashedLastRun: Bool; static var details: String? }`. The mark is a file in Application Support. `MXMetricManager` subscriber stores the latest `MXCrashDiagnostic` text (scrubbed) for `details`; if the system has not delivered any, `details` is nil and the report says `crash on last run, no details delivered yet`. Launch argument `-simulateCrashMark 1` (DEBUG only) leaves the mark set before launch, for the test. The offer is a `ReportContext` with trigger `crash`, shown once, then the mark is reset.

- [ ] **Step 1: Write failing unit tests** in `ReportBuilderUnitTests`: `testMarkSetOnStartAndClearedOnClean`, `testMarkStillSetMeansCrashedLastRun`, `testCrashPromptOffNeverOffers`, `testCrashDetailsScrubbed`.
- [ ] **Step 2: Run** them via the VM runner. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** them via the VM runner. Expected: PASS.
- [ ] **Step 5: Commit** in the private repo `Reports: offer a report after a crash`.

## Task 16: XCUITests per platform, TestFlight

**Files:**
- Modify: `UITests/Phone/PhoneTests.swift` (or a new `UITests/Phone/ReportTests.swift`), `UITests/TV/TVTests.swift` (or `UITests/TV/ReportTests.swift`), `UITests/Shared/TestServer.swift` (helpers `reports() -> [[String: Any]]`, `resetReports()` using `/test/reports` from task 10, and opening a report document with the vector private key via `Seal.open` for assertions), `README.md` (document the report path, the keys, and the launch arguments), `Info.plist` version patch bump in the project's existing way.

**Interfaces:** consumes the test server from task 10 (`scripts/apple_test_server.sh`, fake Firestore on 8792; the app is launched with `-reportFirestore http://<test server>:8792`), and the accessibility identifiers from task 14.

- [ ] **Step 1: Write the failing tests.** Phone (run on the iPhone simulator, and the same class on iPad): `testSettingsReportAnIssueConsentThenSend` (tap Settings, Report an issue, consent Send, type message, Send, see `report.sent`, the fake holds one doc that opens with the vector key and has `source` `ios` or `ipados`), `testNeverMindSendsNothing`, `testErrorAlertOffersReportThisProblemWithTheError` (play a failed recording, alert, tap the button, the sealed doc's `trigger.error` matches), `testEmailPrefilledFromServerAndRememberedAfterRelaunch`, `testServerDownStillSendsAppPart` (stop the test server's `/api/report/server`; the doc has `serverNote`), `testSendFailureShowsSavedCopyAndRetriesLater`, `testCrashOfferAfterRunningMark` (launch with `-simulateCrashMark 1`, the offer shows, send, then relaunch shows no offer), `testCrashSwitchOffNoOffer`. TV: `testTVReportAnIssueMessageFieldAcceptsText` (focus the message field, type with the simulator keyboard, assert the typed text is in the field; dictation itself cannot run in the simulator, say so in a comment), `testTVErrorAlertOffersReport`, `testTVSendAndSentScreen`, `testTVCrashOffer`. Copy assertions use the exact strings in Global Constraints.
- [ ] **Step 2: Run** `PhoneTests`/`ReportTests` and TV `ReportTests` via the VM runner (iPhone, iPad and Apple TV simulators). Expected: FAIL before tasks 11 to 15 are in; after them, continue.
- [ ] **Step 3: Fix** whatever the tests show in tasks 11 to 15 until green; do not weaken a test.
- [ ] **Step 4: Run** the full XCUITest suites for Phone, iPad and TV via the VM runner, and look at the screen with the VM screen viewer for the consent and report screens on all three. Expected: all pass, layouts clean.
- [ ] **Step 5: Bump the patch version**, add the changelog line to the apps' README section, commit in the private repo (`Reports: iPhone, iPad and Apple TV can send a report`), then upload to TestFlight with the `*-testflight-upload` wrappers (standing OK for finished versions, per skill apple-mac-deploy). The App Store privacy label note (Diagnostics and Contact Info, optional) goes in `README.md` under "Before the App Store".

---

# Phase C: the reader (owner side, private repo)

## Task 17: The reader tool

**Files:**
- Create: `tools/ce_reports.py`, `tools/test_ce_reports.py`
- Modify: `README.md` (usage)

**Interfaces:**
- Consumes: the Firestore document shape (task 5), the sealed format (task 1: seal(gzip(json))), service-account JSON path from env `CE_REPORTS_SERVICE_ACCOUNT` (the file lives outside every repo), the private keys from the folder in env `CE_REPORTS_KEYS` (one file per key id, `<keyid>.key`, base64), `google-auth` for the token, `httpx` for the REST calls, `nacl` for opening.
- Produces: `ce_reports.py pull [--out DIR] [--all]` which lists documents in `reports` where `fetchedAt` is unset, and for each: tries the key named by `keyId` first, then every key held; drops junk (not base64, over the cap, wrong fields, no key opens it, an `installId` sending more than 10 reports in a day: keeps the first 10, marks the rest `junk`) into `DIR/junk/` counts only; writes `DIR/<created>-<source>-<reportId>/` with `report.json`, `message.txt`, `app.log`, `server.log`, `summary.md` (one screen: source, version, OS, trigger, error, whether the server answered, message, email, install id) then PATCHes `fetchedAt`. `pull` prints counts only (`pulled N, junk M`) and never prints any report content or key. `--dry-run` writes nothing and marks nothing.

- [ ] **Step 1: Write failing tests** (`tools/test_ce_reports.py`, using `tests/fake_firestore.py` copied or imported from the public checkout by path, and the vector keys): `test_pull_writes_readable_folder`, `test_marks_fetched_and_second_pull_gets_none`, `test_tries_every_key_when_key_id_unknown`, `test_junk_is_counted_not_written`, `test_flood_from_one_install_capped_at_ten`, `test_dry_run_changes_nothing`, `test_output_never_prints_content` (capture stdout), `test_old_key_id_still_opens_after_new_key_added`.
- [ ] **Step 2: Run** `python -I -m pytest tools/test_ce_reports.py -v` in `clients/apple`. Expected: FAIL.
- [ ] **Step 3: Implement** `tools/ce_reports.py`.
- [ ] **Step 4: Run** the tests. Expected: PASS. Then, lead only, run `pull --dry-run` against the real project once after the first TestFlight report.
- [ ] **Step 5: Commit** in the private repo `Reports: reader tool for the owner`.

---

## Self-Review (done)

- **Spec coverage:** Goals (button on errors, support form, crash offer, rich report, optional message, TV dictation field, email prefill and memory, consent) map to tasks 8, 14, 15, 16. Sealed box, queue, no cleartext: 1, 5, 13. Firebase docs, rules, App Check, abuse limits: 0, 5, 9, 13. Report contents and "server did not answer": 4, 12, 14. Scrubbing and tests: 3, 12. Server part, ring buffer, server-sent reports, plex.tv email: 2, 4, 6, 7. Consent and the crash-prompt switch: 7, 8, 14, 15. Reader: 17. Calls 1 to 10: path per source (5, 7, 13), quota and install id (5, 13), who may fetch (6), keys and key id (0, 1, 5, 17), email (6, 14), crash (15), shared button and non-error exclusion (8, 14), masking with per-report salt (3, 12), order (phases), App Store label (16 README).
- **Gaps found and noted:** Task 0 step 2, App Check is enforced per Firestore project, so enforcing it breaks the web and server paths; the plan makes it a lead decision with the spec's design as the first option.
- **Type consistency:** `Scrubber`, `build_report`/`ReportBuilder.build`, `pack`, `submit`/`ReportSender.submit`, the eight document fields, accessibility identifiers and exact copy strings are named once and reused.
- **Proportion:** signatures, test names and assertions only; no function bodies.
