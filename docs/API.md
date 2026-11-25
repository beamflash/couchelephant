# HTTP API

Everything the browser does, it does through these. There is no separate
public API and no versioning: this is one app talking to itself, written down
so you can drive it from a script if you want to.

With sign-in on, every path except the sign-in screens and `/healthz` needs a
session. An unauthenticated call to anything under `/api/` gets `401` with a
JSON body rather than a redirect. A signed-in account without the `admin`
role gets `403` on settings, accounts, backups, the backing store, and
export/import. See [AUTH.md](AUTH.md).

## Guide

| | |
| --- | --- |
| `GET /api/grid` | A window of the guide. `start`, `end`, `choffset`, `chlimit`. Returns channels and airings for that window |
| `GET /api/program?airing_id=` | One programme: every airing of it, which is live, the teams, and **why** it is being recorded if it is |
| `GET /api/facets` | Counts for the filter panel: kinds, channels, genres, teams |
| `GET /api/notices` | What is wrong with Plex right now, and how long it has been wrong |
| `GET /partial/airings` | Search result rows, for infinite scroll |
| `GET /logo/{vcn}` | A channel logo. Yours if you supplied one, otherwise the guide's, otherwise a blank |

## Recording

| | |
| --- | --- |
| `GET /api/record/options?airing_id=` | What Plex offers for this programme, read from its own template, plus the networks and channels a rule could be limited to |
| `POST /api/record` | Schedule a broadcast. `airing_id`, `template`, `settings` (JSON), `networks` (JSON), `channels` (JSON), and `recorder`: `plex` or `couchelephant`. Left out, `recorder` is the default setting |
| `POST /api/record/cancel` | Cancel something we booked, with either recorder. `airing_id` |
| `POST /api/pass` | Follow a team straight from the programme panel. `team_id`. Runs the passes at once, so the next game is booked now rather than at the next sync |

`POST /api/record` refuses while preview mode is on, and says so.

`GET /api/record/options` also answers `recorder`: whether CouchElephant's own
recorder can run here, and which recorder a new booking starts with.
`POST /api/rules` and `POST /api/rules/{id}` take the same `recorder` field,
and each pass in `GET /api/rules` carries it.

### Channels per game

A pass (`team` or `smart`, never `series`) says how many airings of one game it
books with `channels_per_game`: `one` (the default, and what every pass made
before 1.5.35 has), `preferred` (every channel carrying the game in the
`preferred_language` setting, or one airing when none does) or `any` (every
channel carrying it). Sent on `POST /api/rules`, `POST /api/rules/{id}` and
`POST /api/tv/passes/create`, and on `POST /api/tv/passes/{id}`; left out on an
edit it means unchanged. Every pass in `GET /api/rules` and in
`GET /api/tv/recordings` (`passes`) carries it. Any other value, or a value
other than `one` on a series pass, answers `400`.

| | |
| --- | --- |
| `POST /api/channels-preview` | What the choice would cost, before it is saved. `channels_per_game` (`preferred` or `any`), then either `rule_id` (a pass that exists) or `kind` with `team_id` or `team`, or `kind` `smart` with `filter`; optional `networks` and `channels` (JSON lists). Answers `recordings` and `recordings_one` over the next `days` (30), `peak` and `peak_one` (the most at once), `tuners` (the tuner count, or null when it cannot be asked), `over_tuners`, and `message`, a sentence to show |

## Television clients and live viewing

What the Apple TV and iPhone apps read, and the web app's **Home** tab and
Recorded list.

