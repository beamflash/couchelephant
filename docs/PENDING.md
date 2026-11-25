# Pending changes

Things agreed for a future build, not yet started. Newest at the bottom.
When one is built, move it into `CHANGELOG.md` and delete it here.

## Feature: warn before a Plex library folder blocks a handover

Found 2026-11-23. Plex made `tv/NFL Football (2002)` as its own user with
mode 755, so CouchElephant (root, squashed to another user over NFS) could
not copy the Chiefs game in. Setup checks only the library root today.

- **Install:** add `PUID`/`PGID` to the container and the Unraid template.
  Setup reads which user Plex runs as and says to match it. With the same
  user, CouchElephant can write every folder Plex makes.
- **Setup, and once a day:** check every show and season folder one level
  under each library root. One notice lists the folders it cannot write.
- **When a recording is booked:** check the folder it goes to. A warning on
  the booking and a notice, days before it matters.
- **When the recording starts:** check again. The notice turns red and the
  apps get a push.
- Each notice names the folder and gives the fix to copy, for example
  `chmod -R a+rwX "<folder>"`, and on Unraid names the New Permissions tool.
- **After a failure:** say "CouchElephant cannot write to the folder X" in
  plain words, not the errno. Add "Retry now" next to the automatic retry.
- CouchElephant cannot change the permissions itself: only the folder's
  owner, or root on the NAS, can.

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
