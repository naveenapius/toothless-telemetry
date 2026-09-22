// config.h — YOUR real values. GITIGNORED, never committed.

#pragma once

// --- WiFi (2.4 GHz only) ----------------------------------------------------
// iPhone renamed to "naveena-iphone" (ASCII, no apostrophe). If it ever won't
// join, the SSID bytes are the first suspect — confirm the phone's name matches
// this string exactly, and that "Maximize Compatibility" (2.4 GHz) is ON.
#define WIFI_SSID   "naveena-iphone"
#define WIFI_PASS   "a1b22c333z26"

// --- MQTT broker (Mosquitto on the VPS, TLS) --------------------------------
#define MQTT_HOST    "toothless-telemetry.naveenapius.com"
#define MQTT_PORT    8883
#define MQTT_USER    "toothless"
#define MQTT_PASS    "%Zzun4rM7gDvB8"
#define MQTT_CLIENT  "toothless-esp32"
#define MQTT_TOPIC   "toothless/telemetry"

// --- Identity + timing ------------------------------------------------------
#define DEVICE_SOURCE "esp32"
#define POLL_MS       1000UL

// --- BLE (the ELM327 dongle) ------------------------------------------------
#define BLE_DEVICE_NAME "OBDII"