| | |
| --- | --- |
| `GET /api/tv/hello` | Open. Who this server is (`id` is the install id, the same as in its Bonjour record), and whether a client must pair first |
| `GET /api/connect.svg` | Administrators only once sign-in is on. A QR code of `couchelephant://connect?url=<address>`, the address a phone on the home network can reach. With Use away from home on, the address is `https://` and the link ends `&fp=<fingerprint>`, the server's key (SHA-256 of the public key, base64url). 409 with `detail` when there is none |
| `POST /api/tv/pair` | Open. A client asks to be let in. `name`. Answers a six-letter `code` to show and a `secret` to poll with |
| `POST /api/tv/pair/poll` | Open. `secret`. Answers `approved`, and the client's `key` exactly once |
| `GET /api/tv/schedule` | Paired device. `offset`, `limit`, `start`, `end`. The schedule the web Agenda and Calendar draw: bookings and failed ones, with `more` for paging |
| `GET /api/tv/expectations` | Paired device. What every pass is still waiting for, with `when` worded at the guide's precision |
| `GET /api/tv/recordings/<id>/stream` | Paired device. The playlist address to play from, asked for when play starts. With sign-in on it is `/hlsk/<pass>/<uid>/master.m3u8`, a pass for one recording (24 hours, signed with a server secret kept outside the database, ends when the device is deleted or re-keyed), for a TV that fetches the stream itself over AirPlay; the segments inherit the pass from the path |
| `GET /api/tv/devices` | Administrators. Paired televisions, and codes waiting |
| `POST /api/tv/devices/approve` | Administrators. `code`. Lets that television in |
| `POST /api/tv/devices/{id}/delete` | Administrators. Locks that television out |
| `GET /api/tv/home` | Three lists: `live_now` (recording now, by either recorder), `recent`, `on_now` |
| `GET /api/tv/recordings/{id}` | One recording. Asking also has its viewing copy made again if it was tidied away |
| `POST /api/tv/recordings/{id}/position` | `seconds`. Where the player is. This is also the "somebody is watching" signal that holds back the move to Plex |
| `POST /api/tv/live/{vcn}` | Watch a channel now. Joins a recording of that channel if there is one, so no second tuner is used |
| `POST /api/tv/live/{id}/keepalive` | Still watching. Live viewing nobody keeps alive is closed after a minute and its tuner given back |
| `GET /api/tv/guide` | The grid for a television: every channel and its programmes from now for `hours` (24). Each programme says whether it is set to record and by whom |
| `GET /api/tv/program?airing_id=` | One broadcast: summary, whether it is on now, whether it records, and what "every one of these" can mean for it (its series, or its teams) |
| `POST /api/tv/record` | Record from a television, always with CouchElephant's recorder. `airing_id`, and `scope`: `one`, `series`, or `team` with `team_id` |
| `GET /api/tv/record/options` | What "Record every episode" or "Record every <team> game" can be set to before the pass is made. `airing_id`, `scope` (`series` or `team`), `team_id`. The web's `/api/record/options` answer with only Plex's recurring choices left, `pick` (the index of the one asked for), and the recorder default `couchelephant` wherever it can run |
| `POST /api/tv/record/all` | Make that pass with the chosen options, through the web's own `/api/record`. `airing_id`, `template` (an index from the options), `settings` (JSON), `networks` and `channels` (JSON lists), `recorder` |
| `GET /api/tv/passes/options` | The same options for a new pass from the + button, before it is made. `kind` (`series`, `announced` or `team`), `id`, `title`. The answer has the shape of `/api/tv/record/options` |
| `POST /api/tv/passes/create` | Make that pass. `kind`, `id`, `title`, `template`, `settings`, `networks`, `channels`, `recorder`, `channels_per_game`. A series or a team goes through the web's own `POST /api/rules` |
| `POST /api/tv/record/cancel` | Cancel one booking, with either recorder. `airing_id` |
| `GET /api/tv/recordings` | `upcoming`: what is set to record, each with `booked_by` and `recorder`. `recorded`: everything recorded, each with `group` and `category` (`sports`, `tv`, `movies`). `passes`: every pass and Plex rule, each with `tab` (`sports` or `season`) and `upcoming`, how many of its bookings have not started. A pass that was deleted with recordings left booked is one more row with `who` `orphan`, titled "Title (pass deleted)", `lost_id`, `upcoming` and `can_follow` |
| `POST /api/tv/passes/{id}` | Change a pass from a television. Any of `recorder`, `enabled`, `networks` (JSON), `start_minutes`, `end_minutes`. Only what is sent is changed |
| `POST /api/tv/passes/{id}/delete` | Stop following. `who`: `ce` for a pass, `plex` for a Plex rule. `cancel_upcoming` (`1` or `0`, default `0`): `1` also cancels what the pass booked and has not started, with either recorder, and the answer has `cancelled`. Recording, finished and moved recordings are never touched. If Plex cannot cancel, nothing changes and the answer is `502` |
| `GET /api/tv/passes/lost` | Passes that were deleted with recordings left booked, one per lost pass: `lost_id`, `title`, `upcoming`, `can_follow` |
| `POST /api/tv/passes/lost/{lost_id}/cancel` | Cancel every upcoming booking of a lost pass |
| `POST /api/tv/passes/lost/{lost_id}/follow` | Follow the series again with default settings and give it those bookings. `409` for a lost team pass, which cannot be rebuilt from its games |
| `POST /api/tv/record/move` | Hand one booked broadcast to the other recorder. `airing_id`, `recorder` |
| `POST /api/tv/recordings/{id}/retry` | Give a finished recording to Plex again now, instead of waiting for the automatic try. `409` unless it is finished and waiting. `GET /api/tv/recordings` gives such a row `handover_error` (the reason, in words) and `retry_in` (seconds to the automatic try) |
| `POST /api/tv/recordings/{id}/delete` | Delete for good, from Plex too. `403` unless this television's Allow delete is on |
| `POST /api/tv/devices/{id}/settings` | Administrators. `allow_delete` for one television |
| `GET /hls/{uid}/index.m3u8` | The viewing copy, as an HLS playlist. `?start=0` marks it to open at its first second, which is how a player starts a recording that is still growing from the beginning |
| `GET /hls/{uid}/seg00000.ts` ... | Its segments |
| `GET /api/recorder/library-suggest` | TV and Movies folders read from the Plex libraries, each with `writable` and `why`. Administrators only. Nothing is saved |
| `GET /api/recorder/status` | What the CouchElephant DVR settings show: can it run, the tuner, the library folders and Plex libraries to choose from |
| `POST /settings/recorder` | Save the CouchElephant DVR settings |

