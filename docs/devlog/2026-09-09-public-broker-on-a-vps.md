# The broker goes public (a rendezvous on a rented box)

*Devlog — 2026-09-09*

The edge node already publishes MQTT to a broker on the Mac. That proves the transport on the
LAN, but it can't survive the actual use case: a bike on a phone hotspot, on a different network,
often nowhere near home. This session's job was to move the broker off the Mac and onto a
**publicly-reachable, self-managed VPS** — so the bike can publish from anywhere — and to do it
*properly*: TLS, auth, hardening, config-as-code, and hands-off cert renewal. By the end the
broker was live on the internet and I'd verified a real message round-trip to it from the Mac.

## A design detour first: store-and-forward comes back

Before any infra, a design question resurfaced: what happens when the bike rides through a
no-coverage dead zone? An earlier decision had *dropped* store-and-forward entirely, on the
reasoning that SD-card replay "traded away the live-telemetry payoff." Talking it through, that
turned out to conflate two separate things — the **concept** (don't lose data in a dead zone,
which on a motorcycle is a real, recurring situation) with one heavy **medium** (adding an SD
module). The concept is worth keeping; the medium was the objectionable part.

So the design is now **live-first with a store-and-forward safety net**: publish live when
connected, buffer while offline, and on reconnect flush the backlog *and* resume live. Live is
never traded away — buffering just sits underneath it. This works cleanly because every reading
already carries its own timestamp, so a message buffered in a tunnel and flushed minutes later
lands in the time-series store *at the moment it was recorded*, not at flush time — the graph
back-fills with no gap.

The medium, deliberately scoped small for now: a **bounded in-RAM ring buffer** on the ESP32
(it has plenty of PSRAM), which covers dead zones *within a continuous power session* with zero
new hardware. Surviving a power loss (ignition off mid-dead-zone) and whole-ride offline capture
are explicitly deferred to a durable tier (flash, or the SD module — deferred, not dropped). The
discipline that makes that safe to defer: write the buffer behind an **append/drain interface**
now, so swapping in a durable backing later is a storage change, not a re-architecture.

## Why a self-managed VPS (and not managed anything)

The reachability model was already settled: MQTT clients all connect *outbound* to a single
public broker, which sails through NAT/CGNAT on both ends — so "live from anywhere" needs exactly
one publicly-addressable broker, not any inbound routing. The open question was *where*.

The choice was a **self-managed Linux VM on cloud IaaS**, not a managed MQTT SaaS. Two reasons:
it avoids the subscription trap this project is deliberately steering around, and standing up and
securing the box *is* the DevOps skill the project exists to demonstrate. I picked a low-cost VPS
tier from a mainstream IaaS provider, on the current Ubuntu LTS. Region/latency turned out to be a
non-issue: telemetry is a few readings a second with client-side timestamps, so tens of
milliseconds of transit is invisible on the dashboards. The only thing that actually matters for
the architecture is a public IPv4 the clients can dial.

## Locking the box down before anything runs

A public IP means bots start knocking within minutes, so hardening came before the broker:

- **Firewall, default-deny.** Everything inbound is blocked except the handful of ports actually
  needed. The mental model that made this click: the firewall controls *who can reach a door*; it
  doesn't *lock the door* — that's the job of the layers behind it.
- **SSH key-only — after a near-miss.** I almost disabled password auth while still logging in *by
  password*, which would have locked me out. A quick "force key-only and see if it works" test
  caught it: key auth wasn't actually set up yet. So the real order emerged — enroll the key,
  *prove* key login works in a second session, *then* disable passwords. Lesson worth keeping:
  never turn off password auth until you've confirmed the key works, with a fallback session open.
- **Backup access, on purpose.** Key backed up in a password manager, plus the provider's
  out-of-band web console as the ultimate lifeline (which makes the cloud account itself the thing
  to protect — strong password + 2FA). A second device key is a future nicety.

