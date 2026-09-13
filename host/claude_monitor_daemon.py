import os
import sys
import json
import glob
import math
import time
import datetime
import socket
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from zeroconf import ServiceInfo, Zeroconf

PORT = 8080
SERVICE_NAME = "claudemonitor"
SERVICE_TYPE = "_http._tcp.local."
# USD per million tokens, (input, output). Cache writes bill at 1.25x input and
# cache reads at 0.1x input.
PRICES_PER_M = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
DEFAULT_PRICE_PER_M = PRICES_PER_M["claude-opus-5"]
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1

# Starting points only. Anthropic does not publish these as token counts and
# nothing local reports them, so run `--calibrate` against what /usage shows to
# pin them to this account's actual plan.
DEFAULT_LIMIT_5H = 8000000
DEFAULT_LIMIT_WEEKLY = 80000000

WEEK_SECONDS = 7 * 24 * 3600
BLOCK_SECONDS = 5 * 3600
IDLE_SECONDS = 15 * 60
REFRESH_INTERVAL = 2.0

# The authoritative source: the endpoint Claude Code's own /usage reads, via the
# OAuth token Claude Code maintains. Utilization comes back as a percentage, so
# no calibration is involved. Polled far less often than the local logs -- the
# figures move over minutes, and this is a shared service.
USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"
CREDENTIALS_PATH = "~/.claude/.credentials.json"
API_POLL_INTERVAL = 60.0
API_STALE_AFTER = 15 * 60

# How each usage component counts toward the limit bars, in input-token
# equivalents. Cache reads are real traffic but are billed at a tenth of an
# input token, and cache writes at 1.25x, so weighting them tracks the limit
# far better than either ignoring cache reads or counting them at face value.
TOKEN_MODES = {
    # input, cache_creation, cache_read, output
    "weighted": (1.0, 1.25, 0.1, 5.0),
    "billable": (1.0, 1.0, 0.0, 1.0),
    "all": (1.0, 1.0, 1.0, 1.0),
}
DEFAULT_TOKEN_MODE = "weighted"

def get_weights(config):
    mode = config.get("token_mode", DEFAULT_TOKEN_MODE)
    custom = config.get("token_weights")
    if isinstance(custom, (list, tuple)) and len(custom) == 4:
        try:
            return tuple(float(w) for w in custom)
        except (TypeError, ValueError):
            pass
    return TOKEN_MODES.get(mode, TOKEN_MODES[DEFAULT_TOKEN_MODE])

def weigh(parts, weights):
    return int(sum(p * w for p, w in zip(parts, weights)))

def cost_of(model, i, cc, cr, o):
    in_price, out_price = PRICES_PER_M.get(model, DEFAULT_PRICE_PER_M)
    return (
        i * in_price
        + cc * in_price * CACHE_WRITE_MULTIPLIER
        + cr * in_price * CACHE_READ_MULTIPLIER
        + o * out_price
    ) / 1_000_000.0

def get_config():
    config_path = os.path.expanduser("~/.claude_monitor.json")
    default_config = {
        "limit_5h": DEFAULT_LIMIT_5H,
        "limit_weekly": DEFAULT_LIMIT_WEEKLY,
        "token_mode": DEFAULT_TOKEN_MODE,
    }
    if not os.path.exists(config_path):
        try:
            with open(config_path, "w") as f:
                json.dump(default_config, f, indent=4)
        except Exception:
            pass
        return default_config

    try:
        with open(config_path, "r") as f:
            user_config = json.load(f)
            return {**default_config, **user_config}
    except Exception:
        return default_config

def format_tokens(tokens):
    if tokens >= 1000000:
        return f"{tokens / 1000000.0:.1f}M"
    elif tokens >= 1000:
        return f"{tokens / 1000.0:.1f}k"
    return str(tokens)

