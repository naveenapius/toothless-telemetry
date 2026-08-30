# Yamaha CAN reference (head-start for MT-15 discovery)

> **These are from the Yamaha MT-07 (2014), NOT the MT-15.** CAN IDs and encodings
> are model-specific. Use them as the *first hypotheses to test* on the MT-15, not
> as confirmed values. The gear step pattern (N=0, +0x20 per gear) is a common
> Yamaha convention and the most likely to carry over.

## MT-07 broadcast CAN map — 500 kbps
Source: `JackHat1/MT07_CAN_Project` (`src/MT07_CAN_Library.h`, `src/MT07_CANBus.cpp`).
These are **broadcast arbitration IDs on the internal bus**, read with a raw CAN
controller (MCP2515) — NOT mode-22 OBD DIDs.

| Signal      | CAN ID  | Byte(s)        | Decode |
|-------------|---------|----------------|--------|
| **Gear**    | `0x236` | byte 0         | `N=0x00, 1=0x20, 2=0x40, 3=0x60, 4=0x80, 5=0xA0, 6=0xC0` → gear = byte0 / 0x20 |
| Throttle    | `0x216` | byte 0         | `(b0 / 255) * 100` % |
| RPM / Speed | `0x20A` | RPM b3-b4, spd b2 | RPM = `b3*100 + b4`; speed ≈ `b2 * 0.75` |
| Temps       | `0x23E` | b0 motor, b1 air | motor °C = `(b0-0x70)*0.625 + 40`; air °C = `(b1-0x30)*0.625` |

## Cross-validation
- The **Yamaha R7 forum** independently reports **throttle at `0x216`** — matching the
  MT-07 library. Two independent sources agreeing suggests Yamaha reuses this CAN ID
  scheme across models, raising the odds the MT-15 is similar.

## How this applies to the MT-15
- The MT-15 **OBD diagnostic port is poll-only** (`ATMA` returned `NO DATA`) — see
  the project finding. So these broadcast IDs are reachable only via a **direct CAN
  tap** (SN65HVD230 + ESP32 on the actual bus wires), OR the ELM327's `ATMA` may have
  failed where a raw transceiver would succeed — an **open question to re-test** with
  the tap.
- **When the transceiver arrives:** listen-only at 500 kbps, then check `0x236 byte0`
  for the N/1..6 step pattern FIRST. Also look for `0x216` (throttle) and `0x20A`
  (rpm/speed) to confirm the ID scheme matches before trusting `0x236` for gear.

## Sources
- https://github.com/JackHat1/MT07_CAN_Project
- https://github.com/terrafirma2021/Yamaha-DataLogger (K-line euro3 Yamahas; gear WIP)
- https://www.r1-forum.com/threads/can-bus-protocol.512553/ (R1 CAN gear/throttle/rpm)
- https://www.r7forums.com/threads/can-sniffing.751/ (R7: throttle = 0x216)
- https://github.com/MotorvateDIY/ESP32_RET_SD (generic ESP32 CAN logger, SavvyCAN CSV)
