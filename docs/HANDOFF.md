# Handoff — BLE acquisition & the gear-signal investigation

*Last updated: 2026-09-01. For the next agent/session picking this up cold.*

Read `CLAUDE.md` first for the full project context. This file captures **what was
done, what was decided and why, what's open, and what's next** — the reasoning that
isn't obvious from the code alone.

---

## 0. Current status (2026-09-01) — read this first

**The gear hunt is settled *as far as polling goes*: gear is NOT reachable via the ELM327,
and the MVP will use RPM/speed ratio-derivation instead.** Full narrative in
`docs/devlog/2026-09-01-exhausting-the-poll-routes.md`. The short version:

- **Every poll route is exhausted.** Standard PIDs (no gear PID), **mode 22** (identification
  + PID-mirrors only), and **service 21** (ReadDataByLocalIdentifier — cleanly scanned on both
  ECUs, dash confirmed moving, no gear byte). Details in §5.
- **Two diagnostic ECUs on the bus:** `7E8` = **ECM** (request header `7E0`), `7EB` = **ABS**
  (request `7E3`; bike runs dual-channel ABS). Functional requests get answered by both and
  collide — you must **physically address one at a time** (`--header/--rx`). This was the key
  unlock for clean captures.
- **Why gear isn't pollable:** it lives on the **broadcast/operational plane** (per the MT-07
  `0x236` map), which the diagnostic port doesn't surface (last session's `ATMA = NO DATA`).
  The dash gets gear/fuel/odo via broadcast + direct wiring + its own cluster memory — none of
  it the diagnostic plane.
- **MVP DECISION (deliberate trade-off):** ship **ESP32 + ELM327 + ratio-derived gear** now;
  defer the **CAN tap** (SN65HVD230 + ESP32 TWAI, listen-only) to **v2**. Ratio-gear needs zero
  new hardware, doubles as shift-point analysis, and becomes a permanent sensor-independent
  cross-check when the tap lands. Blind spots accepted: no gear at standstill/clutch-in, noisy
  at crawl. See §7.
- **One un-ticked box (low priority):** a *clean, physically-addressed* mode-22 full discover
  per ECU was never run (mode-22 work was jumbled + the survey proved unreliable). Gear-as-mode-22
  is unlikely given the evidence, but this is the last route if a fully airtight "polling is
  dead" is ever wanted.

New tooling this session: **`scripts/gear_hunt.py`** (see §3). New reference:
`references/mt15-mode22-targets.md`. Working branch: **`groundwork`**.

---

## 1. What this session accomplished

- Stood up the Python project: **uv**-managed, pinned to **Python 3.12** (`.python-version`,
  `pyproject.toml`, `uv.lock`). Only runtime dep is **bleak**. Run things with `uv run …`.
- Built `scripts/ble_logger.py` — a Bleak-based ELM327-BLE tool for the MT-15 with many
  modes (see §3).
- Did a garage session on the bike and established several hard findings (see §4).
- Hunted the **gear** signal through multiple layers; it's **not yet solved** but its
  location is now well understood (see §5).
- Found a Yamaha CAN head-start (MT-07) and saved it to `references/yamaha-can-ids.md`.
- Wrote a narrative devlog: `docs/devlog/2026-08-30-hunting-the-gear-signal.md`.
- Committed everything to branch **`feat/ble-acquisition`** (repo is local-only, no remote).

---

## 2. Hardware / environment (as of now)

- **On hand:** ELM327 **BLE clone** dongle (name "OBDII", service `0xFFF0`, write `0xFFF2`,
  notify `0xFFF1`), MODAXE 6-pin adapter, on **macOS** (BLE works natively via CoreBluetooth).
- **Ordered, not arrived:** ESP32-S3 (USB-C).
- **Not yet owned:** SN65HVD230 CAN transceiver, and everything downstream (SD, IMU, GPS).
- Linux laptop + Android phone are on loan out (unavailable).

**Operational gotchas learned the hard way:**
- **One BLE central at a time.** Disconnect the phone/any OBD app from the dongle or the
  Mac can't connect.
- macOS prompts for Bluetooth permission for the terminal on first run — allow it.
- The dongle uses **write-without-response** (`response=False`); `0xFFF1` is *also* writable
  (fallback via `--write-char fff1` if `0xFFF2` ever goes silent).
- The OBD port is **always-hot** — unplug the dongle when done or it drains the battery.

---

## 3. The tool: `scripts/ble_logger.py`

Every run writes a raw JSONL session log to `logs/session-<UTC>.jsonl` (every notification
chunk captured as hex — "always capture raw"). `logs/` and `channels*.txt` are gitignored.

