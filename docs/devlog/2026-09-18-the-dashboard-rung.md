# The dashboard rung (choosing, and building, the viz layer)

*Devlog — 2026-09-18*

For a week the pipeline has been complete except for the one part a human actually looks at. Data
flowed edge → TLS broker → bridge → database and could be *queried*, but only through a database's
bare Data Explorer. This session built the last rung of the ladder — the visualization layer — and,
just as importantly, made the deliberate choice of *what* that layer should be rather than reaching
for the obvious default on reflex.

## Choosing the tool, not defaulting to it

The favourite going in was Grafana, but the project's own rule is that the favourite still has to win
a comparison. Three families were on the table:

- **Grafana (self-hosted).** Native to the database, real alerting, and — decisively for a portfolio
  aimed at observability work — the tool those jobs actually operate. It deploys as one more container
  on the existing stack, configured as code.
- **The database's own UI.** Already running, zero new moving parts. Fine for verification, weak as a
  dashboarding-and-alerting layer, and it demonstrates no separate, marketable skill.
- **Hosted SaaS (Datadog / New Relic).** These *are* the tools the target jobs run on, which is a real
  pull. But adopting one as the pipeline's home contradicts the project's founding thesis — the whole
  reason the broker and store are self-hosted is to avoid the subscription trap. Practically, they also
  don't read from the existing store; you'd bolt on a second ingest path and a duplicate backend, and
  hand your data to someone else's vault.

The tension with the SaaS option was worth sitting with rather than dismissing, because it surfaced
the genuinely portable skill hiding underneath it: **OpenTelemetry**. OTel is the vendor-neutral way
to *produce and ship* observability data — instrument once, export anywhere. A collector is a pipe,
not a store; it can fan the same telemetry out to several backends at once. That reframes the SaaS
tools from "a foundation we bet on" to "a later learning chapter" — export the same rides into a free
tier *alongside* the self-hosted stack, learning the industry-standard export path without giving up
ownership of the data. Noted for the roadmap, not built now.

So: **Grafana now, self-hosted; OTel-to-SaaS as a deliberate over-engineering/learning chapter later.**
A real comparison that landed on the favourite — not an assumption dressed up as one.

## What "viz as code" actually means here

Grafana stores none of the telemetry; it queries the database on demand and draws the result. The
database stays the single source of truth. The whole point of the build was that the dashboard layer
be *reproducible from the repo*, the same discipline as the rest of the stack:

- A **datasource** definition (which database, which org and bucket, which query language) provisioned
  from a file the tool loads on startup.
- A **dashboard provider** — a small loader that tells the tool to import dashboard definitions from a
  directory.
- The **dashboard itself**, exported as JSON and committed. The UI becomes an editor, not the storage:
  edit visually, export back into the repo, commit. Nothing that matters lives only inside the running
  container.

This distinction — the tool's *own state* (users, edits, alert rules, share links) versus the
*definitions* that come from git — is the thing that makes moving hosts painless later. If the
definitions are all in the repo, standing the whole layer up on a different box is a clone and a start.
The container keeps its own state on a named volume so a restart doesn't wipe UI work, but the repo is
the truth.

## A read-only path, on purpose

The dashboard only ever *reads*. So the token it uses to reach the database is scoped read-only to the
one telemetry bucket — a least-privilege choice that costs nothing now and matters a great deal the
moment any of this is exposed to the public internet (the next session's work). A compromised or
embedded dashboard should never be able to write, or to reach anything but the data it renders.

Access stayed private this session: the dashboard is bound to the box's loopback and reached over an
SSH tunnel, exactly like the database UI. The public surface of the whole stack is still the single
broker port and nothing else. Making it publicly viewable — a reverse proxy with its own TLS, a
scoped public dashboard, and an embed locked to one origin — is its own rung, deliberately not bolted
onto an unproven dashboard.

## The lesson in the word "now"

The first working dashboard had four "current value" tiles, and they behaved in a way that looked like
a bug but was really a lesson in what a query *means*. Long after the test stream had stopped, the
tiles still proudly showed the last values. They were doing exactly what was asked: "give me the last
reading in the visible time window" — and the window was fifteen minutes wide, so a reading three
minutes old was still, by that definition, the latest.

The fix draws the right line between the two kinds of panel. A **history graph** should look back over
the whole window — that's the point of it. A **"now" tile** should look back only a few seconds, so
that when the stream genuinely stops, the tile goes blank instead of lying. Narrowing those tiles to a
short lookback makes "now" mean *now*: they light up the instant real data arrives and blank the
instant it stops. A small change that encodes an honest idea — a dashboard should distinguish "this is
the current state" from "this is the last thing I ever saw."

## Proving it with a ride that never happened

True to the "fake data before real data" discipline, the layer was exercised entirely with simulated
telemetry published over the real TLS path — same broker, same auth, same bridge, same database. First
a simple acceleration ramp, then a full minute modelled as six gear pulls: engine speed sawtoothing up
to redline and dropping on each shift while road speed climbed steadily, so each successive peak landed
at a higher speed. That sawtooth-against-rising-speed is not just a pretty trace — it's precisely the
signature the future gear-from-ratio analyzer will key off, so seeing it render correctly was a preview
of two rungs at once.

Everything drew as expected: the ramps, the shift-drops, the thresholds turning the coolant and battery
tiles their warning colours. With the layer proven, the throwaway test data was cleared out of the
database — surgically, deleting the points while leaving the bucket, its retention, the tokens, and the
dashboard wiring untouched, so tomorrow's first *real* ride starts against a clean, correctly-configured
store rather than a freshly-reinitialised one.

## Where this leaves things

The ladder — `blink → serial → MQTT-with-fake-data → real-data → storage → viz` — is now complete. Every
rung is built and proven. The next session takes the dashboard from private-behind-a-tunnel to
publicly viewable, embedded on a website: the reverse proxy, its TLS, the scoped public dashboard, and
the hardening that has to come with opening any new door. After that, the analysis work the raw channels
were always collected for — deriving gear from the RPM-to-speed ratio, and the shift-point and
throttle-aggression views built on top of it.
