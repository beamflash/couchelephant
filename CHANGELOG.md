# Changelog

## 1.5.30 - 2026-11-20

### Added

- **HTTPS on the same port.** With Use away from home on, the one port
  speaks HTTPS as well as HTTP. Nothing else needs forwarding. The server makes
  its own certificate. Browsers warn about it; the apps check its key.
- **A real certificate for a domain.** Under Settings, Accounts, Sign-in, enter
  a domain and CouchElephant gets a free Let's Encrypt certificate with
  TLS-ALPN-01 on port 443, then renews it. A failed try is retried every 15
  minutes. Over HTTPS to that domain a device can pair from outside.
- **The apps pin the server's key.** The phone and TV apps learn the key at
  home (Bonjour record and QR code) and trust only it, or a certificate the
  system trusts, away from home. Video plays through a small proxy inside the
  app that listens on `127.0.0.1` only. A link never replaces a stored pin. If
  the key changes, the app asks you to pair again at home.
- The apps send their key only to the stored address, and play video only
  from it. A video link from behind a TLS proxy is moved onto that address.
- The QR code is an `https://` address and carries `&fp=` while away is on.
- docs/AUTH.md has an "Away from home" section, and docs/INSTALL.md says what
  to forward on the router.

### Security

- **Trusted proxies.** A field under the Use away from home switch (Settings,
  Accounts, Sign-in) takes the address of a reverse proxy or tunnel in front of
  the server (a block no wider than /8, or /32 for IPv6). A request that carries
  `X-Forwarded-For` or `Forwarded` from any other address is never treated as
  home, and the self-check says so.
- Admin APIs (`/api/import`, `/api/export`, `/api/backups`, `/api/backingstore`,
  `/api/update/apply`, `/api/tv/devices`, `/api/connect.svg`) answer 404 from
  outside, and `/api/tv/settings` from outside is for a paired device only.
- A request with no Host header from outside is refused while away is on.
- Headers must be complete within 10 seconds, and open connections are capped
  below the descriptor limit.
- The sign-in limiter has a hard ceiling that also drops the oldest locked keys.
- The first request over HTTPS is told apart from HTTP by its first byte
  alone. A client that says nothing is dropped after five seconds.
- The challenge certificate is served only to a client that offers
  `acme-tls/1`.

### Changed

- The self-check's Encryption line now says whether the connection uses this
  server's own certificate or a real one.

## 1.5.29 - 2026-11-20

### Security

- **Fixed a sign-in bypass.** A request with a crafted Host header could
  reach pages that need sign-in, Settings included, without signing in. It
  affects every earlier version with Local accounts that is reachable from
  outside your home network. Update now. A Host header with a slash, a
  question mark, a hash, an at sign or a space is now refused.
- **Fixed a cross-site form.** A form posted from a sandboxed frame on another
  site (Origin `null`) was taken as coming from CouchElephant itself.
- Sign-in attempts are limited: five wrong passwords in ten minutes, from one
  address or at one account, locks for a minute, doubling up to an hour. Tries
  from your home network never lock an account.
- Every page carries security headers (no framing by other sites, no
  sniffing, a content security policy) and no `Server` header.

### Added

- **Use away from home**, under Settings, Accounts, Sign-in. It lets only
  sign-in, paired apps and video links in from outside your home network, and
  keeps Settings, setup and pairing home only. It will not turn on while any
  way in has no password, and it offers to make the first account. HTTPS on
  the same port comes in the next release; until then the self-check says the
  connection is not encrypted.

## 1.5.28 - 2026-11-20

### Fixed

- A container on host networking whose home network uses 172.16 to 172.31
  addresses did not announce itself, and its QR code found no address.
  CouchElephant now tells Docker's own networks apart by their interfaces,
  not by the address range.

## 1.5.27 - 2026-11-20

### Added

- **The apps find the server.** CouchElephant announces itself on the home
  network, and the iPhone, iPad and Apple TV apps list every CouchElephant
  they find by name; one tap or press connects. Typing the address is still
  there, below the list. If nothing shows after a few seconds, the typed
  address leads, with the last address that worked.
- If the server's address changes, a paired app finds it again by itself and
  keeps its pairing.
- **A QR code to connect a phone**, under Settings, Recording, Televisions and
  on the welcome page once Plex is set. Scan it with the iPhone or iPad camera.
- A switch under Settings, Recording, Televisions turns the announcement off.
- `COUCHELEPHANT_ADDRESS` names the home network address for the QR code, for
  a container on Docker's bridge network.

### Changed

- The relay alert destination is now a general JSON webhook that you can point
  at your own script, relay, Home Assistant or n8n. Existing relay destinations
  move over by themselves.

### Notes

- On Docker's default bridge network the server cannot announce itself, and
  says so once in its log. Use host networking, or type the address in the
  app. On Unraid, set Network Type to Host.

## 1.5.26 - 2026-11-19

### Added

- **Sign in with Plex.** The welcome page and Settings, Plex have one button.
  It shows a four-character code to type at plex.tv/link, lists the Plex
  servers your account owns by name, and saves the first address of the chosen
  server that answers and has a DVR. For a local server it tries the plain
  address (`http://host:port`) after Plex's `plex.direct` name, because many
  home routers will not resolve that name. A relay address is never saved: if
  only the relay answers, it says so. If none answers it lists every address
  it tried. Servers shared with you by a friend are not listed. Typing the
  address and token yourself is still there: a link, "Enter it yourself", on
  the welcome page, and the form itself in Settings, Plex.
- The welcome page's save is locked once CouchElephant is set up, so nobody
  on the network can repoint Plex. Change it under Settings.
- **Recording folders suggested from Plex.** Settings, Recording fills in the
  TV and Movies folders from your Plex libraries and says whether CouchElephant
  can write to each. Nothing is saved until you press "Use these folders".

### Fixed

- On the macOS and Linux installs, a full folder path for TV or Movies
  recordings lost its leading slash when saved, so recordings were not moved
  into Plex. A normal Windows `C:\` path was not affected.

## 1.5.25 - 2026-11-19

### Added

- **One download per system.** Windows (10 and 11), macOS (13 and later,
  Apple silicon and Intel) and Linux (Debian, Ubuntu, Fedora, RHEL and
  Rocky, amd64 and arm64) each have an installer that sets up CouchElephant
  with everything it needs, including ffmpeg and comskip. It listens on port
  8710 and moves up to the next free port, up to 8759, if that one is taken.
  Docker and Unraid installs are unchanged.
- **couchelephant.app** is the download page. On Linux, one command
  installs it: `curl -fsSL https://couchelephant.app/install.sh | sudo sh`.
- **Updates.** CouchElephant checks once a day for a newer release and shows
  a banner. On Windows the icon next to the clock offers Update, on macOS
  the menu bar icon does, each checking the download against its checksum
  first. On Linux the banner shows the one command to run.
- **Pause recordings** is now a switch in Settings, in the CouchElephant DVR
  section, as well as in the Windows icon. It matters when sign-in is on,
  because the icon cannot sign in.

### Changed

- On Linux the data folder is private to the CouchElephant service, the
  service cannot gain new privileges or change system folders, and it joins
  the video and render groups when they exist.
- The macOS app makes its recordings folder itself, remembers the port it
  used last time, and an update replaces the copy that is running. Both
  builds of macOS programs are made for macOS 13, and the build refuses a
  program that needs a newer one or loads a library from Homebrew. The macOS
  app has no PostgreSQL store, because its driver needs macOS 14; SQLite and
  MySQL work as before.
- The Windows uninstaller asks whether to remove your recordings list and
  settings (default No). A silent uninstall still keeps them unless told not
  to.

### Fixed

- The Windows icon next to the clock could fail to start, because it loaded
  the whole web app, which its build does not carry. It now loads only what
  it needs, and the install test checks it is still running after ten
  seconds, on a fresh install and an upgrade.
- A Windows update is checked again right before it runs, in case the
  downloaded file was swapped in between.
- The Docker health check follows `COUCHELEPHANT_PORT`.
- Time zone data is bundled, so times draw correctly in the Windows build.
- A release tag that does not match the version is refused.
- On Linux, recordings handed over to Plex stay readable by a Plex server on
  the same computer, even though the service keeps its own data private.
- Windows setup stops with a message when ports 8710 to 8759 are all in use,
  instead of picking a port outside that range.

## 1.5.24 - 2026-11-19

### Changed

- **The logo and tagline are at the top of every tab** on the Apple TV,
  iPhone and iPad, above the tab's content, and the server version is no
  longer shown on Home. Settings has an About section at the bottom with
  the app version and the server version. On the Guide tab the logo stays
  above the grid, because the grid scrolls both ways with its times held
  still.

### Fixed

- **Watching a channel that is being recorded plays it.** Choosing the
  channel in the Guide on the Apple TV, iPhone or iPad showed the player
  with a spinner that never ended. A recording is ready at once, so it was
  put on screen while the channel's own sheet was still closing, and the
  player was shut down as that happened. Live TV was never affected,
  because tuning takes a few seconds.
- **Play/Pause on the Apple TV remote pauses live TV on a real remote.** A
  real Siri Remote sends the button to the app as a playback command, not as
  a press on the screen, so the 1.5.23 fix only worked in the simulator.
  The app now answers the command too, and still never toggles twice.

## 1.5.23 - 2026-11-18

### Changed

- **The guide keeps its channels and times in view.** On the Apple TV,
  iPhone and iPad, the channel column stays at the left and the row of
  times stays at the top while the guide scrolls either way, in the Guide
  tab and over live TV. The web guide already did this.
- **The guide over live TV is about half see-through**, in the apps and on
  the web, so the programme stays easy to follow while you browse.
