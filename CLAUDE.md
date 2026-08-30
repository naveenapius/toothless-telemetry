# Toothless Telemetry — Motorcycle Telemetry & Observability Pipeline

## What this is

A telemetry/observability pipeline that pulls live data off a motorcycle, ships it
through a real time-series stack, and visualizes + alerts on it. It is simultaneously:

- **A portfolio centerpiece** for a career pivot into **DevOps / observability / APM**
  engineering. That is the target career; this project exists to demonstrate real
  telemetry/observability skills.
- **A passion project** (the owner rides and makes automotive content). Motorsport is the
  *story* that fuels the build and the public narrative — it is **not** the career bet.

Keep those two threads separate: infra/observability is the profession; the bike is the
passion and the hook. Built in public (LinkedIn + Instagram), iterating toward a strong v1.

## The bike

- **2024 Yamaha MT-15 v2** — BS6.2 / OBD-2B compliant, 155cc single-cylinder, VVA.
- **Diagnostic port:** 6-pin Yamaha coupler under the seat. **Always-hot** (powered even
  with ignition off) — anything drawing from it must be switched/unplugged or it drains the
  battery.
- Almost certainly **CAN (ISO 15765, likely 500 kbps)** given the model year — **confirm on
  first connection** (`ATDPN`).

## Hardware

**On hand / incoming:**
- **ELM327 BLE OBD dongle** — confirmed **BLE, not Bluetooth Classic** (Mac can talk to it
  directly). Captured BLE profile:
  - Service `0xFFF0`
  - **Write** characteristic `0xFFF2`
  - **Notify** characteristic `0xFFF1`
  - Device name **"OBDII"**
- **MODAXE 6-pin BS6 bike-to-OBD2 adapter cable.**
- **ESP32-S3 (USB-C)** — ordered. Chosen for: BLE-capable (works with the BLE dongle),
  USB-C (not micro-USB), and built-in **CAN/TWAI controller** for later direct-bus work.

**Later additions:**
- SN65HVD230 CAN transceiver (direct-CAN build)
- MicroSD module (store-and-forward buffering)
- IMU (MPU-6050 — lean angle, G-force)
- GPS module (position/route)
- Buck converter (permanent/switched power tap)

## Computer situation

- Currently on **macOS** (Apple hardware). Mac handles BLE cleanly (dongle is BLE), but has
  **no native SocketCAN** or Linux CAN tooling.
- Linux laptop + Android phone are on loan and unavailable right now.
- Owner is a **power Linux user** and prefers Linux-native infra work; not blocking.
- **Raspberry Pi / always-on home server** is a likely later addition to host the pipeline.

## Architecture (decided)

Common message shape for every reading, from every source:

```
{ ts, source, metric, value }
```

**Edge (on bike):** ESP32 reads data (from ELM327 over BLE now; from CAN bus directly via
transceiver later), normalizes each reading into the shape above, and publishes over WiFi
via **MQTT**. Powered by a **power bank** during development (no bike wiring yet); the
switched/ignition-line power tap is deferred to the permanent-install stage.

**Pipeline:** `MQTT (Mosquitto) → InfluxDB (time-series) → Grafana (dashboards + alerting)`.
Runs on the Mac now; Pi / home server later.

**Store-and-forward:** ESP32 buffers to SD card when off-network (riding), replays when back
in WiFi range. "Live-from-anywhere" (self-hosted mesh VPN / own domain, **avoiding cloud
subscriptions**) is a later advanced chapter.

**Languages:**
- **Firmware:** C++/Arduino on the ESP32 (better BLE/CAN/MQTT library support than
  MicroPython).
- **Everything else:** Python — **Bleak** for BLE on Mac, the pipeline glue, and analysis.

## Key technical realities

- Yamaha bikes **don't use standard car PIDs cleanly** — they need manufacturer-specific
  PIDs (**mode 21/22**) with model-specific headers/init. A 2024 CAN bike *may* expose some
  standard PIDs directly, so try those first.
- **No published MT-15 PID map exists.** Discover PIDs by **correlation**: rev the engine,
  watch which bytes change; aided by phone-recorded sessions synced to the data log via a
  **"sync blip."**
- The **Yamaha R15 shares the MT-15's ECU platform** → R15 community PIDs are the best
  head-start.
- Reference: **`evrenonur/obd2-elm327-pid-reference`** on GitHub (PID formulas + init
  sequences).
- **CONFIRMED (2026-08-30): the MT-15 OBD diagnostic port is request/response only.**
  Protocol locked at **`A6`** = ISO 15765, 500 kbps, 11-bit. Standard PIDs answer when
  polled, but **`ATMA` (monitor-all) returns `NO DATA`** — there is *no* free-running
  broadcast traffic on the diagnostic connector. Consequence: **broadcast-only signals
  (gear, and likely dash chatter) are unreachable via the ELM327 dongle.** Gear must come
  from either a **mode-22 manufacturer DID** (if Yamaha exposes one — pollable) or a
  **direct CAN tap** (SN65HVD230 + ESP32 on the actual bus wires). Passive `ATMA` monitoring
  of the OBD port is a dead end for gear.

## Data goals

**Core channels** (fast/high-value): RPM, throttle position, speed, gear.
**Slow channels:** coolant temp, battery voltage.
**Later sources** (added as more normalized inputs to the same pipeline): IMU (lean angle,
G-force), GPS (position/route), TPMS (tire pressure).

**Primary derived metrics:**
- **Shift-point analysis** — RPM at each gear change.
- **Throttle aggression** — rate-of-change of throttle, harsh-event counting; **smooth
  before differentiating**.

## Development philosophy

- **Isolate one variable at a time.** Prove each layer before adding the next:
  `blink → serial → MQTT with fake data → real data → storage → viz`.
- **Always capture raw data.** Annotate sessions (phone recording synced via a sync blip);
  analyze at the desk.
- **Build in public** as it goes.
- Deliberate, transparent **over-engineering** (e.g. Kubernetes later) is framed as
  *learning*, not necessity — call it out as such.

## Where we are / next step

**Immediate next step:** pull real data from the bike over **BLE using Python (Bleak) on the
Mac**:
1. Connect to **"OBDII"**.`
2. Subscribe to notify `0xFFF1`, write to `0xFFF2`.
3. Send `ATZ` → `ATDPN` (confirm protocol/CAN) → `0100` → `010C`.
4. Confirm live **RPM** off the idling bike.

**Then:** stand up the pipeline (Mosquitto → InfluxDB → Grafana) and the PID-discovery
process. The **ESP32 comes in afterward** to make acquisition mobile, **reusing the same BLE
UUIDs**.
