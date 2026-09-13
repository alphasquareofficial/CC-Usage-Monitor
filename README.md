# Claude Code Usage Monitor

A small desk device that shows how much of your Claude Code usage limits you have left,
so you can see it without typing `/usage`.

An ESP32 with a 128x64 OLED sits on your desk. A daemon on your computer reads your real
usage and serves it over your local network. The device finds the daemon by itself and
polls it every 5 seconds.

```
 +--------------------------------+      +--------------------------------+
 |             5-HOUR             |      |             WEEKLY             |
 |  ----------------------------  |      |  ----------------------------  |
 |                                |      |                                |
 |  [####################     ]   |      |  [##                      ]    |
 |                                |      |                                |
 |  LIVE      59%         3h 37m  |      |  LIVE       6%         6d 19h  |
 +--------------------------------+      +--------------------------------+
```

The screen alternates between the two limits every 5 seconds. `LIVE` / `IDLE` shows
whether Claude Code is currently running, the centre is percent used, and the right is
time until that limit resets. Once Claude Code has been idle for 15 minutes the screen
gives way to an animated pixel-art mascot that blinks at you, surfacing the dashboard for
5 seconds out of every 30 so you can still glance at your numbers.

**The numbers are real, not estimated.** The daemon reads the same endpoint Claude Code's
own `/usage` command uses, so the bars match what `/usage` reports. There is nothing to
calibrate.

---

## What you need

