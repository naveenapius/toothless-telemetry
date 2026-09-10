# Public MQTT broker (TLS + auth, containerized)

The public, internet-reachable MQTT broker for the Toothless Telemetry pipeline. It's the
**rendezvous point**: the ESP32 on the motorcycle (publishing from a phone hotspot) and the
subscriber that feeds the time-series store both connect *outbound* to this broker, so telemetry
flows across different networks without any inbound routing or VPN.

This directory is **config-as-code** — the broker is reproducible from these files. Secrets (the
credential store and the TLS private key) are generated on the host and never committed.

## Architecture

```
ESP32 (bike hotspot)  ─┐
                       ├─►  Mosquitto broker (this VPS, TLS 8883)  ─►  subscriber ─► InfluxDB ─► dashboards
subscriber / pipeline ─┘
```

- Runs as a Docker container (`eclipse-mosquitto`) on a self-managed Linux VPS.
- Every client dials out to a single publicly-addressable broker, which sidesteps NAT/CGNAT on
  both ends — the edge code is identical regardless of where the broker is hosted.

## Security posture

- **TLS-only** listener on 8883 (Let's Encrypt certificate) — no plaintext listener. Credentials
  are never sent in the clear, and clients verify the broker's identity against the cert.
- **Authentication + ACL** — anonymous access disabled; each client authenticates with a
  username/password, and an ACL confines it to the project's own topic tree (least privilege).
- **Automatic certificate renewal** — certbot renews on a timer and reloads the broker via a
  deploy hook, so TLS stays valid unattended.
- **Host hardening** — default-deny firewall (only SSH + the ACME challenge port + 8883 open),
  and key-only SSH (password login disabled).

## Files

| File | Purpose |
|---|---|
| `mosquitto.conf` | Broker configuration: TLS listener, auth, ACL, persistence |
| `acl` | Access-control list scoping each user to its topic tree |
| `docker-compose.yml` | Container definition — image, published port, mounts |
| `deploy-certs.sh` | Makes the Let's Encrypt cert readable by the container; also the renewal hook |
| `README.md` | This overview |

Secrets (`passwd`, `certs/`) are generated on the host and gitignored — they are never part of
this repository.

## Deployment overview

At a high level, standing this up on a fresh VPS involves: pointing a DNS record at the host,
opening the firewall, installing Docker, obtaining a TLS certificate, generating the broker
credentials on the box, and starting the container with `docker compose`. The broker then comes
back automatically on reboot and keeps its certificate current on its own.
