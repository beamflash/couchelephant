# Pending changes

Things agreed for a future build, not yet started. Newest at the bottom.
When one is built, move it into `CHANGELOG.md` and delete it here.

## Feature: the wiki, written in GitHub, shown on couchelephant.app

- The GitHub Wiki of this repo is the single source. Only the two owner
  accounts edit it (the repo's collaborators). "Restrict editing to
  collaborators only" stays on. Nobody else gets write access.
- A wiki edit (GitHub's `gollum` event) starts a workflow here. It clones
  the wiki repo, turns `[[Page]]` links and page file names into site links,
  uses `_Sidebar.md` as the menu, and builds it with MkDocs Material.
- The site job deploys it to Cloudflare Pages at `/wiki`, with search.
  Each page has an "Edit this page" link to the GitHub wiki page.
- Fits the free Pages plan (20,000 files, 25 MB per file, 500 builds a
  month). No server, no database.

## Bug: resume and Skip ad do nothing early in playback (owner report 2026-11-26)

Owner played recording 10, "Arizona Cardinals at Kansas City Chiefs"
(in_plex, Plex rating key 248917), on Apple TV 1.5.33 against server 1.5.40.

- **Resume:** the app offered to resume at 2:47 (`plex_position` 10045 s).
  He said yes, and playback started at 0:00. CouchElephant's own `position`
  was then 445.7 s from this viewing.
- **Skip ad:** the button showed in the first minutes, during the pregame
  team intro. Pressing it did nothing several times. A later press worked.
- **Ad breaks for this recording** start with `(0.0, 187.92)`. The opening
  3 minutes (the team intro) are marked as an ad, which is likely a false
  positive.

Likely single cause, to confirm first (systematic debugging): a seek to a
point the stream does not have yet is dropped. If the player streams a
transcode that is still being produced, 10045 s and 187.92 s do not exist
yet at start. That would drop the resume, and make Skip ad work only once the
stream passes 188 s. Fix the class: every seek (resume, Skip ad, scrubbing,
chapter jumps) must wait for the target to become playable, or ask the
server to start the stream at the target, and the UI must say so. It must
never silently do nothing. Also check which value the resume prompt and the
player read (`position` vs `plex_position`), and whether an "ad" at 0 s
should be offered at all. Test on iPhone, iPad and Apple TV with a long
recording, resuming near the end.