With sign-in on, a paired client sends `Authorization: Bearer <key>`. The key
opens `/api/tv/` (not the device list), `/hls/` and `/logo/`, and nothing
else. It is stored hashed.

A recording in these lists has `recorded_by`. One Plex is making has
`"recorded_by": "plex"`, `"playable": false` and a `note`, and an id that is
not a number.

## Rules and passes

| | |
| --- | --- |
| `GET /api/rules` | Every rule, ours and Plex's own, with what it follows and where it may record from, and `upcoming` for each of ours. Lost passes come last as `who` `orphan` |
| `GET /api/rules/options` | Plex's own choices for a rule. `kind` (`team`/`series`), `team_id` or `series` |
| `POST /api/rules` | Create one. `kind` is `team`, `series` or `smart`. With no source limit a team or series rule becomes a plain Plex rule instead; a smart one is always ours. `channels_per_game` (below) makes a team pass ours too |
| `POST /api/rules/{id}` | Change a pass: `networks`, `channels`, `enabled`, `settings`, `channels_per_game`, and for a smart pass `filter` and `name` |
| `GET /api/filter/fields` | What a smart filter can ask about: fields, the comparisons each kind accepts, the values in your guide, and how much of it carries a content rating |
| `POST /api/filter/preview` | Count what a filter would record before creating it. `filter` (JSON tree), `networks`, `channels`. Returns the count, a sample, and a warning when it is loose |
| `GET /api/rules/{id}/upcoming` | What that pass will record next, and why it chose each broadcast |
| `POST /api/plexrule/{key}/delete` | Remove one of Plex's own rules |
| `POST /passes/{id}/toggle` | Pause or resume a pass |
| `POST /passes/{id}/delete` | Remove a pass. `cancel_upcoming` as for `/api/tv/passes/{id}/delete`. Answers JSON |

