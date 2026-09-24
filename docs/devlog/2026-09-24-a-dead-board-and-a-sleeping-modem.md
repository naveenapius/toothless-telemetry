# A dead board and a sleeping modem

*Devlog — 2026-09-24*

No new features today. Instead: a hardware post-mortem, a methodical isolation, and a pivot
that ran into its own wall. Both situations resolved the same way — with a clearer understanding
of what the hardware actually needs and a plan for when the replacement arrives.

## The board that stopped coming back

The first sign was subtle: the ELM327 dongle stopped connecting, and the status LED went dark.
Not blinking the wrong colour — completely off, which the LED table says should never happen once
the firmware is running. A reflash completed cleanly with no errors, and the board still wouldn't
run. That combination — flashing works, running doesn't — is a useful first clue, because it
means the flash path is intact and the failure is somewhere in normal operation.

The serial monitor showed why. The board was stuck in a tight boot loop, printing the same two
lines on every pass — PSRAM queue allocated, GPS UART initialised — then resetting before getting
any further. The reset code in the ROM output read `rst:0x1 (POWERON)`, which on the ESP32-S3
is the signature of a genuine power loss rather than a software crash, and it appeared on every
single cycle.

The obvious move was to add instrumentation and watch what line of `setup()` it was dying on.
Flushed `Serial` calls went in before and after each step in `bringUpWifi()`, along with a
print of the IDF-level reset reason at the very top of `setup()`. The result was immediate: the
board printed `WiFi: mode(STA)` and then reset. It never returned from `WiFi.mode()` — the call
that physically powers up the RF modem, and the single highest-current event in the entire boot
sequence.

Buried in the scroll, one reset in ten read `rst:0xf (BROWNOUT_RST)` instead of `0x1`. The
rest showed `POWERON` because the voltage collapsed fast enough to corrupt the reset reason
register before it could be written — but that one honest line named the cause. The board was
browning out the instant the WiFi modem drew its inrush current, the rail was collapsing, and
the chip was waking up again with no memory of why.

Multiple power sources ruled out the supply side: the Mac USB port, both ports on the power
bank, two different cables. All produced the same result at the same moment. When every upstream
variable is eliminated and the failure happens at exactly the same trigger each time, the fault
is on the board itself — specifically, most likely the AMS1117-3.3 LDO or a cracked decoupling
capacitor on the 3.3V rail. Either one under mechanical stress can hold up at idle but collapse
under the modem's inrush. The board had been travelling loose in a bag with the GPS module and
wiring, which is exactly the kind of vibration and flex that kills SMD passives quietly. The
board is done.

The lesson is simple and worth keeping: dev boards are not field hardware. A bare PCB rattling
against other components in a bag is not a reasonable transport method once the build is leaving
the desk. An enclosure — even a foam-lined ziplock — is not optional at that point.

## The modem that wouldn't wake up

With the ESP32 sidelined, attention turned to the A7670C cellular modem that's been waiting to
be tested. The plan was to talk to it directly from the Mac over its RS232 DB9 port while
waiting for the replacement board.

The first check was whether the RS232 cable was even the right tool. The board is labelled
`TX, RX — 1.8V operational`, which sounds like raw TTL, but a small chip near the DB9 connector
— an ILX232D — confirmed that RS232 level conversion is present on the board. The cable was
safe. A Python script tried every common baud rate against every flow control combination and
got silence on all of them.

The silence turned out to be the modem's fault, not the cable's. SIMCom modules — and the
A7670C is a SIMCom module — do not boot when power is applied. They sit inert until a PWRKEY
line is held low for about a second, at which point they come to life and the NET LED starts
blinking as they search for a network. Without asserting PWRKEY, the power LED lights up,
everything looks alive, and the modem says nothing, because it isn't on yet.

The board has no PWRKEY button. The pin is not broken out to the header. The only way to assert
it is via a GPIO from a microcontroller — which, today, doesn't exist. The modem cannot be
tested standalone.

One thing the investigation did settle: the board's three onboard 470µF capacitors are there
specifically to buffer the LTE modem's 1–2A inrush bursts, which dwarf anything the ESP32's
WiFi draws. With 1410µF of bulk storage already on the board, no additional capacitors will be
needed when the modem is wired up.

## Where this leaves things

Two pieces of hardware sit waiting for the same thing: a working ESP32. When the replacement
arrives, the plan is:

1. Confirm the new board boots cleanly on the bench before wiring anything to it.
2. Verify the NEO-7M by powering it from the ESP32 and checking for NMEA sentences — rules
   in or out any damage from the brownout it may have witnessed.
3. Wire the A7670C: PWRKEY to a GPIO (pull low 1 second on boot, then release), UART2 to the
   1.8V TX/RX header pins via a level shifter, and prove basic AT communication before writing
   any integration code.

The GPS and modem hardware are almost certainly fine. The only thing actually broken is the
board that was loose in a bag.
