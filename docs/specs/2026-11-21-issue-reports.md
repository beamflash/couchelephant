# Issue reports

Status: design, 2026-11-21. Covers the server, the web app and the Apple apps
(iPhone, iPad, Apple TV). Guide search and the guide category tabs are separate
projects with their own designs.

## Why

CouchElephant is offered by one person, with an AI doing the first reading.
There is no company and no support desk. The people who use it are strangers on
their own networks with their own servers. Nobody can SSH in or look at their
screen. The report they send is the only thing support ever sees, so it has to
be enough on its own.

Success: an AI reading a report can sort it into "known bug", "setup mistake"
or "new problem" without asking the sender anything.

## Goals

- A failure offers **Report this problem** right where it shows, with what
  failed already attached.
- A **support form** (Settings, "Report an issue") on the web and in every app,
  for anything, any time.
- After a crash, the next launch offers to send a report.
- Every report carries enough to diagnose: the app's recent log, the server's
  recent log and settings, versions, device, the error and screen, and an
  optional message.
- The message is optional. On Apple TV it works with Siri remote dictation (the
  standard tvOS text field with the dictation button).
- An optional email address, so the owner can write back by hand. It starts
  filled in with the email of the Plex account the server is signed in to; the
  person can change or clear it; the app remembers it.
- Nothing is sent until the person agrees. They see a plain summary of what
  will go before they send.

## Non-goals

- No replies inside the app. A report is one way. Any answer goes by email.
- No automatic sending without consent, no background analytics, no usage
  tracking.
- No support dashboard yet. Reports are read with a small command-line tool and
  sorted by an AI.

## Anti-goals (a failure even if it works)

- A report that can be read by anyone but the owner: on the device, in
  transit, or in the database.
- A report that contains a password, a session or device token, a Plex token,
  the server's TLS key, an ACME account key, or any other secret.
- The report screen itself failing in a way the person cannot see.
- A dead server stopping a report from being sent.

## How it works

### Sealed for the owner only

- The owner has one key pair. The **public key** is built into the server and
  the apps. The **private key** never leaves the owner's machine; it is not in
  any repo, app or server.
- The sender builds the report, compresses it, and seals it to the public key
  (an anonymous sealed box: X25519 + XSalsa20-Poly1305, libsodium
  `crypto_box_seal`, or an equivalent the builder picks that both Python and
  Swift can do without heavy dependencies).
- A report waiting to be sent (offline, or the send failed) is stored sealed on
  the device and retried. Nothing readable is written to disk.
- It travels over HTTPS and is stored still sealed. The database never sees
  the contents.

### Where it lands: Firebase

- A new Firebase project on the owner's Google account, free (Spark) plan.
  Cloud Firestore holds the reports.
- Each document holds: the sealed blob, its size, a created time, the source
  kind (`web`, `ios`, `ipados`, `tvos`, `server`), the app version, and a
  random report id. Nothing else in clear: no email, no address, no message.
- **Security rules:** create only. No read, update, delete or list from any
  client. Size capped (builder picks, about 1 MB sealed). Required fields and
  types checked.
- **Abuse:** the Apple apps use Firebase App Check with App Attest, so only the
  real app can write. The web app and server cannot be attested because their
  code is public; for them the rules cap the size, and the sending side limits
  itself (builder picks, about 10 reports a day per install). The owner accepts
  that the open source path can be spammed; the reader tool drops junk.
- The owner reads with an admin key, on their own machine.

### What goes in a report

- Report id, time, source, app version and build, OS version, device model.
- The trigger: which error, on which screen, with the error text verbatim; or
  "support form"; or "crash on last run" with the crash details.
- The app's own log for the last while (builder picks the window, about the
  last 15 minutes or 2,000 lines).
- From the server, when it answers: version, uptime, recent log, settings,
  tuner and recorder state, pass list, recent schedule failures, disk space of
  the recording folder, and the OS and container details.
- If the server does not answer: the app's part only, plus a line saying the
  server did not answer and the address tried (masked).
- The person's message and email, if given.

### Scrubbing

- Secrets are removed before sealing, by an allow list where possible (send
  named settings, not "all settings") and a deny pattern for anything shaped
  like a token or key.
- Addresses on the person's network are masked to the shape that matters
  (private range, port, scheme). Hostnames are hashed.
- The person's own message is sent as typed.
- Tests prove that no known secret field survives into a report.

### The server's part

- A new authenticated endpoint returns the server's part of a report
  (scrubbed), for the web app and paired apps to fold in.
