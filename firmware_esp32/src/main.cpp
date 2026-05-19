/*
 * Energy_System_Diploma — ESP32 firmware (VS Code / PlatformIO build)
 * Uplink   : POST http://host.wokwi.internal:8000/api/telemetry/
 * Downlink : GET  http://host.wokwi.internal:8000/api/device-state/
 */

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>


// Configuration
// Secrets (SERVER_URL, API_KEY, WIFI_SSID, WIFI_PASSWORD) live in secrets.h,
// which is gitignored. Copy secrets.h.example to secrets.h on first checkout
// and fill in real values. See README.md for details.
#include "secrets.h"

#define UPLINK_INTERVAL_MS    5000
#define DOWNLINK_INTERVAL_MS  2000

#define SCREEN_WIDTH  128
#define SCREEN_HEIGHT 64
#define OLED_RESET    -1
#define OLED_ADDRESS  0x3C

struct DeviceMap {
  const char* device_id;
  const char* label;
  uint8_t     adc_pin;
  uint8_t     led_pin;
  float       max_watts;
  bool        is_on;
  float       last_watts;
};

DeviceMap devices[] = {
  { "1", "Fridge", 32, 25, 1500.0f, true, 0.0f },
  { "2", "Boiler", 33, 26, 2500.0f, true, 0.0f },
  { "3", "Iron",   34, 27, 2000.0f, true, 0.0f }
};
const size_t kDeviceCount = sizeof(devices) / sizeof(devices[0]);


// Globals
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

unsigned long lastUplinkMs   = 0;
unsigned long lastDownlinkMs = 0;

float lastTotalWatts   = 0.0f;
int   lastLimitWatts   = 0;
bool  lastIsOverloaded = false;

void connectWifi() {
  Serial.printf("Connecting to WiFi: %s", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  display.clearDisplay();
  display.setCursor(0, 0);
  display.print("WiFi: ");
  display.println(WIFI_SSID);
  display.display();

  uint8_t attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 60) {
    delay(500);
    Serial.print(".");
    ++attempts;
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("Connected, IP: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("WiFi connection FAILED");
  }
}

float readPowerWatts(const DeviceMap& dev) {
  int raw = analogRead(dev.adc_pin);
  return (raw / 4095.0f) * dev.max_watts;
}

void applyRelayPins() {
  for (size_t i = 0; i < kDeviceCount; ++i) {
    digitalWrite(devices[i].led_pin, devices[i].is_on ? HIGH : LOW);
  }
}

bool sendTelemetry(const DeviceMap& dev, float watts) {
  if (WiFi.status() != WL_CONNECTED) return false;

  HTTPClient http;
  String url = String(SERVER_URL) + "/api/telemetry/";
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", API_KEY);

  StaticJsonDocument<128> body;
  body["device_id"]   = dev.device_id;
  body["power_watts"] = watts;
  String payload;
  serializeJson(body, payload);

  int code = http.POST(payload);
  if (code > 0) {
    Serial.printf("[uplink ] %s -> %.1f W (HTTP %d)\n", dev.device_id, watts, code);
    String resp = http.getString();
    StaticJsonDocument<512> doc;
    if (!deserializeJson(doc, resp)) {
      lastTotalWatts   = doc["balancing"]["total_power_watts"] | lastTotalWatts;
      lastLimitWatts   = doc["balancing"]["power_limit_watts"] | lastLimitWatts;
      lastIsOverloaded = doc["balancing"]["is_overloaded"]     | lastIsOverloaded;
    }
  } else {
    Serial.printf("[uplink ] %s FAILED: %s\n", dev.device_id, http.errorToString(code).c_str());
  }
  http.end();
  return code > 0;
}

bool pollStates() {
  if (WiFi.status() != WL_CONNECTED) return false;

  HTTPClient http;
  String url = String(SERVER_URL) + "/api/device-state/";
  http.begin(url);
  http.addHeader("X-API-Key", API_KEY);

  int code = http.GET();
  if (code != 200) {
    Serial.printf("[downlnk] FAILED HTTP %d\n", code);
    http.end();
    return false;
  }
  String resp = http.getString();
  http.end();

  StaticJsonDocument<1024> doc;
  DeserializationError err = deserializeJson(doc, resp);
  if (err) {
    Serial.printf("[downlnk] parse error: %s\n", err.c_str());
    return false;
  }

  for (JsonObject item : doc.as<JsonArray>()) {
    const char* id = item["device_id"];
    bool is_on     = item["is_on"];
    for (size_t i = 0; i < kDeviceCount; ++i) {
      if (strcmp(devices[i].device_id, id) == 0) {
        if (devices[i].is_on != is_on) {
          Serial.printf("[downlnk] %s -> %s\n", id, is_on ? "ON" : "OFF");
        }
        devices[i].is_on = is_on;
      }
    }
  }
  applyRelayPins();
  return true;
}

void drawOled() {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 0);
  display.printf("Total %4.0f/%4dW\n", lastTotalWatts, lastLimitWatts);
  display.println(lastIsOverloaded ? "STATUS: OVERLOAD" : "STATUS: OK");
  display.println();
  for (size_t i = 0; i < kDeviceCount; ++i) {
    display.printf("%s %-6s %4.0fW\n",
      devices[i].is_on ? "[ON ]" : "[OFF]",
      devices[i].label,
      devices[i].last_watts);
  }
  display.display();
}


// Arduino entry
void setup() {
  Serial.begin(115200);
  delay(200);

  for (size_t i = 0; i < kDeviceCount; ++i) {
    pinMode(devices[i].led_pin, OUTPUT);
    pinMode(devices[i].adc_pin, INPUT);
  }
  applyRelayPins();

  Wire.begin(21, 22);
  if (!display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDRESS)) {
    Serial.println("[!] SSD1306 init failed");
  } else {
    display.clearDisplay();
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    display.setCursor(0, 0);
    display.println("Booting...");
    display.display();
  }

  connectWifi();
}

void loop() {
  unsigned long now = millis();

  if (now - lastUplinkMs >= UPLINK_INTERVAL_MS) {
    lastUplinkMs = now;
    for (size_t i = 0; i < kDeviceCount; ++i) {
      devices[i].last_watts = readPowerWatts(devices[i]);
      sendTelemetry(devices[i], devices[i].last_watts);
      delay(80);
    }
    drawOled();
  }

  if (now - lastDownlinkMs >= DOWNLINK_INTERVAL_MS) {
    lastDownlinkMs = now;
    pollStates();
    drawOled();
  }
}