## Past the end of the guide

Plex's guide reaches about twelve days. These cover the things it has not
reached, which are held as intentions and never booked: only a guide airing
carries a channel.

| | |
| --- | --- |
| `GET /api/announced?q=` | Series and films the outside sources know about, whether or not the guide has them. A separate request from `/api/series` on purpose, so a slow third party can never slow the guide search down. A source that fails is skipped rather than failing the lot |
| `POST /api/announced/follow` | Follow one of them. `source`, `source_id`, `title`. Creates an ordinary pass, so nothing special has to happen when the guide finally carries it. Following the same thing twice reuses the pass |
| `GET /api/expectations` | What every pass is still waiting for, with `when` already rendered. Rendered on the server because `precision` decides the format, and a client left to guess would show a time nobody published |

A team is never followed through here. The team picker makes that pass, and the
sync fills in its games.

## Notices

| | |
| --- | --- |
| `POST /api/notices/{code}/dismiss` | Wave off a suggestion. **Refuses any severity but `tip`.** A health problem is answered by fixing it, not by clicking it away |
| `POST /passes/run` | Run every pass now |

## Pages

| | |
| --- | --- |
| `GET /` | The first tab: Home where CouchElephant DVR is on, the guide where it is not. With a search or a day in the address it is the guide, as it used to be |
| `GET /home` | Home: recording now, recent recordings, on now |
| `GET /guide` | The guide |
| `GET /recordings` | Recordings: Scheduled, Sports pass, Season pass and Recorded. All fetched from the API above |
| `GET /settings` | The settings window, on its own page |
| `GET /search` | Guide search results |
| `GET /partial/settings` | Just the settings window, for the gear to open in place |

## Schedule

| | |
| --- | --- |
| `GET /api/schedule` | The effective schedule on the Plex server. `offset`, `limit`, `start`, `end`. Every row says who booked it and why |
| `GET /api/teams?q=` | Teams appearing in the guide |
| `GET /api/series?q=` | Programmes still to air, grouped by show |
| `GET /api/sources` | Networks and channels a rule can be limited to |

## Away from home

With **Use away from home** on, a request from outside the home network
reaches only `/login`, `/favicon.ico`, `/api/tv/hello`, static files, paired
device routes and `/hlsk/` video links that carry a valid pass. Everything
else answers 401 (API paths) or redirects to `/login`. `/setup`, `/welcome`,
`/settings`, `/partial/settings`, `/api/away`, `/docs`, `/openapi.json` and
`/redoc` answer 404 from outside, signed in or not, and so do the admin APIs:
`/api/import`, `/api/export`, `/api/backups`, `/api/backingstore`,
`/api/update/apply`, `/api/tv/devices` and `/api/connect.svg`. `/api/tv/settings`
from outside is for a paired device with its key only. A request with no `Host`
header from outside answers 400. `/api/tv/pair` and
`/api/tv/pair/poll` answer 403 "Pair this device at home first." from outside,
unless the request is HTTPS to the domain that holds a real certificate.

From outside, `POST /login` and `POST /api/tv/pair` answer **429** with a
`Retry-After` header (seconds) after repeated failures or asks, per address and,
for `/login`, per account.

A request that carries `X-Forwarded-For` or `Forwarded` from a peer that is not
under Trusted proxies is never treated as home.

The Bonjour record carries `tls=1` and `fp=<fingerprint>` while away is on,
and `tls=0` otherwise. `GET /api/tv/hello` has no new fields.

## Settings