- The server keeps its recent log in memory (a ring buffer) so there is always
  something to send, even with no log file.
- The server can also send a report itself, from the web app's form or a
  failure page.
- The server asks plex.tv for the signed-in account's email, only to fill in
  the email field. It is never sent anywhere unless the person keeps it in the
  field and sends.

### Consent

- First time a person sends, a short screen says what a report contains and
  who reads it, with Send and Never mind. That is the agreement.
- Each send shows the summary again in a line or two, the message field and
  the email field.
- Settings has a switch to turn the crash prompt off.

### Where it shows

- **Apple apps:** every error alert and error state gets "Report this problem".
  Settings gets "Report an issue". The next launch after a crash offers a
  report.
- **Web:** every error message and failure panel gets "Report this problem".
  Settings gets "Report an issue".
- After sending: "Sent. Thank you." with the report id, so a person who writes
  in later can quote it.

### The reader

- A small tool in the private Apple repo (not the public one): pulls new
  reports with the admin key, opens them with the private key, writes each one
  as a readable folder, and marks it fetched. An AI then reads and sorts them.

## Constraints

- Public repo rules hold: no host names, LAN addresses or people's names, and
  never the private key or an admin key.
- Regression tests click the real UI: Playwright on web, XCUITest on Apple.
  Include a TV test that the message field accepts text (dictation itself
  cannot run in the simulator).
- Free tier only. Any paid Firebase feature needs the owner's yes first.
- Patch version bumps.

## Left to the builder

- The exact sealing library on each side.
- Log windows, size caps, rate limits (numbers above are guides).
- Screen layout and wording, in the app's existing style.
- How the Apple apps keep their own log (OSLog store or a ring buffer).

## Calls made after the builder check (the designer's, not the owner's)

1. **Path per source.**
   - Apple apps send straight to Firestore over its REST API, with an App Check token from App Attest (the Firebase App Check package is the only Firebase code in the apps). A dead server never blocks them.
   - The web app sends through its own server, which sends to Firestore. When that server is dead the web page cannot load anyway, so there is nothing to report from it.
   - The server sends its own reports directly.
   - The Firebase web config (project id, API key) is public by design. It grants only what the rules allow.
2. **Quota and junk.** If a send fails for any reason, the person sees "Could not send. It is saved and will be sent later.", and the sealed report waits in the queue. The queue keeps at most 20 reports, and the oldest goes first. The per-install limit is enforced on the sending side only. A random install id is made on first send and stays in the clear. It is not linked to anything else, and it lets the reader group reports and drop floods. Spark limits (20,000 writes a day, 1 GiB) are accepted.
3. **Who may fetch the server's part.** An administrator signed in on the web, or a paired device with its key. The settings that go are an explicit allow list in code, reviewed in the code review. Show titles, team names and pass names are fine to send, because they are programmes and teams, not people. Plex server address, tuner address and the server's own address are masked.
4. **Keys.** The builder generates the key pair on the build machine. The private key goes into the owner's password vault (Vaultwarden), and the local copy is deleted. Each report names its key id, so a later key can be added without orphaning old builds. The reader tries every key it holds.
5. **Email.** The app asks the server, which asks plex.tv with the token it already holds. Only the email comes back, never the token. If the server has no Plex sign-in or does not answer, the field starts empty. The app remembers the address in the Keychain on Apple and in the browser on the web. That is the person's own setting, not a queued report.
6. **Crash.** The app sets a "running" mark when it starts and clears it on a clean exit or when it goes to the background. If the mark is still there at the next launch, the app offers a report. MetricKit crash details are added when the system has delivered them. The XCUITest covers the offer by launching with the mark set.
7. **Which errors.** One shared report button component on each platform, used wherever an error is shown to the person: failed requests, failed playback, failed saves, server not reachable. Expected states that are not errors, such as "no tuner found yet" during setup, do not get it.
8. **Masking.** Hostnames and public addresses are hashed with a salt made fresh for each report, so reports cannot be linked through them.
9. **Order.** The server and web ship first (one patch release). The Apple apps follow in the next TestFlight builds. Older apps simply have no report button.
10. **App Store.** Before the apps go to the App Store, their privacy label must list Diagnostics and Contact Info (optional, sent only by the person). TestFlight needs nothing.

## Needs the owner

- Nothing before building. The builder creates the Firebase project (free)
  through the owner's signed-in Google account, and the key pair as in call 4.
