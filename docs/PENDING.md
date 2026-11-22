# Pending changes

Things agreed for a future build, not yet started. Newest at the bottom.
When one is built, move it into `CHANGELOG.md` and delete it here.

## Bug: a pass books the same game on two channels

Found 2026-11-22. The Chiefs pass booked Chiefs at Bills (Thu 2026-11-26)
twice: once on 41.1 KSHBDT (NBC) and once on 39.1 KGKCLD (Telemundo). The
second booking uses a second tuner and records a game nobody asked for.
A pass must book ONE airing per game unless the user turns on the option below.

## Feature: simultaneous and other-language broadcasts on sports passes

- Add a per-pass setting on team and season passes: "Also record the same game
  on other channels (simulcasts and other languages)".
- **Default is OFF.** With it off, the pass books one airing per game and
  never books a simulcast or an other-language feed.
- With it on, the user picks what to add: simulcasts in the same language,
  other languages, or both. Show the extra tuner use before saving.
- One game across channels is ONE game: match on teams plus kickoff, not on
  title, since "at" and "vs." titles differ by network.
- Existing passes keep the default (off). The upgrade must cancel extra
  bookings that the old behavior made, and leave one per game.
- Show it in the web UI and the Apple apps (iPhone, iPad, TV).

## Feature: guide search

Search the guide by channel (for example "Fox"), title and description.
Apple TV takes Siri dictation. Maybe recordings too. Brainstorm first.

## Feature: guide tabs

Tabs above the guide: All, Favorites, News, Sports. Brainstorm first.
