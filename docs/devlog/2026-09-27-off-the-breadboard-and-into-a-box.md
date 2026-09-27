# Off the breadboard and into a box

*Devlog — 2026-09-27*

For weeks the edge node has been a spread of dev boards and jumper wires — functionally complete,
physically a hazard. A loose tangle of exposed electronics is fine on a desk and absurd on a motorcycle,
where it would rattle itself apart in a mile and look, to anyone who glanced at it, like something you'd
want to back away from. This session was about turning that tangle into a *device*: everything soldered
down onto a single board, and the board and its battery housed in a box you could actually strap to
something. It's the least glamorous kind of progress and one of the most satisfying, because for the
first time the thing is a self-contained object instead of a lab setup.

## From jumpers to joints

The core work was moving the ESP32 and the GPS module off the breadboard and onto perfboard — real
solder joints in place of friction-fit jumpers. That single change trades one set of problems for
another, and the swap is worth naming. Jumpers fail *loudly and intermittently*: a wire wiggles loose,
something stops working, you push it back in. Solder fails *quietly and permanently*: a joint looks
perfect and isn't, or two things that shouldn't touch do, and nothing you can see tells you which. You
give up the convenience of poking at connections in exchange for a build that survives vibration — which
is exactly the trade you want for something bolted to a bike, but it means the debugging tools change
too.

Then a plastic box became the enclosure — a makeshift casing that holds the board *and* the battery
together as one unit. Nothing fancy, but it crosses an important line: the electronics and their power
source are now a single thing you can pick up, carry, and mount, rather than a pile you have to keep
from touching itself. It's the first rung of the packaging progression the plan has always pointed at —
survive vibration, stop looking alarming, become mountable — and the cheapest possible version of it,
which is the right version for a prototype.

## The two ways soldering bit back

True to the trade described above, the move to solder introduced two failures that a breadboard would
never have produced, and both were instructive.

The first was a phantom. After soldering, the connection to the computer simply died — no serial port,
nothing. The instinct is to suspect the soldering, and a good chunk of time went into hunting for a
short or a cracked joint that wasn't there. The culprit turned out to be a **flaky cable** — the board
was fine all along, powered and running; only the data path to the laptop was intermittently dead. A
useful reminder that "it stopped working right after I changed X" is a hypothesis, not a diagnosis, and
that the dullest possible cause is always worth eliminating first.

The second was real, and it's the classic. The GPS had power and a satellite lock — its own light was
happily blinking — yet the microcontroller was receiving *nothing* from it. Every joint looked clean.
The fault was invisible to the eye because it wasn't a bad joint at all: the two data lines were
**crossed**, transmit wired where receive should be. On a breadboard you'd never make this mistake the
same way, or you'd fix it by moving one wire; soldered down, it's committed. The fix was to stop trusting
the eyes and let the firmware settle it — swap the two lines in software, reflash, and watch the data
suddenly pour in. Confirmation in seconds, and then the board's real wiring was made official in the
firmware so the software matches the object. The lesson that keeps recurring on this project: when a
thing that must work doesn't, don't inspect harder — *test the specific hypothesis*.

There was a small coda to that, too. The status light briefly looked "wrong" — red where green was
expected — until it clicked that the two firmwares in play mean different things by the same colour. The
bring-up sketch treats green as "GPS has a fix"; the real edge firmware treats the light as the whole
pipeline's health, where green demands the *bike* be talking, not just the sky. Same LED, different
story. Worth remembering that a status indicator is only as clear as your memory of which program is
driving it.

## Setting the modem aside — on purpose

One deliberate subtraction: the cellular modem that was going to give the node its own uplink has been
**shelved for now.** It's not a failure so much as a decision to not fight that particular battle in this
pass — the plan is to come back to cellular later, likely with a *different* module rather than the one
first chosen. Cutting it loose keeps this session honest: the goal here was a packaged, self-contained,
GPS-carrying node, not a cellular one, and bolting an unproven radio into the same box would have muddied
both. The uplink stays WiFi for the moment, which is perfectly fine for a proving-ground bike within
reach of a hotspot; the cellular chapter reopens when the right module is in hand and can get its own
isolation rung, the way the GPS just did.

That's a small but real shift in the roadmap. Cellular was drawn as the next big hardware step; it's now
explicitly *paused and to-be-restarted*, and the physical packaging quietly took its place as the thing
that actually advanced.

## Where this leaves things

The edge node is, for the first time, a **thing** — soldered, boxed, battery included, GPS integrated
and confirmed live on the board after the wiring was made honest. It's no longer a demo that only exists
while nobody breathes near the wires; it's an object that could ride along. The cellular uplink is
deferred and will restart fresh with a different module. What's left to make it genuinely track-ready is
unchanged in shape but clearer in order: a sturdier, deliberate enclosure beyond the makeshift box, the
faster GPS sampling that turns the coarse position dot into a smooth line, and — when its module is
chosen — the cellular radio that finally cuts the tether to a phone. For today, the win is simple and
physical: it fits in a box, it runs off its own battery, and it knows where it is.