- **One layout for every season pass prompt.** Recorded by comes first.
  Plex settings, Filter networks and Filter channels follow, each closed
  until opened. This is the same on the web, the Apple TV, the iPhone and
  the iPad.
- **A new season or sports pass from the + button asks how first.** In the
  apps, picking a series, a movie or a team opens those options before the
  pass is made, as Record every episode does.
- **The programme with focus in the Apple TV guide is easy to read.** Its
  box stays dark with an amber edge, so white channel logos no longer
  disappear into it.

### Fixed

- **Play/Pause on the Apple TV remote pauses live TV.** The system player
  ignored it on a live channel. The app now pauses and resumes itself when
  the player does not.
- **The Apple TV Top Shelf shows your recordings.** The rows over the home
  screen (Recording now, Recent recordings) never appeared: the extension
  was built as an old kind of tvOS extension that stopped as it started.
  It also no longer needs a sign-in key on a server with sign-in off.
- **A programme the remote moves to is never left under the channel
  column** in the Apple TV guide.


## 1.5.22 - 2026-11-18

### Changed

- **Record every episode asks how first, in the apps as on the web.** On the
  Apple TV, iPhone and iPad, Record every episode and Record every <team>
  game no longer make the pass at once with no say. They open the same
  options the web shows before its Schedule button: who records it,
  networks and channels to limit it to, and Plex's own settings for the
  rule (resolution, replacing lower resolution, partial airings, minutes
  before and after, and whatever else Plex offers), read from Plex. Record
  all makes the pass through the web's own code, and the answer shows on
  the same screen with Done to close it.

## 1.5.21 - 2026-11-18

### Fixed

- **The player's buttons no longer cover the volume.** On the iPhone turned
  sideways, the i, Guide and Live buttons sat over the player's volume
  slider. They now sit under the player's top row, upright and turned, on
  the iPhone and iPad. A clicking test checks that none of our buttons
  touches one of the player's own.

## 1.5.20 - 2026-11-17

### Added

- **Background play on the iPhone and iPad.** Leaving the app while
  something plays no longer stops it. Swipe Home or change app and the video
  floats in Picture in Picture; lock the screen and the sound goes on, with
  the title, the programme, its picture and play, pause and skip buttons on
  the lock screen and in Control Center. Live TV keeps its tuner while it
  plays this way. A live channel you paused is still held for the pause limit,
  as before. Settings has a new Background play switch, on by default, for
  this device only. Off, leaving the app stops things as it always did, and
  live TV gives its tuner back at once. The Apple TV is unchanged.
- **AirPlay to a TV.** The video, not just the sound, can be sent to an Apple
  TV or an AirPlay TV from the player's AirPlay button, for recordings and for
  live TV. It plays on when the phone is locked or the app is put away. With
  sign-in on, the playlist address a paired device is given now carries its
  own pass, because a TV that fetches the stream itself cannot send the
  device's key. The pass is good for 24 hours for one recording and stops
  working when the device is removed.
- **Swipe down opens the guide** over live TV on the iPhone and iPad, as
  swipe up and the Guide button already did.
- **The logo has its tagline.** The apps draw the elephant with the name and
  "A CouchElephant never forgets." under it, the way the web header does, on
  the iPhone, iPad and Apple TV.
- **Agenda and Calendar in the apps.** Recordings, Scheduled has the web
  page's two views and its switch on the Apple TV, iPhone and iPad. The
  Agenda lists every booking, soonest first, a page at a time, with failed
  bookings shown in place. The Calendar is a month with a day's recordings in
  its cells and its list under the grid (on the iPhone the cells show a
  count and dots), previous and next month, Today, and what is still awaited
  with no day under the month. Press a recording to open its details.

## 1.5.19 - 2026-11-17

### Added

- **The guide over live TV.** While a channel plays (live TV, or a channel
  being recorded), the guide opens over the lower part of the picture and the
  channel plays on above and behind it.
  On the Apple TV press down (up, or a touch, still brings up the player's
  bar, which also has a Guide button). On the iPhone and iPad swipe up or
  press Guide; in the web app press the down arrow or Guide. It is the same
  guide as the Guide tab: a channel, or what is on it now, changes to that
  channel (live TV gives its tuner back first; a recording carries on);
  anything later opens its details, where it can be recorded, and on the
  iPhone and iPad a long press records it too. Menu, Close, Escape or the up
  arrow close it and the channel plays on.
- **The now line in the apps.** The orange line the web guide draws at the
  current time, with its dot at the top, is in the Apple TV, iPhone and iPad
  guides too, and moves on while the guide is open.
- **Clicking tests for the Apple apps.** Two XCUITest suites press real
  buttons in the simulator, against a test server with an invented guide and
  a fake tuner (`scripts/apple_test_server.sh`): the guide and its now line,
  the iPad turned, the guide over live TV changing channel and recording,
  live TV left with the Home button and opened again, a paused channel kept
  while away, Skip ad, a setting saved, and a new season pass; on the Apple
  TV, the same with the remote.

### Fixed

- The web player changing channel left the old channel's tuner held for a
  minute.
- Found by the new clicking tests: changing channel while the player was open
  kept the old player on screen, black, on the Apple TV, iPhone and iPad.

## 1.5.18 - 2026-11-17

### Changed

- **The iPad.** The iPhone app has always installed on an iPad; it now lays
  itself out for one. The guide has a wider channel column, taller rows and
  bigger text on a full iPad screen, and keeps the phone's sizes in a narrow
  Split View or Stage Manager window. Settings keeps to a readable width on a
  wide screen. The pairing screen says "Let this iPad in" on an iPad. Checked
  on the 13-inch iPad Pro and the iPad mini in portrait and landscape, at half
  and one-third Split View widths, and on the iPhone 17e and an iPhone Pro Max
  on its side.

## 1.5.17 - 2026-11-17

### Fixed

- Live TV left while it played (the app put away, closed, or a browser tab
  lost) held its tuner for the whole pause limit, 30 minutes by default. Only
  a pause the person made holds it now; otherwise the tuner goes back within a
  minute, and the Apple TV and iPhone apps give it back at once as they are
  put away. Coming back to the app tunes the channel again.

## 1.5.16 - 2026-11-17

### Fixed

- A programme with no ads counted as a failed ad scan and was scanned three
  more times. comskip exits with 1 when it finds no ads; that is now an answer.
- A live ad scan started again (after a tuner dropped, or a restart) hid the
  breaks it had already shown until it caught up. They now stay. It also
  waits three minutes, not 40 seconds, for a tuner that drops.
- Sending a resume point to Plex ran inside the recorder's loop, so a Plex
  slow to answer could delay the start or end of a recording. It now runs
  apart, with sports overtime.
- A resume point Plex refused at the move was never sent again.
- Overtime: one league's scoreboard not answering no longer reads as "no
  game"; an unexpected answer no longer stops overtime for every recording
  that minute; and a recording that is starting is no longer taken for one
  waiting for a tuner.
- The players kept asking for ad breaks twice after a quick close and reopen,
  and stopped asking before a finished recording's breaks were final.

## 1.5.15 - 2026-11-17

### Added