| | |
| --- | --- |
| `POST /settings` | Server address, token, timezone, sync interval, preview mode, `preferred_language` (`en`, `es`, `fr`, `pt`, `de` or `it`; empty leaves it). `POST /welcome` and `POST /settings/plex/signin/{pin}/use` take it too, at setup |
| `POST /settings/plex/signin` | Start Sign in with Plex. `{ok, pin, code, link}`: show `code` and send the person to `link` |
| `GET /settings/plex/signin/{pin}` | Poll it. `{ok, state}` where `state` is `waiting`, `servers` (with `servers: [{name, id}]`, owned servers only, and a `detail` when the list is empty), `expired` (also for a pin this install did not start) or `error` (with `detail`: plex.tv did not answer or refused; the pin is still good, so keep polling) |
| `POST /settings/plex/signin/{pin}/use` | Form `server` (an id from the poll), optional `timezone`. Tests each address (for a local server, the `plex.direct` name then the plain `http://host:port`) and saves the first direct one that works; a relay is never saved: `{ok, detail, address}`, or `{ok: false, detail, tried}` |
| `POST /settings/test` | Check the Plex connection. Send `Accept: application/json` for `{ok, detail}` |
| `POST /settings/auth` | Sign-in mode and the Cloudflare fields. Turning sign-in off is refused while away is on |
| `POST /settings/away` | `on` is `1` or `0`. Use away from home. Refused with the reason while any way in has no password |
| `POST /settings/away/domain` | `domain`. Saves the name and starts a Let's Encrypt order in the background. Empty drops back to the own certificate. `{ok, domain}` or `{ok: false, error}`. Needs away on |
| `GET /api/away/domain` | How the order is going: `{domain, state, say, until}`, `state` being `none`, `working`, `ok`, `error` or `waiting` |
| `GET /api/away/check` | The self-check in plain words: a list of `{name, ok, say}`, with an Encryption item |
| `POST /settings/away/account` | `username`, `password`. Turns on Local accounts and makes the first administrator, the fix for a switch with no sign-in |
| `POST /settings/away/password` | `uid`, `password`. Sets a password for an account that has none |
| `POST /settings/logos` | Re-fetch every channel logo |
| `POST /settings/channels/{vcn}/logo` | Upload your own logo for a channel. Multipart, field `logo` |
| `POST /settings/channels/{vcn}/logo/reset` | Go back to the guide's logo |
| `GET /api/channels/languages` | Every channel with its language: `language` (what it counts as: your choice, else the detected one, else the preferred language), `language_detected` and `language_override` (empty when none). Also `preferred_language` and `languages`, the list to choose from (`code`, `name` in its own language, `english`) |
| `POST /settings/channels/{vcn}/language` | Set a channel's language by hand. `language_override` is a code from `languages`, or empty to go back to the detected one. `400` for a code not on the list or a channel that does not exist |
| `GET /api/tv/settings` and `POST /api/tv/settings` | The settings the apps read and change. `values` carries `preferred_language`, and `languages` lists the choices |
| `GET /api/tv/settings/channels` | The same channel list, for the apps |
| `POST /api/tv/settings/channels/{vcn}/language` | The same override, for the apps. Needs the right to change settings, like `POST /api/tv/settings` |
| `POST /settings/users/{id}/delete` | Remove an account |
| `POST /settings/notify/save` | Create or update an alert destination. `events` repeats, once per ticked event. A masked secret posted back is dropped rather than saved over the real one |
| `POST /settings/notify/{id}/test` | Send a real test message. Redirects with the verdict in `?notified=` |
| `POST /settings/notify/{id}/enabled` | Turn one destination on or off. `on` is `1` or `0` |
| `POST /settings/notify/{id}/delete` | Remove a destination, and everything it had been told |
| `POST /settings/notify/chatid` | Ask Telegram for the chat id of whoever messaged the bot |
| `POST /api/theme` | Remember the theme against the account. `theme` is `light` or `dark` |
| `POST /sync` | Run a guide sync now |

