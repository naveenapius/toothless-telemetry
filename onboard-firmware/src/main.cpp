// Toothless Telemetry — ESP32-S3 edge firmware: real bike data over TLS MQTT,
// with a store-and-forward PSRAM queue.
//
// Pipeline role (edge node): scan+connect the ELM327 dongle over BLE, poll the
// MT-15's standard OBD-II PIDs, bundle every poll cycle into one JSON message,
// and publish to the VPS Mosquitto broker over TLS. Acquisition is DECOUPLED
// from publishing: every sample is appended to a bounded PSRAM ring buffer, and
// a separate drain step ships the queue oldest-first only when MQTT is up. Lose
// the network in a dead zone and nothing is lost — samples keep their own `ts`
// (SNTP time), so InfluxDB back-fills correctly when the backlog flushes.
//
// Volatile PSRAM is the accepted medium (no power-loss survival): the rider does
// not cut power until the backlog has drained (see the LED table below). A
// durable flash/SD backing is a later swap behind the same append/drain API.
//
// STATUS via the onboard RGB LED (no serial on the bike). Checked in priority
// order — the LED shows the FRONTIER (highest state reached / what's blocking):
//   blinking YELLOW = boot / hotspot (WiFi) not connected — nothing up yet
//   blinking PURPLE = WiFi up, MQTT/TLS to the broker not connected
//   blinking BLUE   = broker up, ELM327 BLE not connected
//   static  RED     = all links up, but the bike is not answering (engine off?)
//   WHITE           = transmitting bike data, NO GPS fix
//                       (static = queue empty / safe to cut power; blinking = draining)
//   GREEN           = transmitting bike data WITH a GPS fix
//                       (static = queue empty / safe to cut power; blinking = draining)
// At boot each stage flashes then shows its color solid briefly as a "passed" tick.
//
// GPS: a u-blox NEO-7M streams NMEA on UART1 (RX=GPIO17 <- 7M TX, TX=GPIO18 -> 7M
// RX, 9600 baud). Its lat/lon/gps_speed/course/satellites/hdop are folded into each bundled
// message WHEN a fix is valid (each field guarded independently — omitted when not,
// same sparse-record discipline as the PIDs). SNTP stays the clock source; GPS time
// is not used. No fix simply means those fields are absent — nothing else changes.

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include <NimBLEDevice.h>
#include <TinyGPSPlus.h>
#include <time.h>
#include <esp_heap_caps.h>
#include "config.h"
#include "isrg_root_x1.h"

#ifndef RGB_BUILTIN
#define RGB_BUILTIN 48                 // DevKitC-1 addressable RGB LED
#endif

// ---- BLE identifiers (from the dongle's captured GATT profile) --------------
static const NimBLEUUID SVC_UUID((uint16_t)0xFFF0);
static const NimBLEUUID WRITE_UUID((uint16_t)0xFFF2);   // Mac/ESP -> dongle
static const NimBLEUUID NOTIFY_UUID((uint16_t)0xFFF1);  // dongle -> Mac/ESP

// ---- GPS (u-blox NEO-7M on UART1) -------------------------------------------
// UART0 is the USB/COM debug console; the modem (later) takes UART2 — no conflict.
static const int GPS_RX_PIN = 17;      // ESP32 RX  <- NEO-7M TX (data: GPS -> ESP32)
static const int GPS_TX_PIN = 18;      // ESP32 TX  -> NEO-7M RX (config; unused for now)
static const uint32_t GPS_BAUD = 9600; // NEO-7M factory default
TinyGPSPlus  gps;
HardwareSerial GPS(1);

