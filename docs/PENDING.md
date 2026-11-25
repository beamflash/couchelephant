# Pending changes

Things agreed for a future build, not yet started. Newest at the bottom.
When one is built, move it into `CHANGELOG.md` and delete it here.

## Feature: guide search

Search the guide by channel (for example "Fox"), title and description.
Apple TV takes Siri dictation. Maybe recordings too. Brainstorm first.

## Feature: guide tabs

Tabs above the guide: All, Favorites, News, Sports. Brainstorm first.

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
