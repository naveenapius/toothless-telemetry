# Exhausting the poll routes (and choosing the ratio)

*Devlog — 2026-09-01*

Last session ended on a promise: the passive broadcast route (`ATMA`) had hit its wall, but
**polling a manufacturer DID** was untried, and the dongle does request/response well. If
Yamaha exposes gear as a readable identifier, the hardware I already own wins with no new
purchases. This session was the whole of that test — every poll route, run to the end.

Short version: gear is not pollable. But I didn't just *conclude* that this time — I *proved*
it, one addressing mode at a time, and learned exactly how the bike is wired in the process.
And it ended with a clean MVP decision. Good session.

## Test 1 — mode 22, the educated guesses

Mode 22 is `ReadDataByIdentifier`: `22 <2-byte DID>` → `62 <DID> <data>`. No published MT-15
map exists, so I built a *prioritized* target list instead of a blind 65,536-DID crawl. Two
anchors seeded it:

- **UDS standardises `F4xx` as a mirror of OBD PID `xx`** — so `22 F40C` should echo RPM
  (`01 0C`). Since RPM already works, that's a self-checking probe.
- **Yamaha's dashboard diagnostic monitors (`d01`–`d64`, with `d21` = gear)** gave candidate
  DIDs (`0x15`/`0x21` and `F4xx` variants).

Ran it on the stand, shifted through the gears. Result:

```
22F190 -> 62F190 4D4531...   (VIN: "ME1…" — real data!)
22F421 -> 62F421 0000        (positive, but constant)
22F40C -> 7F 22 12           (requestOutOfRange)
```

**Two findings, one good one deflating.** Good: **mode 22 works** — the VIN came back, so the
dongle *can* read manufacturer DIDs. Deflating: the only gear-ish hit, `F421`, sat at `0000`
while I shifted. Decoding it settled the `F4xx` question — PID `0x21` is "distance travelled
with MIL on" = `0000` (no fault light). So `F4xx` is just the standard-PID mirror block. Gear
isn't there. Every `d21`-derived guess came back `requestOutOfRange`.

Contribution: proved mode 22 is alive, killed the `F4xx` hypothesis, exhausted the guesses.

## Test 2 — the survey, and a lesson in sampling

If guessing failed, map it. But 65,536 DIDs at ~120 ms each is ~2.5 hours. So I wrote a
**coarse survey**: sample the space at a stride, find which 256-DID *pages* are alive, then
densely sweep only those. Stride 32 → 2,048 probes, 8 samples per page, ~4 minutes.

It reported exactly one live page: `F1` (identification).

And then it lied to me — usefully. I *knew* `F421` answered (Test 1), yet the survey flagged
page `F4` as dead. Why? Stride 32 samples `F400, F420, F440…` — and `F421` sits *between*
`F420` and `F440`. The survey stepped right over the one responder I already had.

**Lesson: this ECU populates DIDs sparsely, and sparse responders defeat striding.** "Only F1
is alive" really meant "only F1 is dense enough to survive an 8-samples-per-page sieve." A
good reminder that a negative from a sampling method is only as strong as the sampling.

## Test 3 — discover the F1 page, and meet the second ECU

So I densely swept the one page the survey trusted. Thirteen DIDs answered — and every one
decoded as **identification**: VIN, supplier = **Denso**, hardware/software versions, spare
part numbers. All static, forever-constant. No sensor data, no gear.

But the raw responses hid the real discovery:

```
22F1A1 -> 62F1A1 0034D8F4   \r   62F1A1 002BB9C0
```

`62F1A1` appears **twice**, with different data. That's not a parse bug — it's **two ECUs both
answering the same broadcast request.** One of them even had a value incrementing ~1/second (an
uptime counter). I'd been reading the *sum* of two modules talking over each other this whole
time, and my byte-diff was interpreting the collisions as "changing" data.

Contribution: F1 page is identification-only (no gear), and — bigger — the bus has **more than
one ECU**, and functional (broadcast) addressing scrambles them together.

## Test 4 — enumerate the ECUs

Turn headers on (`ATH1`), fire a broadcast request, read which CAN IDs reply. Two:

- **`7E8`** (request header `7E0`) — the **ECM**. It answers OBD `0100`.
- **`7EB`** (request `7E3`) — the **ABS ECU** (the bike runs dual-channel ABS).

Two diagnostic ECUs isn't strange — it's the minimum for an EFI bike with ABS. Each is an
independent, separately-serviceable controller; ISO 15765-4 hands out addresses (`7E0–7E7` →
`7E8–7EF`) precisely so they coexist on one connector. Cars have 5–10+; a bike having 2 is
lean. But it explains every jumbled capture so far, and it means I have to **physically address
one ECU at a time** to get clean data.

## Test 5 — service 21, the legacy door

A hunch worth chasing: Yamaha's `d01`–`d64` monitors are *live sensor* readouts, and on older
diagnostic stacks those are read via **service `21` (ReadDataByLocalIdentifier)** — a 1-byte
address space, `21 XX` → `61 XX <data>`. Modern ECUs often drop it, but it costs 256 requests
(~30 s) to find out.

It answered. Six local IDs replied — and `21 01` / `21 09` returned **40-byte blocks**: bulk
"read all sensors" packets. Exactly the shape live monitor data takes. Gear, if pollable, is a
byte *inside* one of these. So this was the most promising lead of the day.

## Test 6 — the multi-frame mess, and the fix

First watch of those blocks while shifting: pure noise. Byte[1] took 44 near-random values —
40 sensors don't all change at once. Two problems, stacked:

