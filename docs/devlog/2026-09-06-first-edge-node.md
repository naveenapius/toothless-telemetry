# The first edge node (a heartbeat off the bench)

*Devlog — 2026-09-06*

New hardware arrived — the **ESP32-S3** and a **power bank** — so this session pivoted off the
gear hunt entirely and onto the *pipeline*. The goal for the day was deliberately tiny, per the
"isolate one variable" rule: get the ESP32 to publish a **heartbeat over MQTT** to a broker, and
see it land. No BLE, no bike data, no gear math — just prove the transport that everything else
will ride on.

Short version: it works. The ESP32 now boots, joins WiFi, authenticates to a Mosquitto broker on
the Mac, and publishes a JSON heartbeat every 10 s — **untethered, off the power bank**. Getting
there meant a long detour through an ESP32-S3 USB quirk and a power-bank gotcha, both of which
were educational enough to write down.

## The architecture argument (settled before any code)

Most of the session's *value* was in decisions, not lines. The thread that mattered:

- **The cross-LAN problem, dissolved.** The bike (ESP32 on a phone hotspot) and the pipeline (a
  machine at home) live on different networks behind different NATs — neither can reach *into* the
  other. The instinct is to make the bike reach home; the fix is realizing **MQTT already solves
  this**. The broker is a rendezvous point and *every* client connects **outbound** to it, which
  sails through any NAT. So you never route between LANs — you just need one publicly-addressable
  broker, and both ends dial out. That reframing makes the ESP32 code identical regardless of
  where the broker eventually lives.
- **Broker host: cloud VPS, eventually — but not yet.** For a productionised, reachable-from-the-
  road node, a self-managed Linux VM on cloud IaaS (not managed SaaS — that's the subscription
  trap CLAUDE.md warns against) is the right home: always-on, public IP, and it *is* the DevOps
  skill this project exists to demonstrate. But the cheapest sensible option (Hostinger VPS) came
  in at ~₹10k/yr, which is more than a heartbeat test justifies. So: **do the whole thing on the
  LAN first at ₹0**, and only rent cloud once there's real data worth shipping from the road.
  Nothing is wasted — moving the broker later is a hostname change.
- **Storage/viz, noted and deferred.** InfluxDB will be self-hosted locally (OSS v2, the DB +
  UI + query engine in one). The visualisation layer is **not yet decided** — Grafana is the
  obvious fit (native Influx, alerting, and the tool the target jobs use), but a proper
  comparison against alternatives is a deliberate later step, not a default.

## What got built

Two new trees, both following a **config-as-code + secrets-split** convention that carries to
the VPS unchanged:

- **`infra/mosquitto/`** — the broker as config-as-code. `mosquitto.conf` (LAN listener +
  `allow_anonymous false`), a gitignored `passwd` (hashed creds via `mosquitto_passwd`, never
  committed), and a README documenting how to regenerate the secret. Auth now, so it carries over
  to the VPS; TLS is the only thing left to add before going public.
- **`firmware/toothless-edge/`** — a **PlatformIO** project (chosen over the Arduino IDE:
  `platformio.ini` is the build config, reproducible, version-pinned deps). ESP32-S3-DevKitC-1,
  Arduino framework, PubSubClient. The heartbeat firmware reads WiFi + broker creds from a
  gitignored `config.h` (with a committed `config.h.example`), and self-heals both the WiFi and
  MQTT connections.

## Test 1 — the silent serial (a two-port trap)

First flash: uploaded clean (`Hash of data verified`), but the serial monitor showed only the
**ROM bootloader log**, stopping dead at `entry 0x...`. None of my `Serial.print` output. Chased
it through USB-CDC flags (`ARDUINO_USB_CDC_ON_BOOT`, `ARDUINO_USB_MODE`) with no luck.

The breakthrough was a **hardware observation, not a software one**: the board has **two USB-C
ports**, marked `COM` and `USB`. They're wired differently — `COM` goes through a USB-to-UART
bridge chip (this is where the ROM log and flashing happen); `USB` is the S3's *native* USB. The
`CDC_ON_BOOT=1` flag was routing my app's `Serial` to the **native `USB`** port — the one I
wasn't plugged into. Output was leaving through a door I wasn't standing at.

Before finding that, I'd isolated the failure cleanly with an **LED blink test** — a sketch that
just alternates the onboard RGB LED, no serial. It blinked, which *proved the chip was running
fine* and the problem was purely serial visibility, not a crash. That saved me from debugging a
non-existent firmware bug.

Fix: `-DARDUINO_USB_CDC_ON_BOOT=0`, which routes `Serial` back to hardware UART0 → the `COM`
port I flash and monitor on. One port for everything.

**Contribution:** serial works; and the LED-as-status-channel idea (below) was born from the
blink test.

## Test 2 — `rc=-2`, or, DHCP moved the goalposts

With serial alive, WiFi connected instantly (`IP = 192.168.1.14`) but MQTT failed with `rc=-2` —
a *TCP-level* failure (can't reach the broker at all; not an auth `rc=4/5`). Cause: the Mac's IP
had drifted from `.11` to `.12` under DHCP while `config.h` still pointed at `.11`. Updated the
IP, reflashed, and the heartbeat landed. **Takeaway:** pin the broker host — a DHCP reservation
on the router is the clean fix (and it evaporates entirely once the broker is a stable VPS
hostname).

## Test 3 — the power bank that kept dozing off

Untethered on the power bank, it ran, then died. The bank's **low-current auto-shutoff**: between
heartbeats the ESP32 drops into WiFi modem-sleep, current sags below ~50–100 mA, and the bank
decides nothing's plugged in and cuts power.

Fix, firmware-only (no new hardware, because the buck-converter power tap is chapters away):
`WiFi.setSleep(false)` keeps the radio hot so idle current holds at ~130 mA, plus a bright LED
for extra steady draw. That clears the cutoff threshold. Confirmed the chip is happy at that draw
(nowhere near its 300–500 mA TX peaks), and a 10 000 mAh bank still gives **~40 h** of runtime —
the tradeoff is battery life for reliability, and 40 h covers any ride many times over.

## Bonus — the LED is the console now

Since there's no serial on the bike, the RGB LED became the status channel: **blue** at boot,
**blinking blue** = joining WiFi, **solid red** = connecting MQTT, **red flashes** = MQTT
failed/retrying, **sustained white** = connected + publishing, with a **green flash** on each beat. At a glance,
from across the garage, you can tell exactly where it is — and *which layer* failed if it does.

## Where this leaves things

A **working, untethered edge node**: ESP32-S3 → WiFi → authenticated MQTT → Mosquitto on the Mac,
heartbeat every 10 s, powered off a bank. The `blink → serial → MQTT-with-fake-data → real-data`
ladder is now three rungs up.

**Next:**
1. **Real data** — bolt the BLE-central + ELM327 polling (proven in `ble_logger.py`) onto this
   firmware, so it publishes actual RPM + speed instead of a heartbeat.
2. **The pipeline** — `Mosquitto → InfluxDB → Grafana` (or whatever the viz comparison picks),
   fake data first, then the live reader.
3. **Gear-from-ratio** — the original goal: cluster RPM/speed into gear bands, derive gear, feed
   it back into the pipeline as another metric.
4. **Housekeeping** — a static/reserved IP for the Mac; and, before any of this goes on a public
   VPS, TLS on top of the auth that's already in place.