> **The mode-22 gear hunt lives in its own script**, `scripts/gear_hunt.py` (reuses
> this file's BLE transport). It polls the curated DID list and flags payloads that
> change as you shift — run `uv run scripts/gear_hunt.py` at the bike. See §7 step 1.

Modes (all via `uv run scripts/ble_logger.py …`):

| Flag | What it does |
|---|---|
| *(default)* | connect → GATT dump → ELM init → supported-PID scan → probe → poll loop |
| `--rpm-only` | skip discovery, stream only RPM (`010C`) |
| `--pids 010C,010D` | poll an arbitrary PID list |
| `--scan-channels` | discover supported PIDs, write `channels.txt`, exit |
| `--sniff` | query each supported PID live → value / needs decoding / silent → `channels.txt` |
| `--sweep-mode22` | brute-force manufacturer mode-22 DIDs (resumable); default range `0000–00FF` (validation), `--m22-end FFFF` for full 65536; `--m22-header 7E0 --m22-rx 7E8` for physical addressing |
| `--monitor` | ATMA passive CAN monitor → streams frames to `logs/canbus-<UTC>.csv`, no runtime analysis, runs until Ctrl+C |
| `--ride` | poll RPM+speed → `logs/ride-<UTC>.csv` until Ctrl+C (for gear-from-ratio) |

Design decisions worth knowing:
- **Monitor and ride write line-buffered CSVs** and do **no runtime analysis** — a deliberate
  choice so a mid-run kill loses nothing; we analyze at the desk.
- Monitor **locks the protocol via a real `0100` first** (a forced `ATSP` can silently
  monitor the wrong bus).
- Bitmask decode (`decode_supported`) is offset per bank — an earlier bug mislabeled the
  `0120`+ banks and stopped the scan early; fixed.
- **Transport fix (2026-09-01):** `command()` now drains stragglers on timeout — a timed-out
  command's slow reply used to bleed into the next command and desync every response by one.

### 3b. The gear-hunt tool: `scripts/gear_hunt.py`

Standalone script (reuses `ble_logger.py`'s BLE transport) built this session to run the
mode-22 / service-21 gear hunt. **All modes auto-lock the protocol first** (a real `0100`,
avoids the `SEARCHING` desync). Every mode respects `--header <req> --rx <id>` for physical
ECU addressing — **essential**, since two ECUs answer functional requests and collide.

| Flag | What it does |
|---|---|
| *(default)* | poll the curated `M22_TARGETS` DID guesses (`references/mt15-mode22-targets.md`), flag changing payloads |
| `--enumerate` | headers-on: list every responding ECU (CAN ID) + its `--header/--rx`. Found `7E8`=ECM, `7EB`=ABS |
| `--survey` | coarse mode-22 stride scan → live 256-DID pages. **Caveat: sparse responders slip between samples** (it missed `F421`); unreliable here |
| `--discover [--pages 00,F1] [--resume]` | dense mode-22 sweep → responders + payloads to `did-map-*.csv` (do IN NEUTRAL) |
| `--watch <did-map.csv>` | re-poll discovered DIDs, flag any differing from the neutral baseline while shifting |
| `--service21 [--s21-end 40]` | scan service-21 local IDs (`21 XX`) for responders → `s21-scan-*.csv` |
| `--s21-watch <scan.csv>` / `--lids 01,09` | poll service-21 blocks, **byte-level** diff while shifting (gear would be a byte that steps-and-holds) |

- **ISO-TP reassembly (`reassemble_isotp`)** stitches the ELM's segmented `0:.. 1:..` multi-frame
  output and drops interleaved second-ECU negatives — without it the long service-21 blocks
  mis-align every poll and byte-diffing is noise.
- `M22_TARGETS` (in the script) and the reasoning table in `references/mt15-mode22-targets.md`
  should be kept in sync.

---

## 4. Hard findings (confirmed on the bike)

- **Protocol = `A6`** (ISO 15765, 500 kbps, 11-bit CAN). Standard PIDs answer when polled.
- **~20 standard PIDs supported**, of which these return useful live data: **RPM (`010C`),
  throttle (`0111`), speed (`010D`), coolant (`0105`), engine load (`0104`)**, plus battery
  via `ATRV`. That's the working channel set for the pipeline.
- **The OBD diagnostic port is REQUEST/RESPONSE ONLY.** `ATMA` (monitor-all) returned
  **`NO DATA`** — zero frames. There is no free-running broadcast on the diagnostic
  connector. (Documented in `CLAUDE.md` and memory.)
- **There is no standard OBD PID for gear** on a manual — confirmed absent from the scan.

---

## 5. The gear investigation — state & reasoning

Gear is displayed on the dash but has been hard to acquire. The chain of reasoning:

1. **Not a standard PID** (confirmed by scan). Standard OBD has no manual-gear PID.
2. **Ratio derivation works** — within a gear `RPM = k×speed`; each gear is a distinct
   slope. Redlining does NOT break this (it lengthens the line, doesn't change the slope).
   This is the zero-hardware interim answer and doubles as shift-point analysis. **The
   `--ride` tool exists to capture the data for this; not yet run on a real ride.**
3. **Standstill test:** dash shows gear while stationary, clutch in → the bike has a
   **physical gear-position sensor** (not ratio-computed). So a clean discrete gear value
   exists in the ECU and is broadcast to the dash.
4. **That broadcast is NOT on the OBD port** (`ATMA` = `NO DATA`). So passive monitoring via
   the dongle can't reach gear.
5. **MT-07 head-start** (`references/yamaha-can-ids.md`): sibling Yamaha broadcasts
   **gear on CAN ID `0x236`, byte 0** (`N=0x00, 1=0x20 … 6=0xC0`). Throttle `0x216`
   (independently confirmed by the R7 forum), RPM/speed `0x20A`. **Caveat: MT-07 ≠ MT-15** —
   treat as first hypotheses, not confirmed.

**RESOLVED 2026-09-01 — polling is exhausted; gear is broadcast-only.** Ran every poll route
with `gear_hunt.py`, physically addressed per ECU, dash confirmed moving:
- **Mode 22:** works (VIN via `F190`), but returns only **identification** DIDs (`F1xx`) and
  **OBD-PID mirrors** (`F4xx` — `F421` decoded as distance-with-MIL = `0000`, not gear). No gear.
- **Service 21** (ReadDataByLocalIdentifier): live sensor blocks exist — ECM answers LIDs
  `{01,03,09,20,F1}`, ABS answers `{0B}`. Full `00–FF` physical scan of both. With the dash gear
  digit **confirmed changing**, **no byte steps-and-holds with the gear.** Cleanly gear-free.
- **Conclusion:** gear lives on the broadcast/operational plane (the MT-07 `0x236` frame), which
  the diagnostic port doesn't surface. It is **not reachable by any poll** on the ELM327.
- **Remaining (low-priority) gap:** a clean physical mode-22 *full discover* per ECU was never run
  (mode-22 work was jumbled + the survey unreliable). Unlikely to hold gear; left as the last box.

**Routes to real gear (deferred to v2):**
- **Direct CAN tap** — SN65HVD230 transceiver + ESP32 TWAI, listen-only, on the CAN-H/L pair
  behind the dash. Reads the broadcast plane: gear (`0x236`) **plus** throttle/RPM/speed/temps,
  likely fuel/odo. The right long-term route; parts-gated and an electronics chapter.
- **Analog sensor-wire tap** — the gear-position sensor is a rotary pot on the shift drum with a
  distinct voltage per gear; tap its signal wire into an ADC (+ divider). Gear-only, simple, but
  splices the engine harness near the sprocket (hot/dirty). *Verify sensor type + wire from the
  R15/MT-15 service manual — don't trust forum wire colors.*

**MVP path taken instead (see §7): ratio-derived gear from RPM÷speed — zero new hardware.**

---

## 6. Important deliberations (don't re-litigate)

> **Status note (2026-09-01):** the mode-22/service-21 empirical tests below are now DONE (§5) —
> gear is not pollable. The clone's request/response clearly works (VIN + live blocks came back),
> so the "broken clone" worry is largely moot for polling; the `ATMA` control test only matters
> if you later want to *passively monitor* rather than tap. The hardware options list is still
> current (now v2). Kept for the reasoning trail.

- **"Dealer tool shows gear ⟹ it's a pollable DID" is WEAK evidence.** A diagnostic tool
  could be catching a broadcast, not polling. It only supports "pollable" *when combined
  with* the poll-only finding (no broadcast on the port to catch), and that finding has a
  caveat (see next). A value can also be **both** broadcast and pollable. Only an empirical
  mode-22 test settles it.
- **The `ATMA` = NO DATA could be a broken clone, not (only) the bike.** Cheap ELM327 clones
  sometimes botch monitor mode. So "port is poll-only" isn't 100% airtight.
  - **Proposed control experiment (not yet done):** run `--monitor` on a **car known to
    broadcast on OBD**. A **flood of frames on any car = hardware proven** (⟹ bike really is
    poll-only). A single silent car is **inconclusive** (that car may also gateway its OBD
    port). **Gate any control car by confirming `0100` works first** (`--no-poll`); if basic
    PIDs fail, the test is void.
  - **Maruti/Suzuki is a poor control** — Suzuki OBD is notoriously quirky (proprietary
    protocols; some Swifts don't work with generic clones at all), so a null result there
    proves nothing. Prefer **VW/Škoda or Hyundai** (more reliably ELM327-friendly and more
    likely to broadcast). Team-BHP "OBD for Indian Cars" thread has model-by-model notes.
- **Mode-22 full sweep is ~2–4 hours** over BLE (65536 DIDs). Don't guess — the 256-DID
  validation measures the real per-request time AND confirms mode 22 answers at all. If
  sweeping fully: do it **key-on/engine-OFF with a battery charger** (gear sensor reads
  KOEO; avoids hours of idling), and it's resumable (`--m22-resume`).
- **Hardware options if current gear routes fail** (in order of simplicity): (1) a genuine
  **STN-chip dongle (OBDLink SX/MX+)** — same port, no wiring, reliable `ATMA`, rules out the
  broken-clone theory; (2) a **USB-CAN adapter (CANable 2.0)** + SavvyCAN / `python-can` —
  no soldering/firmware, reuses Python, connect to OBD pins 6/14 or splice the harness;
  (3) the bare **SN65HVD230 + ESP32** (most DIY, but embeddable for the mobile edge later).

---

## 7. Recommended next steps (prioritized) — MVP path

The gear poll-hunt is closed (§5). Focus shifts to shipping the MVP: **ESP32 + ELM327 +
ratio-derived gear → MQTT → InfluxDB → Grafana**, built in public.

1. **Get the ESP32 recording ride data.** The ESP32-S3 makes acquisition mobile (untethered
   from the Mac). Firmware = BLE central → ELM327, reusing the same UUIDs (service `FFF0`,
   write `FFF2`, notify `FFF1`), C++/Arduino. **Prioritise polling RPM+speed fast** (for ratio
   / shift points); coolant/voltage are slow channels, sample occasionally. *(For a tethered
   first test before the board arrives, `ble_logger.py --ride` on the Mac already works.)*
2. **Capture a calibration ride, build the gear-from-ratio analyzer.** Log RPM+speed; at the
   desk, cluster `RPM/speed` into 6 bands (steady-state cruise = tightest clusters), sort →
   gear numbers, shift points fall out for free. Optional: film the **dash gear digit** for
   ground-truth labels to validate the mapping. **The analyzer is NOT built yet** — it's a
   clean desk-side task (feed it a `--ride` CSV → bands + shift points).
3. **Stand up the pipeline** (Mosquitto → InfluxDB → Grafana) on the working channels — RPM,
   throttle, speed, coolant, load, voltage, + derived gear/shift-points/throttle-aggression —
   using `{ts, source, metric, value}`. Per philosophy: **MQTT publishing fake data first**,
   then wire the live reader. This is the core observability/DevOps portfolio work. Keep
   publishing **raw RPM+speed** and derive gear downstream (recalibrate without reflashing).
4. **(v2, deferred) The CAN tap.** Order the SN65HVD230 when ready to start the broadcast-plane
   chapter — gives real gear + the rest of the operational plane, and makes ratio-gear a
   permanent sensor-independent cross-check. Not a blocker for the MVP.
5. **(optional, low-priority) Airtight the poll finding:** a clean physical mode-22 full
   discover per ECU (`--discover --header 7E0 --rx 7E8`, then `7E3/7EB`), KOEO + charger,
   resumable. Only if you want "polling is dead" 100% proven; evidence says it won't find gear.

---

## 8. Repo / git state

- Working branch **`groundwork`** (earlier work was on `feat/ble-acquisition`). **No git remote
  configured** — local only. Today's changes (`gear_hunt.py`, the mode-22 reference, devlog,
  this handoff, `ble_logger.py` transport fix) were **not yet committed** as of session end.
- Tracked: `scripts/ble_logger.py`, `scripts/gear_hunt.py`, `CLAUDE.md`, `pyproject.toml`,
  `uv.lock`, `.python-version`, `references/`, `docs/`, `.gitignore`.
- Gitignored (local only): `logs/` (raw captures — includes this session's `s21-scan-*`,
  `s21-watch-*`, `did-map-*`, `gear-hunt-*` CSVs), `channels*.txt` (regenerated per run).

## 9. Pointers
- `CLAUDE.md` — project context + Key technical realities (incl. poll-only finding).
- `references/yamaha-can-ids.md` — MT-07 CAN map head-start (broadcast IDs, `0x236` gear).
- `references/mt15-mode22-targets.md` — prioritized mode-22 DID gear-hunt list + reasoning.
- `docs/devlog/2026-08-30-hunting-the-gear-signal.md` — the gear hunt, part 1.
- `docs/devlog/2026-09-01-exhausting-the-poll-routes.md` — part 2: all poll routes exhausted,
  the two-ECU discovery, and the MVP ratio decision.
- Memory (`~/.claude/projects/…/memory/`): `finding-obd-port-poll-only` (updated with the
  service-21/mode-22 exhaustion), `reference-yamaha-can-ids`, `project-toothless-telemetry`,
  `user-profile`.