// =====================  LED  ================================================
inline void led(uint8_t r, uint8_t g, uint8_t b) { neopixelWrite(RGB_BUILTIN, r, g, b); }
static inline bool blinkOn() { return (millis() / 300) & 1; }   // ~1.6 Hz blink phase
// Brightness is intentionally LOW now. It used to be high as a power-bank current
// floor (WiFi modem-sleep — required for WiFi+BLE coexistence — lets idle current
// dip, and a bank's low-current auto-shutoff can then trip). The NEO-7M's steady
// ~45 mA draw now holds that floor on its own, so the LED no longer has to. The
// blink "off" phase still stays DIMLY lit (LO, not 0) rather than fully dark.
static const uint8_t HI = 60, LO = 3;
static void ledYellow(bool solid){ bool on=solid||blinkOn(); led(on?HI:LO, on?(HI*3/5):0, 0); }
static void ledPurple(bool solid){ bool on=solid||blinkOn(); led(on?(HI*4/5):LO, 0, on?HI:LO); }
static void ledBlue  (bool solid){ bool on=solid||blinkOn(); led(0, 0, on?HI:LO); }
static void ledRed   (){ led(HI, 0, 0); }
static void ledGreen (bool solid){ bool on=solid||blinkOn(); led(0, on?HI:LO, 0); }
static void ledWhite (bool solid){ bool on=solid||blinkOn(); uint8_t v=on?HI:LO; led(v, v, v); }

// =====================  PSRAM store-and-forward queue  ======================
// Fixed-slot ring buffer. Append/drain interface is the whole contract — the
// PSRAM backing here can later be swapped for flash/SD without touching callers.
#define Q_SLOTS   20000               // ~5.5 h at 1 msg/s; ~5.8 MB in PSRAM
#define Q_MSG_CAP 288                 // max bytes per bundled JSON message (incl. NUL).
                                      // 8 PIDs (~172B) + GPS lat/lon/gps_speed/course/
                                      // sats/hdop (~90B) worst-case ~263B — 288 leaves headroom.

struct RideQueue {
  char*  store = nullptr;
  size_t cap = 0, head = 0, tail = 0, count = 0;
  bool   inPsram = false;

  bool begin() {
    // Prefer PSRAM; fall back to a tiny internal-RAM queue so a no-PSRAM board still runs.
    store = (char*)heap_caps_malloc((size_t)Q_SLOTS * Q_MSG_CAP, MALLOC_CAP_SPIRAM);
    if (store) { cap = Q_SLOTS; inPsram = true; return true; }
    const size_t fallback = 200;
    store = (char*)malloc(fallback * Q_MSG_CAP);
    if (store) { cap = fallback; inPsram = false; }
    return store != nullptr;
  }
  char* slot(size_t i) { return store + i * Q_MSG_CAP; }
  void append(const char* m) {
    strlcpy(slot(head), m, Q_MSG_CAP);
    head = (head + 1) % cap;
    if (count == cap) tail = (tail + 1) % cap;   // full: drop oldest (bounded, avoids OOM)
    else count++;
  }
  const char* peek() { return count ? slot(tail) : nullptr; }
  void pop() { if (count) { tail = (tail + 1) % cap; count--; } }
};
RideQueue queue;

// =====================  networking  ========================================
WiFiClientSecure net;
PubSubClient mqtt(net);
unsigned long lastMqttTry = 0;

// =====================  BLE central + ELM327 transport  =====================
NimBLEClient*             bleClient = nullptr;
NimBLERemoteCharacteristic* chWrite = nullptr;
NimBLERemoteCharacteristic* chNotify = nullptr;
volatile bool bleConnected = false;
bool useWriteNoResp = true;            // set from the write char's actual properties

// Notify data arrives on the NimBLE task, so guard the rx buffer with a spinlock.
static portMUX_TYPE rxMux = portMUX_INITIALIZER_UNLOCKED;
static char   rxBuf[512];
static volatile size_t rxLen = 0;
static volatile bool   rxPrompt = false;   // set when the ELM '>' prompt is seen

class ClientCB : public NimBLEClientCallbacks {
  void onDisconnect(NimBLEClient*) override { bleConnected = false; }
};

static void onNotify(NimBLERemoteCharacteristic*, uint8_t* data, size_t len, bool) {
  portENTER_CRITICAL(&rxMux);
  for (size_t i = 0; i < len; i++) {
    if (rxLen < sizeof(rxBuf) - 1) rxBuf[rxLen++] = (char)data[i];
    if (data[i] == '>') rxPrompt = true;
  }
  portEXIT_CRITICAL(&rxMux);
}

