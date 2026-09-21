// Toothless Telemetry — GPS isolation rung (u-blox NEO-7M).
//
// Goal of THIS sketch: prove the NEO-7M in isolation — the UART link is alive,
// NMEA is streaming, and the module can actually lock a satellite fix — before
// any of it is folded into the bike firmware. Nothing here touches WiFi/BLE/MQTT.
//
// What to expect on power-up:
//   - Bytes should start streaming IMMEDIATELY (the module talks even with no fix).
//   - `sats` climbs from 0 as it hears satellites; a real lat/lon fix needs CLEAR
//     SKY and can take 30 s to several minutes cold (indoors/near a window may
//     never lock — that's the antenna, not the wiring).
//
// Serial console (UART0 -> the COM bridge port) prints once a second:
//   GPS: chars=<n> sentences=<n> checksumErr=<n> | sats=<n> fix=<Y/N> lat=.. lon=.. alt=.. speed=..
//   - chars climbing but sentences=0 / checksumErr climbing  => wrong baud or noisy wiring
//   - chars staying 0                                        => wrong pins / TX<->RX swapped / no power
//   - sentences climbing, fix=N, sats slowly rising          => wiring GOOD, just needs sky + time
//
// Onboard RGB LED mirrors fix state so you can watch it outdoors without a laptop:
//   blinking BLUE  = bytes arriving, no fix yet (searching)
//   static  GREEN  = valid fix (lat/lon locked)
//   static  RED    = no bytes at all in the last second (check wiring/baud/power)

#include <Arduino.h>
#include <TinyGPSPlus.h>

#ifndef RGB_BUILTIN
#define RGB_BUILTIN 48                 // DevKitC-1 addressable RGB LED
#endif
inline void led(uint8_t r, uint8_t g, uint8_t b) { neopixelWrite(RGB_BUILTIN, r, g, b); }
static inline bool blinkOn() { return (millis() / 300) & 1; }

// --- GPS UART wiring (see platformio.ini header) ----------------------------
static const int GPS_RX_PIN = 17;      // ESP32 RX  <- NEO-7M TX
static const int GPS_TX_PIN = 18;      // ESP32 TX  -> NEO-7M RX
static const uint32_t GPS_BAUD = 9600; // NEO-7M factory default

TinyGPSPlus gps;
HardwareSerial GPS(1);                  // UART1 (UART0 is the USB/COM debug console)

static unsigned long lastReport = 0;
static unsigned long charsAtLastReport = 0;

void setup() {
  Serial.begin(115200);
  delay(300);
  led(0, 0, 40);                        // blue at boot
  GPS.begin(GPS_BAUD, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
  Serial.println();
  Serial.printf("GPS test: UART1 @ %lu baud, RX=GPIO%d (<-7M TX), TX=GPIO%d (->7M RX)\n",
                (unsigned long)GPS_BAUD, GPS_RX_PIN, GPS_TX_PIN);
  Serial.println("Waiting for NMEA... (needs clear sky for a real fix; sats climb first)");
}

void loop() {
  // Pump every available byte through the parser. This must run continuously so
  // no serial bytes are dropped (the UART FIFO is small).
  while (GPS.available() > 0) {
    gps.encode(GPS.read());
  }

  if (millis() - lastReport >= 1000) {
    lastReport = millis();
    unsigned long chars = gps.charsProcessed();
    bool gotBytes = chars > charsAtLastReport;   // did anything arrive this second?
    charsAtLastReport = chars;

    bool haveFix = gps.location.isValid();
    Serial.printf("GPS: chars=%lu sentences=%lu checksumErr=%lu | sats=%d fix=%s",
                  chars, gps.sentencesWithFix(), gps.failedChecksum(),
                  gps.satellites.isValid() ? gps.satellites.value() : 0,
                  haveFix ? "Y" : "N");
    if (haveFix) {
      Serial.printf(" lat=%.6f lon=%.6f", gps.location.lat(), gps.location.lng());
      if (gps.altitude.isValid()) Serial.printf(" alt=%.1fm", gps.altitude.meters());
      if (gps.speed.isValid())    Serial.printf(" speed=%.1fkmh", gps.speed.kmph());
    }
    Serial.println();

    // LED status
    if (!gotBytes)      led(40, 0, 0);                          // red: nothing arriving — wiring/baud
    else if (haveFix)   led(0, 40, 0);                          // green: locked
    else                led(0, 0, blinkOn() ? 40 : 2);          // blinking blue: searching
  }
}
