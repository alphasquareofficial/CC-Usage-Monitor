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

String hostIP = "";
long total5hTokens = 0;
long totalWeeklyTokens = 0;
bool isActive = false;
String statusMsg = "WiFi Setup Needed";

long limit5h = 2500000;
long limitWeekly = 10000000;

unsigned long lastPollTime = 0;
const unsigned long pollInterval = 5000;

int displayMode = 0; // 0=5h, 1=Weekly
unsigned long lastDisplayModeChange = 0;
const unsigned long displayModeInterval = 5000;

String formatTokens(long tokens) {
    if (tokens >= 1000000) {
        return String(tokens / 1000000.0, 1) + "M";
    } else if (tokens >= 1000) {
        return String(tokens / 1000.0, 1) + "k";
    }
    return String(tokens);
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
    if (WiFi.status() == WL_CONNECTED && hostIP != "" && !isActive) {
        drawClaudeBotScreensaver(millis());
        return;
    }
    
    u8g2.clearBuffer();
    
    // Top Bar
    u8g2.setFont(u8g2_font_helvB08_tr);
    if (displayMode == 0) {
        u8g2.drawStr(0, 10, "CLAUDE: 5H LIMIT");
    } else {
        u8g2.drawStr(0, 10, "CLAUDE: WEEKLY");
    }
    u8g2.drawLine(0, 13, 128, 13);
    
    if (WiFi.status() != WL_CONNECTED) {
        u8g2.setFont(u8g2_font_helvR08_tr);
        u8g2.drawStr(0, 30, "Connect to AP:");
        u8g2.drawStr(0, 42, "Claude-Monitor-Setup");
    } else if (hostIP == "") {
        u8g2.setFont(u8g2_font_helvR08_tr);
        u8g2.drawStr(0, 30, "Searching daemon...");
        u8g2.drawStr(0, 42, "claudemonitor.local");
    } else {
        long currentTokens = (displayMode == 0) ? total5hTokens : totalWeeklyTokens;
        long currentLimit = (displayMode == 0) ? limit5h : limitWeekly;
        
        String tokenStr = formatTokens(currentTokens) + " / " + formatTokens(currentLimit);
        
        u8g2.setFont(u8g2_font_helvB10_tr);
        int strWidth = u8g2.getStrWidth(tokenStr.c_str());
        u8g2.setCursor((128 - strWidth) / 2, 32);
        u8g2.print(tokenStr);
        
        int barWidth = 110;
        int barHeight = 12;
        int barX = (128 - barWidth) / 2;
        int barY = 40;
        
        float progress = (float)currentTokens / currentLimit;
        if (progress > 1.0) progress = 1.0;
        
        u8g2.drawFrame(barX, barY, barWidth, barHeight);
        int fillWidth = (barWidth - 4) * progress;
        if (fillWidth > 0) {
            u8g2.drawBox(barX + 2, barY + 2, fillWidth, barHeight - 4);
        }
    }
    
    // Footer
    if (WiFi.status() == WL_CONNECTED && hostIP != "") {
        u8g2.setFont(u8g2_font_profont10_tr);
        if (isActive) {
            u8g2.drawStr(0, 64, "LIVE");
        } else {
            u8g2.drawStr(0, 64, "IDLE");
        }
        
        long currentTokens = (displayMode == 0) ? total5hTokens : totalWeeklyTokens;
        long currentLimit = (displayMode == 0) ? limit5h : limitWeekly;
        int pct = (currentTokens * 100) / currentLimit;
        String pctStr = String(pct) + "%";
        int pctWidth = u8g2.getStrWidth(pctStr.c_str());
        u8g2.setCursor(128 - pctWidth, 64);
        u8g2.print(pctStr);
    } else {
        u8g2.setFont(u8g2_font_profont10_tr);
        u8g2.drawStr(0, 64, statusMsg.c_str());
    }
    
    u8g2.sendBuffer();
}

void resolveHost() {
    if (hostIP != "") return;
    
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
    
    int httpCode = http.GET();
    if (httpCode == 200) {
        String payload = http.getString();
        
        JsonDocument doc;
        DeserializationError error = deserializeJson(doc, payload);
        if (!error) {
            total5hTokens = doc["5h_total"] | 0;
            totalWeeklyTokens = doc["weekly_total"] | 0;
            limit5h = doc["limit_5h"] | 2500000;
            limitWeekly = doc["limit_weekly"] | 10000000;
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