A useful reframing came out of the "won't an open broker port get abused?" question: you can't
stop bots from *connecting* to a public port — that's what public means. What you can do is make
every connection *fruitless* (TLS + auth), and *bounded* (an ACL confines even a valid client to
its own topic tree). You don't stop the knocking; you make it harmless.

## TLS: Let's Encrypt over self-signed

For a private broker with a couple of known clients, a self-signed CA would have worked and
avoided renewals. I went with **Let's Encrypt** anyway — real public TLS on a real subdomain is a
better portfolio artifact and removes the need to distribute a private CA to every client. The
tradeoff is 90-day certs, so **automatic renewal** had to be part of the build, not an afterthought.

The one genuinely fiddly bit: the broker runs as an unprivileged user inside its container and
can't read the certificate authority's root-owned key files directly. The fix is a small hook
script that copies the renewed cert into a location the container can read, sets the right
ownership, and reloads the broker — and that same script is wired as the renewal deploy-hook, so
every future renewal is hands-off. A dry-run of the whole renewal path confirmed it end to end.

## Docker, and an honest look at whether it was worth it

Standing the broker up in a container invited a fair challenge: for a single small daemon, isn't
`apt install` simpler? Honestly, yes — for the broker *alone*. The justification isn't the broker;
it's the trajectory. This is about to become a multi-service pipeline (broker → time-series store →
dashboards → a bridge), and running that as one declarative stack is where containers earn their
keep — plus "containerized the stack" is exactly the skill the target roles look for. The one-time
awkwardness (hashing the broker password through a throwaway container) I later sidestepped by just
installing the native client tools for that admin task — a reasonable pragmatic exception that
doesn't compromise the containerized broker itself.

## Config-as-code, secrets split, and a CI/CD-friendly deploy

The whole broker is defined in committed files — the container definition, the broker config, the
ACL, the renewal hook. **Secrets never enter git**: the credential store and the TLS private key
are generated on the box and gitignored. Getting the config onto the box went through a
**read-only deploy key** rather than a copy-and-paste, so updates become "pull and restart" — the
first inch of a real CD loop. (It also forced a small git lesson: don't amend a commit that's
already been pushed — it rewrites history and diverges the remote.)

## The bug that ate an hour: "Protocol error" from the Mac

The end-to-end test worked immediately *on the box* but failed from the Mac with a blunt
`Error: Protocol error`. The cause was a cross-platform trap: the client only speaks TLS if you
hand it a certificate authority to trust, and the way you point at the system trust store differs
by OS. The command that worked on Linux pointed at a directory that simply isn't populated on
macOS — so TLS never engaged, plaintext hit the TLS port, and the client bailed with a vague
protocol error. Pointing the Mac client at an explicit CA file fixed it instantly. The keeper:
**"protocol error" from an MQTT client almost always means TLS didn't engage — not an MQTT
problem.**

## A note on what gets written down where

One meta-decision this session: committed docs and READMEs are treated as external-facing, so they
carry overview, architecture, and security *posture* only — never runbooks or host specifics
(addresses, hostnames, credentials, exact commands) that would be handy to an attacker if the repo
ever leaked. The step-by-step operational runbook lives in a **gitignored** local handoff instead.
This very devlog follows that rule: it's the story and the reasoning, with the specifics left out.

## Where this leaves things

The broker is **live, secured, and self-sustaining**: publicly reachable, TLS-encrypted,
authenticated, ACL-scoped, firewalled, key-only SSH, and auto-renewing its certificate — verified
with a real round-trip from the Mac over the internet. It survives reboots and needs no babysitting;
it's just sitting there as a ready endpoint, idle until something publishes.

**Next:** point the ESP32 at it — TLS to the public broker, same credentials, publishing real
**RPM + speed** (with the in-RAM store-and-forward buffer) instead of a LAN heartbeat. That's the
"real data" rung of the ladder, and the transport it'll ride on is now production-grade.
