# Pending changes

Things agreed for a future build, not yet started. Newest at the bottom.
When one is built, move it into `CHANGELOG.md` and delete it here.

## The now line in the apps' guides

Asked 2026-11-17.

The web guide draws an orange line down the grid at the current time
(`#nowline`, class `.gnow`, in `app/templates/guide.html` and
`app/static/css/app.css`: a 2 px line in the accent colour with a dot at the
top, moved as the clock runs). The Apple TV, iPhone and iPad guides have none.

- Draw the same line in `clients/apple/TV/GuideView.swift` and
  `clients/apple/Phone/GuideView.swift`, in `Theme.accent`, over the grid at
  `now - guide.start` scaled by the grid's points per minute.
- Keep it moving while the guide is open, as the web page does.
- Same look on every client: line plus the dot at the top.

## A clicking test suite for the Apple apps

Asked 2026-11-17: regression tests press real buttons, not only launch
flags and screenshots.

The web UI already has one (Playwright, `tests/ui`). The Apple apps have
none. Add an XCUITest target for the iPhone/iPad app and one for the Apple TV
app, run with `xcodebuild test` in the funtimes VM. Cover what the debug
flags could only fake:

- iPad turned to landscape and back (`XCUIDevice.shared.orientation`).
- Live TV left with the Home button and opened again (`press(.home)`, then
  `activate()`): the tuner is given back and the channel tunes again.
- Pause, leave, come back: the channel is still held.
- Skip ad pressed inside a break moves past it.
- Settings: a switch turned and saved; New season pass searched and followed.
- The Apple TV remote (`XCUIRemote`) through the tabs, the guide and the player.

## The guide over live TV

Asked 2026-11-17.

While watching live TV, the guide opens over the picture, and the channel
keeps playing behind it.

- Press up: the player buttons show over the picture (as now).
- Press down: the guide shows over the picture. Browse it with the remote,
  and the live channel keeps playing and keeps its tuner.
- It is the same guide as the Guide tab (`clients/apple/TV/GuideView.swift`,
  `clients/apple/Phone/GuideView.swift`, `app/templates/guide.html`) in
  overlay form: same grid, same now line, same details.
- Record from the overlay: a programme or a season pass, the same choices
  as the Guide tab.
- Back (Menu on the remote, Escape on the web) closes the overlay and
  leaves the channel playing.
- Picking a programme on now on another channel tunes to that channel.
- Apple TV first (up and down on the remote, in `PlayerView.swift` beside
  the Skip ad action). iPhone/iPad: a swipe or a Guide button. Web: the
  arrow keys in `app/static/js/player.js`.
