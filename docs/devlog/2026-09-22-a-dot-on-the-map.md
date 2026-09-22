# A dot on the map

*Devlog — 2026-09-22*

The bike can now say *where* it is, not just how hard it's working. A GPS module went from a bag of
loose parts to a live moving dot on the dashboard in one session — and, true to form, the interesting
parts were the two places it quietly refused to work and had to be talked into it.

## Prove the sensor alone before it joins the crowd

The project has one stubborn habit that keeps paying off: **never add a new part to the working system
until it has proven itself in isolation.** So the GPS didn't go anywhere near the bike firmware at
first. It got its own throwaway project whose entire job was to read the module and print what it heard
— no WiFi, no Bluetooth, no broker, nothing that could hide a wiring mistake behind a more interesting
failure. If the dot never showed up later, this rung meant the module itself was already ruled in.

That sketch also taught the module's body language through the onboard light: dark-and-silent means the
wiring is wrong, a patient blink means it's listening but hasn't found itself yet, and a steady glow
means it has a lock. Being able to read the state across a room, without a laptop tethered to it, made
the outdoor testing that followed almost pleasant.

## The part where you have to go outside

Two facts about satellite positioning are easy to forget until they cost you time. The first is that a
cold receiver, one that has never had a fix or has been moved a long way, is genuinely slow the first
time — it has to hear each satellite out, at a trickle of data, without interruption, before it can
place itself. The second is that it needs actual sky. Indoors, near a window, it will happily count
satellites it can *see* and still never gather enough clean signal to commit to a location.

So the first real milestone was mundane and completely necessary: take the thing outside, point its
antenna at the sky, and wait. The blink held, the visible-satellite count climbed, and then it settled
— a real latitude and longitude, the module announcing where in the world it was. Only after that did
the soldering iron come out: four header pins onto the module, the one small piece of handwork the
whole build needed, so the connection stops being a prayer over jumper wires and becomes something that
survives a bike.

## Folding it in without lying

Bringing GPS into the main firmware reused a discipline the project already trusts: **the edge node
reports only what its sensors actually observe, and it never pads a record to look complete.** GPS
values ride along inside the same once-a-second bundle as the engine data, but each field is included
only when it's genuinely valid — no fix, no coordinates, rather than a stale or zero position pretending
to be real. A partial fix contributes what it has and stays quiet about the rest. The same honesty the
engine channels already followed now covers location too, which means the record downstream is either
true or absent, never fabricated.

A deliberate consequence fell out of this: the position only travels when the bike is also talking. The
node's identity is *bike sensor first* — a fix with the engine off is not a telemetry event worth
sending — so a stationary module with a perfect lock and nothing else to say stays silent. That is a
feature, and it explains a moment of confusion later on.

Keeping time honest got its own small decision. The obvious temptation was to let the satellites, which
carry an extremely accurate clock, become the system's timekeeper. But the network clock the node
already uses is available the instant it has a connection — indoors, cold, anywhere — whereas satellite
time doesn't exist until there's a fix, and the secure connection to the broker needs a correct clock
*before* any of that. So the existing time source stays in charge; GPS simply contributes position.
When the node eventually moves to a cellular life with no network to lean on, the satellites become the
natural clock — but that's a bridge for the day it's actually standing on.

The status light also grew up. It now tells a fuller story at a glance — nothing connected, then each
link coming up in turn, then the bike answering, and finally whether it's transmitting *with* a GPS fix
or without one — and, importantly, it still says out loud when the buffer has drained and it's safe to
cut power. And because the new module draws a steady, honest amount of current on its own, the old trick
of running the light blindingly bright just to keep a power bank awake could finally be retired; the
light can go back to being a modest indicator instead of a crutch.

## Two ways the map said no

The dashboard put up the same kind of fight the gear visualization did, in two rounds.

The first was the map refusing to draw itself at all, demanding a key for a commercial map provider.
The fix was to stop leaning on a default that expected an account and pin the map explicitly to the
open, free-to-use street map that needs no such thing. A reminder that "the default" is a decision
someone else made, and not always the one you want.

The second was subtler and more instructive: the map appeared, the coordinates were provably in the
database, the speed readout beside it was alive — and yet the map showed the whole world with not a
single point on it. The temptation is always to suspect the data; the data was fine. The culprit was an
over-clever first design that split the plot into two layers — a faint trail and a bright "you are here"
dot — using a filter to route the right points to each. The filter was matching nothing, so *both*
layers came up empty and the map, with nothing to focus on, shrugged and showed the entire planet. The
cure was to stop being clever: one layer, all the points, no filter, and the coordinates wired
explicitly to the map instead of hoped-for by auto-detection. The dot appeared. The fancy version can
come back later, once the plain one is trusted — the same "make it work, then make it nice" order the
gear panels eventually needed.

## Where this leaves things

The pipeline now carries a sixth kind of truth about the bike — not how it's running, but where it's
running — end to end, from a soldered module on the bench to a live position on the dashboard. Ground
speed came along with it, and it's quietly more than a duplicate of the bike's own speedometer: one
measures wheel rotation and the other measures actual movement over the ground, and the gap between them
is where interesting things like wheelspin will eventually show up. For now the extras it can report —
heading, satellite count, fix quality — are being *captured* but left on the shelf; there's no rush to
decide what they become.

The bigger significance is directional. Location was always on the list for the track version, the lap
map that makes on-track telemetry worth watching. Getting it working now, on the daily proving-ground
bike, means the hard part is de-risked well before it has to survive a race weekend. The next real
gains are about *speed of sampling* — a location fix once a second is a coarse, jumpy trail, and the
same faster polling that would clean up the gear signal is what turns this dot into a smooth line
tracing a lap.
