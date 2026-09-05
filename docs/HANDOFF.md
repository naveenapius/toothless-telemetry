# Handoff — the telemetry pipeline (edge node → broker, and what's next)

*Last updated: 2026-09-06. For the next agent/session picking this up cold.*

Read `CLAUDE.md` first for full project context. This file is the **current state, the
decisions and why, what's open, and what's next** — the reasoning that isn't obvious from
the code. The **gear-signal investigation is settled and archived** (see §6 + the devlogs);
this project has moved on to **building the live pipeline**.

---

## 0. Current status (2026-09-06) — read this first

**There is a working, untethered edge node.** The ESP32-S3 boots, joins WiFi, authenticates to
a Mosquitto broker on the Mac, and publishes a JSON heartbeat every 10 s — **powered off a power
bank, no Mac tether**. This is the first real rung of the pipeline: the MQTT transport is proven.
Full narrative: `docs/devlog/2026-09-06-first-edge-node.md`.

The build ladder (`blink → serial → MQTT-with-fake-data → real-data → storage → viz`) is now at
**MQTT-with-fake-data**, working. Next is **real data** (RPM/speed) and then the **storage + viz**
tail.

New this session: `firmware/toothless-edge/` (PlatformIO/Arduino edge firmware) and
`infra/mosquitto/` (broker config-as-code). Working branch: **`groundwork`**.

