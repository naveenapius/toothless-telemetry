# Toothless — Grafana dashboard & derived-metric methodology

This directory holds the provisioned Grafana setup for the telemetry pipeline: the datasource,
the dashboard provider, and the dashboard definition itself
(`dashboard-defs/toothless.dashboard.json`). This README documents the **dashboard layout** and,
in detail, the **methodology behind the derived metrics** — currently **gear**.

## Dashboard layout

- **Top row — live stats:** RPM, Speed, Coolant, Battery (`last()` over a short window).
- **Measured time-series:** RPM, Speed, Throttle, Coolant temperature, Battery voltage,
  Engine load — each read straight from the `telemetry` measurement.
- **Derived — Gear:** current gear (stat), gear-over-time (state timeline), and the raw
  `rpm / speed` ratio with the gear centroids drawn as reference lines.

## Measured vs. derived

The edge node publishes **only signals the bike actually reports** (rpm, speed, throttle,
coolant, voltage, …). Anything the bike does *not* send — **gear**, and later shift-point and
throttle-aggression analysis — is **derived**, and it is derived **downstream in the Grafana/Flux
query, never on the firmware and never stored as a fake measurement.**

Why this line matters:

- The firmware stays a faithful sensor. A `gear` field written as if it came from the bike would
  misrepresent the data model — the MT-15's OBD port cannot report gear at all (the diagnostic
  port is poll-only; there is no gear PID).
- Derivation logic is cheap to retune in a query; baking it into firmware would mean a reflash
  every time the calibration changes.
- If a derived metric is ever persisted for performance, it should carry `source: derived` to
  keep it distinct from real telemetry.

## Gear derivation

### The idea

Within a single gear the engine and rear wheel are locked through a **fixed mechanical ratio**,
so `rpm / speed` is constant for that gear — tall in 1st (engine spins a lot per km/h), short in
6th. Gear is the only thing that changes that relationship, so **the ratio is a fingerprint of
the gear.**

### The ratio ladder (2024 Yamaha MT-15)

Derived from real ride data, not a spec sheet (no published MT-15 gear-ratio map exists).
`ratio = rpm ÷ speed(km/h)`:

| Gear | Centroid (ratio) | Observed range | Boundary to next gear |
|-----:|-----------------:|:--------------:|:---------------------:|
| 1 | 278 | 269 – 309 | **225.6** |
| 2 | 183 | 180 – 198 | **156.0** |
| 3 | 133 | 127 – 139 | **121.5** |
| 4 | 111 | 105 – 117 | **101.6** |
| 5 | 93  | 88 – 98    | **87.3**  |
| 6 | 82  | 79 – 85    | —         |

A sample is classified into the gear whose band it falls in; below the "moving & pulling"
threshold (**speed < 8 km/h or rpm < 800**) the ratio is meaningless, so it is reported as
**N** (neutral/unknown — really "no reliable ratio": stopped, crawling, or clutch-in).

### Why each gear is a band, not a single value

- **Integer-km/h quantization (dominant):** speed is reported as a whole number, so at 21 km/h
  the true value is 20.5–21.5 and the ratio smears ±~2.4% from rounding alone — worse at low
  speed (why 1st is the widest band and why crawl-speed ratios below ~8 km/h are discarded).
- **Non-simultaneous sampling:** rpm and speed aren't sampled at the same instant, so they drift
  slightly out of phase under acceleration.
- **Driveline give:** chain lash, tyre slip, and partial clutch engagement add real wobble.

### How the centroids were found (calibration)

1. **Keep only clean cruise samples** — moving, engine pulling, and the ratio stable versus both
   neighbouring samples (throws out shifts, clutch-in, and launches).
2. **Let them cluster** — those clean ratios fall into six piles separated by empty gaps; the
   number of gears is discovered from the data, not assumed.
3. **Average each pile** → the centroid.
4. **Label against reality** — sorting by ratio gives the order (highest = 1st); the labels were
   then anchored to segments confirmed by the rider (a ~21 s held-1st pull, a ~28 s 6th-gear
   cruise).

### Why the boundaries are geometric means

Classification is "nearest centroid." Because gear ratios are **multiplicative** (a fixed
*percentage* is what's meaningful, not a fixed absolute), distance is measured in **log space**.
The point exactly halfway between two centroids in log space is their **geometric mean**:

```
boundary = √(a · b)      e.g. 1|2 = √(278 · 183) ≈ 225.6
```

Here it barely differs from the arithmetic mean, but it is the principled choice and matters more
as the gap widens. Crucially, the observed bands **do not overlap** — every boundary sits in an
empty gap between two gears' ranges, so any threshold in that gap classifies correctly. The
geometric mean simply maximises the proportional margin on both sides.

### Known limitations

- **Shift transients:** mid-shift the ratio sweeps through the gap between two gears, so a sample
  or two get whatever gear they land nearest — visible as brief flickers / vertical smears on the
  ratio panel. A future consumer-side classifier can debounce (require N consecutive samples) and
  hold-last-gear through clutch dips; that smoothing does not fit cleanly in the dashboard query.
- **5↔6 is the tightest boundary** (87.3): 5th and 6th are only ×1.13 apart, so their gap is
  narrow. A deliberate rev-range calibration run in both gears would tighten those two centroids.
- **1st gear rarely reaches steady state** (hard launch), so it is anchored from the confirmed
  held-1st pull rather than from cruise clustering.
- **Validation:** the ladder was cross-checked across three independent session windows,
  including a near-redline run to 106 km/h, and matches the rider's own account of each session.

### Retuning

The centroids/boundaries live as literal numbers in the gear panel queries in
`dashboard-defs/toothless.dashboard.json` (each query is commented and points back here). To
recalibrate: rerun the calibration on a fresh session export, then update the boundary constants
in those queries and the ladder table above.
