# Several HDHomeRun Boxes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CouchElephant records and plays live TV from every HDHomeRun the house has, picks a box per channel, moves a recording to another box when one fails, and shows each box and tuner in Settings and the apps.

**Architecture:** A new table `tuner_boxes` holds the boxes. `app/tunerbox.py` owns the rows, discovery (Plex device list, UDP 65001) and the per-box lineup and status cache. `app/tuner.py` keeps the public API the rest of the app uses (`pick`, `free`, `total`, `check`, `refusal`, the errors) and the single gate `allowed_boxes()`. `recorder.Capture` takes a picker and fails over between boxes, appending to the same `broadcast.ts`.

**Tech Stack:** Python 3 / FastAPI / httpx / sqlite, Jinja + plain JS, pytest + Playwright, Swift / SwiftUI + XCUITest in the funtimes VM.

**Spec:** `docs/specs/2026-11-27-several-tuners.md` (read it first; its "Settled after the builder check" section is binding).

## Global Constraints

- No emoji and no em dash anywhere (code, comments, copy, commits).
- Comments: short and human, the house style of the file you edit.
- Patch version bumps only. This ships as the next patch after 1.5.42.
- No Claude attribution, no Co-Authored-By line in commits or tags.
- Commits name their paths: `git add <files>` then `git commit <paths> -m "..."`. Never `git add -A` or `git commit -a`.
- Public repo: no host names, LAN addresses or people's names in code, tests or docs.
- A one-box install behaves exactly as today after the upgrade, with no new setting.
- CouchElephant never asks for a named tuner (`/tunerN/`). Always `/auto/v<n>` or the lineup's URL.
- Copy, exact: busy tuner held by someone else is `In use by another app`; recording tuner is `Recording: <title>`; live tuner is `Live: <vcn>`; bridge-network note is `Boxes on your network cannot be found from inside this container. Add them by address.`; gap note is `<n> seconds missing at <h:mm:ss>, the tuner stopped answering`.
- Python tests: `.venv/bin/python -m pytest <path> -v` from the repo root, or `scripts/test.sh --unit -k <name>`. Lint: `.venv/bin/ruff check app tests`. Browser: `scripts/test.sh --ui -k <name>`.
- Apple code lives in `clients/apple` (its own private git repo; commit with `git -C clients/apple`). Builds and tests run in the funtimes VM only (skill apple-mac-deploy).

## Review Focus

1. A box that answers `/discover.json` but times out on `/status.json` every time (some emulators): `pick` must still return it, counted as unknown, and must not slow the 2-second recorder tick. Test in Task 2.
2. Two boxes both answer 503 for the whole broadcast: the recording carries "Every tuner is in use." and retries, it does not spin faster than every 3 seconds. Test in Task 4.
3. The user removes or turns off the box a recording is on while it records: the recording moves to another box at the next failure, it does not crash the loop. Test in Task 4.
4. A typed address with a trailing slash, no scheme, or a port (`192.0.2.5:8080/`): stored normalised once, never doubled into a second row. Test in Task 1.
5. Discovery finds the same box by Plex and by broadcast with different address forms (IP vs host name): one row, not two. Test in Task 3.

---

### Task 1: The box table, migration and rows

**Files:**
- Create: `app/tunerbox.py`
- Modify: `app/db.py` (schema: new table; MIGRATIONS: `recordings.tuner_box TEXT`, `recordings.gaps TEXT`)
- Modify: `tests/fake_tuner.py` (instances, so a test can run two boxes)
- Test: `tests/test_tunerbox.py`

