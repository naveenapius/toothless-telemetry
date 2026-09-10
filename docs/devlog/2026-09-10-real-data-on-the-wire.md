# Real data on the wire (and a dongle that wouldn't talk)

*Devlog — 2026-09-10*

The broker is public and the edge node has published a heartbeat over TLS. This session was the
next rung of the ladder: **real bike data**. Fold the BLE/ELM327 acquisition — already proven
from the Mac in `ble_logger.py` — into the ESP32 firmware, so the edge node reads the MT-15's
live PIDs and publishes them to the VPS broker, and confirm the whole path end-to-end. By the end
there was live idle telemetry flowing bike → BLE → ESP32 → WiFi → TLS MQTT → the Mac. It did not
go in a straight line.

## What the firmware does now

The heartbeat firmware became a real edge node. Each ~1 s cycle it polls the eight standard
OBD-II PIDs the MT-15 actually answers *and* we can decode (rpm, speed, throttle, engine load,
intake MAP, coolant, intake-air temp, module voltage), bundles them into a single JSON message
with its own timestamp, and publishes to one topic. Bundling one message per cycle — rather than
one per metric — is deliberate: every field shares a timestamp, which is exactly the shape a
time-series store wants (one point, many fields).

Wired in alongside acquisition:

- **TLS to the broker**, with the public Let's Encrypt root pinned in the firmware so it trusts
  only that chain. Secrets (WiFi + broker credentials) stay in a gitignored `config.h`; the CA is
  public and committed so builds are reproducible.
- **SNTP time** at boot — there's no RTC, and the store-and-forward design needs each sample to
  carry a true timestamp. It's async and self-healing, so a slow first sync isn't fatal.
- **A PSRAM store-and-forward queue.** Acquisition is *decoupled* from publishing: every sample is
  appended to a bounded ring buffer regardless of network, and a separate drain step ships the
  backlog oldest-first only when the broker is reachable, stopping the instant it isn't. Lose
  signal in a dead zone and nothing is lost — the queue holds, then flushes with original
  timestamps so the graph back-fills. It's written behind an append/drain interface, so a durable
  flash/SD backing later is a backend swap, not a rewrite.
- **A status LED scheme** so the untethered node is legible with no serial: blinking yellow →
  connecting WiFi, purple → broker, blue → dongle, then static red (no bike data) vs green
  (receiving). Green blinks while a backlog is still draining and goes solid once the queue is
  empty — i.e. *solid green means it's safe to cut power*.

## The debugging saga

**First, a crash.** The very first boot with both radios up aborted inside the Bluetooth
controller. The cause was the old power-bank trick: I'd disabled WiFi modem-sleep to keep the
radio hot, but WiFi and BLE share one 2.4 GHz radio and coexistence only works if WiFi is allowed
to *yield*. "Never sleep" leaves no window for BLE, so enabling the BT controller aborts. Turning
modem-sleep back on fixed it — and quietly set up the power problem below.

**Then, a dongle that connected but wouldn't speak.** This was the long one. The ESP32 found the
dongle, connected, reported the notify subscription as OK, and its writes were even acknowledged
at the protocol layer — yet *zero* bytes ever came back. Every ELM command timed out empty. The
same dongle streamed fine to a phone and the Mac, so it was ESP32-specific.

Following the project's own rule — *isolate one variable at a time* — I stripped everything back
to a BLE-only sketch: no WiFi, MQTT, TLS, SNTP, or buffer, just scan → connect → poll → print,
and made it loud (raw hex of every reply, which characteristic answered, the negotiated MTU).
That turned a vague "no data" into a precise picture:

- Ruled out **MTU**: the ESP negotiates a large ATT MTU by default and these cheap clones can
  choke on it; pinning it to the 23-byte minimum changed nothing on its own.
- Ruled out **encryption/bonding**: forcing a paired, encrypted link *failed* — and data flowed
  anyway once the real fix was in. So that wasn't it either.
- The real cause: **the notification-enable never actually took.** The BLE stack's `subscribe()`
  call reported success but did not flip the notify switch on this clone. Writing the CCCD
  descriptor (the standard 0x2902 "start notifying me" descriptor) *by hand*, with response, is
  what made the dongle start streaming. Everything had *looked* fine the whole time — connected,
  subscribed, writes acked — which is exactly why it was so opaque.

With that one write added, the isolation sketch immediately printed live idle data: ~1500 rpm,
coolant climbing as the engine warmed, ~14 V charging. Folded the fix back into the full firmware,
removed the throwaway sketch, and the complete pipeline came up — including WiFi+BLE coexistence,
the integration risk I'd been most wary of. It just worked.

## The power tax

The coexistence fix has a cost. The old "keep the radio hot" hack was what kept a power bank from
low-current auto-shutoff, and it's now *unavailable* — not tunable, structurally incompatible with
running BLE. Modem-sleep lets current dip between activity, and sure enough the bank cut out
untethered.

There's no radio setting that both keeps WiFi hot and lets BLE run, so the fix has to come from
outside the radio: hold the current up with a real load. Options weighed — a brighter always-on
LED floor (helps at the margin, not enough alone), a USB dummy-load / keep-alive dongle on the
bank's spare port, a resistor on the 5 V rail, or just running off a battery pack (dumb cells have
no cutoff logic — NiMH or the newer USB-C 1.5 V lithium AAs, 4 in series into VIN). The real
answer is the roadmap's permanent bike/buck power, which retires the whole question. For now, the
pragmatic stopgap: charge a phone off the same bank — the phone is the load, and the bank stays
awake. Good enough to keep testing.

## Where this leaves things

The **real-data rung is done**: the edge node reads the bike and publishes live telemetry over
TLS, verified end-to-end. The finalized firmware was flattened into its own `onboard-firmware/`
project and pushed to a dedicated branch to merge to `main` — it's stable and not expected to
change soon.

Next is the rung after: stand the pipeline up past the broker — a time-series store and an
MQTT→store bridge — so this data lands somewhere queryable (fake data in first, per the usual
prove-each-layer discipline). Then the gear-from-ratio analyzer, the durable buffer tier for
whole-ride/power-loss capture, and eventually permanent power on the bike.
