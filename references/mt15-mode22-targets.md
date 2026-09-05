# MT-15 mode-22 target list (garage priority order)

> Goal: find **gear** (and ideally a cleaner throttle) as a **pollable** manufacturer
> DID, so current hardware wins without a CAN tap. See `docs/HANDOFF.md` §5–§7.
>
> **Nothing here is a confirmed MT-15 DID** — no published map exists. These are
> *prioritized hypotheses* with built-in validation, so the garage run is targeted
> instead of a blind 2–4 hr `0000–FFFF` sweep. Run them with
> `uv run scripts/gear_hunt.py` (polls this list, flags changing bytes). The list itself
> lives in `M22_TARGETS` in that script — keep the two in sync.

## The reasoning (why these, in this order)

Three independent conventions give us candidates:

1. **UDS standard `F4XX` = OBD PID mirror.** ISO 14229 maps DID `0xF4XX` to OBD-II PID
   `XX`. So `22 F40C` should return the same RPM as `01 0C`. We *already know* RPM,
   throttle, speed, coolant answer via mode 01 (HANDOFF §4) — so **these are validation
   anchors**: if `F40C` matches live RPM, mode 22 works AND the `F4XX` convention holds
   on this ECU. That's the single most informative first test.
2. **Yamaha FI diagnostic-monitor codes (`d01`–`d64`).** Yamaha's own dealer/dash
   diagnostic mode exposes numbered sensor monitors. The R15 (shared ECU platform with
   the MT-15) list includes **`d21` = gear/neutral position sensor**. If the ECU surfaces
   these monitors as DIDs, gear is near `d21`. We don't know the encoding, so we try both
   plausible mappings (decimal-literal `0x0021` and hex-of-21 `0x0015`) plus a small
   sweep around them.
3. **Low-range `00XX` measured values.** Many OEMs put live measured values in the low
   DID range, often mirroring the OBD PID number (`000C` = RPM, `0011` = throttle).

## Yamaha diagnostic-monitor codes (R15 platform → MT-15 head-start)

Source: Yamaha R15 / R-series service-manual "Diagnostic Mode" tables (see Sources).
These are **dash diagnostic-mode item numbers**, not DIDs — the mapping to DIDs is the
hypothesis we're testing.

| Code  | Monitors                          | Known-good cross-check |
|-------|-----------------------------------|------------------------|
| d01   | Throttle position sensor          | mode 01 `0111` works   |
| d02   | Atmospheric pressure (%)          |                        |
| d03   | Intake absolute pressure          |                        |
| d05   | Intake air temp                   |                        |
| d06   | Coolant temp                      | mode 01 `0105` works   |
| d07   | Speed sensor                      | mode 01 `010D` works   |
| d08   | Lean/drop sensor                  |                        |
| d09   | Vehicle (battery) voltage         | `ATRV` works           |
| d20   | Sidestand position                |                        |
| **d21** | **Gearbox position sensor**     | ← the target          |

Note: the R1/R15 label is "gearbox position sensor (neutral)". Some Yamahas report only
a neutral flag here; the MT-15 dash shows a full 1–6+N digit, so a **full discrete gear**
value exists somewhere — either at this monitor or an adjacent one. Poll the neighbours.

## Target DIDs, in run order

### Tier 0 — does mode 22 answer at all? (static, key-on)
| DID     | Hypothesis                        | Expect |
|---------|-----------------------------------|--------|
| `F190`  | VIN (UDS standard ident DID)      | ASCII VIN, or `7F 22 xx` |
| `F1A0`–`F1FF` | Yamaha ident block          | any positive `62…` |

A positive here proves the ECU speaks mode 22 before we chase live data.

### Tier 1 — validation anchors (KOEO/idle; compare to known mode-01 values)
| DID     | If `F4XX` convention holds | Cross-check against |
|---------|----------------------------|---------------------|
| `F40C`  | RPM                        | `01 0C`             |
| `F40D`  | Speed                      | `01 0D`             |
| `F411`  | Throttle                   | `01 11`             |
| `F405`  | Coolant                    | `01 05`             |
| `F404`  | Engine load                | `01 04`             |
| `000C`  | RPM (low-range mirror)     | `01 0C`             |
| `0011`  | Throttle (low-range mirror)| `01 11`             |

**Decision:** if any Tier-1 anchor matches its known value → mode 22 works, note which
convention (`F4XX` vs `00XX`) answered, and gear is very likely reachable the same way.
If **all** silent → jump to the physical-addressing retry (below) before giving up.

### Tier 2 — gear candidates (the actual hunt)
Poll these **on the centre-stand / stationary, clutch in, engine running or KOEO**, and
**click N→1→2→3→4→5→6→N slowly** while `--m22-targets` flags which payload byte steps.
The MT-07 gear convention is `N=0x00, +0x20 per gear` (see `yamaha-can-ids.md`) — watch
for a byte marching in `0x20` steps, or a clean `0,1,2…6`.

| DID     | Hypothesis                                   |
|---------|----------------------------------------------|
| `0015`  | d21 as hex-of-21                             |
| `0021`  | d21 as decimal-literal                       |
| `F415`  | d21 via `F4` range, hex                       |
| `F421`  | d21 via `F4` range, decimal-literal          |
| `0014`–`0018` | small sweep around the hex hypothesis  |
| `0020`–`0024` | small sweep around the decimal hypothesis |

### Tier 3 — fallback: full sweep, but informed
If Tiers 0–2 are all silent on functional addressing, the ECU may need **physical
addressing** (`--m22-header 7E0 --m22-rx 7E8`). Re-run Tier 0–1 with that first. Only
then fall back to the full `--sweep-mode22 --m22-end FFFF` (HANDOFF §6: KOEO + battery
charger, resumable).

## How to run it

```bash
# Targeted, correlation-friendly (this list; flags changing bytes as you click gears):
uv run scripts/gear_hunt.py

# If everything is silent, retry with physical addressing:
uv run scripts/gear_hunt.py --header 7E0 --rx 7E8
```

Every response is captured raw to the session JSONL (always-capture-raw). Decode at the
desk; this run is only about *finding which DID carries gear*.

## Sources
- Yamaha R15 / R-series service manual, "Diagnostic Mode" (d01–d64 monitor table).
- ISO 14229 (UDS) DID ranges: `F1XX` identification, `F4XX` = OBD PID `XX` mirror.
- `references/yamaha-can-ids.md` — MT-07 broadcast gear convention (`0x236`, +0x20/gear).