// Send one ELM command, wait for the '>' prompt, return the response text.
static String elmCommand(const char* cmd, uint32_t timeoutMs = 900) {
  portENTER_CRITICAL(&rxMux); rxLen = 0; rxPrompt = false; portEXIT_CRITICAL(&rxMux);
  if (!chWrite) return "";
  String line = String(cmd) + "\r";
  chWrite->writeValue((uint8_t*)line.c_str(), line.length(), !useWriteNoResp);
  uint32_t start = millis();
  while (!rxPrompt && millis() - start < timeoutMs) delay(5);
  portENTER_CRITICAL(&rxMux);
  String out(rxBuf, rxLen);
  size_t got = rxLen;
  portEXIT_CRITICAL(&rxMux);
  if (got == 0) Serial.printf("ELM: <%s> no reply (dongle busy/held by another app?)\n", cmd);
  out.replace(">", "");
  out.trim();
  return out;
}

// Scan for the dongle, connect, bind the characteristics, subscribe to notify.
static bool bleConnect() {
  if (bleClient && bleClient->isConnected()) { bleConnected = true; return true; }
  Serial.println("BLE: scanning for the dongle...");
  NimBLEScan* scan = NimBLEDevice::getScan();
  scan->setActiveScan(true);
  NimBLEScanResults res = scan->start(6, false);
  NimBLEAdvertisedDevice found;
  bool haveFound = false;
  for (int i = 0; i < res.getCount(); i++) {
    NimBLEAdvertisedDevice d = res.getDevice(i);
    if (d.getName() == BLE_DEVICE_NAME || d.isAdvertisingService(SVC_UUID)) { found = d; haveFound = true; break; }
  }
  scan->clearResults();
  if (!haveFound) { Serial.println("BLE: dongle not found"); return false; }

  if (!bleClient) { bleClient = NimBLEDevice::createClient(); bleClient->setClientCallbacks(new ClientCB(), false); }
  if (!bleClient->connect(&found)) { Serial.println("BLE: connect failed"); return false; }
  NimBLERemoteService* svc = bleClient->getService(SVC_UUID);
  if (!svc) { Serial.println("BLE: FFF0 service missing"); bleClient->disconnect(); return false; }
  chWrite  = svc->getCharacteristic(WRITE_UUID);
  chNotify = svc->getCharacteristic(NOTIFY_UUID);
  if (!chWrite || !chNotify) { Serial.println("BLE: FFF1/FFF2 missing"); bleClient->disconnect(); return false; }
  bool sub = chNotify->subscribe(chNotify->canNotify(), onNotify);  // notify if it can, else indicate
  Serial.printf("BLE: notify canNotify=%d canIndicate=%d ; subscribe %s\n",
                chNotify->canNotify(), chNotify->canIndicate(), sub ? "ok" : "FAILED");
  // CRITICAL for this clone: NimBLE's subscribe() reports ok but does NOT actually
  // enable notifications — the dongle stays silent. Writing the CCCD (0x2902) value
  // 0x0001 ourselves, with response, is what flips notify on. (This dongle returns
  // empty on a CCCD read, so we can't verify by reading back — just write it.)
  NimBLERemoteDescriptor* cccd = chNotify->getDescriptor(NimBLEUUID((uint16_t)0x2902));
  if (cccd) { uint8_t on[2] = {0x01, 0x00}; cccd->writeValue(on, 2, true); }
  else Serial.println("BLE: CCCD 0x2902 not found");
  bleConnected = true;
  delay(300);   // let the CCCD write settle before the first command

  // Auto-detect the working write characteristic. This clone is documented to
  // sometimes ignore FFF2 and require commands on FFF1 (which is write + notify).
  NimBLERemoteCharacteristic* cands[] = { chWrite /*FFF2*/, chNotify /*FFF1*/ };
  bool answered = false;
  for (auto c : cands) {
    if (!c || !(c->canWrite() || c->canWriteNoResponse())) continue;
    chWrite = c; useWriteNoResp = c->canWriteNoResponse();
    String r = elmCommand("ATZ", 2000);
    Serial.printf("BLE: probe write on %s -> %d bytes\n", c->getUUID().toString().c_str(), r.length());
    if (r.length() > 0) { answered = true; break; }
  }
  Serial.println(answered ? "BLE: connected (dongle answering)"
                          : "BLE: connected BUT dongle silent on both FFF1/FFF2 (bond/encryption?)");
  return true;
}

