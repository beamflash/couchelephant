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

## Television clients and live viewing

What the Apple TV and iPhone apps read, and the web app's **Home** tab and
Recorded list.

| | |
| --- | --- |
| `GET /api/tv/hello` | Open. Who this server is, and whether a client must pair first |
| `POST /api/tv/pair` | Open. A client asks to be let in. `name`. Answers a six-letter `code` to show and a `secret` to poll with |
| `POST /api/tv/pair/poll` | Open. `secret`. Answers `approved`, and the client's `key` exactly once |
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
| `POST /api/tv/record/cancel` | Cancel one booking, with either recorder. `airing_id` |
| `GET /api/tv/recordings` | `upcoming`: what is set to record, each with `booked_by` and `recorder`. `recorded`: everything recorded, each with `group` and `category` (`sports`, `tv`, `movies`). `passes`: every pass and Plex rule, each with `tab` (`sports` or `season`) |
| `POST /api/tv/passes/{id}` | Change a pass from a television. Any of `recorder`, `enabled`, `networks` (JSON), `start_minutes`, `end_minutes`. Only what is sent is changed |
| `POST /api/tv/passes/{id}/delete` | Stop following. `who`: `ce` for a pass, `plex` for a Plex rule. What is already booked stays |
| `POST /api/tv/record/move` | Hand one booked broadcast to the other recorder. `airing_id`, `recorder` |
| `POST /api/tv/recordings/{id}/delete` | Delete for good, from Plex too. `403` unless this television's Allow delete is on |
| `POST /api/tv/devices/{id}/settings` | Administrators. `allow_delete` for one television |
| `GET /hls/{uid}/index.m3u8` | The viewing copy, as an HLS playlist. `?start=0` marks it to open at its first second, which is how a player starts a recording that is still growing from the beginning |
| `GET /hls/{uid}/seg00000.ts` ... | Its segments |
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
| `GET /api/rules` | Every rule, ours and Plex's own, with what it follows and where it may record from |
| `GET /api/rules/options` | Plex's own choices for a rule. `kind` (`team`/`series`), `team_id` or `series` |
| `POST /api/rules` | Create one. `kind` is `team`, `series` or `smart`. With no source limit a team or series rule becomes a plain Plex rule instead; a smart one is always ours |
| `POST /api/rules/{id}` | Change a pass: `networks`, `channels`, `enabled`, `settings`, and for a smart pass `filter` and `name` |
| `GET /api/filter/fields` | What a smart filter can ask about: fields, the comparisons each kind accepts, the values in your guide, and how much of it carries a content rating |
| `POST /api/filter/preview` | Count what a filter would record before creating it. `filter` (JSON tree), `networks`, `channels`. Returns the count, a sample, and a warning when it is loose |
| `GET /api/rules/{id}/upcoming` | What that pass will record next, and why it chose each broadcast |
| `POST /api/plexrule/{key}/delete` | Remove one of Plex's own rules |
| `POST /passes/{id}/toggle` | Pause or resume a pass |
| `POST /passes/{id}/delete` | Remove a pass |

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

## Settings

| | |
| --- | --- |
| `POST /settings` | Server address, token, timezone, sync interval, preview mode |
| `POST /settings/test` | Check the Plex connection. Send `Accept: application/json` for `{ok, detail}` |
| `POST /settings/auth` | Sign-in mode and the Cloudflare fields |
| `POST /settings/logos` | Re-fetch every channel logo |
| `POST /settings/channels/{vcn}/logo` | Upload your own logo for a channel. Multipart, field `logo` |
| `POST /settings/channels/{vcn}/logo/reset` | Go back to the guide's logo |
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