| Part | Notes | Approx. cost |
|---|---|---|
| ESP32-C3 SuperMini | Any ESP32 works with a config change - see [Using a different board](#using-a-different-board) | $3 |
| 0.96" 128x64 I2C OLED | SH1106 or SSD1306 controller, **4-pin I2C** (not 7-pin SPI) | $3 |
| 4 jumper wires | Female-to-female if your board has headers | $1 |
| USB-C cable | Must be a **data** cable, not charge-only | - |

Total is around $7. No soldering is needed if both boards come with headers pre-attached.

You will also need a computer running Claude Code, on the **same Wi-Fi network** as the
device, with [Python 3.9+](https://www.python.org/downloads/) and
[PlatformIO](https://platformio.org/install/cli) installed.

> **2.4GHz Wi-Fi only.** ESP32 chips cannot see 5GHz networks. If your router publishes
> both bands under one name, you may need to temporarily split them to get the device
> connected.

---

## Wiring

Four wires, OLED to ESP32:

| OLED pin | ESP32-C3 pin | Wire |
|---|---|---|
| `VCC` (or `VDD`) | `3V3` | Red |
| `GND` | `GND` | Black |
| `SDA` | `GPIO8` | Blue |
| `SCL` (or `SCK`) | `GPIO9` | Yellow |

```
   ESP32-C3 SuperMini            0.96" OLED
   +------------------+         +-------------+
   |              3V3 |---------| VCC         |
   |              GND |---------| GND         |
   |           GPIO8  |---------| SDA         |
   |           GPIO9  |---------| SCL         |
   +------------------+         +-------------+
```

**Connect VCC to 3.3V, not 5V.** Most of these OLED modules are 3.3V parts and the
ESP32-C3's I/O is 3.3V.

If your board labels pins differently, the two data pins are set in
[`firmware/src/main.cpp`](firmware/src/main.cpp) as `I2C_SDA` and `I2C_SCL` - change them
to match.

---

## Build it

### Step 1 - Start the daemon

On the computer where you run Claude Code:

```bash
git clone <this-repo>
cd "CC Usage Monitor"
pip install -r host/requirements.txt
python host/claude_monitor_daemon.py
```

You should see:

```
Building initial usage index...
Live usage API reachable -- reporting real utilization.
Starting server on port 8080...
```

Check it works:

```bash
curl http://localhost:8080/stats
```

`"source": "api"` in the output means it is reading your real usage. (`"source": "local"`
means it fell back to estimating from local logs - see [Troubleshooting](#the-daemon-says-source-local).)

The daemon registers itself to start automatically on login: Registry `Run` key on
Windows, a LaunchAgent on macOS, a systemd user service on Linux.

> **Allow it through your firewall.** The device needs to reach port 8080 on your
> computer. On Windows you will usually get a prompt the first time - choose **Private
> networks**. Without this the device will never find the daemon.

### Step 2 - Flash the firmware

Plug the ESP32 into your computer by USB, then:

```bash
cd firmware
pio run -t upload
```

PlatformIO downloads the toolchain and libraries on the first run, which takes a few
minutes. If the port is not detected automatically, list ports with `pio device list` and
pass it explicitly:

```bash
pio run -t upload --upload-port COM5        # Windows
pio run -t upload --upload-port /dev/ttyUSB0 # Linux
```

To watch the device's log output: `pio device monitor`.

### Step 3 - Put it on your Wi-Fi

On first boot the device has no network, so it creates its own:

1. The screen shows `Connect to AP: Claude-Monitor-Setup`.
2. On your phone or laptop, join the Wi-Fi network **`Claude-Monitor-Setup`**, password
   **`password123`**.
3. A setup page should open automatically. If not, browse to **`http://192.168.4.1`**.
4. Pick your home Wi-Fi, enter its password, and save. The device reboots and connects.

Credentials are stored on the device, so this is a one-time step. The portal times out
after 3 minutes and the device retries its saved network.

> **Change the portal password** in [`firmware/src/main.cpp`](firmware/src/main.cpp)
> (search for `password123`) before building, if you would rather not use the default.
> It only protects the one-time setup window, but it is trivial to change.

That is it. Within about 10 seconds of booting, the device finds the daemon over mDNS and
starts showing your usage.

---

## Troubleshooting

### The screen is blank
- Check `VCC` is on **3.3V** and `GND` is connected.
- Swap `SDA` and `SCL` - these are mislabelled on some modules.
- Confirm the module is the **I2C** version (4 pins). A 7-pin module is SPI and will not
  work with this wiring.
- Some modules use I2C address `0x3D` rather than the default `0x3C`. Run an I2C scanner
  sketch to check.

### Text runs off the edge of the screen
Your panel's controller does not match the one configured. SH1106 controllers drive 132
columns behind a 128px glass, and modules differ in which columns are wired. Change the
constructor at the top of [`firmware/src/main.cpp`](firmware/src/main.cpp) and re-flash:

```cpp
U8G2_SH1106_128X64_NONAME_F_SW_I2C  u8g2(U8G2_R0, ...);  // default
U8G2_SH1106_128X64_VCOMH0_F_SW_I2C  u8g2(U8G2_R0, ...);  // alternate SH1106 mapping
U8G2_SSD1306_128X64_NONAME_F_SW_I2C u8g2(U8G2_R0, ...);  // if yours is an SSD1306
```

The layout keeps a 4px margin on every edge, so small offsets are absorbed. If content is
still cut, you have a hard column-offset mismatch and one of the above will fix it.

### The screen says "Searching daemon..." forever
The device is on Wi-Fi but cannot find the daemon.
- Is the daemon actually running? `curl http://localhost:8080/stats` on the computer.
- Are both on the **same network**? A "Guest" SSID or client isolation will block this.
- **Firewall.** This is the most common cause. Allow Python (or port 8080) on private
  networks.
- Some routers block mDNS. Check the daemon is discoverable:
  ```bash
  # macOS
  dns-sd -B _http._tcp
  # Linux
  avahi-browse -rt _http._tcp
  ```
- As a workaround, skip discovery entirely by hard-coding your computer's LAN IP in
  `resolveHost()` in [`firmware/src/main.cpp`](firmware/src/main.cpp):
  ```cpp
  hostIP = "192.168.1.42";   // your computer's IP
  return;
  ```

### The screen says "Host Offline"
The device found the daemon once and then lost it. Usually the daemon stopped, the
computer slept, or its IP changed. The device retries every 10 seconds and recovers on
its own.

### The daemon says `"source": "local"`
It could not reach the usage API and is estimating from your local logs instead. Common
causes: no internet, or you are not logged in to Claude Code (`claude` then `/login`).
The estimate is self-calibrating and still useful - see [Accuracy](#accuracy).

### Upload fails with "could not open port"
- Close any serial monitor (`pio device monitor`, Arduino IDE) holding the port.
- Use a **data** USB cable - charge-only cables are a frequent culprit.
- Some boards need manual bootloader entry: hold `BOOT`, tap `RESET`, release `BOOT`,
  then upload.
- On Linux, add yourself to the `dialout` group: `sudo usermod -a -G dialout $USER`
  (log out and back in).

---

## Using a different board

The project targets the ESP32-C3 SuperMini, but any ESP32 with Wi-Fi works. Edit
[`firmware/platformio.ini`](firmware/platformio.ini):

```ini
[env:lolin_c3_mini]      ; e.g. esp32dev, nodemcu-32s, seeed_xiao_esp32c3
board = lolin_c3_mini
```

Then set `I2C_SDA` / `I2C_SCL` in [`firmware/src/main.cpp`](firmware/src/main.cpp) to
your board's I2C pins - classic ESP32 dev boards usually use GPIO21 (SDA) and GPIO22
(SCL).

For a different display size, adjust `SCREEN_W` / `SCREEN_H` and the u8g2 constructor.
The layout is drawn from those constants and a `MARGIN`, so it adapts rather than being
hard-coded.

---

## How it works

```
  ~/.claude/projects/**/*.jsonl  ─┐
                                  ├─>  daemon (Python)  ─── HTTP :8080 ──>  ESP32 ──> OLED
  api.anthropic.com/api/oauth/usage ┘        │
                                             └── mDNS: claudemonitor.local
```

**The daemon** ([`host/claude_monitor_daemon.py`](host/claude_monitor_daemon.py)) does all
the work. It fetches your real utilization once a minute, tails your local Claude Code
logs every 2 seconds as a backup, advertises itself over mDNS, and serves a cached JSON
snapshot on port 8080. Requests return in about 2ms no matter how large your logs get.

**The firmware** ([`firmware/src/main.cpp`](firmware/src/main.cpp)) is deliberately dumb:
find the daemon, poll `/stats`, draw what came back. All the logic lives on the computer,
so changing how usage is calculated never requires re-flashing.

### Accuracy

The percentages come from Anthropic's own usage endpoint, authenticated with the OAuth
token Claude Code already keeps at `~/.claude/.credentials.json`. The daemon only reads
this file; it never transmits the token anywhere except to Anthropic.

> This endpoint is internal, not a documented public API. It works today, but it is not a
> contract Anthropic has committed to keeping stable. The fallback below exists so the
> monitor keeps working if it ever changes.

When the API cannot be reached, the daemon estimates from your local `.jsonl` logs. That
path is not naive, and the details matter if you are writing your own tooling:

- **Records are deduplicated.** Claude Code writes one log line per streaming update, so
  the same assistant message appears many times carrying identical cumulative usage.
  Counting every line inflates totals by 2-4x. Records are keyed on
  `(message.id, requestId)` and only the first occurrence counts.
- **`<synthetic>` messages are skipped.** They are generated locally for API errors and
  interrupts, and never hit the API.
- **Cache reads are weighted, not ignored.** They are roughly 97% of all tokens but bill
  at 0.1x an input token, so they are counted at that weight. Cache writes count at 1.25x
  and output at 5x, matching how each is billed.
- **The 5-hour window is a block, not a sliding window.** It opens on the first message
  after a gap, runs five hours, then resets to zero.
- **The weekly window is anchored, not rolling.** A rolling 7-day sum badly over-reports
  just after a reset - on real data, a 4-hour-old week read 64.1M rolling against a true
  4.3M.
- **The fallback calibrates itself.** Whenever the API is reachable, the daemon knows both
  the true percentage and its own token estimate, so it derives your actual limits and
  saves them. You should never need to calibrate by hand.

Run the test suite with `cd host && python -m pytest test_daemon.py`.

---

## Configuration

The daemon writes `~/.claude_monitor.json` on first run. **You do not need to edit this**
- it is populated automatically and only affects the offline fallback.

```json
{
    "limit_5h": 9011825,
    "limit_weekly": 109225650,
    "token_mode": "weighted",
    "weekly_reset": "2026-09-20T06:30:00+05:30"
}
```

| Key | Meaning |
|---|---|
| `limit_5h`, `limit_weekly` | Learned automatically from the API. Used only by the fallback. |
| `token_mode` | `weighted` (default), `billable`, or `all` - see below. |
| `weekly_reset` | One known weekly reset instant, ISO 8601 with UTC offset. `/usage` names the next one. Used only by the fallback. |

`token_mode` sets how each usage component counts in the fallback estimate:

| Mode | input | cache write | cache read | output |
|---|---|---|---|---|
| `weighted` (default) | 1.0 | 1.25 | 0.1 | 5.0 |
| `billable` | 1.0 | 1.0 | 0.0 | 1.0 |
| `all` | 1.0 | 1.0 | 1.0 | 1.0 |

Supply your own with `"token_weights": [1.0, 1.25, 0.1, 5.0]`.

If you are permanently offline, calibrate the fallback by hand - run `/usage` in Claude
Code and pass the two percentages:

```bash
python host/claude_monitor_daemon.py --calibrate 44 4
```

---

## API reference

`GET http://claudemonitor.local:8080/stats`

```json
{
  "pct_5h": 59,
  "pct_weekly": 6,
  "progress_5h": 0.59,
  "progress_weekly": 0.06,
  "str_5h": "59% - 3h 37m",
  "str_weekly": "6% - 6d 19h",
  "reset_str_5h": "3h 37m",
  "reset_str_weekly": "6d 19h",
  "reset_in": 13048,
  "weekly_reset_in": 588787,
  "active": true,
  "source": "api",
  "5h_total": 4980092,
  "weekly_total": 4980092,
  "5h_cost": 24.9,
  "weekly_cost": 24.9,
  "limit_5h": 9011825,
  "limit_weekly": 109225650,
  "updated_at": 1789277212
}
```

| Field | Meaning |
|---|---|
| `pct_5h`, `pct_weekly` | Percent of each limit used, 0-100+ |
| `progress_*` | The same as a 0.0-1.0 fraction, clamped at 1.0, for drawing bars |
| `str_*` | Pre-formatted display strings |
| `reset_str_*` | Just the countdown, e.g. `3h 37m` |
| `reset_in`, `weekly_reset_in` | Seconds until each limit resets |
| `active` | Whether Claude Code has written a log in the last 15 minutes |
| `source` | `api` for real utilization, `local` for the offline estimate |
| `*_total`, `*_cost` | Token counts and USD cost from local logs - always present |
| `updated_at` | Unix time of the last refresh |

Since it is plain JSON over HTTP, you can drive anything else from it - a menu-bar app, a
Stream Deck key, a smart bulb that turns red near your limit.

---

## Repository layout

```
host/
  claude_monitor_daemon.py   Daemon: usage API client, log tailer, HTTP server, mDNS
  test_daemon.py             Test suite
  requirements.txt
  build.py                   Builds a standalone .exe with PyInstaller
firmware/
  platformio.ini             Board and library definitions
  src/main.cpp               Wi-Fi, mDNS discovery, HTTP polling, OLED rendering
```

## License

[MIT](LICENSE). Do what you like with it.
