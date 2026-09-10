# Telemetry pipeline stack (broker + store + bridge, containerized)

The server-side pipeline for Toothless Telemetry, run as one Docker Compose stack on a
self-managed Linux VPS. Three services:

- **Mosquitto** — the public, internet-reachable MQTT broker. It's the **rendezvous point**: the
  ESP32 on the motorcycle (publishing from a phone hotspot) and the pipeline both connect
  *outbound* to it, so telemetry flows across different networks without any inbound routing or VPN.
- **InfluxDB** — the time-series store where readings land and are queried.
- **Telegraf** — the bridge that subscribes to the broker and writes each message into InfluxDB.

This directory is **config-as-code** — the whole stack is reproducible from these files. Secrets
(credential stores, TLS private key, InfluxDB admin token) are generated on the host and never committed.

## Architecture

```
ESP32 (bike hotspot) ─► Mosquitto (TLS 8883, public) ─► Telegraf ─► InfluxDB ─► dashboards (TBD)
                                                     └── all in-stack hops stay on the docker network ──┘
```

- Runs as Docker containers on a self-managed Linux VPS, one `docker compose up -d`.
- Every external client dials out to a single publicly-addressable broker, which sidesteps
  NAT/CGNAT on both ends — the edge code is identical regardless of where the broker is hosted.
- **Only the broker's TLS port is exposed.** InfluxDB and Telegraf have no public ports; they
  talk to the broker and each other over the internal docker network. The InfluxDB UI is reached
  from a workstation over an SSH tunnel, not the open internet.

## Security posture

- **TLS-only** listener on 8883 (Let's Encrypt certificate) — no plaintext listener. Credentials
  are never sent in the clear, and clients verify the broker's identity against the cert.
- **Authentication + ACL** — anonymous access disabled; each client authenticates with a
  username/password, and an ACL confines it to the project's own topic tree (least privilege).
  The bridge (Telegraf) uses a separate read-only user — it can ingest but never publish.
- **Store + bridge are not exposed** — InfluxDB and Telegraf publish no ports; InfluxDB is
  reachable only in-network and, for the UI, via an SSH tunnel. The stack's public surface is
  unchanged: the broker's TLS port only.
- **Automatic certificate renewal** — certbot renews on a timer and reloads the broker via a
  deploy hook, so TLS stays valid unattended.
- **Host hardening** — default-deny firewall (only SSH + the ACME challenge port + 8883 open),
  and key-only SSH (password login disabled).

## Files

| File | Purpose |
|---|---|
| `docker-compose.yml` | The stack: mosquitto + influxdb + telegraf — images, ports, volumes, mounts |
| `mosquitto.conf` | Broker configuration: TLS + internal listeners, auth, ACL, persistence |
| `acl` | Access-control list scoping each user to its topic tree |
| `telegraf.conf` | The MQTT→InfluxDB bridge: which topic to read, how to parse it, where to write |
| `deploy-certs.sh` | Makes the Let's Encrypt cert readable by the container; also the renewal hook |
| `.env.example` | Template for the gitignored `.env` (InfluxDB admin creds/token, bridge password) |
| `README.md` | This overview |

Secrets (`passwd`, `certs/`, `.env`) are generated on the host and gitignored — they are never
part of this repository.

## Deployment overview

At a high level, standing this up on a fresh VPS involves: pointing a DNS record at the host,
opening the firewall, installing Docker, obtaining a TLS certificate, generating the broker
credentials on the box, and starting the container with `docker compose`. The broker then comes
back automatically on reboot and keeps its certificate current on its own.
