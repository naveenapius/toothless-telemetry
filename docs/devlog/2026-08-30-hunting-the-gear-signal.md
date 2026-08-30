# Hunting the gear signal

*Devlog — 2026-08-30*

The MT-15 shows the current gear right there on the dash. A single digit, 1 through 6,
plus N. It's *right there*. So getting it into the telemetry pipeline should be trivial,
right?

This is the story of why it isn't — and what chasing it taught me about how the bike
actually talks.

## The goal

Four core channels: RPM, throttle, speed, **gear**. The first three turned out to be
easy. Gear became a multi-layer investigation that's still open — and that's exactly why
it's worth writing down.

## Layer 1 — the standard OBD front door

First connection over the ELM327 BLE dongle went better than expected. The bike answered
on CAN: protocol locked at **`A6`** (ISO 15765, 500 kbps, 11-bit). A scan of the standard
OBD-II PIDs (`01 00/20/40/60`) returned ~20 supported channels — RPM (`010C`), throttle
(`0111`), speed (`010D`), coolant, load, the usual suspects.

No gear.

That's not a surprise in hindsight: **there is no standard OBD-II PID for gear on a
manual.** Gear is a manufacturer thing. So the standard front door was never going to have
it. First dead end, cheaply confirmed.

## Layer 2 — "just derive it from the ratios"

The classic trick: within a gear, `RPM = k × speed`, and `k` is fixed by the gearbox. Plot
RPM vs speed and each gear is a straight line with its own slope. Cluster the slopes, label
the gears. Bonus: the messy points between the clean bands *are* the shifts — which is
literally the shift-point metric I want anyway.

I almost talked myself out of it ("I redline through the gears, my shift points are all over
the place") — but that's a misread. Where you shift moves you *along* each gear's line; it
doesn't change the line's slope. Redlining actually gives cleaner, longer lines. So the
ratio method stands as the zero-hardware interim answer, and it kicked off shift-point
analysis for free.

But it's a *re-derivation*. I wanted the real thing.

## Layer 3 — the standstill tell

Here's the clue that reframed everything. Engine on, stationary, clutch in, click into
first — **the dash shows "1."** It can't be doing ratio math; speed is zero, the ratio is
undefined. So the bike must have a **physical gear-position sensor** on the shift drum, and
the ECU is reading a clean discrete value and broadcasting it to the cluster.

Which means the real gear value *exists on the bus*. I just have to reach it.

## Layer 4 — trying to listen to the broadcast

If the ECU broadcasts gear to the dash, I should be able to passively watch the bus and see
it. The ELM327 has a monitor-all mode: `ATMA`. Point it at the bus, shift through the gears,
watch which byte steps 1→6.

I built the monitor. Ran it. Shifted through every gear.

```
TX ATMA
   RX 'NO DATA'
   RX '>'
```

Zero frames. Not a parse bug — literally zero bytes came back.

The lesson landed hard: **the ELM327 is a request/response translator, not a firehose.**
The MT-15's OBD *diagnostic* port is poll-only — it answers when addressed, but the
free-running ECU↔dash broadcast (where gear lives) simply isn't present on the pin the
dongle taps. `ATMA` will never find gear here. The tool worked perfectly; it correctly
reported an empty bus.

*(Open caveat: cheap ELM327 clones sometimes have broken `ATMA`. So "silent port" vs
"broken clone" isn't 100% settled without different hardware. Noted for later.)*

## The head-start hunt

No published MT-15 gear PID exists. Searching for the R15 (shared ECU platform) drowned in
aftermarket-ECU and exhaust-mod noise. But the sibling **MT-07** has an open-source CAN
library, and its source gave up the map:

- **Gear: CAN ID `0x236`, byte 0** — `N=0x00, 1=0x20, 2=0x40 … 6=0xC0`. Clean `0x20` steps.
- Throttle `0x216`, RPM/speed `0x20A`, temps `0x23E`.

And the Yamaha R7 forum independently reports **throttle at `0x216`** — the same ID. Two
sources agreeing suggests Yamaha reuses this scheme across models. That's a strong,
testable hypothesis for the MT-15 (saved in `references/yamaha-can-ids.md`). But note: these
are **broadcast arbitration IDs**, not pollable DIDs — the exact traffic the diagnostic port
doesn't surface.

## Where it stands

Two routes remain, and they split cleanly by what the current hardware can do:

- **Passive broadcast (`ATMA`)** — current dongle has hit its wall. Reaching the broadcast
  bus needs different hardware: a genuine STN-chip dongle (to rule out the broken-clone
  theory), or a real CAN tap (USB-CAN adapter, or the SN65HVD230 + ESP32 on the actual bus
  wires). This is the guaranteed route, but it's parts-gated.
- **Polling mode-22 DIDs** — *not yet tried.* This is request/response, which the dongle does
  well. If Yamaha exposes gear as a readable DID (the dealer tool shows gear, so plausible),
  the current hardware can get it with zero new purchases. The gear sensor reads at
  standstill, so correlation is a 2-minute job on the stand. This is the next test.

The decision rule: run the mode-22 validation first. If DIDs answer, the hardware I already
own might be enough. If they don't, the hardware gap is *proven*, not guessed — and the
CAN-tap chapter begins in earnest.

## Why this is the good part

A signal I can see with my eyes, that took four layers of elimination to even locate:
standard PIDs → ratio derivation → a standstill test that proved a sensor exists →
`NO DATA` that proved the front door is the wrong door. That's the whole point of building
in public — the gear indicator on a ₹1.5L motorcycle turns out to be a small lesson in
diagnostic vs. broadcast buses, addressing modes, and knowing when your tool has hit its
honest limit.

Gear's not solved yet. But I know exactly where it lives now, and exactly what it'll take.
