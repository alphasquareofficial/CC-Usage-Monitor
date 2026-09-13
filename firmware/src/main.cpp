#include <Arduino.h>
#include <Wire.h>
#include <U8g2lib.h>
#include <WiFi.h>
#include <WiFiManager.h>
#include <ESPmDNS.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

// I2C pins for ESP32-C3 SuperMini (SDA=8, SCL=9)
#define I2C_SDA 8
#define I2C_SCL 9

U8G2_SH1106_128X64_NONAME_F_SW_I2C u8g2(U8G2_R0, /* clock=*/ I2C_SCL, /* data=*/ I2C_SDA, /* reset=*/ U8X8_PIN_NONE);

// SH1106/SSD1306 panel geometry. Two things bite here:
//  - u8g2 positions text by its BASELINE, so a baseline at SCREEN_H renders the
//    glyphs entirely below the last visible row. The lowest safe baseline is
//    SCREEN_H - 3, which leaves room for descenders.
//  - SH1106 controllers drive 132 columns behind a 128px panel, and modules
//    disagree about which columns are wired. Drawing inside a margin instead of
//    hard against x=0 / x=127 keeps content on the glass either way.
#define SCREEN_W 128
#define SCREEN_H 64
#define MARGIN 4
#define CONTENT_X0 MARGIN
#define CONTENT_X1 (SCREEN_W - 1 - MARGIN)
#define CONTENT_W (CONTENT_X1 - CONTENT_X0 + 1)
#define HEADER_BASELINE 9
#define FOOTER_BASELINE (SCREEN_H - 3)

String hostIP = "";
String str5h = "";
String strWeekly = "";
String reset5h = "";
String resetWeekly = "";
int pct5h = 0;
int pctWeekly = 0;
float progress5h = 0.0;
float progressWeekly = 0.0;
bool isActive = false;
String statusMsg = "WiFi Setup Needed";

unsigned long lastPollTime = 0;
const unsigned long pollInterval = 5000;

unsigned long lastResolveTime = 0;
const unsigned long resolveInterval = 10000;

int displayMode = 0; // 0=5h, 1=Weekly
unsigned long lastDisplayModeChange = 0;
const unsigned long displayModeInterval = 5000;

// Text helpers. Each measures the string with the CURRENT font and clamps the
// result into the content area, so a string that turns out wider than expected
// gets pinned to the left edge of the safe area instead of running off the
// panel. Set the font before calling.
static void drawClamped(const char *text, int x, int baseline) {
    int w = u8g2.getStrWidth(text);
    if (x + w > CONTENT_X1) x = CONTENT_X1 - w + 1;
    if (x < CONTENT_X0) x = CONTENT_X0;
    u8g2.drawStr(x, baseline, text);
}

static void drawLeft(const char *text, int baseline) {
    drawClamped(text, CONTENT_X0, baseline);
}

static void drawRight(const char *text, int baseline) {
    drawClamped(text, CONTENT_X1 - u8g2.getStrWidth(text) + 1, baseline);
}

static void drawCentered(const char *text, int baseline) {
    drawClamped(text, CONTENT_X0 + (CONTENT_W - u8g2.getStrWidth(text)) / 2, baseline);
}

void drawClaudeBotScreensaver(unsigned long time_ms) {
    u8g2.clearBuffer();
    
    int scale = 6;
    int ox = 16; // Center horizontally (128 - 96) / 2
    int oy = 5;  // Center vertically (64 - 54) / 2
    
    // Head: 12 wide, 7 high, x=2, y=0
    u8g2.drawBox(ox + 2*scale, oy + 0*scale, 12*scale, 7*scale);
    
    // Left Arm: 2 wide, 2 high, x=0, y=3
    u8g2.drawBox(ox + 0*scale, oy + 3*scale, 2*scale, 2*scale);
    
    // Right Arm: 2 wide, 2 high, x=14, y=3
    u8g2.drawBox(ox + 14*scale, oy + 3*scale, 2*scale, 2*scale);
    
    // Legs: 1 wide, 2 high, y=7
    u8g2.drawBox(ox + 3*scale, oy + 7*scale, 1*scale, 2*scale);
    u8g2.drawBox(ox + 5*scale, oy + 7*scale, 1*scale, 2*scale);
    u8g2.drawBox(ox + 10*scale, oy + 7*scale, 1*scale, 2*scale);
    u8g2.drawBox(ox + 12*scale, oy + 7*scale, 1*scale, 2*scale);
    
    // Blinking logic (Double blink every 4.5 seconds)
    int blinkCycle = (time_ms / 150) % 30; 
    bool isBlinking = (blinkCycle == 0 || blinkCycle == 2);
    
    // Eyes (drawn by cutting out pixels using background color)
    u8g2.setDrawColor(0); 
    if (!isBlinking) {
        // Open eyes: 1 wide, 2 high, y=2
        u8g2.drawBox(ox + 4*scale, oy + 2*scale, 1*scale, 2*scale);
        u8g2.drawBox(ox + 11*scale, oy + 2*scale, 1*scale, 2*scale);
    } else {
        // Closed eyes: a thin horizontal slit
        u8g2.drawBox(ox + 4*scale, oy + 3*scale, 1*scale, 2);
        u8g2.drawBox(ox + 11*scale, oy + 3*scale, 1*scale, 2);
    }
    u8g2.setDrawColor(1); // Restore to normal drawing color
    
    u8g2.sendBuffer();
}

