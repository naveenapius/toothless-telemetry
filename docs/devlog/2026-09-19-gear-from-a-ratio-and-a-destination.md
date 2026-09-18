# Gear from a ratio, and the project finds its finish line

*Devlog — 2026-09-19*

Two things happened this session that don't usually happen on the same day: a piece of the system
got *derived from real data* for the first time, and the project quietly discovered what it's actually
for. The first was hands-on analysis; the second was a conversation that kept following its own logic
until the destination was obvious.

## Reading gear out of numbers the bike never sends

The bike does not report what gear it's in — its diagnostic port answers questions but volunteers
nothing, and gear isn't a question it will answer. But gear *is* hiding in plain sight: within any one
gear the engine and the rear wheel are locked by a fixed mechanical ratio, so **engine speed ÷ road
speed is a constant that's different for each gear** — tall in first, short in top. Record a ride that
walks up and back down through the box, and the gears fall out as distinct horizontal bands in that
ratio.

The method was deliberately un-clever: throw away the messy samples (stopped, crawling, mid-shift,
clutch in), let the clean cruising points cluster on their own, and take the middle of each cluster as
that gear's signature. No spec sheet exists for this bike's ratios; they were reverse-engineered
straight from a couple of recorded sessions.

The interesting part was being **wrong first, and letting the data correct it.** An early pass invented
a phantom "first gear" at a ratio far too tall to be real — an artefact of road speed being reported as
whole numbers, which turns to garbage at crawling speed. A physics sanity check killed it (that ratio
would mean first gear tops out at bicycle speeds). Then the rider's own memory of a long, deliberate
first-gear pull anchored the real first gear. Later, a flat assumption that the ride never reached top
gear turned out to be a *merge* of two gears the clustering had lumped together — splitting them
revealed the sixth, and, satisfyingly, the six ratios then stepped down in a smooth, shrinking
progression, exactly the shape a real close-ratio gearbox has. The ladder was cross-checked against
three separate ride windows, including one hard run near the redline, and matched the rider's account
of each. What began as a guess ended as six numbers grounded in evidence.

## Deriving, not faking — and where the derivation lives

A gear reading the bike never transmitted is a *derived* metric, not a measured one, and that
distinction earned a firm architectural line: **the edge node stays a faithful sensor and publishes
only what the bike actually says.** Anything computed — gear today, shift points and throttle
aggression tomorrow — happens *downstream*, in the dashboard's own query language, not baked into
firmware and not written back into the store dressed up as real telemetry. The raw record stays honest;
the interpretation is a presentation-layer concern that can be retuned any time without touching a
single stored point or reflashing anything on the bike.

That gave the dashboard a genuine new capability: the six ratio boundaries live in the query, and gear
is computed on the fly from the engine-speed and road-speed channels already in the store. The
methodology — the ladder, why each gear is a *band* and not a single value, why the boundaries sit at
the geometric means between neighbours, and where it's still weak — got written down as a committed,
outward-facing document, because "here's how I reverse-engineered gear from a ratio" is exactly the
kind of thing this project exists to show.

## Making the picture tell the truth

Getting the *visualization* right took a few honest wrong turns of its own. A first attempt drew gear
as a single band of colour over time, which turned out to be almost meaningless — the colours carried
no information without the vertical context of the ratio they came from. The fix was to plot gear on
its own axis, so a higher gear physically sits higher, read directly beneath the ratio scatter it's
derived from. A separate lesson: asking the database to *average* the raw channels before computing the
ratio quietly manufactured wrong gears at every shift, because a blended engine-and-road speed across a
gear change lands between bands. Computing per raw sample and only *thinning* the points for drawing
fixed it.

The last touch was about absence. A line that simply connects the last reading before the bike was
switched off to the first reading after it comes back is telling a small lie — dragging a value across
a gap where there was no data at all. Teaching the graph to **break the line on a real gap, while still
bridging a brief signal dropout**, makes the picture honest about when the bike was and wasn't talking.
And, pleasingly, that rule cooperates with the buffer already in the design: a dead-zone gap gets
back-filled on reconnect and stays continuous, so only a true power-off ever breaks the line.

## The conversation that found the finish line

Alongside the hands-on work, a long design thread kept pulling toward a conclusion the project hadn't
stated out loud before: **the real destination is on-track telemetry for the bikes amateur racers
actually ride.** The daily bike is the proving ground; the track bike is the target. That single
reframing cascaded into a set of decisions that all reinforced each other:

- **Two identities, not one.** A *bike* is a physical unit; a *model* is a calibration profile. Gear
  ratios and the sensor map belong to the model — calibrate a model once and every bike of that model
  benefits — while the bike id just says which machine a ride came from. The rule for the model id is
  simply "one id, one calibration," which is why two variants that look similar but have different
  engines have to be told apart.
- **The edge node grows up for the track.** No rider carries a phone at speed, so the node needs its
  own cellular uplink — which happens to be exactly the publicly-reachable-broker design already built,
  now finally justified. Location for a lap map comes from a small GPS module riding alongside. And the
  whole thing runs off a switched power tap on the bike, tucked under the bodywork, which turns out to
  double as weather protection because the signal passes straight through plastic.
- **Accessible beats clever.** The strongest single constraint that emerged is that this has to be
  *cheap enough for an amateur to adopt*. That quietly settled a real temptation — a full little Linux
  computer on the bike would have made the software far friendlier — in favour of the cheaper, tougher,
  power-loss-proof microcontroller. The friendly-Linux world is where the *pipeline* already lives; the
  thing bolted to a vibrating bike should be dumb, robust, and inexpensive.
- **Faster sampling is the real gear fix.** The shift-flicker that smoothing only papers over largely
  disappears if the data is sampled several times a second instead of once — fast enough that a shift
  is a handful of points instead of a single ambiguous one, and fast enough that the eventual debounced
  gear signal becomes trivially clean. The limit there isn't the hardware; it's how the bike is polled,
  and there's plenty of room to go faster.

## Where this leaves things

The pipeline is complete and now *interprets*, not just records: gear is read live off the ratio and
drawn honestly. The bigger shift is one of intent — this stopped being "telemetry for my bike" and
became "a low-cost, live, on-track telemetry system for a grid of ordinary race bikes," with a clear
first target and a clear reason every earlier self-hosting decision was right. The next real steps are
physical and can't be planned any further from a desk: get the cellular-plus-GPS node onto a bike, and
run the same reverse-engineering — sensor map, then gear ladder — on the first track machine.