// ELM init — answered by the ELM chip; the engine is not required for this part.
// Echo/linefeeds/spaces OFF and headers OFF keep standard-PID parsing clean.
static void elmInit() {
  elmCommand("ATZ", 1500); delay(200);
  elmCommand("ATE0");
  elmCommand("ATL0");
  elmCommand("ATS0");
  elmCommand("ATH0");
  elmCommand("ATSP0");
  Serial.printf("ELM: protocol=%s voltage=%s\n",
                elmCommand("ATDPN").c_str(), elmCommand("ATRV").c_str());
}

// =====================  PID table + decoders  ===============================
// Only the PIDs the MT-15 actually answers AND we can decode (channels.txt).
struct Pid { const char* req; const char* key; uint8_t nbytes; const char* fmt; float (*fn)(const uint8_t*); };
static const Pid PIDS[] = {
  {"010C", "rpm",             2, "%.0f", [](const uint8_t* b){ return (b[0] * 256 + b[1]) / 4.0f; }},
  {"010D", "speed",           1, "%.0f", [](const uint8_t* b){ return (float)b[0]; }},
  {"0111", "throttle",        1, "%.1f", [](const uint8_t* b){ return b[0] * 100.0f / 255.0f; }},
  {"0104", "engine_load",     1, "%.1f", [](const uint8_t* b){ return b[0] * 100.0f / 255.0f; }},
  {"010B", "intake_map",      1, "%.0f", [](const uint8_t* b){ return (float)b[0]; }},
  {"0105", "coolant_temp",    1, "%.0f", [](const uint8_t* b){ return (float)((int)b[0] - 40); }},
  {"010F", "intake_air_temp", 1, "%.0f", [](const uint8_t* b){ return (float)((int)b[0] - 40); }},
  {"0142", "module_voltage",  2, "%.2f", [](const uint8_t* b){ return (b[0] * 256 + b[1]) / 1000.0f; }},
};
static const size_t N_PIDS = sizeof(PIDS) / sizeof(PIDS[0]);

// From a response, pull this request's data bytes. Expected reply prefix is
// (mode|0x40)+PID, e.g. request 010C -> "410C...."; spaces are off (ATS0).
static int extractBytes(const String& resp, const Pid& p, uint8_t* out) {
  String hex; for (size_t i = 0; i < resp.length(); i++) { char c = resp[i]; if (isxdigit(c)) hex += (char)toupper(c); }
  String prefix = String("41") + (p.req + 2);          // "41" + "0C"
  int idx = hex.indexOf(prefix);
  if (idx < 0) return 0;
  idx += prefix.length();
  int n = 0;
  for (; n < p.nbytes && idx + 1 < (int)hex.length(); n++, idx += 2)
    out[n] = (uint8_t)strtol(hex.substring(idx, idx + 2).c_str(), nullptr, 16);
  return n == p.nbytes ? n : 0;
}

// =====================  time (SNTP)  ========================================
static const time_t TIME_VALID = 1700000000;   // ~2023-11; anything below = clock not set yet
static bool timeValid() { return time(nullptr) > TIME_VALID; }
// SNTP is async and self-healing: configTime keeps a client running, so the clock
// gets set within seconds even if it isn't ready when we stop waiting here. We wait
// briefly (TLS wants a valid clock), then move on — if it lands late that's fine.
static void syncTime() {
  configTime(0, 0, "pool.ntp.org", "time.google.com", "time.nist.gov");   // UTC
  Serial.print("SNTP: syncing");
  for (int i = 0; i < 30 && !timeValid(); i++) { Serial.print("."); delay(500); }
  time_t now = time(nullptr);
  if (timeValid()) Serial.printf("\nSNTP: %s", ctime(&now));
  else             Serial.println("\nSNTP: not ready yet (async — will set shortly)");
}
static void isoNow(char* buf, size_t n) {
  time_t t = time(nullptr); struct tm tmv; gmtime_r(&t, &tmv);
  strftime(buf, n, "%Y-%m-%dT%H:%M:%SZ", &tmv);
}