**Key scope change:** **store-and-forward (SD-card buffering) is ABANDONED.** It trades away the
live-telemetry wow factor that is the whole point of the build. The bike publishes live or not at
all. (CLAUDE.md still lists it under architecture + hardware — that's now stale; see §8.)

---

## 1. What this session accomplished

- Settled the **networking architecture** for the whole project (§2) — the reachability model,
  broker hosting, and what's deferred.
- Built the **broker as config-as-code**: `infra/mosquitto/` — authenticated, secrets gitignored,
  reproducible, ready to lift onto a VPS.
- Built the **edge firmware**: `firmware/toothless-edge/` — a PlatformIO project that publishes an
  MQTT heartbeat with an RGB-LED status channel, running untethered off a power bank.
- Debugged two real gotchas to a clean finish: the **ESP32-S3 two-USB-port serial trap** and the
  **power-bank auto-shutoff** (both in §4).
- Wrote the devlog and this handoff.

---

## 2. Architecture decisions (settled — don't re-litigate)

- **Cross-LAN reachability is solved by MQTT's design, not by routing.** The bike (hotspot) and the
  pipeline host (home/VPS) are on different NATs. Don't try to route between them. The **broker is a
  rendezvous point**; every client — publisher and subscriber — connects **outbound** to it, which
  passes through any NAT/CGNAT. So all you need is **one publicly-addressable broker**, and both ends
  dial out. Consequence: the ESP32 code is identical no matter where the broker lives; migrating is a
  hostname change.
- **Broker host: cloud VPS eventually, LAN for now.** The permanent home is a **self-managed Linux VM
  on cloud IaaS** (NOT managed SaaS — that's the subscription trap CLAUDE.md avoids). It's always-on,
  publicly reachable, and *is* the DevOps skill this project demonstrates. Deferred because the cheap
  option priced at ~₹10k/yr, unjustified for a heartbeat. **Everything runs on the LAN at ₹0 until
  there's real data worth shipping from the road.** Indian-payment note: Hostinger/E2E/Utho take UPI;
  most foreign VPS hosts need an international card or PayPal.
- **Storage: InfluxDB, self-hosted locally** (OSS v2 — DB + UI + query engine in one binary). Local
  now, VPS/Pi later, same container.
- **Visualisation: UNDECIDED.** Grafana is the strong favourite (native Influx, alerting, and it's the
  tool the target observability jobs use), but a deliberate comparison vs. alternatives is a pending
  step, not a foregone conclusion. Don't assume Grafana in code until it's chosen.
- **Store-and-forward: DROPPED** (see §0). Live or nothing.
- **Security posture:** auth (username/password) is in place on the broker **now**, so it carries to
  the VPS. **TLS (port 8883) is the one thing to add before the broker is ever exposed publicly** —
  it's deferred, not forgotten. On the LAN, plaintext-with-auth is an accepted temporary posture.

---

## 3. What's built

### 3a. `infra/mosquitto/` — the broker (config-as-code)
- `mosquitto.conf` — LAN listener (`listener 1883 0.0.0.0`), `allow_anonymous false`, relative
  `password_file passwd`. Launch **from this dir** so the relative path resolves:
  `cd infra/mosquitto && mosquitto -c mosquitto.conf -v`.
- `passwd` — **gitignored secret** (hashed creds). Regenerate: `mosquitto_passwd -c passwd toothless`
  (interactive, no plaintext in history), then `chmod 600 passwd`.
- `README.md` — documents setup + the `sub`/`pub` test.
- Convention: **config committed, secret ignored, reproducible via a documented command.** Same
  pattern used for the firmware. This lifts onto the VPS as a Docker volume mount unchanged.

### 3b. `firmware/toothless-edge/` — the edge node (PlatformIO/Arduino)
- **PlatformIO**, chosen over Arduino IDE (`platformio.ini` = reproducible build config, pinned deps).
  Board `esp32-s3-devkitc-1`, framework `arduino`, dep `knolleary/PubSubClient`.
- **Critical build flag:** `-DARDUINO_USB_CDC_ON_BOOT=0` — routes `Serial` to hardware UART0 → the
  **COM** port (see §4). Do NOT flip this back to 1 without understanding the two-port trap.
- `src/main.cpp` — connects WiFi + authenticated MQTT (both self-healing), publishes
  `{"source":"esp32","seq":N,"uptime_s":N}` to `toothless/heartbeat` every 10 s. `WiFi.setSleep(false)`
  keeps the power bank alive (§4).
- `include/config.h` — **gitignored secret** (WiFi + broker creds + Mac IP). `config.h.example` is the
  committed template. Copy example → `config.h` and fill in on a new machine.
- **LED status channel** (no serial on the bike): **blue** boot, **blinking blue** = WiFi connecting,
  **solid red** = MQTT connecting, **red flashes** = MQTT failed/retrying, **sustained white** =
  connected+publishing, **green flash** each beat.

---

## 4. Hard-won gotchas (don't rediscover these)

- **The ESP32-S3 has TWO USB-C ports (`COM` and `USB`) that are wired differently.** `COM` = a
  USB-to-UART bridge chip (flashing + serial console, foolproof, firmware-independent). `USB` = the
  chip's *native* USB (can do JTAG debug + USB-device roles). `ARDUINO_USB_CDC_ON_BOOT=1` routes the
  app's `Serial` to the **native USB** port — so if you're plugged into `COM`, your prints vanish while
  the ROM log (UART0) still shows. **Fix in use:** `CDC_ON_BOOT=0` → serial on the `COM` port you flash
  from. If serial ever goes silent again, this is the first thing to check.
  - Debug tip that worked: an **LED-blink-only sketch** proves "app is running" independently of serial,
    separating a crash from a serial-visibility problem in one flash.
- **Power banks auto-shut-off on low current.** Between beats the ESP32 modem-sleeps, current sags below
  the bank's ~50–100 mA threshold, and it cuts power. **Fix:** `WiFi.setSleep(false)` (radio stays hot,
  ~130 mA steady) + a bright LED for margin. Costs battery (10 000 mAh still lasts ~40 h — plenty), safe
  for the chip. Real bike power (buck converter) makes this moot, but that's far off.
- **DHCP moves the Mac's IP.** MQTT `rc=-2` (TCP can't reach broker, not an auth failure) was the Mac's
  IP drifting `.11→.12` while `config.h` was stale. **Fix:** set a **DHCP reservation** on the router (or
  a manual static IP) so the broker host is stable. Evaporates once the broker is a VPS hostname.
- **MQTT `rc=` codes are your debugging vocabulary:** `-2` = can't reach broker (IP/firewall/not running),
  `4/5` = bad credentials. `WiFiClient` + `PubSubClient` 3-arg `connect(id,user,pass)` is what satisfies
  `allow_anonymous false`.
- **macOS firewall** can block inbound 1883 from other devices even with the broker on `0.0.0.0`. Wasn't
  the culprit this time (the IP was), but check it if a device can't reach a running broker.

---

## 5. Hardware / environment (as of now)

- **In hand:** ESP32-S3-DevKitC-1 (WROOM-1-**N16R8** = 16 MB flash / 8 MB PSRAM; we build with the base
  `esp32-s3-devkitc-1` def = 8 MB/no-PSRAM, which is fine — under-declaring flash is safe), a **power
  bank**, the **ELM327 BLE clone** dongle (name "OBDII", service `FFF0`, write `FFF2`, notify `FFF1`),
  MODAXE 6-pin adapter. On **macOS** (BLE works natively via CoreBluetooth).
