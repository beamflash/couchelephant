# Accounts

Three modes, under **Settings, Accounts**. A fresh install uses the first.

| Mode | Who decides | Right for |
| --- | --- | --- |
| **Off** | nobody | A box on your own network |
| **Local** | CouchElephant | A handful of people, no other infrastructure |
| **Cloudflare Access** | Cloudflare | Anything reachable from the internet |

## Off

No sign-in. Anyone who can reach the page can use it, including changing
settings and creating recordings.

This is the default because a new install has nothing worth protecting yet and
a forced account creation is a wall in front of a thing you have not decided to
keep. It is the wrong setting the moment this is reachable from anywhere but
your own network.

## Local accounts

Turn it on and the next visitor is asked to create the first account, which is
the administrator. After that, `/login`.

Every account can use the guide, the schedule and the passes. Only an
administrator can change settings, manage accounts or artwork, run backups,
configure the backing store, or export and import. The first account is the
administrator; with Cloudflare Access, later visitors arrive as plain users.

Passwords are scrypt hashed with a per-user random salt. Session tokens are
random, stored hashed, and last 30 days. A copy of `auth.db` grants no logins.

A username that does not exist is still hashed against before failing, so a
wrong username cannot answer measurably faster than a wrong password.

## Cloudflare Access

Put CouchElephant behind a Cloudflare Access application and let Cloudflare
authenticate. You need two things from the Access dashboard:

- **Team domain**, for example `yourteam.cloudflareaccess.com`
- **AUD**, the application's Audience tag

Cloudflare forwards each request with a signed token in
`Cf-Access-Jwt-Assertion`, mirrored in the `CF_Authorization` cookie.
CouchElephant verifies that token against your team's public keys and the AUD.

**The plain email header is never trusted on its own.** A request that reaches
the origin without passing through Cloudflare cannot claim an identity, which
is the point of verifying rather than reading.

An email Cloudflare has authenticated but that has no local account gets one on
first sight. Cloudflare has already decided who may reach the app; asking you
to add each person again after that would be theatre.

### It refuses to lock you out

Switching to Cloudflare mode fetches your team's signing keys first and refuses
the change if it cannot reach them. A typo in the team domain would otherwise
lock everyone out of a box that may not be reachable another way.

For the same reason, turning sign-in **off** is always allowed from inside. If
Access breaks, reach the box on your own network and switch back.

## Away from home

Under **Settings, Accounts, Sign-in**, **Use away from home**. It is off by
default. Leave it off if you reach the box over a VPN or an existing tunnel:
those look like your home network, and nothing here is needed.

### What the switch does

With it on, someone outside your home network can reach only:

- the sign-in page,
- paired apps (TV and phone), which prove themselves with their own key,
- video links that carry their own pass.

Settings, setup and pairing stay home only. From outside they answer "Not
found" (pairing answers "Pair this device at home first"), even for a signed
in administrator. The one exception is pairing over HTTPS to your own domain
once it holds a real certificate (see below).

The admin tools behind Settings stay home only too: import and export,
backups, the backing store, updates, the device list, the connect QR code and
the library folder suggestions. `/api/tv/settings` answers from outside only to
a paired device with its key.

A request from outside with no `Host` header is refused while the switch is
on.

It will not turn on while any way in has no password. If sign-in is Off, the
page offers to make the first account. While away is on, turning sign-in Off
is refused. Turn away off first.

The self-check beside the switch says in plain words what is safe and what is
not, including whether the connection is encrypted.

### How home is decided

Home means loopback, the private ranges (10.x, 172.16 to 172.31, 192.168.x),
link-local and IPv6 unique-local addresses, and the subnets this machine sits
on. Everything else is outside, including your router's public address, so a
device that goes out through the internet address and back in (hairpin NAT)
is asked to sign in.

Docker's own networks are never home. A container on a bridge network does
not count its bridge subnet or its gateway as home, because every outside
visitor arrives from there. The exception is a macvlan or ipvlan network,
whose subnet is your real LAN. On a plain Docker install, a browser on the
host itself or a LAN client that reaches the container through Docker's IPv6
proxy therefore signs in too.

Forwarding headers (`X-Forwarded-For`, `Forwarded`, `X-Real-IP`) are read in
one place and only from a trusted proxy. List your proxies under the switch, in
**Trusted proxies**, one address or range per line. Ranges wider than /8 for
IPv4 or /32 for IPv6 are refused. A request that carries a forwarding header
from a peer that is not listed is never home, and the self-check warns about
it for a day.

