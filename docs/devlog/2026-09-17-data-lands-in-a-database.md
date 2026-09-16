# The data lands in a database (the storage rung)

*Devlog — 2026-09-17*

The broker has been sitting on the internet for a week, taking authenticated TLS messages and
passing them straight through to nowhere. This session built the next rung of the ladder — the
place the data actually *lands*: a time-series store, and the bridge that carries readings from the
broker into it. By the end, a fake "acceleration run" — rpm climbing from idle to redline, speed
rising with it — was flowing Mac → TLS broker → bridge → database and drawing as a line on a
dashboard. Like every session so far, it did not go straight there.

## The shape of the storage tier

The pipeline is now three containers in one stack instead of one:

- **The broker** (unchanged) — the public rendezvous point.
- **A time-series database** — InfluxDB (OSS v2: database, query engine, and a web UI in one
  image). This is where readings live and are queried.
- **A bridge** — Telegraf, the piece that subscribes to the broker's telemetry topic and writes
  each message into the database.

Standing the database up as a **container in the same compose stack** — rather than an `apt`-installed
service — is where the earlier "why containers for a single daemon?" bet finally pays off. The
justification was never the broker; it was the trajectory toward exactly this: a multi-service stack
brought up as one declarative unit. This is that moment. One `up` now composes broker + store +
bridge on a shared private network.

## Why a bridge at all (and why Telegraf)

A recurring question worth answering plainly: if the broker already has the data, why not have it
write to the database itself? Because the broker only *passes messages through* — it stores nothing.
Something has to catch each message and file it away. On self-hosted OSS, that "something" is an
external bridge.

Telegraf earned the role over a hand-rolled subscriber script for three reasons: it's **zero-code**
(a config file, not a program to own), it **buffers and retries** on its own if the database blips
(a second safety net beneath the edge node's buffer), and it's the **standard tool** for this shape
of job — which is the point of a portfolio built to demonstrate exactly these skills.

Two configuration decisions mattered more than the rest:

- **Honor the reading's own timestamp.** Each message carries its own `ts`. The bridge is told to
  use it as the record's time, rather than stamping it at ingest. This is the linchpin of the whole
  store-and-forward design: a reading buffered in a dead zone and flushed minutes later lands in the
  database *at the moment it was recorded*, so the graph back-fills with no smear.
- **Coerce everything numeric to one type.** The database locks each field to the type it first sees,
  and would later reject a value that changed type (an integer-looking `0` one cycle, a decimal the
  next). Parsing every number as a float from the start sidesteps that entirely — a small choice that
  avoids a whole category of silent write failures down the line.

`source` (which node sent the reading) is stored as a **tag** — a label to filter and group by —
rather than a field, which is the natural shape for a dimension you slice on.

## Least privilege, and not exposing the database

Two security choices, both cheap, both worth making now so they carry forward:

- **The bridge gets its own read-only account** on the broker rather than reusing the edge node's
  credential. It only ever subscribes, so it only needs read. If that credential leaks, a read-only
  account can't publish forged telemetry, and it can be rotated without touching the edge node. The
  same reasoning as never sharing one password across accounts.
- **The database is not exposed to the internet.** It publishes no public port. In-stack services
  reach it over the private network; a human reaches its dashboard through an **SSH tunnel** — a
  private, encrypted hallway from the workstation to the box that no one else can use. The stack's
  entire public surface stays exactly one port: the broker's TLS listener.

(That last point had a wrinkle — see the tunnel bug below.)

## On durability, since it came up

A fair worry: if the database container is taken down, is the data gone? No — and the distinction is
the whole point of how containers store state. The database writes to a **named volume**, which is a
directory on the host disk, *outside* the container's own throwaway layer. Restart, recreate,
upgrade, reboot — all preserve it. The one genuine footgun is the single command that deletes named
volumes, which simply never belongs in a script. The real residual risk isn't container churn at all;
it's **whole-box loss**, which no local setup solves and which a bare-metal install would share
identically. The answer to that is off-box backups — flagged now as the next durability step, not
built yet.

## A tidy-up along the way

As the directory outgrew its name — it started life holding only the broker and now holds the whole
stack — the layout was flattened one level so the folder reflects what it actually is. A small thing,
but it also surfaced a reusable lesson about moving a running stack: the tool derives its project
identity (and therefore its data-volume names) from the directory name, so keeping that name stable
across the move is what kept the existing data volume from being silently orphaned.

## The debugging saga

Four things bit, in order:

**1. Random secrets that quietly ate themselves.** The first generated passwords used an encoding
that can include `$`. The stack's config layer treats `$` as the start of a variable reference — so a
password like `…$gv6Ub…` had the `$gv6Ub` chunk interpreted as a variable, found undefined, and
**silently blanked out**. The visible symptom was a spray of "variable not set" warnings; the
invisible one was that the secret the database actually received wasn't the secret that got saved.
The fix: generate secrets as **hex** (letters and digits only), which can't collide with the syntax.
A good reminder that "random" isn't enough — random *and* safe for every layer it passes through.

**2. A database that wouldn't take the new password.** After fixing the secrets, sign-in still
failed. Cause: the database only runs its first-boot setup while its storage is **empty**, and the
first (corrupted) attempt had already initialized it — so the second start ignored the corrected
credentials entirely. The setup values are a one-time seed, not a live config. The fix was to
genuinely wipe the (still-empty-of-real-data) volume and let setup run cleanly against the good
secrets — and to *confirm* the wipe actually happened, which the first attempt hadn't.

**3. The bug that broke two listeners at once.** The internal, in-stack listener the bridge uses was
meant to be plaintext (it never leaves the private network); the public one is TLS. But the broker
applies its TLS settings to the *most recently declared* listener above them — and the config had the
listeners in an order that bound the certificate to the **internal** listener instead of the public
one. Two failures for the price of one ordering mistake: the internal listener became TLS, so the
bridge's plaintext connection got reset the instant it connected ("connection reset by peer"), *and*
the public listener lost its certificate. Reordering so the TLS block sits directly under the public
listener fixed both. The keeper: **in this broker's config, TLS options attach to a listener by
position — order is load-bearing, not cosmetic.**

**4. A dashboard that served an empty page.** With the database up, the tunnel reached the box but got
nothing back. The cause was the earlier "don't expose it" decision taken slightly too literally: a
container with *no* published port isn't reachable even from the box's own loopback, so the tunnel
terminated on a dead port. The fix is the standard pattern for exactly this: publish the database to
the box's **loopback interface only** — reachable from the box itself (and therefore through the
tunnel), but never from the internet. Public surface unchanged; dashboard now reachable.

With those cleared, the bridge logged a clean `Connected` to the broker, and ten fake readings sent
from the workstation over the real TLS path showed up in the database and graphed as a rising line.

## Where this leaves things

The **storage rung is done and proven end-to-end with fake data** — the whole path from an
authenticated TLS publish to a queryable, graphable point is working, and it was exercised exactly the
way the philosophy asks: prove the layer with synthetic data before trusting the real thing. The
edge node needs no change to go live; it already publishes to the same topic.

Next: point the bike at it and watch real telemetry fill the database; **choose the visualization
tool** (still deliberately open — the database's built-in UI is fine for verification, but the
dashboards/alerting layer is its own decision); build the gear-from-ratio analyzer; and close the one
real durability gap with **off-box backups**.
