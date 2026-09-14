# Toothless Telemetry

**A real-time telemetry & observability pipeline that pulls live data off a motorcycle, ships it over an authenticated broker, and lands it in a time-series store — built end-to-end, in public.**

Toothless Telemetry treats a 2024 Yamaha MT-15 as a live data source and builds a full observability pipeline around it: an ESP32-S3 edge node reads the bike's engine PIDs, normalizes every reading into one canonical message shape, and ships it over an authenticated TLS broker into a time-series store for visualization and alerting. It's the same architecture as production telemetry work — edge collection, a message bus, a time-series backend, store-and-forward durability, TLS, secrets hygiene, config-as-code — with a motorcycle on the sensor end instead of a fleet of servers.

If you're skimming: **the interesting engineering is in the pipeline, not the motorcycle.**

---

## What it does today

Live engine telemetry flows off the bike and across the internet, end-to-end:

```
  MT-15 ECU
     │  (OBD-II / CAN, ISO 15765 @ 500 kbps)
     ▼
  ELM327 BLE dongle
     │  BLE (GATT)
     ▼
  ESP32-S3 edge node ──── PSRAM store-and-forward queue (survives dead zones)
     │  WiFi → TLS 1.2 → MQTT (auth)
     ▼
  Mosquitto broker  (self-hosted, public, Let's Encrypt TLS)
     │
     ▼
  InfluxDB (time-series)  →  visualization (Grafana favored; comparison pending)
```

The edge node reads eight live OBD-II PIDs each cycle (RPM, speed, throttle, engine load, intake MAP, coolant temp, intake-air temp, module voltage), bundles them into a single timestamped JSON message, and publishes to the broker over TLS. I've verified the full path with the bike in motion: **bike → BLE → ESP32 → WiFi → TLS MQTT → broker → my machine.**

---

## Skills this demonstrates

| Area | What's in the repo |
|---|---|
| **Edge / IoT collection** | C++/Arduino firmware on an ESP32-S3: concurrent BLE + WiFi radio coexistence, async SNTP time sync, MQTT publishing |
| **Message shape / schema discipline** | Every reading normalized to `{ ts, source, metric, value }` from day one — one canonical shape for every source, present and future |
| **Store-and-forward reliability** | Bounded PSRAM ring buffer behind an append/drain interface: acquisition is decoupled from publishing, so a dead zone loses no data and the graph back-fills with original timestamps on reconnect |
| **Message bus** | Mosquitto MQTT, self-hosted on a public VPS so the bike can publish from any network (outbound-only, no home-LAN inbound routing) |
| **Security posture** | TLS with the CA root pinned in firmware; broker auth; all secrets (`config.h`, credentials, TLS keys) gitignored and generated on-host; committed docs kept free of host specifics |
| **Config-as-code & reproducibility** | `platformio.ini`, checked-in broker config, committed public CA, `uv`/`pyproject` for the Python side |
| **Time-series thinking** | Bundling one message per cycle (one point, many fields) — the shape a TSDB actually wants |
| **Reverse engineering** | No published PID map exists for the MT-15; PIDs discovered by correlation and cross-referenced against the shared-ECU R15/MT-07 community data |

---

## Tech stack

- **Firmware:** C++/Arduino, ESP32-S3, PlatformIO, NimBLE, PubSubClient
- **Transport:** MQTT over TLS 1.2 (Mosquitto)
- **Storage:** InfluxDB (time-series)
- **Visualization:** Grafana (leading candidate — deliberate comparison still pending)
- **Tooling / analysis:** Python (Bleak for BLE, pipeline glue, analysis), `uv`
- **Hardware:** ESP32-S3 (N16R8), ELM327 BLE OBD-II dongle, MODAXE BS6 adapter

---

## Engineering philosophy

**Isolate one variable at a time** — prove each layer before adding the next (`blink → serial → MQTT with fake data → real data → storage → viz`). **Always capture raw data.** Design for the durable version now (interfaces), build the simple version first (implementation). Where I over-engineer deliberately, I call it out as learning, not necessity.