1. **Multiple ECUs** still colliding (one positive block + the other's `7F 21 12` rejection,
   interleaved).
2. The blocks are **multi-frame ISO-TP**, printed by the ELM in a segmented `0:… 1:… 2:…`
   format that I was mashing into one hex string — so byte offsets shifted every poll depending
   on how the frames interleaved.

The raw made it obvious:

```
2101: 023  0:6101061C0004  1:7F2112  2:1E00BC22010000  3:11001D6B597000 ...
```

The fix was twofold: **physically address one ECU** (`ATSH`/`ATCRA`) to kill the collisions,
and **reassemble the ELM frame lines** (stitch the `N:` segments, drop the interleaved
negative) into one stable block. Suddenly all 41 samples reassembled to a clean, uniform
39 bytes. Now byte-diffing meant something.

## Test 7 — the clean runs, and the verdict

With reassembly working and one ECU at a time, I watched both modules **with the engine
running and the dash gear digit confirmed moving** — the ground truth I'd been missing on
earlier runs.

- **ECM (`7E8`)** — blocks `01/03/09/20/F1`. Every varying byte was analog jitter (contiguous
  drift like `BC…C2`). No byte stepped-and-held through the gears.
- **ABS (`7EB`)** — block `0B`. Same story.
- Full `00–FF` physical scan of both ECUs confirmed the complete LID set: ECM `{01,03,09,20,F1}`,
  ABS `{0B}`. Nothing else answered.

The tell for gear would be a byte that **dwells, then steps** — `00→20→40…` or `00→01→02…` — as
I hold each gear. Across every block on both ECUs, with the dash provably changing, **not one
byte did that.** Service 21 is cleanly, exhaustively gear-free.

## What all of it added up to

Every poll route, ticked off:

| Route | Result |
|---|---|
| Standard OBD PIDs | no gear PID (there isn't one for a manual) |
| Mode 22, both ECUs | identification + PID-mirrors only |
| Service 21, both ECUs, dash moving | no gear byte — **cleanly exhausted** |

(One honest asterisk: a *clean, physically-addressed* mode-22 full discover per ECU is the one
box left un-ticked — the mode-22 work was jumbled + the unreliable survey. But this ECU's live
data lives in service-21 blocks and mode 22 returned only ident/mirrors, so gear-as-mode-22-DID
is unlikely. Left as the last poll route if I ever want it airtight.)

## Why the bike is built this way

The negative finally *made sense* rather than just being a wall. A vehicle bus carries two
planes: a **broadcast/operational plane** (ECUs firing fixed-ID frames hundreds of times a
second — where gear lives, per the MT-07 `0x236` map) and a **diagnostic plane** (addressed
request/response, what the dongle speaks). The OBD port is wired to the diagnostic plane only —
which is exactly what last session's `ATMA = NO DATA` was telling me.

And the dash gets gear, fuel, odo, trip *without* the diagnostic plane: direct sensor wiring
(fuel float), broadcast subscription (gear from the ECM), and its own stored memory (the odo
lives in the cluster's EEPROM). The cluster isn't even a diagnostically-addressable node — it
listens and displays. So gear was never going to be a poll response. It's operational-plane
data, and the only way to read that plane is to tap the actual bus wires.

## The MVP decision — take the ratio

Here's the part that turns a dead end into a shipped product. Reaching the broadcast plane means
the **CAN tap**: SN65HVD230 transceiver + the ESP32's TWAI controller, spliced onto the CAN-H/L
pair behind the dash, listen-only. It's the *right* long-term move — it hands you gear **plus**
throttle, RPM, speed, temps, likely fuel/odo, all at higher rate than polling. But it's an
electronics chapter I don't yet have the depth to do quickly, and my goal is a **public MVP
first**.

So: **gear from the ratio.** Within a gear, `RPM = k × speed`; each gear is a distinct slope.
RPM and speed both poll cleanly today over the dongle — zero new hardware. Capture a ride, let
the `RPM/speed` values cluster into six bands at the desk, and gear falls out. It doubles as the
shift-point metric (a shift *is* a slope change), which I wanted anyway.

The blind spots are real and I'm accepting them knowingly: no gear at standstill or clutch-in,
and noise at crawling speed. For a telemetry dashboard, those are cosmetic. And there's a bonus
I like: ratio-gear is **derived independently of the sensor**, so when the CAN tap does land in
v2, the ratio becomes a permanent **cross-check** — two independent gear signals, alert on
divergence. That's an observability pattern, not a compromise.

The trade: I give up always-valid standstill gear *now*, in exchange for a working end-to-end
pipeline *now* — ESP32 (BLE central → ELM327, reusing the same UUIDs) → normalize to
`{ts, source, metric, value}` → MQTT → InfluxDB → Grafana. The CAN tap becomes the documented
v2 upgrade, not a blocker.

## Why this is the good part

I started the day hoping the dongle I own could pull gear, and I ended it *knowing* it can't —
not from a vague wall, but from having addressed each ECU, reassembled each frame, and watched
each byte with the gear digit moving in front of me. Standard PIDs → mode 22 → a survey that
taught me about sparse sampling → the two-ECU discovery → service 21 → ISO-TP reassembly →
clean per-ECU watches. Every layer eliminated something and taught the bike's actual
architecture: diagnostic vs broadcast planes, addressing modes, where a cluster really gets its
numbers.

Gear still isn't coming off a sensor. But the MVP doesn't need it to — the ratio gets me a
working gear channel and shift points today, and the hard-won map of this bus makes the v2
CAN-tap chapter a matter of wiring, not mystery.

Next: order the ESP32, capture a real ride, cluster the ratios.
