# IoT Token Monitor

A production-ready IoT desk monitor that tracks Claude Code usage and displays it on an ESP32-C3 SuperMini with an SH1106/SSD1306 OLED display.

## Features
- **Dynamic Limits**: Tracks your 5-Hour and Weekly Claude Code token limits using graphical progress bars.
- **Claude Bot Screensaver**: When your session is idle for 15 minutes, the screen switches to an adorable animated Claude Code pixel-art mascot that blinks at you!
- **Zero-Config Wi-Fi**: Uses a captive portal to securely connect to your home network.
- **mDNS Discovery**: Automatically finds the Python daemon running on your computer.

## Architecture

The project consists of two core components:
1.  **Desktop Daemon (`host/`)**: A Python background process that parses `~/.claude/projects/**/*.jsonl` log files, computes 5h/weekly token usage, and serves it via an HTTP endpoint. It broadcasts its presence using mDNS.
2.  **ESP32 Firmware (`firmware/`)**: C++ code using PlatformIO. It connects to Wi-Fi, discovers the daemon via mDNS, polls the HTTP endpoint, and draws the UI.

## 1. Desktop Daemon Setup

The host daemon needs to be run on the machine running Claude Code.

### Requirements
- Python 3.9+
- Pip dependencies: `pip install -r host/requirements.txt`

### Running
Run the daemon (it will automatically register itself to run on startup):
```bash
python host/claude_monitor_daemon.py
```

### Configuration (Token Limits)
When you run the daemon for the first time, it generates a configuration file at **`~/.claude_monitor.json`** in your home directory. 
You can edit this file to customize the token limits for your progress bars!
```json
{
    "limit_5h": 2500000,
    "limit_weekly": 10000000
}
```

## 2. Firmware Setup

The firmware targets the ESP32-C3 SuperMini with an I2C SH1106 128x64 OLED display.

### Wiring
- **SDA**: GPIO8
- **SCL**: GPIO9
- **VCC**: 3.3V
- **GND**: GND

### Build and Upload
1. Navigate to the `firmware/` directory:
   ```bash
   cd firmware
   ```
2. Upload to the ESP32-C3:
   ```bash
   pio run -t upload
   ```

### First Boot (Wi-Fi Provisioning)
On first boot, if the ESP32 cannot connect to a known Wi-Fi network, it will spawn an Access Point named **`Claude-Monitor-Setup`**.
1. Connect to this AP using the password: **`password123`**.
2. If a login page does not automatically appear on your phone/PC, open a web browser and navigate to **`http://192.168.4.1`**.
3. Select your home Wi-Fi and enter your credentials.

## API Reference

The desktop daemon exposes a single endpoint on port `8080`:

**`GET /stats`**
```json
{
  "total": 125000,
  "cost": 1.25,
  "5h_total": 45000,
  "5h_cost": 0.45,
  "weekly_total": 125000,
  "weekly_cost": 1.25,
  "limit_5h": 2500000,
  "limit_weekly": 10000000,
  "active": true
}
```
- The ESP32 parses this JSON to draw the token progress bars and determine whether to show the active dashboard or the idle screensaver.