void drawDashboard() {
    // If connected but idle, show the Claude Bot screensaver!
    // However, show the usage dashboard for 5 seconds every 30 seconds.
    if (WiFi.status() == WL_CONNECTED && hostIP != "" && !isActive) {
        if ((millis() % 30000) > 5000) {
            drawClaudeBotScreensaver(millis());
            return;
        }
    }

    u8g2.clearBuffer();

    // Everything is drawn inside CONTENT_X0..CONTENT_X1 rather than the full
    // panel width. SH1106 controllers have 132 columns driving a 128px glass,
    // and modules differ in which columns are wired up, so anything anchored
    // hard against x=0 or x=127 can fall off the edge on some panels. The
    // margin gives every edge-anchored element slack to absorb that.

    // Header: which limit this screen is showing.
    u8g2.setFont(u8g2_font_helvB08_tr);
    const char *title = (displayMode == 0) ? "5-HOUR" : "WEEKLY";
    drawCentered(title, HEADER_BASELINE);
    u8g2.drawLine(CONTENT_X0, 12, CONTENT_X1, 12);

    if (WiFi.status() != WL_CONNECTED) {
        u8g2.setFont(u8g2_font_helvR08_tr);
        drawLeft("Connect to AP:", 30);
        drawLeft("Claude-Monitor-Setup", 42);
    } else if (hostIP == "") {
        u8g2.setFont(u8g2_font_helvR08_tr);
        drawLeft("Searching daemon...", 30);
        drawLeft("claudemonitor.local", 42);
    } else {
        // Progress bar: the whole middle band, as large as the panel allows.
        const int barY = 18;
        const int barH = 29;             // spans y 18..46

        float progress = (displayMode == 0) ? progress5h : progressWeekly;
        if (progress < 0.0f) progress = 0.0f;
        if (progress > 1.0f) progress = 1.0f;

        u8g2.drawFrame(CONTENT_X0, barY, CONTENT_W, barH);
        int fillWidth = (int)((CONTENT_W - 4) * progress);
        if (fillWidth > 0) {
            u8g2.drawBox(CONTENT_X0 + 2, barY + 2, fillWidth, barH - 4);
        }
    }

    // Footer: live/idle on the left, percentage centred, reset countdown right.
    if (WiFi.status() == WL_CONNECTED && hostIP != "") {
        u8g2.setFont(u8g2_font_profont10_tr);
        drawLeft(isActive ? "LIVE" : "IDLE", FOOTER_BASELINE);

        int pct = (displayMode == 0) ? pct5h : pctWeekly;
        String pctStr = String(pct) + "%";
        drawCentered(pctStr.c_str(), FOOTER_BASELINE);

        String resetStr = (displayMode == 0) ? reset5h : resetWeekly;
        if (resetStr.length() > 0) {
            drawRight(resetStr.c_str(), FOOTER_BASELINE);
        }
    } else {
        u8g2.setFont(u8g2_font_profont10_tr);
        drawLeft(statusMsg.c_str(), FOOTER_BASELINE);
    }

    u8g2.sendBuffer();
}

void resolveHost() {
    if (hostIP != "") return;
    // MDNS.queryHost() blocks while it waits, so don't retry it on every
    // loop pass or the UI freezes for as long as the daemon is unreachable.
    if (lastResolveTime != 0 && millis() - lastResolveTime < resolveInterval) return;
    lastResolveTime = millis();
    
    Serial.println("Resolving claudemonitor.local...");
    IPAddress serverIP = MDNS.queryHost("claudemonitor");
    if (serverIP.toString() != "0.0.0.0") {
        hostIP = serverIP.toString();
        Serial.println("Resolved host to: " + hostIP);
    } else {
        Serial.println("MDNS resolve failed");
    }
}