If a reverse proxy runs on the same machine (nginx, Caddy, a tunnel), add its
address, often `127.0.0.1`, and have it send `X-Forwarded-For`. Without the
header, every visitor arrives from the proxy and looks like home. Stock nginx
`proxy_pass` sends no forwarding header until you add one. The same holds for
a sidecar proxy that shares a container's network.

### Sign-in limits

Five wrong passwords in ten minutes, from one address or at one account,
lock that address or account for a minute. Each further lock doubles, up to an
hour. IPv6 is counted by /64. Tries from home never lock an account. A locked
`/login` or `/api/tv/pair` answers 429 with `Retry-After`.

Connections are limited for outsiders only. One outside address (IPv6 by /64)
may hold 16 at a time, and all outsiders together share a cap set from the
open file limit. A home device is never refused by it. On a Docker bridge
network every visitor arrives from the gateway, so the gateway is held only to
the shared cap. Each request's headers must arrive within 10 seconds of the
connection going idle. A stream or download that is still running is never
cut.

### HTTPS on the same port

With the switch on, the port speaks HTTPS as well as HTTP. CouchElephant tells
the two apart by the first byte of each connection, so you forward one port and
nothing else changes. An `http://` request to an outside visitor is redirected
to `https://`; home keeps plain HTTP. With the switch off the port speaks plain HTTP only.

The server makes its own certificate the first time it needs one, kept under
`tls/` in the data folder, valid for ten years. Browsers warn about it. The
apps do not mind: they check the key, not the name (see below). Replies over
HTTPS carry `Strict-Transport-Security`. With a real certificate, browsers
then remember to use HTTPS for that domain for a year. Turning the switch off
closes HTTPS, so those browsers cannot open the domain until it is on again.

### A real certificate for a domain

Under the switch, **A real certificate**, enter a domain (for example
`tv.example.org`). The domain must point at your home's internet address, and
your router must send port 443 to the CouchElephant port. CouchElephant gets a free
certificate from Let's Encrypt with TLS-ALPN-01: Let's Encrypt connects to the
domain on port 443 and the server answers on the same port it always uses.
Nothing else listens, and no port 80 is needed.

- A failed try, or a failed renewal while the old certificate is still
  valid, is retried by itself every 15 minutes, which keeps under Let's
  Encrypt's limit on failed checks. **Try again** asks now, within that limit.
- The certificate renews itself 30 days before it ends.
- Until it works, or if it breaks, the own certificate still answers and the
  apps still connect.
- Home-only names (`.local`, `.lan`, `.home.arpa`, `.internal`) are refused.
- Over HTTPS to that domain, a device may pair from outside, because the
  system itself can check who it is talking to.

### How the apps pin the key

The apps learn the server's key at home. The Bonjour record and the QR code
under Settings carry its fingerprint (`fp`, the SHA-256 of the public key,
base64url) whenever the switch is on. The app stores it in its keychain, per
server.

The app checks the certificate on every connection to its server: the system
must trust it, or its key must match the stored pin. Whether the device key may
go to an address is a separate rule, and system trust is not enough for it.

The device key goes only to:

- the address the app has stored, or
- a new https address whose key matches the pin stored for that server, checked
  on that same connection.

With no pin stored, only an address Bonjour found at home, over plain HTTP on a
private network range, may take the key. That is how a server that moves at
home is found again.

A link, a QR code, a switch or a typed address to a different address never
carries the key. The app asks before it switches, and then pairs again with a
code.

- A link or QR code never replaces a stored pin. A pin that differs from the
  stored one is refused. A new pin is kept only when none is stored for that
  server, and only if the server's key really matches it. The system trusting
  the certificate does not count. A pin on an `http://` link is ignored.
- A refused link leaves the app where it was and shows the reason there.
  Whenever an address is stored, the app asks before it switches, in every
  screen.
- A server with no install id has no pin.
- If the key changes (you deleted the `tls/` folder, or restored onto a new
  machine), the app says "This is not the server you paired with. Pair again
  at home." Forget the server in the app and pair again at home.
- A server whose away switch is off does not answer on HTTPS. The app says so
  and points you at the server's settings.
- Video plays only from the stored address. Behind a TLS proxy the server
  hands out `http://` video links to its own name; the app moves those onto
  the stored address. A video link to another host does not play.
- AVPlayer cannot be told to trust a pin, so when the pin vouches for the
  server, video plays through a small proxy inside the app. It listens on
  `127.0.0.1` only and wants a random token on every path.

## Per-account preferences

Dark mode belongs to the person, not the server. Signed in, the choice is
stored against the account and stamped into the page by the server, so it
follows you to another browser and cannot flash the previous account's choice
on the way in. Signed out, the browser keeps it, which is the only place it can
live when nobody is identified.