**Interfaces:**
- Produces, in `app/tunerbox.py`:
  - `normalise(address: str) -> str`: `http://host[:port]`, no path, no trailing slash, scheme added.
  - `rows(enabled_only: bool = False) -> list[sqlite3.Row]`: ordered typed first, then by id.
  - `add_typed(address: str) -> int` (row id; an existing row with the same normalised address is returned, not duplicated; probes `/discover.json` once and fills the row if it answers).
  - `add_found(address: str, found_by: str, info: dict) -> int | None` (`info` is a `/discover.json` body; merge rule from spec call 3; never touches a typed row's address).
  - `set_enabled(box_id: int, on: bool) -> None`, `remove(box_id: int) -> bool` (False and no change for a non-typed row).
  - `migrate_settings() -> None`: idempotent; `tuner_url` becomes a typed row, `tuner_detected` a plex row; called from `db` init after the schema.
- Table `tuner_boxes`: `id INTEGER PRIMARY KEY, device_id TEXT, address TEXT NOT NULL, name TEXT, model TEXT, firmware TEXT, tuner_count INTEGER, found_by TEXT NOT NULL, enabled INTEGER DEFAULT 1, last_seen INTEGER, error TEXT, added_at INTEGER`.
- Produces, in `tests/fake_tuner.py`: `start(host="127.0.0.1", state: State | None = None, device_id="FAKE0001", lineup=LINEUP, discover=True, status=True) -> (url, stop, state)`; the old two-value module behaviour stays for the default box through a wrapper so existing tests keep passing (`fake_tuner.STATE` and the `tuner` fixture unchanged). Add `/discover.json` (`DeviceID`, `FriendlyName`, `ModelNumber`, `FirmwareVersion`, `TunerCount`, `LineupURL`, `BaseURL`). `status=False` answers 404 on `/status.json`.
- Add fixture `two_boxes` in `tests/conftest.py`: box A (4 tuners, lineup 5.1, 9.1, 41.1) and box B (2 tuners, lineup 5.1, 62.1), both added as typed rows, yields `(stateA, stateB)`.

- [ ] **Step 1: Write the failing tests** in `tests/test_tunerbox.py`:
  - `test_old_typed_address_becomes_one_typed_row`: set `tuner_url` to the fake box URL, `migrate_settings()` twice, `rows()` has exactly one row, `found_by == "typed"`, `device_id == "FAKE0001"`, `tuner_count == 4`.
  - `test_old_found_address_becomes_a_plex_row`: `tuner_detected` set, row `found_by == "plex"`.
  - `test_an_offline_typed_address_keeps_a_row_without_a_device_id`: `tuner_url = "http://192.0.2.1"`, row exists, `device_id is None`, `error` is text.
  - `test_typed_addresses_are_normalised_once` (Review Focus 4): `add_typed("192.0.2.5:8080/")` then `add_typed("http://192.0.2.5:8080")` returns the same id; address `http://192.0.2.5:8080`.
  - `test_discovery_never_changes_a_typed_row`: typed row for box A; `add_found(<other address>, "network", <A's discover body>)` returns the typed id, typed address unchanged, no new row.
  - `test_an_emulator_reusing_a_device_id_with_another_count_stays_separate`: same DeviceID, TunerCount differs, two rows.
  - `test_only_typed_rows_can_be_removed`.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_tunerbox.py -v`. Expected: FAIL (no module).
- [ ] **Step 3: Implement** the table, migrations, `app/tunerbox.py` functions above and the fake tuner changes.
- [ ] **Step 4: Run** `tests/test_tunerbox.py` and `tests/test_recorder.py`. Expected: PASS.
- [ ] **Step 5: Commit** `Tuners: a table of HDHomeRun boxes, the old setting moved into it`.

### Task 2: Lineups, status and picking a box

**Files:**
- Modify: `app/tunerbox.py` (cache), `app/tuner.py` (public API)
- Modify callers: `app/routes/tv.py:570-580` and `:1599`, `app/routes/record.py:375`, `app/routes/passes.py:905-910`
- Test: `tests/test_tuner_pick.py`

**Interfaces:**
- `tunerbox.lineup(box) -> tuple[list[dict], bool]` (rows, stale). Cached 10 minutes per box id; a failed read keeps the last good lineup up to 3600 s and marks it stale; never raises.
- `tunerbox.status(box) -> list[dict] | None` (None when `/status.json` does not answer). Timeout 2 s, cached 5 s, so one tick asks each box at most once.
- `tuner.allowed_boxes() -> list[Row]`: the gate; returns `tunerbox.rows(enabled_only=True)` today.
- `tuner.ours: dict[int, list[tuple[int, str]]]` is NOT exposed; instead `tuner.hold(job_id: int, box_id: int, vcn: str)` and `tuner.release(job_id: int)` record CouchElephant's own streams in memory (Capture calls them).
- `tuner.pick(vcn: str) -> list[tuple[Row, str]]`: spec "Picking a box" plus calls 6 and 7. URL is the lineup row's `URL`, else `<scheme>://<host>:5004/auto/v<vcn>`. Raises `TunerError(refusal)` when every box that carries the channel refuses it, `TunerError("No tuner address is set. Add one in Settings, Recording, CouchElephant DVR.")` with no boxes, `TunerError("No tuner carries channel <vcn>.")` when lineups exist and none has it.
- `tuner.check(vcn: str) -> None`: `pick(vcn)` without using the result (replaces the `stream_url` calls used as refusal checks).
- `tuner.free(vcn: str | None = None) -> int` and `tuner.total() -> int` (spec calls 4 and 5). An unknown-status box counts `tuner_count` minus our holds on it.
- `tuner.refused_channels() -> set[str]`: channels every carrying box refuses (replaces `tv._unwatchable`'s body).
- `tuner.box_tuners(box) -> list[dict]`: `[{"state": "free"|"ours"|"other", "label": str}]` per spec call 2 and the exact copy in Global Constraints; labels for ours come from a callback `tuner.describe_hold(job_id) -> str` set by the recorder.
- Remove `tuner.base`, `tuner.lineup`, `tuner.channel`, `tuner.stream_url`, `tuner.tuners`; grep must show no caller left (`grep -rn "tuner\.\(base\|stream_url\|lineup\|tuners\|channel\)(" app` is empty).

- [ ] **Step 1: Write the failing tests** in `tests/test_tuner_pick.py`, using `two_boxes`:
  - `test_a_channel_only_one_box_carries_goes_to_that_box`: `pick("62.1")` is `[B]`.
  - `test_the_box_with_more_free_tuners_comes_first`: A has 4 free, B 2: `pick("5.1")[0]` is A; with A's `plex_holds` at 3 (1 free), B first.
  - `test_a_box_without_status_counts_as_unknown_and_sorts_after` (Review Focus 1): B started with `status=False`; B listed after A; `free("62.1") == 2`; whole `pick` call under 3 s when B's status hangs (fake with a sleeping `/status.json`).
  - `test_a_stale_box_is_tried_last`.
  - `test_one_box_with_no_lineup_gets_the_hand_built_url`: single box whose `/lineup.json` 404s, `pick("5.1")` returns it with `.../auto/v5.1` on port 5004.
  - `test_a_refused_channel_is_refused_in_words`: A lists 5.1 with `DRM: 1`, B has no 5.1: `check("5.1")` raises with "encrypted".
  - `test_a_station_one_box_refuses_goes_to_the_other`: A refuses 5.1 (AC4), B carries it plainly: `pick("5.1")` is `[B]`.
  - `test_free_for_a_channel_counts_only_boxes_that_carry_it`: `free("62.1") == 2`, `free() == 6`.
  - `test_total_skips_a_box_not_seen_for_an_hour`.
  - `test_a_turned_off_box_is_never_picked`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement**, then change the callers listed under Files to `check`, `refused_channels`, `total`. `passes._tuner_total` returns `tuner.total() or None` when no boxes.
- [ ] **Step 4: Run** `tests/test_tuner_pick.py tests/test_recorder.py tests/test_api.py` and the existing tuner tests. Expected: PASS. Update `test_the_tuner_says_how_many_are_free` and `test_no_tuner_address_is_said_plainly` only if their call names changed, keeping what they assert.
- [ ] **Step 5: Commit** `Tuners: pick a box per channel, count free tuners across every box`.

### Task 3: Discovery

**Files:**
- Modify: `app/tunerbox.py` (`discover`), `app/web.py:372-390` (replace `_find_tuner` and the `not tuner.base()` check)
- Test: `tests/test_tuner_discovery.py`

**Interfaces:**
- `tunerbox.discover(plex=None) -> dict`: Plex `grabber_devices()` (each answering `/discover.json` becomes `add_found(..., "plex", body)`), then `broadcast()`; refreshes `last_seen`, `error`, name/model/firmware/tuner_count on every row it reaches (typed rows too, address untouched). Returns `{"found": int, "can_find_on_network": bool}` and stores the bool in setting `tuner_broadcast_ok` ("1"/"0").
- `tunerbox.broadcast(timeout: float = 1.5) -> list[str]`: sends one HDHomeRun discover packet (type 0x0002, tags 0x01 device type 0xFFFFFFFF and 0x02 device id 0xFFFFFFFF, CRC32 little endian trailer) to `255.255.255.255:65001`, returns the source IPs that reply. No new dependency. `can_find_on_network` is False when the socket cannot broadcast or nothing answers while the container's only non-loopback interface is a Docker bridge (`172.16.0.0/12` address and no reply).
- `web.recorder_loop` runs `discover` in a thread at start and every 600 s, off the request path, only when `recorder.available()`.
- Same box reached by two address forms: merged by DeviceID plus TunerCount (Task 1 rule); the found row keeps the address it was first found at.

- [ ] **Step 1: Write the failing tests**:
  - `test_plex_named_boxes_are_added_once` (fake Plex `grabber_devices` names box A twice with different forms, one row; Review Focus 5).
  - `test_discovery_refreshes_a_typed_row_but_keeps_its_address`.
  - `test_the_discover_packet_is_what_hdhomerun_expects`: build the packet, assert bytes equal the known-good packet `00 02 00 0c 01 04 ff ff ff ff 02 04 ff ff ff ff` plus its CRC.
  - `test_a_reply_from_a_box_is_read_back`: a local UDP responder on a free port (patch the port) replies; `broadcast` returns `127.0.0.1`.
  - `test_no_broadcast_says_so_and_breaks_nothing`: socket error path, `can_find_on_network` False, no exception.
- [ ] **Step 2: Run**. Expected: FAIL.
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run** the new tests plus `tests/test_tunerbox.py`. Expected: PASS.
- [ ] **Step 5: Commit** `Tuners: find every box Plex names and every box on the network`.

### Task 4: The recorder across boxes, with failover

**Files:**
- Modify: `app/recorder.py` (`Capture`, `Job.start_capture`, `_start_due`, `_make_room`, `_watch_running`), `app/overtime.py:_tuner_wanted`
- Modify: recording JSON in `app/routes/tv.py` (`recording_json`) to carry `gaps` as `[{"at": seconds, "seconds": n}]` and a `gap_note` string list
- Test: `tests/test_recorder_boxes.py`

**Interfaces:**
- `Capture(picker: Callable[[], list[tuple[Row, str]]], path: str, job_id: int)`. On a 503, a connection error, or 15 s with no bytes, it marks the current box and moves to the next entry of a fresh `picker()` call; it waits 3 s only after the whole list failed. It calls `tuner.hold(job_id, box.id, vcn)` on connect and `tuner.release(job_id)` on stop. Exposes `box_id: int | None`, and `gaps: list[dict]` (`{"at": seconds since first byte, "seconds": n}`), a gap counted from the last byte to the next byte after a failover or reconnect, only when over 2 s.
- `_start_due`: calls `tuner.pick(vcn)` once for the refusal check and error text, then hands `Capture` `lambda: tuner.pick(vcn)`. Stores `tuner_box` and `gaps` (JSON) through `_watch_running`.
- `_make_room(now, vcn)`: spec call 5; called once per waiting recording, oldest first.
- `overtime._tuner_wanted`: `free(vcn) == 0` for any waiting recording's channel (spec call 5). `_plex_starting` case: `free() == 0` as today.
- The recorder sets `tuner.describe_hold` to a function giving `Recording: <title>` or `Live: <vcn>` for a job id.

- [ ] **Step 1: Write the failing tests** with `two_boxes`:
  - `test_a_busy_box_sends_the_recording_to_the_other`: A all held by Plex, record 5.1, lands on B (`tuner_box == B.id`).
  - `test_a_box_that_stops_mid_recording_hands_over_to_the_other`: start on A, kill A's server (or set A `silent` past the 15 s no-data limit; patch the limit to 1 s), recording continues on B, `broadcast.ts` keeps growing, `gaps` has one entry with `seconds >= 1`.
  - `test_every_box_busy_waits_three_seconds_between_rounds` (Review Focus 2): both full; count stream attempts over 7 s; at most 3 rounds.
  - `test_turning_off_the_box_mid_recording_moves_it_at_the_next_failure` (Review Focus 3).
  - `test_a_recording_takes_a_live_tuner_only_on_a_box_that_carries_its_channel`: live 9.1 on A (A full otherwise), recording 62.1 due and B full by Plex: live 9.1 is NOT ended; then with live 62.1 on B: it is ended.
  - `test_overtime_wants_the_tuner_only_for_the_waiting_channel`.
  - `test_a_one_box_install_records_as_before`: the existing `tuner` fixture only, recording finishes, `tuner_box` set, `gaps` empty.
- [ ] **Step 2: Run**. Expected: FAIL.
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run** `tests/test_recorder_boxes.py tests/test_recorder.py tests/test_overtime.py`. Expected: PASS.
- [ ] **Step 5: Commit** `Recorder: any box that carries the channel, and a recording moves when its box fails`.

### Task 5: Settings and the API

**Files:**
- Modify: `app/routes/settings.py` (drop `tuner_url` from KEYS and from `settings_recorder`; `recorder_status` reads boxes), new routes in `app/routes/settings.py`
- Modify: `app/templates/_settings.html:396-403` (the field becomes a box list), `app/static/js/app.js:946-952`, `app/static/css` as needed
- Modify: `app/reportbuild.py:86,108` (report: box count, models, found_by; never addresses), `app/reportscrub.py` (no change to settings lists unless a test needs it), `app/demo.py:425` (demo hides box addresses)
- Modify: recording detail in the web app to show `gap_note`
- Test: `tests/test_tuner_routes.py`, a Playwright test in the existing UI suite

**Interfaces:**
- `GET /api/tv/tuners` exactly as spec call 8. `POST /api/tv/tuners` (form `address`) returns `{"ok", "box": {...}}` or 400 `{"ok": false, "error"}`; `POST /api/tv/tuners/{id}` (form `enabled` "1"/"0"); `DELETE /api/tv/tuners/{id}` (400 for a non-typed row). Admin only, through the same dependency the settings routes use.
- `recorder_status()["tuner"]` keeps `url`, `typed`, `free`, `total` (first box, for older apps) and adds `boxes` (same list as the API).

- [ ] **Step 1: Write the failing tests**: shape of `GET /api/tv/tuners` with `two_boxes` (box A one tuner "Recording: <title>", one "In use by another app"); add, turn off, remove typed, refuse removing a plex row; a non-admin gets 403; the report never carries an address. Playwright: open Settings, Recording, see two boxes and their tuner lines, add an address, turn one off.
- [ ] **Step 2: Run**. Expected: FAIL.
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run** the new tests, `tests/test_api.py`, `tests/test_report*`, and `scripts/test.sh --ui -k settings`. Expected: PASS.
- [ ] **Step 5: Commit** `Settings: every tuner box and what each tuner is doing`.

### Task 6: Apple Settings

**Files:**
- Modify: `clients/apple/Shared/SettingsView.swift` (replace the `tunerURL` field at line 200 and its load at 359 with a read-only box list from `GET /api/tv/tuners`, plus "Add by address"), the API client file that holds the other settings calls, a test-server fixture so the test server starts with two fake boxes.
- Test: an XCUITest per platform family in `clients/apple/UITests` (`testSettingsListsEveryTunerBox`).

- [ ] **Step 1: Write the failing UI test**: Settings shows two boxes by name and the line `In use by another app` under one; Add by address with a third fake address shows three.
- [ ] **Step 2: Run it in the VM.** Expected: FAIL.
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run it on iPhone, iPad and Apple TV simulators.** Expected: PASS. Look at a screenshot of each.
- [ ] **Step 5: Commit** in `clients/apple`.

### Task 7: Real boxes, then ship

- [ ] Real test on the owner's network: the Flex 4K plus a Threadfin container fed by an ffmpeg test pattern served on the LAN (one channel number the Flex carries, one it does not). Record both at once, watch a third live, stop Threadfin mid-recording, read the result. Write what works and what is lost without `/status.json` into the spec's Testing section as results.
- [ ] Full suite `scripts/test.sh`; only the known local-environment failures may fail.
- [ ] Opus review of the whole branch; fix every real finding.
- [ ] Version patch bump, `docs/CHANGELOG` entry, tag, release, deploy to production, check the live Settings page shows the Flex.
- [ ] TestFlight the Apple apps.
