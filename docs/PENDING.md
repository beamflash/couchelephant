# Pending changes

Things agreed for a future build, not yet started. Newest at the bottom.
When one is built, move it into `CHANGELOG.md` and delete it here.

## Feature: simultaneous and other-language broadcasts on sports passes

- Add a per-pass choice on team and season passes, "Channels per game", with
  three options:
  1. **One airing per game** (the default).
  2. **All airings in my preferred language**: every channel that carries the
     game in the preferred language (simulcasts).
  3. **All airings, any language**: every channel that carries the game, in
     any language.
- **Default is option 1.** The pass never books a second channel unless the
  user picks option 2 or 3.
- "Preferred language" is one server setting (default English), shown next to
  the choice.
- Show the extra tuner use before saving.
- One game across channels is ONE game: same teams, kickoffs close together
  (the 1.5.34 grouping).
- Open question: where the language of an airing comes from. The Plex guide
  shows it only indirectly today (for example grandparent title "Fútbol
  Americano NFL" on Telemundo). Check the guide data for a language field
  before building, else use a per-channel language setting.
- Existing passes keep option 1.
- Show it in the web UI and the Apple apps (iPhone, iPad, TV).

## Feature: guide search

Search the guide by channel (for example "Fox"), title and description.
Apple TV takes Siri dictation. Maybe recordings too. Brainstorm first.

## Feature: guide tabs

Tabs above the guide: All, Favorites, News, Sports. Brainstorm first.