## Sign-in

| | |
| --- | --- |
| `GET`/`POST` `/welcome` | First run. Tests the Plex connection before saving anything |
| `GET`/`POST` `/setup` | Create the first account, when sign-in has just been turned on |
| `GET`/`POST` `/login` | Sign in |
| `POST /logout` | Sign out |

## Issue reports

A report is sealed to the owner's public key before it leaves the server, and
only the owner can open it. See "Issue reports" in [ARCHITECTURE.md](ARCHITECTURE.md).

| | |
| --- | --- |
| `GET /api/report/server` | The server's part of a report, scrubbed. Administrators and paired devices only, and from outside only with a device key. `{"ok": true, "server": {...}}`, or `"server": null` with a `note` when it timed out or failed |
| `GET /api/report/email` | The Plex account's email, to fill in the contact line. Same access as above. `{"ok": true, "email": ""}` when there is none |
| `GET /api/report/consent` | `consented` (whether reports may be sent at all) and `queued` (how many wait to be sent) |
| `POST /api/report/consent` | Agree to sending reports. Form field `agree=1` |
| `POST /api/report/send` | Build, seal and send a report. JSON: `message`, `email`, `trigger` (`kind` is `error`, `support` or `crash`, plus optional `error` and `screen`), `client` (`ua`, `screen_size`), `app_log` (up to 500 lines). Answers `sent` or `queued`, the `reportId`, and a `message` to show. `403` before consent |

`consent` and `send` are open to any signed-in person. A send that cannot
reach the owner is kept sealed and tried again every 15 minutes. At most 10
reports a day leave one install.

The document written to Firestore has exactly these fields, and the rules
refuse anything else: `reportId` (32 hex, also the document id), `sealed`
(bytes, at most 900,000), `size`, `created` (a timestamp up to 30 days old),
`source` (`web`, `ios`, `ipados`, `tvos` or `server`), `appVersion`, `keyId`
and `installId` (random, made once). Nothing in it is readable without the
owner's private key.

## Health

`GET /healthz` needs no sign-in and returns whether it is configured, whether
preview mode is on, the last sync, and how many airings and passes it holds.
Useful as a container health check.

## Shapes

Everything under `/api/` answers JSON. Success is `{"ok": true, ...}`. Failure
is `{"ok": false, "error": "..."}` with a matching status code, and the error
is a sentence meant for a person, not a code.

## Backup and restore

See [Your data](DATA.md).

| | |
| --- | --- |
| `GET /api/export` | Download everything you decided, as a zip. `secrets=1` includes the Plex token |
| `POST /api/import/inspect` | What is in an uploaded export, without writing anything. Multipart, field `file` |
| `POST /api/import` | Read an export back in. `file`, `replace`, `secrets` |
| `GET /api/backups/jobs` | Every snapshot job. A stored passphrase is reported as `encrypted`, never returned |
| `POST /api/backups/jobs` | Create or change one. `job_id` to change, `name`, `dest_path`, `every_hours`, `retention`, `passphrase`, `enabled`, `raw_db`, `with_secrets` |
| `POST /api/backups/jobs/{id}/run` | Run it now |
| `POST /api/backups/jobs/{id}/delete` | Remove it |
| `GET /api/backups/archives?dest=` | Archives in a folder, newest first |
| `POST /api/backups/restore` | Put one back. `dest`, `name`, `passphrase`, `replace`. Copies the current state to `before-restore/` first |
| `GET /api/backingstore/config` | Backends, their fields, the current settings and the last run. Passwords are masked |
| `POST /api/backingstore/config` | Save it. A masked password means "leave it alone" |
| `POST /api/backingstore/test` | Connect and touch a store |
| `POST /api/backingstore/run` | Reconcile both ways. `dry_run=1` counts without writing |
| `POST /api/backingstore/restore` | Pull everything down and write nothing back |
| `GET /api/backingstore/status` | What the last run did |