// =====================  acquisition: poll -> bundle -> enqueue  =============
// Returns true if the bike answered at least one PID (bus alive = "receiving data").
static bool pollCycle() {
  char msg[Q_MSG_CAP];
  char ts[24]; isoNow(ts, sizeof(ts));
  int n = snprintf(msg, sizeof(msg), "{\"ts\":\"%s\",\"source\":\"%s\"", ts, DEVICE_SOURCE);
  bool gotAny = false;
  for (size_t i = 0; i < N_PIDS; i++) {
    String resp = elmCommand(PIDS[i].req);
    uint8_t data[4];
    if (extractBytes(resp, PIDS[i], data) == PIDS[i].nbytes) {
      float v = PIDS[i].fn(data);
      n += snprintf(msg + n, sizeof(msg) - n, ",\"%s\":", PIDS[i].key);
      n += snprintf(msg + n, sizeof(msg) - n, PIDS[i].fmt, v);
      gotAny = true;
    }
  }
  // GPS rides along when a fix is valid — each field guarded independently so a
  // partial fix still contributes what it has (sparse-record discipline; omitted,
  // never stale/zero, when invalid). Decimal degrees straight from TinyGPS++.
  if (gps.location.isValid())
    n += snprintf(msg + n, sizeof(msg) - n, ",\"lat\":%.6f,\"lon\":%.6f",
                  gps.location.lat(), gps.location.lng());
  if (gps.speed.isValid())
    n += snprintf(msg + n, sizeof(msg) - n, ",\"gps_speed\":%.1f", gps.speed.kmph());
  if (gps.course.isValid())
    n += snprintf(msg + n, sizeof(msg) - n, ",\"course\":%.1f", gps.course.deg());
  if (gps.satellites.isValid())
    n += snprintf(msg + n, sizeof(msg) - n, ",\"satellites\":%d", (int)gps.satellites.value());
  if (gps.hdop.isValid())
    n += snprintf(msg + n, sizeof(msg) - n, ",\"hdop\":%.1f", gps.hdop.hdop());

  snprintf(msg + n, sizeof(msg) - n, "}");
  if (gotAny) queue.append(msg);       // only enqueue cycles that actually carried bike data
  return gotAny;
}

// =====================  MQTT (TLS) + drain  =================================
static void tryMqtt() {
  if (millis() - lastMqttTry < 2000) return;   // throttle reconnect attempts
  lastMqttTry = millis();
  if (mqtt.connect(MQTT_CLIENT, MQTT_USER, MQTT_PASS))
    Serial.println("MQTT: connected");
  else
    Serial.printf("MQTT: connect failed rc=%d\n", mqtt.state());
}
// Ship the backlog oldest-first; stop on the first failure so nothing is dropped.
static void drain() {
  int budget = 50;                     // bound work per loop so mqtt.loop()/WiFi still run
  while (queue.count > 0 && budget-- > 0) {
    if (mqtt.publish(MQTT_TOPIC, queue.peek())) queue.pop();
    else break;
  }
}