void pollData() {
    if (hostIP == "") return;
    if (WiFi.status() != WL_CONNECTED) return;
    
    HTTPClient http;
    String url = "http://" + hostIP + ":8080/stats";
    http.begin(url);
    http.setConnectTimeout(2000);
    http.setTimeout(3000);
    
    int httpCode = http.GET();
    if (httpCode == 200) {
        String payload = http.getString();
        
        JsonDocument doc;
        DeserializationError error = deserializeJson(doc, payload);
        if (!error) {
            str5h = doc["str_5h"] | "";
            strWeekly = doc["str_weekly"] | "";
            reset5h = doc["reset_str_5h"] | "";
            resetWeekly = doc["reset_str_weekly"] | "";
            pct5h = doc["pct_5h"] | 0;
            pctWeekly = doc["pct_weekly"] | 0;
            progress5h = doc["progress_5h"] | 0.0f;
            progressWeekly = doc["progress_weekly"] | 0.0f;
            isActive = doc["active"] | false;
        } else {
            Serial.println("JSON Parse Error");
        }
    } else {
        Serial.printf("HTTP Error: %d\n", httpCode);
        if (httpCode < 0) {
            hostIP = "";
            statusMsg = "Host Offline";
        }
    }
    http.end();
}

void setup() {
    Serial.begin(115200);
    delay(2000); // Give serial monitor time to open
    
    Wire.begin(I2C_SDA, I2C_SCL);
    
    // --- I2C SCANNER ---
    Serial.println("\n--- Starting I2C Scanner ---");
    byte error, address;
    int nDevices = 0;
    for(address = 1; address < 127; address++ ) {
        Wire.beginTransmission(address);
        error = Wire.endTransmission();
        if (error == 0) {
            Serial.print("I2C device found at address 0x");
            if (address < 16) Serial.print("0");
            Serial.println(address, HEX);
            nDevices++;
        }
    }
    if (nDevices == 0) {
        Serial.println("No I2C devices found. PLEASE CHECK WIRING!");
    } else {
        Serial.println("--- I2C Scan Complete ---\n");
    }
    
    u8g2.setI2CAddress(0x78); // Try 0x78 (0x3C * 2). If this fails, some displays use 0x7A (0x3D * 2)
    u8g2.begin();
    
    drawDashboard();
    
    // Explicitly set WiFi mode to STA before using WiFiManager
    // This resolves issues on ESP32-C3 where the AP fails to broadcast
    WiFi.mode(WIFI_STA);
    
    WiFiManager wm;
    
    // Set a timeout so it doesn't block forever if something goes wrong
    wm.setConfigPortalTimeout(180); 
    
    Serial.println("Starting WiFiManager AP: Claude-Monitor-Setup");
    
    // Draw loading screen before attempting connection
    u8g2.clearBuffer();
    u8g2.setFont(u8g2_font_helvB08_tr);
    u8g2.drawStr(0, 32, "Connecting to WiFi...");
    u8g2.sendBuffer();
    
    // --- BROWNOUT MITIGATION ---
    // Drastically lower the Wi-Fi transmission power to prevent massive current spikes
    // that cause the ESP32 to brownout and reboot when starting the AP.
    // Default is usually ~19.5dBm. We lower it to 8.5dBm (or even lower if needed).
    WiFi.setTxPower(WIFI_POWER_8_5dBm); 
    // ---------------------------
    
    // Adding a password ("password123") prevents iOS/Android/Windows from hiding it as an "insecure open network"
    bool res = wm.autoConnect("Claude-Monitor-Setup", "password123");
    
    if(!res) {
        Serial.println("Failed to connect");
    } else {
        Serial.println("Connected to WiFi");
        statusMsg = "WiFi Connected";
    }
    
    if (!MDNS.begin("claudemonitordevice")) {
        Serial.println("Error setting up MDNS responder!");
    }
}

void loop() {
    if (WiFi.status() == WL_CONNECTED) {
        resolveHost();
        
        if (millis() - lastPollTime > pollInterval) {
            pollData();
            lastPollTime = millis();
        }
    }
    
    if (millis() - lastDisplayModeChange > displayModeInterval) {
        displayMode = (displayMode + 1) % 2; // Only swap between 5H and Weekly
        lastDisplayModeChange = millis();
    }
    
    drawDashboard();
    delay(100); // Fast delay for smooth animation
}
