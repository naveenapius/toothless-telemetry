# Mosquitto broker (MQTT ingest for the telemetry pipeline)

This is the config-as-code for the MQTT broker. The config (`mosquitto.conf`) is
committed; the **credentials (`passwd`) are a secret and are gitignored** — they
never enter version control. To reproduce the broker on a new machine you
regenerate `passwd` locally with the command below.

## Files

| File            | Committed? | What it is |
|-----------------|-----------|------------|
| `mosquitto.conf`| ✅ yes    | Broker configuration (config-as-code) |
| `passwd`        | ❌ no (gitignored) | Hashed username/password store (secret) |
| `README.md`     | ✅ yes    | This file |

## First-time setup — generate the password file

Run this **from this directory** (`infra/mosquitto/`). It prompts for the
password interactively (hidden input) and writes only a hash — no plaintext
touches your shell history, screen, or disk:

```bash
mosquitto_passwd -c passwd toothless
```

- `-c` creates the file (first time only — it overwrites, so drop `-c` to add
  more users later).
- `toothless` is the username the ESP32 and test clients will authenticate with.

Then lock its permissions (Mosquitto warns if it's world-readable):

```bash
chmod 600 passwd
```

## Run the broker (local LAN dev)

From this directory, so the relative `password_file passwd` path resolves:

```bash
mosquitto -c mosquitto.conf -v
```

`-v` = verbose, so you watch every connection and message live. `Ctrl+C` stops it.

## Test it (broker talking to itself, before the ESP32 exists)

Subscriber (new terminal):

```bash
mosquitto_sub -h localhost -t 'toothless/#' -v -u toothless -P 'YOUR_PASSWORD'
```

Publisher (another terminal):

```bash
mosquitto_pub -h localhost -t toothless/ping -m 'hello' -u toothless -P 'YOUR_PASSWORD'
```

The subscriber should print `toothless/ping hello`. Omit `-u/-P` and the
connection is refused — that's auth working.

## Security posture

- **Now (LAN):** authenticated, but plaintext on port 1883. Acceptable on a
  trusted home network — sniffing would require already being on the WiFi.
- **Before public VPS:** add a TLS listener on 8883. The `passwd` auth here
  carries over unchanged; TLS is added on top, not instead.