def format_duration(seconds):
    """Compact countdown for the 128x64 OLED, e.g. "3h 45m" or "12m"."""
    if seconds <= 0:
        return "now"
    hours, minutes = divmod(int(seconds) // 60, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"

def get_all_session_files():
    claude_dir = os.path.expanduser("~/.claude/projects")
    search_pattern = os.path.join(claude_dir, "**", "*.jsonl")
    return glob.glob(search_pattern, recursive=True)


class UsageIndex:
    """Incrementally tails the Claude Code JSONL logs.

    Claude Code appends a log line per streaming update, so the same assistant
    message (same message.id / requestId) shows up many times carrying identical
    cumulative usage. Counting every line double-counts roughly half the tokens,
    so each record is keyed and only its first occurrence is billed.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.offsets = {}
        self.seen = {}
        self.events = []
        self.last_file_mtime = 0.0

    def _parse_timestamp(self, data, fallback):
        timestamp = data.get("timestamp", data.get("created_at"))

        if isinstance(timestamp, str):
            try:
                timestamp = datetime.datetime.fromisoformat(
                    timestamp.replace("Z", "+00:00")
                ).timestamp()
            except Exception:
                timestamp = None

        if not isinstance(timestamp, (int, float)):
            return fallback

        # Handle milliseconds vs seconds timestamp
        if timestamp > 1e11:
            timestamp = timestamp / 1000.0
        return float(timestamp)

    def refresh(self):
        self.refresh_paths(get_all_session_files())

    def refresh_paths(self, filepaths):
        for filepath in filepaths:
            try:
                stat = os.stat(filepath)
            except OSError:
                continue

            self.last_file_mtime = max(self.last_file_mtime, stat.st_mtime)
            offset = self.offsets.get(filepath, 0)

            if stat.st_size < offset:
                # Truncated or rotated: start this file over.
                offset = 0
            elif stat.st_size == offset:
                continue

            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(offset)
                    chunk = f.read()
                    new_offset = f.tell()
            except OSError as e:
                print(f"Error reading file {filepath}: {e}")
                continue

            # The file may be mid-write, so hold back a trailing partial line
            # and let the next pass parse it once it is complete.
            if chunk and not chunk.endswith("\n"):
                cut = chunk.rfind("\n")
                if cut == -1:
                    continue
                new_offset -= len(chunk[cut + 1:].encode("utf-8"))
                chunk = chunk[:cut + 1]

            self._ingest(chunk, stat.st_mtime)
            self.offsets[filepath] = new_offset

    def _ingest(self, chunk, mtime):
        fallback_ts = mtime
        for line in chunk.splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue

            timestamp = self._parse_timestamp(data, fallback_ts)
            fallback_ts = timestamp

            message = data.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue

            # "<synthetic>" messages are generated locally (API error placeholders,
            # interrupts) and never hit the API, so they must not count.
            if message.get("model") == "<synthetic>":
                continue

            key = (message.get("id"), data.get("requestId"))
            if key == (None, None):
                key = ("uuid", data.get("uuid"), timestamp)

            with self.lock:
                if key in self.seen:
                    continue
                self.seen[key] = timestamp
                self.events.append((
                    timestamp,
                    message.get("model"),
                    int(usage.get("input_tokens", 0) or 0),
                    int(usage.get("cache_creation_input_tokens", 0) or 0),
                    int(usage.get("cache_read_input_tokens", 0) or 0),
                    int(usage.get("output_tokens", 0) or 0),
                ))

    def prune(self, now):
        cutoff = now - WEEK_SECONDS
        with self.lock:
            self.events = [e for e in self.events if e[0] >= cutoff]
            self.seen = {k: v for k, v in self.seen.items() if v >= cutoff}

    def current_week_start(self, config, now):
        """Start of the active weekly limit window.

        The weekly limit resets on a fixed 7-day cadence at a specific instant
        (Claude Code's /usage names it, e.g. "Resets Sep 20, 6:30am"), so it is
        an anchored window, not a rolling trailing week. Stepping back from any
        known reset instant in 7-day strides lands on the current window start.
        """
        anchor = config.get("weekly_reset")
        if not anchor:
            return now - WEEK_SECONDS
        try:
            anchor_ts = datetime.datetime.fromisoformat(anchor).timestamp()
        except (TypeError, ValueError):
            return now - WEEK_SECONDS
        # Number of whole weeks between the anchor and now, floored.
        weeks = math.floor((now - anchor_ts) / WEEK_SECONDS)
        return anchor_ts + weeks * WEEK_SECONDS

    def current_block_start(self, timestamps, now):
        """Start of the active 5-hour limit window.

        Claude's 5-hour limit is a block that opens on the first message after a
        gap and then runs for five hours; it is not a window that slides
        continuously, so usage drops to zero at the reset rather than trickling.
        """
        block_start = None
        for ts in sorted(timestamps):
            if block_start is None or ts - block_start >= BLOCK_SECONDS:
                block_start = ts - (ts % 3600)
        if block_start is None or now - block_start >= BLOCK_SECONDS:
            return None
        return block_start

    def snapshot(self, config=None):
        now = time.time()
        self.prune(now)
        if config is None:
            config = get_config()
        limit_5h = config.get("limit_5h", 2500000)
        limit_weekly = config.get("limit_weekly", 10000000)

        with self.lock:
            events = list(self.events)

        block_start = self.current_block_start([e[0] for e in events], now)
        week_start = self.current_week_start(config, now)
        weights = get_weights(config)

        blk = [0, 0, 0, 0]
        wk = [0, 0, 0, 0]
        cost_5h = cost_weekly = 0.0
        latest = 0.0

        for ts, model, i, cc, cr, o in events:
            latest = max(latest, ts)
            if ts >= week_start:
                wk[0] += i; wk[1] += cc; wk[2] += cr; wk[3] += o
                cost_weekly += cost_of(model, i, cc, cr, o)
            if block_start is not None and block_start <= ts < block_start + BLOCK_SECONDS:
                blk[0] += i; blk[1] += cc; blk[2] += cr; blk[3] += o
                cost_5h += cost_of(model, i, cc, cr, o)

        t_5h = weigh(blk, weights)
        t_weekly = weigh(wk, weights)

        active = (now - max(latest, self.last_file_mtime)) < IDLE_SECONDS

        live = API.get()
        if live:
            # Real utilization straight from Anthropic -- no limits, no weights.
            source = "api"
            pct_5h_f = live["pct_5h"]
            pct_weekly_f = live["pct_weekly"]
            reset_in = int(live["reset_5h"] - now) if live["reset_5h"] else 0
            weekly_reset_in = int(live["reset_weekly"] - now) if live["reset_weekly"] else 0
        else:
            # Fallback: estimate from local token counts against learned limits.
            source = "local"
            pct_5h_f = (t_5h * 100.0 / limit_5h) if limit_5h > 0 else 0.0
            pct_weekly_f = (t_weekly * 100.0 / limit_weekly) if limit_weekly > 0 else 0.0
            reset_in = int(block_start + BLOCK_SECONDS - now) if block_start is not None else 0
            weekly_reset_in = int(week_start + WEEK_SECONDS - now)

        reset_in = max(reset_in, 0)
        weekly_reset_in = max(weekly_reset_in, 0)
        pct_5h = int(pct_5h_f)
        pct_weekly = int(pct_weekly_f)
        progress_5h = min(pct_5h_f / 100.0, 1.0)
        progress_weekly = min(pct_weekly_f / 100.0, 1.0)

        return {
            "total": t_weekly,
            "cost": round(cost_weekly, 4),
            "5h_total": t_5h,
            "5h_cost": round(cost_5h, 4),
            "weekly_total": t_weekly,
            "weekly_cost": round(cost_weekly, 4),
            "limit_5h": limit_5h,
            "limit_weekly": limit_weekly,
            "active": active,
            "pct_5h": pct_5h,
            "pct_weekly": pct_weekly,
            "progress_5h": round(progress_5h, 4),
            "progress_weekly": round(progress_weekly, 4),
            "str_5h": f"{pct_5h}% - {format_duration(reset_in)}",
            "str_weekly": f"{pct_weekly}% - {format_duration(weekly_reset_in)}",
            "reset_str_5h": format_duration(reset_in),
            "reset_str_weekly": format_duration(weekly_reset_in),
            "source": source,
            "reset_in": reset_in,
            "weekly_reset_in": weekly_reset_in,
            "updated_at": int(now),
        }


class UsageAPI:
    """Reads the authoritative utilization figures from Anthropic.

    This is the same endpoint Claude Code's own /usage command reads, using the
    OAuth token Claude Code already maintains on disk. It reports utilization as
    a percentage directly, so when it is reachable there is nothing to calibrate
    -- the local token estimate is only a fallback for when it is not.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.data = None
        self.error = None

    def _token(self):
        with open(os.path.expanduser(CREDENTIALS_PATH), "r", encoding="utf-8") as f:
            return json.load(f)["claudeAiOauth"]["accessToken"]

    @staticmethod
    def _parse_reset(value):
        if not value:
            return None
        try:
            return datetime.datetime.fromisoformat(value).timestamp()
        except (TypeError, ValueError):
            return None

    def fetch(self):
        try:
            token = self._token()
        except Exception as e:
            with self.lock:
                self.error = f"no credentials: {e}"
            return None

        request = urllib.request.Request(
            USAGE_API_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "claude-monitor-daemon/1.0",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read())
        except Exception as e:
            with self.lock:
                self.error = str(e)
            return None

        five = payload.get("five_hour") or {}
        week = payload.get("seven_day") or {}
        parsed = {
            "pct_5h": float(five.get("utilization") or 0.0),
            "pct_weekly": float(week.get("utilization") or 0.0),
            "reset_5h": self._parse_reset(five.get("resets_at")),
            "reset_weekly": self._parse_reset(week.get("resets_at")),
            "fetched_at": time.time(),
        }
        with self.lock:
            self.data = parsed
            self.error = None
        return parsed

    def get(self):
        """Last good reading, or None once it has gone too stale to trust."""
        with self.lock:
            data = self.data
        if not data:
            return None
        if time.time() - data["fetched_at"] > API_STALE_AFTER:
            return None
        return data


INDEX = UsageIndex()
API = UsageAPI()
_snapshot = None
_snapshot_lock = threading.Lock()

def get_snapshot():
    with _snapshot_lock:
        if _snapshot is not None:
            return _snapshot
    # Nothing cached yet (a request beat the refresher): build it inline once.
    API.fetch()
    INDEX.refresh()
    return INDEX.snapshot()

def learn_limits(snap, live):
    """Keep the offline fallback honest using the API as a reference.

    While the API is reachable we know both the real utilization and our own
    token estimate, so the implied limit falls out of the two. Persisting it
    means that if the API later goes away, the local estimate is already
    calibrated -- nobody has to run --calibrate by hand.
    """
    if not live:
        return
    config = get_config()
    changed = False
    for pct_key, limit_key, measured in (
        ("pct_5h", "limit_5h", snap["5h_total"]),
        ("pct_weekly", "limit_weekly", snap["weekly_total"]),
    ):
        pct = live[pct_key]
        # Below ~10% the integer rounding makes the implied limit too noisy.
        if pct < 10 or measured <= 0:
            continue
        implied = int(measured / (pct / 100.0))
        current = config.get(limit_key, 0)
        if not current or abs(implied - current) / float(current) > 0.05:
            config[limit_key] = implied
            changed = True
    if changed:
        try:
            with open(os.path.expanduser("~/.claude_monitor.json"), "w") as f:
                json.dump(config, f, indent=4)
        except OSError:
            pass


def refresh_loop():
    global _snapshot
    last_api_poll = 0.0
    while True:
        try:
            if time.time() - last_api_poll >= API_POLL_INTERVAL:
                last_api_poll = time.time()
                API.fetch()

            INDEX.refresh()
            snap = INDEX.snapshot()
            learn_limits(snap, API.get())
            with _snapshot_lock:
                _snapshot = snap
        except Exception as e:
            print(f"Refresh error: {e}")
        time.sleep(REFRESH_INTERVAL)

def parse_session_stats(filepaths):
    """Stateless one-shot parse over the given files (used by tests)."""
    if isinstance(filepaths, str):
        filepaths = [filepaths]
    index = UsageIndex()
    index.refresh_paths(filepaths or [])
    return index.snapshot()

class StatsHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path == '/stats':
            body = json.dumps(get_snapshot()).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress logging
        pass

def register_autostart():
    system = sys.platform
    executable = sys.executable if not getattr(sys, 'frozen', False) else sys.executable
    script_path = os.path.abspath(__file__)

    if getattr(sys, 'frozen', False):
        command = f'"{executable}"'
    else:
        command = f'"{executable}" "{script_path}"'

    if system == "win32":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, "ClaudeMonitorDaemon", 0, winreg.REG_SZ, command)
            winreg.CloseKey(key)
        except Exception as e:
            print(f"Failed to register Windows autostart: {e}")

    elif system == "darwin":
        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claudemonitor.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>{executable}</string>
"""
        if not getattr(sys, 'frozen', False):
            plist_content += f"        <string>{script_path}</string>\n"

        plist_content += """    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>"""
        plist_path = os.path.expanduser("~/Library/LaunchAgents/com.claudemonitor.daemon.plist")
        os.makedirs(os.path.dirname(plist_path), exist_ok=True)
        with open(plist_path, "w") as f:
            f.write(plist_content)

    elif system == "linux":
        service_content = f"""[Unit]
Description=Claude Monitor Daemon
After=network.target

[Service]
ExecStart={command}
Restart=always

[Install]
WantedBy=default.target
"""
        service_path = os.path.expanduser("~/.config/systemd/user/claude-monitor.service")
        os.makedirs(os.path.dirname(service_path), exist_ok=True)
        with open(service_path, "w") as f:
            f.write(service_content)
        # We ideally should run `systemctl --user enable claude-monitor.service` here
        try:
            import subprocess
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            subprocess.run(["systemctl", "--user", "enable", "claude-monitor.service"], check=False)
        except Exception:
            pass

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

def start_mdns():
    zeroconf = Zeroconf()
    ip = get_local_ip()
    info = ServiceInfo(
        SERVICE_TYPE,
        f"{SERVICE_NAME}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton(ip)],
        port=PORT,
        properties={},
        server=f"{SERVICE_NAME}.local.",
    )
    zeroconf.register_service(info)
    return zeroconf, info

def run():
    register_autostart()

    zc, info = start_mdns()

    print("Building initial usage index...")
    if API.fetch():
        print("Live usage API reachable -- reporting real utilization.")
    else:
        print(f"Live usage API unavailable ({API.error}); using local estimate.")
    threading.Thread(target=refresh_loop, daemon=True).start()

    server_address = ('', PORT)
    httpd = ThreadingHTTPServer(server_address, StatsHandler)
    httpd.daemon_threads = True

    print(f"Starting server on port {PORT}...")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        zc.unregister_service(info)
        zc.close()

def calibrate(pct_5h, pct_weekly):
    """Derive real limits from the percentages Claude Code's /usage reports.

    Anthropic publishes the limits as percentages, not token counts, and nothing
    on disk records them -- but if /usage says you are at N%, then the limit is
    whatever we currently measure divided by N/100.
    """
    INDEX.refresh()
    snap = INDEX.snapshot()
    config_path = os.path.expanduser("~/.claude_monitor.json")
    config = get_config()

    for label, key, pct, measured in (
        ("5h", "limit_5h", pct_5h, snap["5h_total"]),
        ("weekly", "limit_weekly", pct_weekly, snap["weekly_total"]),
    ):
        if pct is None:
            continue
        if pct <= 0:
            print(f"  {label}: /usage reads 0% -- nothing to calibrate against yet.")
            continue
        limit = int(measured / (pct / 100.0))
        print(f"  {label}: measured {measured:,} at {pct}%  ->  {key} = {limit:,}")
        config[key] = limit

    config["token_mode"] = config.get("token_mode", DEFAULT_TOKEN_MODE)
    with open(config_path, "w") as f:
        json.dump(config, f, indent=4)
    print(f"\nWrote {config_path}. Restart the daemon to pick it up.")

if __name__ == '__main__':
    if "--calibrate" in sys.argv:
        args = sys.argv[sys.argv.index("--calibrate") + 1:]
        if not args:
            print("Usage: claude_monitor_daemon.py --calibrate <5h_pct> [weekly_pct]")
            print("Read both numbers off `claude` -> /usage, then pass them here.")
            sys.exit(1)
        five = float(args[0])
        week = float(args[1]) if len(args) > 1 else None
        calibrate(five, week)
    else:
        run()