// =====================  boot bring-up (walks the LED sequence)  =============
static void bringUpWifi() {
  WiFi.mode(WIFI_STA);
  // MUST keep WiFi modem-sleep ON: WiFi and BLE share the single 2.4 GHz radio, and
  // coexistence time-slices only if WiFi is allowed to yield. Disabling it
  // (setSleep(false)) aborts inside the BT controller (coex_core_enable) when NimBLE
  // starts. Trade-off vs the power-bank auto-shutoff (§4): the steady-lit status LED
  // plus once-a-second BLE traffic keep idle current up, so this should be fine off a
  // bank; real bike power makes it moot.
  WiFi.setSleep(true);                 // WIFI_PS_MIN_MODEM — required for WiFi+BLE coexistence
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("WiFi: joining %s", WIFI_SSID);
  while (WiFi.status() != WL_CONNECTED) { ledYellow(false); delay(120); Serial.print("."); }
  Serial.printf(" ip=%s\n", WiFi.localIP().toString().c_str());
  ledYellow(true); delay(500);         // static yellow = hotspot passed
}
static void bringUpMqtt() {
  net.setCACert(ISRG_ROOT_X1_PEM);     // pin the Let's Encrypt root; TLS validates the VPS cert
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setBufferSize(512);
  while (!mqtt.connected()) {
    ledPurple(false);
    if (mqtt.connect(MQTT_CLIENT, MQTT_USER, MQTT_PASS)) break;
    Serial.printf("MQTT: connect failed rc=%d\n", mqtt.state());
    for (int i = 0; i < 6; i++) { ledPurple(false); delay(150); }
  }
  Serial.println("MQTT: connected");
  ledPurple(true); delay(500);         // static purple = broker passed
}
static void bringUpBle() {
  NimBLEDevice::init("");
  // Keep the ATT MTU small — a large (255) MTU can silence these cheap clones, and
  // the tiny ELM replies never need more. (Encryption/bonding was tried and is NOT
  // needed here — see finding: the explicit CCCD write in bleConnect() is the fix.)
  NimBLEDevice::setMTU(23);
  while (!bleConnect()) { ledBlue(false); delay(120); }
  elmInit();
  ledBlue(true); delay(500);           // static blue = dongle passed
}

void setup() {
  Serial.begin(115200);
  delay(300);
  led(0, 0, 0);
  if (!queue.begin()) Serial.println("QUEUE: alloc FAILED");
  else Serial.printf("QUEUE: %u slots in %s\n", (unsigned)queue.cap, queue.inPsram ? "PSRAM" : "internal RAM");

  // GPS streams from power-on; it's a passenger (never blocks bring-up). We don't
  // pump it during the blocking bring-up below, so the UART FIFO may drop the first
  // few NMEA sentences — harmless, TinyGPS++ re-syncs once loop() starts reading.
  GPS.begin(GPS_BAUD, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
  Serial.printf("GPS: UART1 @ %lu baud, RX=GPIO%d TX=GPIO%d\n",
                (unsigned long)GPS_BAUD, GPS_RX_PIN, GPS_TX_PIN);

  bringUpWifi();       // 1. hotspot   (yellow)
  syncTime();          //    correct clock BEFORE TLS, or cert validation fails
  bringUpMqtt();       // 2. broker    (purple)
  bringUpBle();        // 3. dongle    (blue)
}

static unsigned long lastPoll = 0;
static bool lastHadData = false;

void loop() {
  // --- GPS: drain the UART every iteration (the FIFO is tiny; read once a second
  //     and we'd drop bytes and corrupt sentences). TinyGPS++ parses incrementally. ---
  while (GPS.available() > 0) gps.encode(GPS.read());

  // --- network upkeep, all non-blocking so acquisition never stalls on it ---
  if (WiFi.status() == WL_CONNECTED && !mqtt.connected()) tryMqtt();
  if (mqtt.connected()) mqtt.loop();

  // --- acquisition: poll the bike and enqueue, independent of the network ---
  if (bleConnected) {
    if (millis() - lastPoll >= POLL_MS) {
      lastPoll = millis();
      lastHadData = pollCycle();
      Serial.printf("cycle: %s, queued=%u\n", lastHadData ? "data" : "NO DATA", (unsigned)queue.count);
    }
  } else {
    bleConnect();      // dongle link down -> no data to lose; a brief reconnect attempt is fine
  }

  // --- drain the backlog whenever the broker is reachable -------------------
  if (mqtt.connected()) drain();

  // --- status LED (priority order — show the frontier / what's blocking) -----
  bool draining = queue.count > 0;           // solid = caught up, blink = backlog pending
  if (WiFi.status() != WL_CONNECTED)      ledYellow(false);            // boot / no hotspot
  else if (!mqtt.connected())             ledPurple(false);           // broker not up
  else if (!bleConnected)                 ledBlue(false);             // dongle not up
  else if (!lastHadData)                  ledRed();                   // engine off (no bike data)
  else if (gps.location.isValid())        ledGreen(!draining);        // transmitting + GPS fix
  else                                    ledWhite(!draining);        // transmitting, no GPS fix

  delay(5);
}
