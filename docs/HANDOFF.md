# Handoff — BLE acquisition & the gear-signal investigation

*Last updated: 2026-08-30. For the next agent/session picking this up cold.*

Read `CLAUDE.md` first for the full project context. This file captures **what was
done, what was decided and why, what's open, and what's next** — the reasoning that
isn't obvious from the code alone.

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

**Two open routes to gear, split by what current hardware can do:**
- **Poll mode-22 DIDs** — request/response, which the current dongle does well. **NOT YET
  TRIED.** If Yamaha exposes gear as a readable DID, current hardware wins with no new
  purchase. Correlation is easy: the sensor reads at standstill, so poll responders while
  clicking through gears on the stand.
- **Direct CAN tap** — read the broadcast bus (`0x236`) with a real CAN reader on the actual
  harness wires. Guaranteed route, but parts-gated (needs a transceiver/adapter).

---

## 6. Important deliberations (don't re-litigate)

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

## 7. Recommended next steps (prioritized)

1. **Run the mode-22 validation** at the bike: `uv run scripts/ble_logger.py --sweep-mode22`
   (then `--m22-header 7E0 --m22-rx 7E8` if silent). This is the one untested route the
   current hardware can win on. Decides "buy hardware" vs. "already had it."
2. **Run the `--monitor` control test on a (non-Suzuki) car** to settle whether the clone's
   `ATMA` works. Gate with `--no-poll` first.
3. **Capture a `--ride` log** and derive gear-from-ratio at the desk (interim gear + starts
   shift-point analysis).
4. **Stand up the pipeline** (Mosquitto → InfluxDB → Grafana) on the five working OBD
   channels, using the `{ts, source, metric, value}` shape. Per project philosophy: start
   with **MQTT publishing fake data** before wiring the live BLE reader. This is the actual
   observability/DevOps portfolio work.
5. **Order** the SN65HVD230 (and/or a CANable 2.0 / OBDLink) so the CAN-tap chapter isn't
   parts-blocked when gear polling is exhausted.

---

## 8. Repo / git state

- Branch **`feat/ble-acquisition`**, one commit (`Add BLE OBD acquisition tooling and
  gear-signal investigation`). **No git remote configured** — local only.
- Tracked: `scripts/ble_logger.py`, `CLAUDE.md`, `pyproject.toml`, `uv.lock`,
  `.python-version`, `references/`, `docs/`, `.gitignore`.
- Gitignored (local only): `logs/` (raw captures), `channels*.txt` (regenerated per run).
  Stale artifacts `scripts/channels.txt` and `channels_canbus.txt` are on disk but ignored;
  safe to `rm`.

## 9. Pointers
- `CLAUDE.md` — project context + Key technical realities (incl. poll-only finding).
- `references/yamaha-can-ids.md` — MT-07 CAN map head-start.
- `docs/devlog/2026-08-30-hunting-the-gear-signal.md` — the narrative.
- Memory (`~/.claude/projects/…/memory/`): `finding-obd-port-poll-only`,
  `reference-yamaha-can-ids`, `project-toothless-telemetry`, `user-profile`.
