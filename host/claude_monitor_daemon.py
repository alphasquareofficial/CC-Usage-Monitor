import os
import sys
import json
import glob
import time
import datetime
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
from zeroconf import ServiceInfo, Zeroconf

PORT = 8080
SERVICE_NAME = "claudemonitor"
SERVICE_TYPE = "_http._tcp.local."
SONNET_INPUT_PRICE_PER_M = 3.00
SONNET_OUTPUT_PRICE_PER_M = 15.00

def get_config():
    config_path = os.path.expanduser("~/.claude_monitor.json")
    default_config = {
        "limit_5h": 2500000,
        "limit_weekly": 10000000
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

def get_all_session_files():
    claude_dir = os.path.expanduser("~/.claude/projects")
    search_pattern = os.path.join(claude_dir, "**", "*.jsonl")
    return glob.glob(search_pattern, recursive=True)

def parse_session_stats(filepaths):
    total_input = 0
    total_output = 0
    total_5h_input = 0
    total_5h_output = 0
    total_weekly_input = 0
    total_weekly_output = 0
    active = False
    
    current_time = time.time()
    
    config = get_config()
    
    if not filepaths:
        return {
            "total": 0, "cost": 0.0,
            "5h_total": 0, "5h_cost": 0.0,
            "weekly_total": 0, "weekly_cost": 0.0,
            "limit_5h": config["limit_5h"],
            "limit_weekly": config["limit_weekly"],
            "active": False
        }
    
    for filepath in filepaths:
        if not os.path.exists(filepath):
            continue
            
        mtime = os.path.getmtime(filepath)
        if (current_time - mtime) < 15 * 60:
            active = True
            
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                        
                        timestamp = None
                        if isinstance(data, dict):
                            if "timestamp" in data:
                                timestamp = data.get("timestamp")
                            elif "created_at" in data:
                                timestamp = data.get("created_at")
                        
                        if timestamp is None:
                            timestamp = mtime
                            
                        if isinstance(timestamp, str):
                            try:
                                ts_str = timestamp.replace("Z", "+00:00")
                                timestamp = datetime.datetime.fromisoformat(ts_str).timestamp()
                            except Exception:
                                timestamp = mtime
                                
                        # Handle milliseconds vs seconds timestamp
                        if isinstance(timestamp, (int, float)) and timestamp > 1e11:
                            timestamp = timestamp / 1000.0
                            
                        in_5h = (current_time - timestamp) <= 5 * 3600
                        in_weekly = (current_time - timestamp) <= 7 * 24 * 3600
                        
                        # Claude Code JSONL logs structure can vary, typically looking for token counts
                        # Let's search recursively for input_tokens and output_tokens
                        def find_tokens(obj):
                            in_t = 0
                            out_t = 0
                            if isinstance(obj, dict):
                                if "input_tokens" in obj:
                                    in_t += obj.get("input_tokens", 0)
                                if "output_tokens" in obj:
                                    out_t += obj.get("output_tokens", 0)
                                for v in obj.values():
                                    res = find_tokens(v)
                                    in_t += res[0]
                                    out_t += res[1]
                            elif isinstance(obj, list):
                                for item in obj:
                                    res = find_tokens(item)
                                    in_t += res[0]
                                    out_t += res[1]
                            return in_t, out_t
                        
                        i, o = find_tokens(data)
                        total_input += i
                        total_output += o
                        if in_5h:
                            total_5h_input += i
                            total_5h_output += o
                        if in_weekly:
                            total_weekly_input += i
                            total_weekly_output += o
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            print(f"Error reading file {filepath}: {e}")
        
    cost_total = (total_input / 1_000_000.0) * SONNET_INPUT_PRICE_PER_M + (total_output / 1_000_000.0) * SONNET_OUTPUT_PRICE_PER_M
    cost_5h = (total_5h_input / 1_000_000.0) * SONNET_INPUT_PRICE_PER_M + (total_5h_output / 1_000_000.0) * SONNET_OUTPUT_PRICE_PER_M
    cost_weekly = (total_weekly_input / 1_000_000.0) * SONNET_INPUT_PRICE_PER_M + (total_weekly_output / 1_000_000.0) * SONNET_OUTPUT_PRICE_PER_M
    
    return {
        "total": total_input + total_output,
        "cost": round(cost_total, 4),
        "5h_total": total_5h_input + total_5h_output,
        "5h_cost": round(cost_5h, 4),
        "weekly_total": total_weekly_input + total_weekly_output,
        "weekly_cost": round(cost_weekly, 4),
        "limit_5h": config["limit_5h"],
        "limit_weekly": config["limit_weekly"],
        "active": active
    }

class StatsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/stats':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            files = get_all_session_files()
            stats = parse_session_stats(files)
            self.wfile.write(json.dumps(stats).encode('utf-8'))
        else:
            self.send_response(404)
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
    
    server_address = ('', PORT)
    httpd = HTTPServer(server_address, StatsHandler)
    
    print(f"Starting server on port {PORT}...")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        zc.unregister_service(info)
        zc.close()

if __name__ == '__main__':
    run()