- **Games that run long keep recording.** For a game CouchElephant DVR
  records, a live scoreboard (ESPN's) says whether it is still being played.
  While it is, the recording keeps going, ten minutes at a time; once it is
  final, it stops five minutes later. Never shorter than the padding, and
  never more than 180 minutes past the scheduled end (a setting, 0 to 360).
  A game the scoreboard does not list keeps its padding. Another recording
  that needs the tuner wins. The player's header keeps naming the game while
  it runs over. On by default; **Games that run long** in Settings, Recording,
  on the web page and in both apps. Covers the NFL, NBA, WNBA, MLB, NHL,
  college football and basketball, and the main soccer leagues.

## 1.5.14 - 2026-11-17

### Added

- **Skip ads** in CouchElephant's own players: the web page, the Apple TV and
  the iPhone. comskip, now in the image, finds the ad breaks, and a Skip ad
  button shows during each one. Nothing is skipped unless you press it. Two
  settings, both on: **Skip ads in finished recordings**, worked out from the
  whole recording once it ends, and **Skip ads while still recording**, which
  also covers paused live TV. While still recording, a break shows a minute or
  so after it ends and can be shorter than the real one. The copy Plex gets is
  never changed.

### Fixed

- A recording watched in CouchElephant after it moved into Plex now moves the
  resume point in Plex too, a minute after the player stops. Plex kept the
  point from the day of the move.

### Changed

- TV and movies are called TV and Movies everywhere, as in Plex: no more
  "Shows" and "Films" in Settings, the guide filters, the new pass search and
  the server's messages.

## 1.5.13 - 2026-11-16

### Added

- **Settings in the Apple TV and iPhone apps.** A new Settings tab changes
  what the web page changes: the Plex address and its test, the timezone,
  how often to sync, Sync now, channel logos, preview mode, the CouchElephant
  DVR (who records, tuner, padding, pause limit, encoder), where a finished
  recording goes in Plex, the TheSportsDB key, and notifications on, off and
  test. Each change saves at once. Left to the web page on purpose: sign-in,
  people, televisions, backups, logo uploads, and typing secret keys (the apps
  show whether each is set). With sign-in on, a television needs **Allow
  settings** in Settings, Televisions, the same way it needs Allow delete.
- **New season pass and new sports pass** buttons on the Recordings tab of
  both apps. Search a show, a film or a team and follow it. A show the guide
  has not reached yet is found through TVmaze and TMDB and records once it
  appears.

### Changed

- The web forms and the apps save settings through one set of checks, so
  neither can store a value the other refuses. The sync interval is now kept
  between 5 and 1440 minutes.

## 1.5.12 - 2026-11-16

### Added

- **Captions.** The broadcast's own closed captions come through to every
  player. A captions button turns them on and off, in the web player, on the
  Apple TV (in the player's bar) and on the iPhone. Each device keeps the last
  choice for the next video. The playlist a player opens is now a master
  playlist that names the captions (CC1), which is what Apple's player needs
  before it offers them.
- **What is on, in the player.** The Apple TV player shows the channel's logo,
  its number and the programme on screen whenever its bar is up. An **Info**
  button opens the guide's description. A live channel follows the guide into
  the next programme while it plays. The web and iPhone players have the same
  "i". A recording keeps its description from the moment it was booked, since
  the guide forgets a programme once it has aired.

- **Apple TV Top Shelf.** With CouchElephant in the top row of the Apple TV
  home screen, the area above it shows **Recording now** and **Recent
  recordings**, with artwork and how far each was watched. A click opens the
  app and asks where to start; the Play button plays at once. The app and its
  Top Shelf extension share the server address and key through a keychain
  group. A live channel's card shows the picture of what is on it now.

- **Live TV pause limit** (Settings, Recording, default 30 minutes). A paused
  live channel is held that long, even when the Apple TV puts the app to
  sleep, and a pause that reaches the limit plays on from where it stopped. A
  player that closes gives its tuner back at once. A recording that needs the
  tuner still takes it.

### Changed

- The Apple TV and iPhone players have one captions button, the player's own
  subtitle menu. Its choice is still kept for the next video.
- `deploy.sh` refuses while anything records or plays
  (`COUCHELEPHANT_DEPLOY_ANYWAY=1` to override): recreating the container ends
  them.

### Fixed

- **Formats beyond this market.** Every path keeps the broadcast's own
  timing (a constant rate filled gaps with copies that repeated captions), a
  subchannel with no sound converts, a 1080/576/480-line channel is always
  treated as interlaced (one opening on film came out combed), and the
  captions entry carries no language. `docs/BROADCAST-FORMATS.md` lists what
  can arrive in ATSC and DVB markets, how each is handled, and the 28 format
  checks behind it (`scripts/format_matrix.py`, `scripts/channel_sweep.py`).
- **Captions on 720p channels were nonsense, and the picture ran at 120 a
  second.** The hardware deinterlacer made two pictures from every
  progressive one, captions included. Progressive channels are no longer
  deinterlaced.
- **Apple TV: the tab bar could not be reached again from Home.** With
  nothing in "Live now", scrolling down left nothing above "Recent
  recordings" to move up to. An empty shelf's sentence can now hold focus.
- **Apple TV: pressing up from a card that was not the first in its row did
  nothing.** Each Home shelf is now its own focus section, so up from any card
  moves to the shelf above.
- **Apple TV: the Guide tab jumped back to Home the first time.** Nothing could
  hold focus while the guide loaded, so the television moved focus back to
  the first tab. The guide now starts loading when the app opens, and every
  loading screen holds focus with a spinner and a sentence.
- **The iPhone guide showed no channel logos.** A picture with an empty
  placeholder made no view at all, so it never loaded. The Apple TV channel
  page had the same fault.
- **The iPhone player's buttons stayed on screen the whole time.** They now
  come and go with the player's own controls, and stay while paused. The extra
  close button is gone, since the player's controls have one.
- **Captions came out with every letter twice on hardware encoding.** Field
  rate makes two pictures from every interlaced one and both carried the same
  captions. The second of each pair now has them removed. Recordings made
  before this keep the doubled captions.

### Changed

- The web app ships the full hls.js build instead of the light one, which has
  no caption support.

## 1.5.11 - 2026-11-16

### Changed

- **The web app has the same three tabs as the apps: Home, Guide and
  Recordings.** The Live TV tab is gone. What it showed is **Home**, which is
  now the first tab and where the bare address opens. An install without
  CouchElephant DVR has nothing for Home to show, so it keeps two tabs and
  opens on the guide, as before.
- **"Schedule" is now "Recordings"**, and its lists carry the names the apps
  use: **Scheduled**, **Sports pass**, **Season pass**, and **Recorded**.
  Recorded is everything CouchElephant DVR has, by programme from A to Z, with
  All, Sports, TV and Movies, and it plays there.
- The guide has its own address, `/guide`. Old links still work: `/recordings`
  never moved, `/live` goes to Home, and a guide link with a search or a day
  in it still opens the guide.
- A pass row says who records it.

### Added

- **Watch live from the guide.** Where CouchElephant DVR is on, a channel in
  the guide's left column is a button that tunes it.
- **Delete on the web.** A recording in the Recorded list can be deleted by an
  administrator, from Plex too, after a prompt.

### Fixed

- **A Plex rule for a team is listed as sport.** "All Kansas City Chiefs
  Events" follows "NFL Football", and neither is a game's title, so it was
  classed as a series. It is now classed by what it follows.

## 1.5.10 - 2026-11-15

### Changed

- **"Own recorder" is now "CouchElephant DVR".** The settings tab, its heading
  and the messages that send you there use the new name. It sits next to
  "Plex DVR", so the name itself says there are two recorders and which one
  this is. Nothing else about it changed.

## 1.5.9 - 2026-11-15

### Fixed

- **Changing who records a pass now moves the recordings it already booked.**
  Switching a pass from Plex to CouchElephant changed only what it booked
  next. The games already on the schedule stayed with Plex and still said
  "Recorded by Plex", which is not what changing the pass means.

  Seen on 2026-11-15: a team pass switched to CouchElephant from the Apple TV
  kept its three booked games in Plex.

  Each future booking is now cancelled with the old recorder and made again
  with the new one, by the pass's own rules. A recording that has started
  stays where it is. The reply says how many moved.

## 1.5.8 - 2026-11-15

### Added

- **An iPhone app**, next to the Apple TV app in `clients/apple`. The same
  three tabs: Home, Guide and Recordings. It plays a recording from its start
  while it is still recording, tunes live channels, records from the guide,
  and edits passes. A long press opens the same menus as on the television.

  It is laid out from the space it is given and has no fixed sizes, so it
  fits a small phone, a large one, a phone on its side and a folding phone
  open or shut. Lists go to two columns where there is room. Everything stays
  inside the safe area, clear of the notch, the island and the home indicator.

### Changed

- The Apple TV app moved from `clients/appletv` to `clients/apple`, and the
  two apps share one Xcode project and the code that talks to the server.
- A phone shows in Settings, Recording, Televisions as "iPhone", with its own
  Allow delete setting.

## 1.5.7 - 2026-11-15

### Changed

- **CouchElephant is the default recorder wherever its recorder is on.** A new
  recording or pass is made by CouchElephant on an install with a
  `/recordings` folder, and by Plex on one without. Settings, Recording, Own
  recorder still lets you pick Plex, and that choice is kept. An install that
  1.5.5 seeded with Plex moves to the new default once. Nothing already booked
  changes, and each recording and pass can still choose the other.

### Added

- **The Apple TV app lists your passes.** "Set to record" on the Recordings tab
  has three lists: Scheduled, Sports pass and Season pass. Plex's own rules are
  listed with them, marked as Plex rules.
- **A pass can be edited from the television.** Who records it, padding before
  and after, the networks it may record from, and pause. Each setting is one
  button that steps to the next value, and nothing is sent until Save. A pass
  can be removed, which leaves what it already booked.
- **One scheduled recording can be handed to the other recorder**, from its
  long-press menu: "Record with CouchElephant instead" or "Record with Plex
  instead". One a pass booked follows its pass.

## 1.5.6 - 2026-11-15

### Added

- **The Apple TV app has three tabs: Home, Guide and Recordings.**
  - **Guide** is a grid of every channel for the next day. The channel is the
    first thing in each row, to the left of what is on now. Press a programme
    for its details, and a channel to watch it.
  - **Recordings** lists what is set to record, each with what booked it (one
    recording, or the pass by name) and who records it. Below that is
    everything recorded, by programme from A to Z, with All, Sports, TV and
    Movies to narrow it.
  - **A long press opens a menu for the thing under it, everywhere.** A
    programme: watch live, record, cancel, details. A channel: watch live,
    details. A recording: start from the beginning, resume, join live, delete.
- **Recording from the television.** One broadcast, every episode of a series,
  or every game a team plays. It is always recorded by CouchElephant, because
  that is the only kind of recording the television can play.
- **Each television has its own settings**, under Settings, Recording,
  Televisions. The first is **Allow delete**, off until you turn it on. A
  television with it can delete a recording for good. One that has moved to
  Plex is deleted from Plex too, file and library entry, after a prompt. If
  Plex cannot be reached nothing is deleted.

### Changed

- Every television now gets a key of its own, with sign-in on or off, because
  its settings hang off it. With sign-in off the key is handed over without a
  code. A television set up before this asks once and carries on.
- A series is listed by its own name with the episode under it. Sport still
  leads with the game.

## 1.5.5 - 2026-11-15

### Added

- **CouchElephant can make a recording itself, so you can watch it from the
  start while it is still recording.** Plex cannot do that: come home an hour
  into a game it is recording and it offers the live channel and nothing else.
  The fault is in the Plex server, so no Plex client fixes it.

  It is optional and off by default. Mount a `/recordings` folder to turn it
  on. Until you do, nothing changes and nothing new appears.

  Every recording and every pass then carries one choice, **Recorded by**:
  Plex, as before, or CouchElephant. A setting picks what a new one starts
  with. A recording CouchElephant makes is given to Plex when it has ended and
  nobody is watching it, as the untouched broadcast under the name Plex would
  have used, with your place in it as Plex's resume point. It stays playable
  in CouchElephant before, during and after the move. See
  [Who makes the recording](docs/RECORDING.md#who-makes-the-recording).

- **A Live TV tab.** What is being recorded right now by either recorder, what
  CouchElephant has recorded, and what is on each channel. It plays in the
  browser. A recording Plex is making is listed as "Cannot play while Plex is
  recording".

- **An Apple TV app**, in `clients/apple` (then `clients/appletv`), built from source. The same three
  lists, "Start from beginning" and "Join live" on a recording in progress,
  and live channels. With sign-in on it pairs by a six-letter code typed under
  Settings, Recording, Televisions.

- **The schedule lists what CouchElephant records itself**, next to what Plex
  will record, and the guide marks those broadcasts as set to record.

### Changed

- The image now carries ffmpeg, which makes the copy of a recording a
  television can play. An install that never turns the recorder on never runs
  it. The image is larger for it.
- The guide sync keeps the season and episode number Plex sends. They name the
  file a recording is given to Plex under.

## 1.5.4 - 2026-10-05

### Fixed

- **A bye week no longer warns that a team pass has stopped working.** The
  "A team you follow has no games in the guide" notice fired whenever nothing
  in the guide matched the team. That is true every week the team does not
  play inside the guide's reach, so it fired in season about a pass that was
  fine.

  Seen on 2026-10-05: the Chiefs played Oct 4, had a bye Oct 11, and the guide
  ended the night before their Oct 18 game.

  A team pass that knows its season now stays quiet while its next game is
  past the end of the guide. A pass with no season known warns as before, and
  so does one whose season puts a game inside the guide that nothing matches.

## 1.5.3 - 2026-09-27

### Fixed

- **A game the guide lists as a bare "NFL Football" is now recorded.** A team
  pass matched games only by the teams a programme carries. Gracenote lists a
  regional game with no teams until the broadcaster assigns it, so nothing
  matched and nothing was booked. A game that stayed generic until kickoff
  would never have recorded.

  Seen on 2026-09-27: both CBS slots for Sunday Oct 4 were still generic a
  week out, one of them the Chiefs at the Raiders at 3:25 PM. The Dolphins
  game the week before had carried its teams eleven days out.

  The league's schedule already gives the kickoff. A generic slot of the same
  league within 15 minutes of it is now booked as a stand-in. Only a kickoff
  with a time qualifies, the pass's source limit applies, a network the
  schedule names must match, and two generic slots at one kickoff book
  neither, because nothing says which is the game.

- **Once the guide names the game, the booking moves onto the named
  listing.** Plex then shows the real title, teams and summary. If the game is
  named in the same slot while Plex is recording it and kickoff is too close
  to book again, Plex's recording is kept and only CouchElephant's record
  moves.

### Changed

- **The schedule shows the matchup on a stand-in, with the guide's name under
  it.** An info icon explains why Plex lists it as "NFL Football". Hover
  shows the note, and a tap shows it on a phone. A booked game no longer also
  sits under "Waiting for the Plex guide data".
- **The schedule shows the guide's current name for anything CouchElephant
  booked,** rather than the name Plex stored when the booking was made.

## 1.5.2 - 2026-09-16

### Fixed

- **A game on its way into the guide no longer warns that it is missing.**
  Plex publishes a game before it tags it with its teams, and a team
  expectation matches on teams, so the first look after the guide extends was
  always too early. Every game warned once on its way in.

  Seen on 2026-09-15: "Something you are waiting for did not reach the guide"
  opened at 07:17 over a Chiefs game that was in the guide the whole time, on
  KCTV 5.1, correctly titled. Plex attached the teams at 04:19 the next
  morning and the notice cleared itself, 21 hours later.

  A miss now waits for the guide to have had a fair chance. Three things must
  be true before it says a word: the guide reaches past the date, a full day
  has passed since it FIRST reached past it, and Plex has refreshed its guide
  again since then. A match clears that clock, so a game that slips a week
  starts over rather than inheriting an old one.

- **A booked window with no matchup yet is no longer called a missing game.**
  A bare "NFL Football", or "Teams TBA", is the broadcaster saying a game goes
  out in this window without saying who is playing. Plex tags it when the
  matchup is announced, which can be a week later.

  Telling that apart from a studio show is the whole difficulty, because most
  of a sports section is untagged and always will be: a shop, a phone-in, a
  pre-match, a countdown. A slot now counts as a pending matchup when its title
  says TBA as a word, or when its league has other programmes that do carry
  teams.

  The second test calibrates itself off the guide in front of it. A league that
  ever tags anything broadcasts games. A shopping channel never does. There is
  no hardcoded list of leagues, so a sport nobody thought of behaves the same.

  Measured on a 63 channel guide: this keeps the 5 genuine placeholders and
  lets the warning through for the other 108 untagged rows.

## 1.5.1 - 2026-09-14

### Fixed

- **A Plex restart no longer reports itself as an outage.** Plex holds every
  request at 503 "running startup maintenance tasks" for well over a minute
  after it starts, and it is not listening at all for the seconds before that.
  A sync landing in that window sent two alerts, "Plex could not be reached"
  and "A sync failed", about a server that was working normally an hour later
  and had never been broken.

  Seen on 2026-09-14: the host rebooted, CouchElephant and Plex came up
  together, and CouchElephant asked for `/livetv/dvrs` three seconds later.

  A sync now waits up to five minutes for a server that says it is starting,
  and reports nothing if it comes up. A restart is ordinary here: it happens on
  every Plex update and every reboot of the host.

  The wait ends. A server still starting after five minutes raises the same
  fault it always did, and the alert says what it waited for. Anything that is
  not Plex saying it is starting, including a 503 from a proxy in front of it,
  is still reported at once.

## 1.5.0 - 2026-09-13

### Added

- **A scheduled recording now says what settings are in force on it.** Open any
  recording, from the guide, the agenda, the calendar or the recordings list,
  and the panel lists the padding, the quality and the rest in plain words:
  "Ends 60 minutes late", not `endOffsetMinutes 60`.

  Read from Plex's own copy of the recording rather than from what the pass
  asked for, because the point is to confirm the setting took effect. It works
  for recordings Plex scheduled by its own rule too. Opening the panel never
  waits on Plex: the values come from the copy every sync refreshes.

  A setting CouchElephant has no words for is shown with its own name and value
  rather than hidden, so a server offering something new cannot disappear from
  the one screen that exists to say what is in force.

### Changed

- The fake Plex server used by the tests answers a setting by its declared
  type. It used to return "true" for any value of `1`, so a one-minute padding
  read back as `true`, and the panel would have said "Starts true". A fake that
  differs from the real server is as useless as one that is more permissive.

## 1.4.0 - 2026-09-13

The second cleanup review, over the routes, the alerts and the rest of the app.

### Fixed

- **Alert times ignored the timezone setting.** Every "a pass booked a
  recording at ..." line was formatted with the container's clock rather than
  the zone the app is set to. It is the one time the product quotes outside its
  own pages, and it was the one time that did not honour the setting.

- **The useful error message was on the path nobody takes.** Sending and the
  Test button each carried their own list of what a destination needs, worded
  differently, and the better sentence was on the button you press once. The
  settings page renders the error from the hourly send, so the sentence you
  read when something is actually broken was the worse of the two. A bot with
  no channel now says where to find the channel id, wherever you meet it.

- **A smart pass with no name read as blank** in the schedule row, the guide
  panel and the failure panel. Four places spelled the label out instead of
  calling the helper that describes the filter when there is no name.

- **The announced search wrote to the passes table directly**, bypassing the
  one function whose docstring calls itself the only place a pass is written,
  after the previous three had drifted apart. It was the fourth.

- **One route answered a failure with HTTP 200**, leaving the client to work it
  out from the body alone.

- A fault code was raised as a bare string rather than its constant. Correct
  today, silently broken on a rename, and now checked by a test.

### Performance

Measured against the live scale: 16,700 programmes, 22,800 airings, and a
60-day history.

- **The schedule is about four times faster.** It ran the most expensive query
  in the app twice, the second time to count rows for a number no page renders.
  One row more than asked for answers the same question. The failure half also
  sorted the whole history to answer a question about broadcasts that have not
  aired yet.
- The filter panel counted genres and teams by parsing every programme's JSON
  in Python. SQLite reads it directly, guarded so one malformed row cannot take
  the panel down, which is the behaviour the Python version had.
- Guide search folded each title once per broadcast rather than once per
  programme.

### Removed

Four pieces of dead code: a route nothing calls, an unused import, a helper
that only its own recursion used, and a function written for a provider that
was rejected and deleted. The sports source also asked its API twice for one
row, against a rate-limited free tier.

## 1.3.1 - 2026-09-13

### Fixed

- **Upgrading to 1.3.0 announced bookings that had already been announced.**
  1.3.0 re-keys existing bookings onto the new airing id. The alert state is
  keyed on that same id, and it was not re-keyed with them, so the next
  dispatch read a booking nobody had heard about and said so again. Seen on a
  live install: two recordings booked days earlier were announced a second time
  at the moment of the upgrade.

  The whole rule `notify_state` runs on is that a row exists means this
  destination has been told, and the migration broke the key that rule depends
  on. Both are now re-keyed together, state first.

  Nothing was booked twice and no recording was affected. Only the messages
  were duplicated.

## 1.3.0 - 2026-09-13

A cleanup review of `sync.py` and `passes.py` found the cause under four fixes
that shipped this week, two silences, and two bugs nobody had reached yet.

### Fixed

- **A broadcast's id is now minted from the broadcast.** It used to prefer
  Plex's own `Media.id`, which is the one value in the payload that moves: a
  guide refresh renumbers airings without changing anything about them, and one
  game here collected nine ids over three weeks. Every lookup keyed on the id
  had to be taught to resolve a retired one, and four of them were taught one at
  a time, each after failing in front of somebody.

  An id is now the programme, the channel and the start time. A renumber
  produces the same id. A re-time still changes it, which is correct: that is a
  different broadcast and the app has to notice. Bookings made before this are
  re-keyed when the app starts.

  Two more places had the same fault and nobody had reached them. The guide's
  "Being recorded" filter stopped matching a booked game after a refresh, and
  cancelling a recording by hand answered "CouchElephant did not schedule this"
  for a recording CouchElephant had scheduled.

- **A re-point that Plex refused was counted and never mentioned.** It
  incremented the failure count without adding to the list the notices are
  built from, so nothing was raised. That is the silence this whole check
  exists to remove.

- **A booking could be trapped in a permanent failure.** Plex was asked about
  the subscription before our own guide was consulted, so a booking whose
  subscription Plex had lost and whose broadcast had left the guide went to the
  re-book path. There is nothing to re-book from, so it failed and raised a
  notice on every sync, for ever, while the branch that exists to cancel
  exactly that booking was unreachable for it.

- **A repaired booking's history row could name two broadcasts at once**,
  recording the old airing id beside the new channel and start time.

### Changed

- One re-book path instead of two. They had already disagreed about when to
  forget the old booking and whether to write down a failure.
- Guide and schedule pulls share one prune helper rather than two copies.
- The booking check counts in one place, so a new outcome cannot be counted in
  one half and forgotten in the other.

### Performance

Measured against the live install: about 16,700 programmes and 22,800 airings
per sync, hourly.

- A channel is written once per sync instead of once per broadcast: about
  22,700 redundant writes and the same number of redundant title parses, gone.
- Two indexes on `plex_grabs`, which holds the whole DVR schedule and was
  scanned in full by three hot questions, one of them once per airing of a
  programme.
- Asking whether a team pass matches anything now stops at the first match
  instead of hydrating every match in the window.
- Re-pointing a booking asks for one programme in SQL rather than the pass's
  whole thirty-day window, and reads the pass rows once per check rather than
  once per booking.
- A pass run reads the last sync time once rather than once per game.

## 1.2.12 - 2026-09-13

### Fixed

- **Clicking a recording could open a panel reading "not found".** A booking
  stores the airing id it was made against, and the schedule row carried that
  id straight through. A guide refresh mints new ids for broadcasts that have
  not changed at all, so the stored one retires and the row points at nothing.
  The recording was perfectly fine; only the link was stale, and refreshing the
  page did not help because the row was rebuilt from the same stored id.

  A row now names the broadcast the guide holds now. A broadcast that really
  has gone says so in words, rather than "not found", because that reads as a
  broken page and the usual cause is not a broken page.

## 1.2.11 - 2026-09-13

### Changed

- **A settings change is now made on the recording Plex already holds, so it
  has no deadline.** Changing a pass used to cancel each recording it had
  booked and make it again, even to move one number. That has a moment in the
  middle with nothing scheduled, so it was refused within two sync intervals of
  the broadcast. The result was that the padding could not be corrected at the
  one time somebody usually notices it is wrong, which is shortly before the
  game.

  Plex takes a partial update on a subscription that exists:
  `PUT /media/subscriptions/<key>` with the settings to change. The pin is
  untouched and the scheduled recording survives, verified against a live
  server. Only the settings that actually differ are sent, and they can only be
  settings the server itself reported, so nothing here can send a setting the
  booking will not take.

  Booking again is still what happens when the guide has moved the broadcast
  under a booking, when Plex has lost the subscription, or when it has
  scheduled nothing against it. Those keep the timing guard, because they are
  the ones with a gap. An edit that somehow leaves nothing scheduled falls back
  to booking again as well.

## 1.2.10 - 2026-09-13

### Fixed

- **A booking stopped being checked the first time Plex renumbered its guide.**
  A guide refresh mints new airing ids for broadcasts that have not changed at
  all: same channel, same time, new id. One game here collected nine ids over
  three weeks. A booking was read only by the id it had stored, so after a
  renumber it was quietly skipped, and every later change to its pass missed it
  with nothing said. Found on a live DVR with one of two recordings already in
  that state.

  A booking is now looked up by its id first and by the broadcast it was made
  against second, and a repair re-books from whichever id the guide holds now.

## 1.2.9 - 2026-09-13

### Fixed

- **Changing a pass did not change the recordings it had already booked.**
  Saving a pass booked any new games and stopped there, so a recording already
  on the DVR kept the settings it was made with until the next sync, up to an
  hour later. The page said one thing and the DVR did another, and a game
  inside that hour was never corrected at all: by then it is too close to
  kickoff to re-book safely.

  The booking check now runs when a pass is saved as well as on every sync. It
  refreshes the copy of Plex's schedule first, so a recording booked minutes
  ago is not read as one Plex has scheduled nothing for and cancelled. The
  reply says how many existing recordings were updated, and how many were left
  alone for being too close to their broadcast.

  The pass is saved whatever Plex is doing. A server unreachable for a moment
  never fails the save, and the next sync carries the change.

## 1.2.8 - 2026-09-13

### Changed

- The Notifications page says what a relay is for, and the documentation
  screenshots show one. A destination nobody can tell apart from the others is
  a destination nobody picks.

## 1.2.7 - 2026-09-13

### Added

- **A relay on your own network as an alert destination**, posting JSON to
  `/notify`. It is the only destination that names no channel. It is handed the
  severity and decides for itself, so a fault lands in the channel you watch
  while a recording that started, a pass that booked something, or a fault that
  cleared lands in the one you read later. Nothing is configured twice and this
  end never learns which channels exist.

  The address is checked as an ordinary http or https address with a host and no
  credentials in it, and deliberately not against a host allowlist: a relay is a
  service you run, usually on the same network as this container, so there is
  nothing to pin it to. Plain http is allowed for the same reason.

  The relay answers 202 once it has queued a message and 503 when its queue is
  full, both with `ok` in the body. Queued is not delivered, and it is the
  strongest thing a relay can honestly say, but a 2xx alone still proves
  nothing, so the body is what is believed and a refusal is never recorded as
  sent.

### Changed

- A destination's address is only masked when the address is itself the
  credential. A Discord webhook URL still is, so it stays masked. A relay's
  address is a machine on your network, and masking it only hid which relay a
  destination pointed at.

### Fixed

- **A subscription this app had just cancelled survived in its copy of Plex's
  schedule.** Our copy was pruned by timestamp, so two pulls inside one second
  kept every row the first one wrote. A stale row then read as a live booking,
  and a pass would skip a game nothing was recording. Both the subscription and
  the grab lists are now pruned by what the pull actually saw. Same fault as the
  guide prune in 1.2.6, in the other mirror.

## 1.2.6 - 2026-09-13

### Fixed

- **A game the guide re-timed was shown as NOT RECORDING while it was
  recording.** A failed booking is drawn against a channel and a start time,
  and a guide refresh moves both. One NFL game shifted fifteen minutes on the
  same channel, and the red row went on claiming the game was not being
  recorded, directly above the booking that was recording it, until kickoff.

  A failure row now clears when the **game** is covered, whoever booked it and
  whatever time it moved to. A programme names one episode and so one game, so
  this is the honest question to ask. A failure with nothing recording the game
  still shows, with its attempt count, which is what the row exists for.

- **A booking whose broadcast left the guide was left on the server for ever.**
  The check that re-reads every future booking gave up as soon as the airing it
  named was no longer in the guide, and counted it unchecked. The subscription
  stayed on Plex, pinned to a slot nothing airs in, and the game itself went
  unrecorded.

  A booking like that is now handled four ways, and only one of them is leave
  it alone. Plex still holding a recording for it means nothing is touched, so
  a guide that has merely shrunk can never read as permission to cancel.
  Another booking of ours already covering the game makes this one a duplicate,
  so it is cancelled and not replaced. The guide still carrying the game moves
  the booking onto the broadcast the pass would choose today, by the pass's own
  rules. The guide having dropped the programme, with nothing scheduled against
  the subscription, cancels it. All four are written into the pass history, and
  a move obeys the same timing guard as any other repair.

- **A pass would not book a game it had once booked, even after the recording
  was gone.** The check asked the log rather than the server: any programme
  ever written down as scheduled was treated as handled for the life of the
  install. A recording lost afterwards, because Plex dropped the subscription,
  because you cancelled it, or because the guide moved the broadcast, was never
  replaced, and nothing said so.

  It now asks the live state. A booking counts when Plex still holds the
  subscription, or when it was made since the last sync, which is the moment
  before Plex has been read back and is what stops two runs in a row booking
  the same game twice.

### Changed

- The fake Plex server used by the tests now drops the recording for a pinned
  one-shot whose slot the guide no longer has, as the real server does. The old
  fake answered with the frozen original listing for ever, which hid every
  consequence above. A fake that is more permissive than the real server is not
  a test double.

- A guide pull now prunes by what it actually saw rather than by timestamp. Two
  pulls inside one second left every airing the first one wrote, because its
  stamp was not lower than the second one's. The result was a ghost airing that
  no longer exists in the guide, which is the same bug shape as everything
  above.

- The booking check re-reads Plex's schedule after it changes anything, so the
  next check is not asking a copy that was read before the change.

## 1.2.5 - 2026-09-12

### Added

- **A Discord bot destination**, so alerts arrive as the bot rather than as a
  webhook.

## 1.2.4 - 2026-09-10

### Fixed

- **A team pass matched teams that were not its own.** A Kansas City Chiefs pass
  was trying to record a Bundesliga match, two college games and a Bears game.

  A pass holds an id from Plex's section-level team list. A programme holds its
  own team array, numbered per programme. They are different numberings, so
  comparing them is a category error rather than a stale lookup. Measured on a
  live guide: of the 89 ids that appear in both, 71 name a different team in
  each. The section list calls 253 "Nevada" while a programme calls it
  "Sporting Kansas City".

  A pass now matches on the team's name, which is what survives a renumbering.
  The id is used only when a pass has no name at all, which is an old pass made
  before the name was stored.

  Nothing was ever recorded wrongly, because the settings fault fixed in 1.2.3
  meant no booking went through at all. Both faults together are why a Chiefs
  pass recorded nothing for three weeks.

## 1.2.3 - 2026-09-10

### Fixed

- **A pass could send Plex a setting the booking cannot take, and every booking
  it made was refused.** A pass always books Plex's one-shot template. That
  template does not offer `onlyNewAirings`, and Plex answers a bare HTML 400 to
  the whole request rather than ignoring one setting it does not know. It logs
  nothing and it answers in no measurable time, so from this side it reads as a
  network fault rather than a bad request.

  A team pass made before the settings panel started hiding that control kept
  it in its stored settings. Every booking it tried then failed: 364 attempts
  over three weeks, two an hour, and no game recorded.

  Three things changed. A booking now drops any setting the chosen template
  does not declare, before it is sent. A pass can no longer store a
  recurring-only setting in the first place, which is what `_pass_prefs`
  already claimed to do. An existing pass is cleaned of one on startup.

  The pinning settings are applied after the filter, so a server that declares
  no settings at all still gets a pinned booking.

### Added

- **A refused booking is now shown, loudly, in both views.** It used to be
  shown nowhere. The programme left "Waiting for the Plex guide data" as soon
  as the guide carried it, and no row took its place, so a wall of failures
  looked exactly like a quiet week.

  A failure now sits in the schedule where the recording should have been: a
  red row in the agenda reading NOT RECORDING with the attempt count and the
  error, and a red block in the calendar. Both views read one feed, so neither
  can forget to show it.

  Clicking it opens the broadcast, which pass asked for it, how many times it
  has been tried, Plex's own error verbatim, and a **Try again** button. A
  retry takes the same path a sync takes, so one that works proves the next
  automatic attempt will work. A retry that fails is written down like any
  other attempt, so the count stays honest.

  One broadcast draws one row, however many airing ids it collected. An airing
  id is not stable, and a guide refresh mints new ones: the game behind this
  release had nine of them over three weeks. Failures are grouped by channel
  and start time, which names one broadcast for as long as it is on, and the
  Try again button uses the id that exists in the guide today rather than the
  one in the oldest log line.

## 1.0.9 - 2026-09-02

### Changed

- **"Waiting for the Plex guide data" opens folded to the next three.** A
  followed team hands this card its whole published season, seventeen games for
  an NFL side, and a few passes would fill the top of the page. A "Show all"
  control expands and collapses it.

  Every row is still rendered and hidden, so expanding needs no second request.
  It is a fold, not a limit: nothing is dropped, and the calendar still draws
  all of it month by month.

- **The schedule now reads outward from now.** What is still to come comes
  first, soonest at the top, because that is what a schedule is for. What
  already aired follows, most recent first, because that is what history is for.

  Plain ascending put the oldest finished recording at the top, so a server
  whose season had not started opened on a month of "complete" rows. Plain
  descending would have been just as wrong the other way: book a season and it
  leads with next January while tonight's game sits at the bottom.

### Fixed

- **`hidden` was losing to a stylesheet.** It is a browser rule, and any author
  `display` beats it, so `.plan{display:flex}` kept every folded row on screen
  while the code believed it had hidden them. The fold shipped inert without
  this. Caught by a browser test, which is the only kind that could.

## 1.0.8 - 2026-09-02

### Fixed

- **An upgrade that changes what we ask for now refills at once.** Each team pass
  records when it was last asked and skips for a day. That gate already knew an
  API key changes the answer. It did not know that changing WHICH CALLS we make
  changes it just as much, and 1.0.7 changed exactly that: from one capped
  endpoint to a walk over the rounds.

  So every existing install would have kept showing the old single fixture for up
  to 24 hours after upgrading, and 1.0.7 would have looked like it did nothing.
  The refill fingerprint now carries the fetch shape alongside the key.

  Same failure as a cache keyed only on age: the record of "already asked"
  outlived the meaning of the question.

## 1.0.7 - 2026-09-02

### Fixed

- **A followed team now shows its published season, not one game.** The card and
  the calendar were right; the source call was not. `season()` asked
  `eventsseason.php`, which is capped, so a team pass held a single fixture while
  months of announced season sat invisible.

  `eventsround.php` is not capped the same way. Measured live on 2026-09-02 for
  the 2026 NFL season:

  | call | key `3` | key `123` |
  |---|---:|---:|
  | `eventsnext.php` (team) | 1 | 1 |
  | `eventsseason.php` (league) | 5 | 15 |
  | `eventsround.php` (league, per round) | 5 | **16** |

  Sixteen is a full NFL week. Walking the 18 rounds returns 272 events holding
  all 17 Kansas City Chiefs games, 2026-09-15 to 2027-01-10, on a free key. So
  the season is walked round by round, and `FREE_KEY` moves from `3` to `123`,
  which is equally documented and less capped.

  The walk stops after three consecutive empty rounds rather than at a fixed
  count, because a round count is per sport: 18 for the NFL, 38 for a football
  league. A 45-round backstop keeps a league that answers for ever from spinning.

- **The app was telling you to pay for something you already had.** "The free
  tier gives no season" was written into four places, including the Settings
  page and a health tip shown to everyone following a team. All four now state
  the measured numbers. The test that asserted it records why it was wrong.

  The reasoning error is the part worth keeping: an endpoint answering thinly is
  not the database lacking the data, and one endpoint is not an API.

## 1.0.6 - 2026-09-01

### Fixed

- **The season lookup was fetching 2007.** `season()` asked
  `eventsseason.php?id=<league>` with no `s` parameter, and that does not mean
  "the current season": the API answers with its earliest. Measured live,
  `id=4391` returned NFL games from 2007-09-06.

  The free tier hid it. Free caps that endpoint at five league-wide rows, so
  after filtering to one team the answer was usually nothing either way, and the
  wrong decade looked exactly like the thin free answer. The cost would have
  landed on the first person to buy a subscriber key: a season paid for, and
  2007 delivered.

  `current_season()` now reads the league's own `strCurrentSeason`, which is free
  to read. The season string cannot be composed from a clock, since the NFL calls
  it "2026" and a football league calls it "2025-2026". A league that will not
  say still gets asked without one, which is no worse than before.

### Changed

- "Waiting for the guide" now reads **"Waiting for the Plex guide data"**, in the
  plan card, the calendar band, the legend and the calendar tooltip. Plex is the
  server; the guide data is the thing that has to arrive.

## 1.0.5 - 2026-09-01

### Added

- **A followed show now learns about episodes announced after you followed it.**
  A series pass got its expectations exactly once, at the moment it was made,
  from a search that answers one row per SHOW carrying its premiere date.
  Nothing ever asked again. So a followed show held a single row for ever: it
  never heard about next season, and for a show already on air that row was a
  date in the past whose only future was to be reported missing.

  `fill_series_passes` runs every sync and asks TVmaze for the show's episodes.
  Nothing had to be bought. That endpoint is free and needs no key, exactly like
  the search already in use. Only future episodes are kept, since an episode
  that already aired is not something anyone is waiting for.

  It keeps the same three disciplines as the sports fill: ask at most once a
  day, because TVmaze allows about 20 calls per 10 seconds; date the attempt
  before the call, or a show with no dated episodes is re-asked for ever; and
  refuse an uncertain identity rather than fill a pass with another programme's
  episodes. A pass made through the announced search already stored its show id
  and needs no lookup at all.

  The premiere row gives way once real episodes arrive, so an already-aired
  premiere date stops being reported as missing. It survives untouched for a
  show that has no dated episodes yet, where it is the whole signal.

- **An episode keeps the broadcast time the source published.** `airstamp` is an
  ISO instant with a zone, which none of the date formats matched, so every
  episode used to lose its time. A bare `airdate` still reads as a day, and an
  episode with neither is dropped rather than dated by guesswork.

### Changed

- The "Waiting for the guide" card shows the soonest eight and counts the rest.
  A season at a time would otherwise turn the top of the page into a wall. The
  data is not capped anywhere: everything is stored, everything is promoted, and
  the calendar still draws all of it month by month.

## 1.0.4 - 2026-09-01

### Added

- **The calendar draws what you are waiting for, not just what is booked.** A
  month holding three announced games and no bookings used to render as an empty
  month, which is the opposite of what the "Waiting for the guide" card was
  saying one pane up. Expectations now appear on the grid in their own colour,
  dashed, with a third legend entry. Dashed is the same "a plan, not a booking"
  language the card already uses, so the difference survives a colour-blind
  reader and a greyscale print.

- **Where one goes is decided by its precision, and that is the honest part.** A
  league gives a kickoff and an announcement often gives only a month. A calendar
  cell IS a day, so putting a month-precision entry in one states a broadcast
  date nobody published. So: time and day precision sit in their day; month and
  year precision sit in a band under the grid, for the month they belong to;
  anything with no date at all stays on the card above, where it already was.
  Nothing invents a day it was not given.

## 1.0.3 - 2026-08-31

### Changed

- **The two tab names swapped, because they were the wrong way round.** The
  top-level tab is now **Schedule** and the sub-tab inside it is **Recordings**.
  That section is where you decide what gets recorded, so Schedule is what it
  does; the list of actual bookings is one level down, so Recordings is what it
  holds. Nothing moved, and the `/recordings` address is unchanged, so existing
  links and bookmarks still work.

- The "Waiting for the guide" note now says an entry becomes a real recording
  when **Plex Guide data** carries an airing for it, rather than when "Plex"
  does. Plex is the server; the guide data is the thing that has to arrive.

## 1.0.2 - 2026-08-31

### Added

- **You can now follow a show or a team the guide has never heard of.** You read
  that a series starts next March, or you want a whole season rather than the
  fortnight the guide holds. Until now a pass could only match guide rows, so
  there was nothing to make.

  The thing worth knowing first: **no source of TV guide data goes further out
  than about three weeks.** Not the free ones and not the paid ones. Schedules
  Direct is the licensed Gracenote reseller and it accepts a request for at most
  21 days. Free XMLTV feeds do 7 to 14. This is not a licensing problem to spend
  money on. In August no broadcaster has decided what airs next March, so there
  is nothing to sell.

  What does exist months ahead is two other things, and no single source carries
  both. Announcements: a title, a network, and a date that is often only a month.
  And published league schedules: real dates and kickoffs, known long before any
  broadcaster is named.

  So this is not a better guide. It is holding an intention until the guide
  catches up. Searching now returns guide results at the speed it always did,
  and appends what the outside sources know behind them. Following one makes an
  ordinary pass with a list of what it is waiting for. Every sync looks for each
  one in the guide, and a match promotes it: from that moment it is a normal
  pass and books through the path that already existed.

  **Nothing outside the guide ever books a recording.** An expectation is an
  intention. Only a guide airing carries a channel.

  A date is only ever shown at the precision it was given. A league says a
  kickoff, so you see a kickoff. An announcement often says only a month, so you
  see a month. Rendering that as the first at midnight would put a broadcast
  time on screen that nobody published, and you would plan around it.

  If the guide reaches past the date and nothing matched, you are told, by name.
  A show can slip, so that is a warning and never a deletion. It keeps looking.

- **Three sources, and only one of them is required to do nothing.** TVmaze
  covers announced series and needs no key and no account, so this works the
  moment the container starts. TheSportsDB covers a team's published season, but
  **only with a subscriber key**: on the free tier it answers one upcoming game
  per team. Measured, not assumed.
  TMDB adds films and needs a free key you register. Both keys live in Settings,
  under Sources.

  The ESPN public JSON was tested and rejected. It works, and it is what
  everyone reaches for, but it returns 403 to a browser user agent and 200 to a
  plain script. It answers only when you do not look like a browser, and that is
  bot protection we would be tiptoeing around until it broke without warning.

- **A suggestion can now be waved off. A health problem still cannot.** If a key
  would actually add something to what you already follow, a yellow badge says
  which one and why, and links to Settings. It has a Not now button.

  That button is new, and the rule it sits beside is not. These notices exist
  because a problem you can click away is a problem you forget about. So a
  suggestion is a separate class rather than a loosening of that: only a `tip`
  may ever be dismissed, the server refuses anything else, and a real fault
  always sorts above a suggestion so it can never be hidden behind one.

  Nobody is nagged about a key they cannot use. Follow only broadcast series and
  you will never see it.

- **CouchElephant now checks that the recordings it booked are still the ones
  it asked for.** A pass books a game once and then stops looking at it. Change
  the pass afterwards and the booking keeps the settings it was made with, for
  ever. Nothing errors: the pass ran, the recording exists, and the game is cut
  off at the end. That is how a real game got recorded with no padding here,
  because the booking was made two hours before the padding setting existed.

  Every sync now re-reads each future booking from Plex and compares it against
  the pass as it stands now. Four things have to be true: Plex still holds the
  subscription, Plex has actually scheduled a recording against it, every
  setting matches, and the pin still names the airing the pass chose. A
  subscription whose settings all read correctly with nothing scheduled behind
  it looks healthy from every angle except the one that matters, so that is
  checked on its own.

  A real difference is repaired: cancelled and booked again from what the pass
  says now, with a `repaired` line in the pass history saying what changed.

  The hard half is refusing to invent a difference. Plex answers `oneShot` as
  the string `true` to the `1` we sent, and returns numbers as strings on one
  payload and ints on another. Reading either as drift would cancel and re-book
  the same recording on every sync, for ever. So values compare as numbers
  first, then booleans, then text, and a setting Plex does not report is
  recorded as unchecked rather than as different. Plex being unreachable is
  unchecked too: only a definite 404 means a recording is gone.

  Nothing is cancelled close to a broadcast. Repair needs more than two sync
  intervals of room, and never less than half an hour, so a re-book that fails
  still has a later sync to put it right. Drift found closer than that raises a
  notice instead. Wrong padding on a game you are recording beats no recording
  of it.

- **CouchElephant now watches whether Plex is keeping its own guide up to
  date.** It can only choose from the airings Plex offers, and when Plex stops
  refreshing, nothing here breaks. Passes keep running, syncs keep succeeding,
  and the guide gets a day shorter every day until the game you wanted is past
  the end of it. Nothing errors. You find out the evening the recording does
  not happen.

  Plex is asked what it intends rather than assumed at. `/butler` gives the
  real interval of its guide refresh and whether it is switched on; the DVR
  gives when it last actually ran. A weekly task five days late is fine and a
  daily one five days late is broken, and now the app can tell the difference
  because Plex told it which this is.

  Four things raise a notice: the refresh task switched off, the guide not
  refreshed within twice its own interval, the guide reaching less than three
  days ahead, and Plex unreachable. A daily task that slipped one day is late
  rather than broken, and saying so would only teach you to ignore the badge.

  It shows as a badge on the sync button, not a fourth icon in the bar. A guide
  that has stopped moving is a sync problem. The badge is its own button, so
  reading what is wrong does not start a sync you did not ask for.

  A notice clears itself when the condition clears, and cannot be dismissed. A
  health warning you can click away is one you will click away. It remembers
  when the problem started, because "since when" is the question you ask on
  finding a stale guide four days late.

  Every sync also writes down when Plex last refreshed and how far its guide
  reached, so a guide that has stopped moving is visible the next day rather
  than the week after.

### Changed

- **With a problem open, the sync button reads the problem instead of
  syncing.** Syncing is the reflex. A badge on the sync button says something
  is wrong, so the hand goes to the sync button, and with a stale guide that
  is a minute of work spent re-reading a guide Plex has not moved. It answers
  nothing and the badge is still there afterwards, so it gets clicked again.

  Clicking the icon now opens the panel that says what is wrong. The badge
  already did that, and the icon under it is the larger target, so both now
  open the same panel. The button says so: its tooltip and its label both name
  the problems rather than promising a sync.

  The sync itself moved into the foot of that panel as **Sync anyway**. None of
  these problems stop a sync from working, and trying one is sometimes the
  right thing to do, so it had to stay reachable rather than be removed.

  With nothing wrong, nothing changes: there is no panel, and the icon syncs
  in one click as it always did.

### Fixed

- **A change to the stylesheet or the scripts never reached a browser that had
  been to the site before.** The version stamped onto every asset URL exists to
  drop the browser's cached copy on a deploy. It was built by walking
  `routes/static`, and `static/` is a sibling of `routes/`, not a child. The
  directory does not exist, every miss was swallowed as `OSError`, and the
  stamp came out `0` for every build ever made. So the URL never changed and
  the cache never dropped. That is the exact fault the code's own comment says
  it exists to prevent, and it had been shipping since the version was added.

  It now walks the right directory, and refuses to start if the walk finds
  nothing to date rather than quietly reporting `0` again.

- **Plex renumbers its team ids, and a team pass did not survive it.** Measured
  on a live server: one guide refresh moved the Kansas City Chiefs from 236 to
  245 and the Seattle Seahawks from 132 to 244, on the same game with the same
  programme guid. A pass followed the stored id, so from that moment it matched
  nothing, and said nothing about it, because matching nothing is what a team
  with no games this week looks like. You would find out on a Sunday.

  The name is the identity and the id is a handle into whatever guide Plex
  currently holds. A pass is now repointed at today's id on every sync, and an
  airing is matched on the id **or** the team's name.

  Both halves are needed. `programs.teams` is enriched once and then preserved,
  so a cached programme keeps the old ids long after Plex has moved on:
  verified live, a pass correctly repointed to 245 matched zero airings against
  a cache still holding 236. Correcting the id alone would have made it worse.

  Names are compared on spelling only: case, accents and punctuation folded,
  every word kept. Not the catalogue's own folding, which also drops club words
  and would make "Real Madrid" match Atletico Madrid, and Cincinnati match FC
  Cincinnati. Where a pass and the guide spell a team differently, that is now
  settled at the source: repointing a pass also adopts Plex's own spelling, so
  a pass made from the shipped catalogue stops carrying a name the guide has
  never used.

- **Team names outside Latin script folded to nothing, so they were all one
  team.** Both folds ended with `[^a-z0-9]`, which does not narrow a Cyrillic,
  Greek, Japanese, Hebrew or Arabic name so much as delete it. An empty string
  is not a miss, it is a key. Three things followed. The catalogue answered VfB
  Stuttgart for every such name, because the alias "VFB" also folds away and
  claimed that key. Every non-Latin team was tagged Bundesliga. And four
  distinct teams in one guide collapsed into a single entry, so a pass for
  Zenit was repointed at the Hanshin Tigers and had its name overwritten to
  match.

  The fold now keeps anything Unicode calls alphanumeric, and every lookup
  refuses an empty key rather than looking it up.

  Combining marks come off only where the letter is ASCII. Beyond Latin they
  carry meaning: the Japanese dakuten is the difference between KA and GA, so
  stripping it turned the Hanshin Tigers into a word that is not "tigers".

  A name made entirely of club words, like "Athletic Club", keeps them rather
  than folding to nothing.

- **A team pass that can find no game now says so.** That silence is what let
  the renumbering hide. A team out of season trips it too, which is the point:
  being told a pass is idle is cheap, and finding out months late is not.

- **A sports programme with no teams was re-fetched from Plex every hour.**
  Enrichment asks Plex for the team tags a bulk listing does not carry, and
  skips any row that already has them. A row Plex has no teams for never gets
  any, so it qualified again on the next sync, and the one after that, for as
  long as it stayed in the guide. Most sport in a guide is not a game: a
  phone-in, a highlights show, a shop. On a 63-channel lineup that was 72
  requests an hour, about 1,700 a day, every one of them answering the same
  nothing. Every attempt is now dated in `programs.teams_tried_at`, whatever
  it found.

  The note expires after a day rather than settling it for good. A game can
  reach the guide before Plex tags it, and a permanent "no teams" would hide
  that game from a team pass for the rest of its run. A call that fails is not
  an answer and is not written down.

  The sync line now reports what it asked as well as what it found. "0 sports
  enriched" used to mean either "nothing to do" or "asked seventy times and
  found nothing".

## 1.0.1 - 2026-08-23

The first public release. Published as `ghcr.io/datbird/couchelephant` for
linux/amd64 and linux/arm64, and listed in Unraid Community Applications.

Nothing in the app is tied to one country. Networks, genres, content ratings
and channels are read from your own guide rather than from a list shipped
here. The team catalogue covers 1,310 teams across 18 leagues, European
football included. Times, dates, day names and the first day of the week all
follow the viewer's own locale, so the same install reads correctly in
Seattle, London and Vancouver.

One-shot recordings are identified by Plex's own subscription type rather than
by the English words in its title. A Plex server in German offers "Diese
Sendung", not "This Episode", and the app used to read the English and pick a
series rule instead: it would have recorded every airing rather than the one
game. Verified against a live server, which also answers type 15 for a team
subscription rather than 2.

Searching and filtering fold case across the whole of Unicode. SQLite stops at
Z, so a search for "muller" never found "MULLER" with an umlaut.

Setting up pulls the guide straight away rather than waiting for the sync
loop's next turn, and the empty grid says what it is doing. It used to read
"End of the channel list", which on a minute-old install looks like a broken
app rather than an early one.

The guide's day strip is written in the viewer's language and date order. It
was built on the server in English.

### Fixed

- **Following a team as a plain Plex rule made a rule for the whole league.**
  The panel lists the named team first; Plex lists the league first. The
  position in the shown list was sent where Plex's own index belonged, so
  "All Kansas City Chiefs Events" became "All NFL Football Events". The payload
  already carried the right index; it is now the thing sent. The fake server
  lists templates in Plex's order so the suite covers it.
- Saving a paused pass's settings no longer resumes it. A field that is not
  sent means unchanged.
- Channel artwork now exports and imports by file name. A row used to carry the
  absolute path of the machine it came from, which put `logos//data/logos/...`
  in the zip and a foreign path in the restored row.
- The backing store's own passwords (`pg_password`, `my_password`) are secrets
  like the Plex token, and stay out of an export unless asked for.
- The Plex token travels as a header, never in a URL, so it cannot end up in
  an error message or a log line.
- `%` and `_` in a search or a smart-filter value are literal, not wildcards.
- A search with `&` or `#` in it survives the redirect.
- The first-run timezone picker starts on UTC. UTC was the default but not in
  the list, so the browser showed the first entry, Africa/Abidjan.
- The export checkbox says "Include secrets" and names them; it covered more
  than the Plex token and the label had not kept up.

### Changed

- `web.py` is split into `app/routes/`, one module per screen, with the app,
  middleware and background loops left in `web.py`. `base.html` no longer
  carries a thousand lines each of CSS and JS; they are `static/css/app.css`
  and `static/js/app.js`, versioned like the other scripts.
- `ruff` lints the tree at the start of every test run (`ruff.toml`). Every
  `raise` inside an `except` now chains its cause. Public functions in the
  core modules carry type hints.
- Every Plex client is closed when its request is done. A handful of routes
  used to leak one connection each.
- Pass airings are selected in SQL (`json_each` on the team list) instead of
  loading every future airing and sifting it in Python. A pass list of forty
  no longer reads twenty thousand rows each.
- Pass history (`our_grabs`, `pass_actions`) is trimmed to sixty days after
  each sync. It was never trimmed.
- A signed-in account with the `user` role can use the guide, the schedule and
  the passes, and is refused at settings, accounts, backups, the backing store
  and export/import. Until now the role was recorded and nothing read it.
- The default timezone is UTC, not one person's. A fresh install asks anyway.
- The backing-store test no longer leaves a `couchelephant_probe` table behind,
  and a sync reads the remote once per store rather than twice.

### Added

- **Plex's own settings on every pass, smart filters included.** Padding before
  and after, resolution, partial airings, commercial detection. They are the
  one-shot template's, because that is what a pass books for each airing, so
  the recurring-only choices are no longer offered and then silently dropped.
- **A sports pass now arrives with padding filled in**, one minute before and
  thirty after, shown before you create it. A game that runs long used to be cut
  off at whatever time the guide claimed.
- Padding suggests up to 180 minutes and caps nothing. Plex sends the field as
  a plain integer with no allowed-values list, so any number you type works.
- Every Plex setting now shows Plex's own explanation of it, as a tooltip on a
  small mark beside the label. Inline it ran to twenty lines: "Detect
  commercials" alone pushed one row past six hundred pixels. The options stay
  two columns of single rows. The tooltip is drawn on the body and clamped to
  the window, because every panel it can appear in scrolls, and a scrolling
  box clips its own children.
- The two padding fields are last, in that order. Plex lists them in the
  middle, which split the pair people actually reach for.
- The option row and the setting renderer existed twice and had drifted. They
  are one component now, in `static/js/ce.js`, with a test that keeps it that
  way.
- **Export and import.** One zip with everything you decided: passes, the
  recordings they booked, channel artwork, settings and accounts. Readable JSON
  inside, on purpose. The Plex token is left out unless you ask.
- **Snapshot backups.** Jobs with their own folder, schedule and retention.
  Optional AES-256 that 7-Zip and Keka can open. The database files are copied
  with SQLite's online backup, so a snapshot taken mid-write is consistent.
  Restoring takes a copy of the current state first.
- **A backing store.** A live two-way copy in PostgreSQL, MySQL or another
  SQLite file, reconciled by three-way merge on demand or on a timer. An edit
  beats a delete; a conflict goes to the later timestamp. Restoring pulls down
  and never writes back, so restoring onto an empty machine cannot erase the
  copy it is restoring from.
- Passes now carry a `uid`, and the recordings they book carry `pass_uid`, so a
  pass means the same thing on another machine.
- **1,310 teams to follow, not 76.** Plex lists only the teams playing in the
  next eleven days. CouchElephant now ships its own catalogue: every NFL, NBA,
  MLB, NHL, WNBA, MLS and NWSL side, 929 NCAA schools, and the top two tiers of
  six football leagues. The picker filters by league and marks which teams are
  in the guide this week.
- A team can be followed before it plays. The pass says it is waiting, and
  starts the moment the team appears in the guide.
- Teams are no longer deleted when they stop playing, so the list grows over a
  season instead of shrinking to whoever is on this week.
- **Smart passes.** The add panel now offers **Smart Pass** (a sports team, or a
  smart filter) and **Programme or Series**.
- A smart filter is a nested tree of conditions. A group matches all, any or
  none of what it holds, and groups nest to any depth. It can ask about title,
  series, description, genre, content rating, year, kind, first shown, length,
  channel, network, high definition and live.
- Content rating and running time are now stored, so a filter can ask about
  them. Both fill in on the next guide sync.
- Every condition carries **or blank**, because the guide rates only part of
  itself and a missing value silently defeats a negative condition otherwise.
- The panel counts what a filter would record, and shows the first matches,
  before anything is created. A loose or large filter has to be confirmed a
  second time, with the number on the button.

### Tests

- A test suite, 195 checks, in `tests/`. It runs against a fake Plex server
  that reproduces the real one's quirks, and a browser suite drives the pages
  themselves.
- `tests/isolation.py` refuses to run unless every path is scratch and Plex is
  on localhost. A suite that can reach production eventually reaches it.
- `scripts/test.sh` runs the whole thing in a throwaway container.

### Fixed

- Scripts are asked for by build version. Without that a deploy never reached
  the browser, so a shipped fix could sit on the server unseen.
- The Schedule legend named a colour per side, and the rows it described had
  no marker on them.
- Two colours in the verdict chip were written as hex instead of coming from
  the token block. There is now an `--on-solid` token, and a test that fails if
  another one appears.

## 0.90

The first release worth showing anyone. It records the live broadcast, and it
explains itself.

### Recording

- Chooses the airing itself, preferring the one the guide flags as live, and
  pins the recording to that channel and start time so Plex cannot re-choose.
- Passes follow a team or a programme and keep matching new airings.
- A rule can be limited to several networks or channels, which Plex cannot
  express. The limit is applied before the airing is chosen, not after.
- The record panel shows Plex's own options, read from Plex rather than copied,
  beside CouchElephant's, and says which of the two will own the rule.
- With no CouchElephant option set, it books a plain Plex rule instead.
- Anything it booked can be cancelled from the same panel.
- Every decision, including the ones it declined, is written down and shown.

### The guide

- Two-axis grid, channels down and time across, both loading as you reach the
  edge.
- Search and a sectioned, searchable filter panel.
- Channel logos, cached and refreshed, with your own artwork allowed to
  override them.

### Recordings

- Schedule reads Plex's own grab list, so it is what will really record.
- Agenda and calendar views over the same data.
- Every entry says who booked it and why. Clicking one opens the programme
  panel with that reason.
- Passes can be paused, edited and removed, and opening one shows what it will
  record next.

### The app

- Light and dark themes, per account when signed in.
- Sign-in off, local accounts, or Cloudflare Access.
- A settings window with sections, sub-tabs and a search that reads all of
  them.
- A first-run screen that tests the Plex connection before saving anything.
- Works on a phone.
