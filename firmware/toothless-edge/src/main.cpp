// Toothless Telemetry — ESP32-S3 edge firmware, step 1: MQTT heartbeat.
//
// Proves the pipe before any bike data: connect to WiFi, connect to the Mosquitto
// broker with username/password, and publish a small JSON heartbeat every
// HEARTBEAT_MS to MQTT_TOPIC. Secrets live in the gitignored config.h.
//
// STATUS via the onboard RGB LED (this board's USB-CDC serial is unreliable, and
// there's no serial on the bike anyway — so the LED is our status channel):
//   blinking BLUE  = connecting to WiFi   (stuck here => WiFi problem)
//   solid  RED     = connecting to MQTT
//   RED flashes    = MQTT connect FAILED, retrying (auth or broker unreachable)
//   solid  WHITE   = connected + publishing (brief GREEN flash on each beat)

#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include "config.h"

#ifndef RGB_BUILTIN
#define RGB_BUILTIN 48          // DevKitC-1 addressable RGB LED
#endif

WiFiClient net;
PubSubClient mqtt(net);
unsigned long lastBeat = 0;
unsigned long seq = 0;

inline void led(uint8_t r, uint8_t g, uint8_t b) { neopixelWrite(RGB_BUILTIN, r, g, b); }

void connectWifi() {
  Serial.printf("WiFi: connecting to %s\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  // Disable WiFi modem-sleep: keeps the radio fully powered so idle current stays
  // high/steady (~100+ mA). Without this, current sags between beats and a power
  // bank's low-current auto-shutoff switches it off. Costs battery, keeps it alive.
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  bool on = false;
  while (WiFi.status() != WL_CONNECTED) {
    on = !on;
    led(0, 0, on ? 40 : 0);          // blink BLUE while associating
    delay(250);
    Serial.print(".");
  }
  Serial.printf("\nWiFi: connected, IP = %s\n", WiFi.localIP().toString().c_str());
}

void connectMqtt() {
  while (!mqtt.connected()) {
    led(40, 0, 0);                    // solid RED = trying MQTT
    Serial.print("MQTT: connecting... ");
    if (mqtt.connect(MQTT_CLIENT, MQTT_USER, MQTT_PASS)) {
      Serial.println("connected");
      led(130, 130, 130);              // WHITE = connected (sustained)
    } else {
      Serial.printf("failed rc=%d\n", mqtt.state());
      for (int i = 0; i < 3; i++) {   // RED flashes = MQTT-stage failure
        led(0, 0, 0); delay(150);
        led(40, 0, 0); delay(150);
      }
      delay(1500);                    // wait before retrying
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  led(0, 0, 40);                      // BLUE at boot
  connectWifi();
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) connectWifi();
  if (!mqtt.connected()) connectMqtt();
  mqtt.loop();

  unsigned long now = millis();
  if (now - lastBeat >= HEARTBEAT_MS) {
    lastBeat = now;
    seq++;
    char payload[128];
    snprintf(payload, sizeof(payload),
             "{\"source\":\"esp32\",\"seq\":%lu,\"uptime_s\":%lu}",
             seq, now / 1000);
    bool ok = mqtt.publish(MQTT_TOPIC, payload);
    led(0, 160, 0); delay(280); led(130, 130, 130);   // GREEN flash on beat, back to sustained WHITE
    Serial.printf("beat %lu (%s)\n", seq, ok ? "ok" : "FAIL");
  }
}