- **Not owned:** SN65HVD230 CAN transceiver (for the v2 direct-CAN gear tap — see §6). **microSD module
  is no longer planned** (store-and-forward dropped).
- **PlatformIO CLI** (`pio`) isn't on PATH by default — it's at `~/.platformio/penv/bin/pio` (add to
  PATH, or use the VS Code buttons).

---

## 6. The gear signal — settled, archived

Gear is **not reachable by any poll** on the ELM327 (every route exhausted: standard PIDs, mode 22,
service 21; the OBD port is request/response only, `ATMA` = NO DATA). Full story in the
`2026-08-30` and `2026-09-01` devlogs. **The plan for gear is RATIO-DERIVATION:** within a gear
`RPM = k×speed`, each gear a distinct slope; cluster RPM/speed from a ride into 6 bands → gear +
shift points, zero extra hardware. The analyzer is **not built yet** (§7). Real discrete gear stays a
**v2 direct-CAN tap** (SN65HVD230 + ESP32 TWAI, listen-only, reads the broadcast plane incl. gear
`0x236` per the MT-07 map) — parts-gated, not a blocker.

---

## 7. Next steps (prioritized)

1. **Real data onto the edge node.** Merge the BLE-central + ELM327 polling proven in
   `scripts/ble_logger.py` into `firmware/toothless-edge` (reuse UUIDs `FFF0/FFF2/FFF1`), so it publishes
   **RPM + speed** (fast channels) as `{ts,source,metric,value}` instead of a heartbeat. This is the
   "real-data" rung. Coolant/voltage are slow channels, sample occasionally.
2. **Stand up the pipeline.** `Mosquitto → InfluxDB → Grafana(?)`. Per the philosophy, **fake data into
   Influx first**, prove the DB + a dashboard, then wire the live MQTT reader (a bridge — Telegraf, or a
   small Python subscriber; decide when you get there). **Pick the viz tool** (Grafana comparison is still
   open — §2). This is the core observability/DevOps portfolio work.
3. **Build the gear-from-ratio analyzer** (§6). Desk-side, still unbuilt: feed it a ride's RPM+speed →
   cluster into 6 slope bands → gear map + shift points. Then publish derived **gear** as another metric.
   Keep publishing **raw RPM+speed** and derive gear downstream (recalibrate without reflashing).
4. **Housekeeping:** a **DHCP reservation / static IP** for the Mac (§4); and **TLS on the broker** before
   it ever goes on a public VPS (§2).
5. **VPS migration** — only when you want live-from-the-road. Self-managed Linux + Docker Compose (the
   mosquitto config lifts over unchanged), UPI-friendly Indian host. Not a blocker for local dev.

---

## 8. Repo / git state

- Branch **`groundwork`**, **no git remote** (local only). As of session end, the new work
  (`firmware/toothless-edge/`, `infra/mosquitto/`, `.gitignore` updates, the devlog, this handoff) may be
  **uncommitted** — check `git status`.
- **Gitignored secrets** (verified with `git check-ignore`): `infra/mosquitto/passwd`,
  `firmware/toothless-edge/include/config.h`. Also ignored: `firmware/toothless-edge/.pio` and `.vscode/`.
- **Stale doc to fix:** `CLAUDE.md` still lists **store-and-forward / microSD** under Architecture and
  Hardware — that path is dropped (§0). Update CLAUDE.md (and the `project-toothless-telemetry` memory)
  to reflect: live-only, no SD buffering.

## 9. Pointers
- `CLAUDE.md` — project context (note the stale store-and-forward references, §8).
- `docs/devlog/2026-09-06-first-edge-node.md` — this session: the edge node + architecture decisions.
- `docs/devlog/2026-08-30-*`, `2026-09-01-*` — the gear-signal hunt (archived).
- `infra/mosquitto/README.md` — broker setup + test.
- `firmware/toothless-edge/` — the edge firmware (read `platformio.ini` and `src/main.cpp`).
- `scripts/ble_logger.py` — the proven BLE/ELM327 acquisition to fold into the firmware next.
- `references/` — Yamaha CAN IDs (v2 tap), mode-22 targets (archived).
- Memory (`~/.claude/projects/…/memory/`): `project-toothless-telemetry`, `finding-obd-port-poll-only`,
  `reference-yamaha-can-ids`, `user-profile`.
