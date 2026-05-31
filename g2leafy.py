import curses
import json
import os
import re
import sys
import time
import subprocess
import threading
import urllib.request
import urllib.parse
import uuid
import base64
import shutil
import textwrap
import socket
import copy
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

LOCAL_VERSION = "2.0.1"
AUTO_UPDATE = True
UPSTREAM_REPO = "Code-Leafy/G2rayXCodeLeafy"
RAW_BASE = f"https://raw.githubusercontent.com/{UPSTREAM_REPO}/refs/heads/main/"
UPDATE_CANDIDATES = ["g2leafy.py", "g2ray.py", "g2ray.sh"]

DONATE_WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbxbTxcCS6sl7HpASqssmr6c9wYL1gsE86fBjFHTcRs0sl0o-R5ZAmKJk-z_GaBBRqcsHw/exec"
DONATE_SECRET = ""
DONATE_IP = "20.207.70.99"
DONATE_HEARTBEAT_SEC = 240
DONATE_TTL_SEC = 720
DONATE_QUOTA_GRACE_SEC = 600

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
WWW_DIR = os.path.join(DATA_DIR, "www")

CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
UUID_FILE = os.path.join(DATA_DIR, "uuid.txt")
TOTAL_UPTIME_FILE = os.path.join(DATA_DIR, "total_uptime.txt")
TOTAL_TRAFFIC_FILE = os.path.join(DATA_DIR, "total_traffic.txt")
XRAY_LOG = os.path.join(LOG_DIR, "xray.log")
SYSTEM_LOG = os.path.join(LOG_DIR, "system.log")
XRAY_BIN = "/usr/local/bin/xray"
KEEPALIVE_FILE = os.path.join(DATA_DIR, "keepalive.touch")

XRAY_PORT = 443
SUB_PORT = 8080
API_PORT = 10085

for d in [DATA_DIR, LOG_DIR, WWW_DIR]:
    os.makedirs(d, exist_ok=True)

state_lock = threading.Lock()
settings_lock = threading.Lock()

state = {
    "total_down": 0, "total_up": 0, "uptime_sec": 0,
    "rx_hist": [], "tx_hist": [],
    "cpu_hist": [], "mem_hist": [],
    "ip": "...", "loc": "...", "is_xray_running": False,
    "sys_cpu": "N/A", "sys_mem": "N/A", "sys_disk": "N/A",
    "cpu_pct": 0.0, "mem_pct": 0.0, "conns": 0,
    "wake_ok": False, "wake_last": 0,
    "ports_ok": False,
    "donate_active": False, "donate_last": 0, "donate_msg": "",
}
settings = {}
nav_current = 1
cfg_sel = 0
engine_running = True
wake_lock_active = True
NCPU = os.cpu_count() or 1
last_saved_uptime = 0
last_saved_traffic = 0
log_scroll_offset = 0
log_search_term = ""

last_sub_state = {"traffic": -1, "uptime": -1, "nodes": ""}
ports_thread_active = False
ports_thread_lock = threading.Lock()

try:
    CODESPACE_NAME = os.environ.get("CODESPACE_NAME")
    if not CODESPACE_NAME:
        CODESPACE_NAME = subprocess.check_output(
            ["gh", "codespace", "list", "--limit", "1", "--json", "name", "--jq", ".[0].name"],
            text=True, stderr=subprocess.DEVNULL).strip()
except Exception:
    CODESPACE_NAME = os.uname().nodename

GITHUB_USER = os.environ.get("GITHUB_USER", CODESPACE_NAME.split('-')[0] if '-' in CODESPACE_NAME else "User")
SERVER_NAME = f"G2Leafy | {GITHUB_USER}"
PORT_DOMAIN = f"{CODESPACE_NAME}-{XRAY_PORT}.app.github.dev"
SUB_DOMAIN = f"{CODESPACE_NAME}-{SUB_PORT}.app.github.dev"

DEFAULT_CONFIG_IPS = [
    {"ip": PORT_DOMAIN, "name": "🍃 G2Leafy | Auto Best"},
]

def log_sys_err(msg):
    try:
        with open(SYSTEM_LOG, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except:
        pass

def fmt_gb(b):
    return f"{(b / 1073741824):.2f}"

def fmt_auto(b):
    if b < 1048576:
        return f"{(b / 1024):.0f} KB"
    elif b < 1073741824:
        return f"{(b / 1048576):.1f} MB"
    return f"{(b / 1073741824):.2f} GB"

def fmt_rate(bps):
    bits = bps * 8
    if bits < 1000:
        return f"{bits:.0f} bps"
    elif bits < 1_000_000:
        return f"{bits/1000:.0f} Kbps"
    elif bits < 1_000_000_000:
        return f"{bits/1_000_000:.0f} Mbps"
    return f"{bits/1_000_000_000:.0f} Gbps"

def fmt_hms(s):
    s = int(max(0, s))
    return f"{s//3600}h {(s%3600)//60:02d}m"

def is_unlimited(v):
    return v is None or v > 900000

def load_settings():
    global settings, wake_lock_active
    defaults = {
        "data_cap_gb": 999999,
        "sub_update_mins": 1,
        "wake_lock": True,
        "nodes": list(DEFAULT_CONFIG_IPS),
    }
    data = {}
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                data = json.load(f)
        except Exception as e:
            log_sys_err(f"load_settings json: {e}")
            data = {}
    with settings_lock:
        settings = {**defaults, **data}
        if not settings.get("nodes"):
            settings["nodes"] = list(DEFAULT_CONFIG_IPS)
        wake_lock_active = settings.get("wake_lock", True)

def save_settings():
    with settings_lock:
        settings["wake_lock"] = wake_lock_active
        data_to_save = copy.deepcopy(settings)
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data_to_save, f, indent=2)
    except Exception as e:
        log_sys_err(f"save_settings: {e}")

def get_uuid():
    if not os.path.exists(UUID_FILE):
        try:
            with open(UUID_FILE, "w") as f:
                f.write(str(uuid.uuid4()))
        except Exception as e:
            log_sys_err(f"get_uuid write: {e}")
    try:
        with open(UUID_FILE) as f:
            return f.read().strip()
    except Exception as e:
        log_sys_err(f"get_uuid read: {e}")
        return str(uuid.uuid4())

def check_xray_running():
    try:
        out = subprocess.check_output(["pgrep", "-x", "xray"], text=True, stderr=subprocess.DEVNULL)
        return bool(out.strip())
    except Exception as e:
        log_sys_err(f"check_xray_running: {e}")
        return False

def check_port_listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except Exception as e:
        log_sys_err(f"check_port_listening ({port}): {e}")
        return False

def free_port(port):
    try:
        subprocess.run(f"fuser -k -9 {port}/tcp", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(f"lsof -ti:{port} | xargs kill -9", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log_sys_err(f"free_port ({port}): {e}")

def full_cleanup():
    try:
        subprocess.run(["pkill", "-9", "-x", "xray"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log_sys_err(f"full_cleanup pkill: {e}")
    free_port(XRAY_PORT)
    free_port(API_PORT)
    time.sleep(0.5)
    try:
        open(XRAY_LOG, "w").close()
    except Exception as e:
        log_sys_err(f"full_cleanup clear log: {e}")

def count_client_connections():
    try:
        count = 0
        hex_port = f":{XRAY_PORT:04X}"
        for net_file in ['/proc/net/tcp', '/proc/net/tcp6']:
            try:
                with open(net_file, 'r') as f:
                    for line in f:
                        parts = line.split()
                        if len(parts) > 3:
                            local_addr = parts[1]
                            conn_state = parts[3]
                            if local_addr.endswith(hex_port) and conn_state == '01':
                                count += 1
            except Exception as e:
                log_sys_err(f"count_client_connections parse: {e}")
        return count
    except Exception as e:
        log_sys_err(f"count_client_connections: {e}")
        return 0

def trigger_make_ports_public():
    global ports_thread_active
    with ports_thread_lock:
        if ports_thread_active: return
        ports_thread_active = True
    threading.Thread(target=_ports_worker, daemon=True).start()

def _ports_worker():
    global ports_thread_active
    cmds = [
        f"gh codespace ports visibility {XRAY_PORT}:public -c {CODESPACE_NAME}",
        f"gh codespace ports visibility {XRAY_PORT}:public",
        f"gh codespace ports visibility {SUB_PORT}:public -c {CODESPACE_NAME}",
        f"gh codespace ports visibility {SUB_PORT}:public"
    ]
    for _ in range(4):
        for cmd in cmds:
            try:
                subprocess.run(cmd, shell=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                log_sys_err(f"ports_worker run cmd: {e}")
        time.sleep(2)
    with ports_thread_lock:
        ports_thread_active = False

_wake_tty = None

def _open_wake_tty():
    global _wake_tty
    if _wake_tty is not None:
        return _wake_tty
    for dev in ("/dev/tty", "/dev/console", "/dev/pts/0"):
        try:
            _wake_tty = open(dev, "w")
            return _wake_tty
        except Exception:
            continue
    _wake_tty = False
    return _wake_tty

def wake_lock_pulse():
    ok = False
    try:
        with open(KEEPALIVE_FILE, "w") as f:
            f.write(str(time.time()))
        os.utime(KEEPALIVE_FILE, None)
        ok = True
    except Exception as e:
        log_sys_err(f"wake_lock_pulse touch: {e}")
    try:
        tty = _open_wake_tty()
        if tty:
            tty.write("\x00")
            tty.flush()
            ok = True
    except Exception as e:
        log_sys_err(f"wake_lock_pulse tty: {e}")
        global _wake_tty
        _wake_tty = None
        
    def ping_url(url):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "G2Leafy/2.0"})
            urllib.request.urlopen(req, timeout=5)
            return True
        except Exception as e:
            log_sys_err(f"wake_lock_pulse ping {url}: {e}")
            return False

    if SUB_DOMAIN and "..." not in SUB_DOMAIN:
        if ping_url(f"https://{SUB_DOMAIN}/?ping={int(time.time())}"):
            ok = True
    if PORT_DOMAIN and "..." not in PORT_DOMAIN:
        if ping_url(f"https://{PORT_DOMAIN}/"):
            ok = True
            
    return ok

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

class SubServerHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        try:
            if self.path == '/health':
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")
                return
            build_subscription()
            sub_file = os.path.join(WWW_DIR, "sub")
            if os.path.exists(sub_file):
                with open(sub_file, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.send_header("Profile-Title", SERVER_NAME)
                with settings_lock:
                    interval = settings.get("sub_update_mins", 1) * 60
                    cap = settings.get("data_cap_gb", 999999)
                with state_lock:
                    t_up = state['total_up']
                    t_down = state['total_down']
                    u_sec = state['uptime_sec']
                self.send_header("Profile-Update-Interval", str(interval))
                total = cap * 1073741824
                exp = int(time.time()) + (60 * 3600) - u_sec
                self.send_header("Subscription-Userinfo",
                                 f"upload={t_up}; download={t_down}; total={total}; expire={exp}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"No subscription yet. Build it in Config Gen.")
        except Exception as e:
            log_sys_err(f"SubServerHandler GET: {e}")
            try:
                self.send_response(500)
                self.end_headers()
            except:
                pass

def sub_server_thread():
    try:
        ThreadedHTTPServer(('0.0.0.0', SUB_PORT), SubServerHandler).serve_forever()
    except Exception as e:
        log_sys_err(f"sub_server_thread: {e}")

last_cpu_idle = 0.0
last_cpu_total = 0.0

def sample_cpu_pct():
    global last_cpu_idle, last_cpu_total
    try:
        with open('/proc/stat') as f:
            line = f.readline()
        fields = [float(column) for column in line.strip().split()[1:]]
        idle = fields[3] + fields[4]
        total = sum(fields)
        idle_delta = idle - last_cpu_idle
        total_delta = total - last_cpu_total
        last_cpu_idle, last_cpu_total = idle, total
        if total_delta <= 0: return 0.0
        return min(100.0, max(0.0, 100.0 * (1.0 - idle_delta / total_delta)))
    except Exception as e:
        log_sys_err(f"sample_cpu_pct: {e}")
        return 0.0

def save_persistent_data():
    global last_saved_uptime, last_saved_traffic
    with state_lock:
        cur_up = state.get("total_up", 0)
        cur_down = state.get("total_down", 0)
        cur_uptime = state.get("uptime_sec", 0)
    try:
        with open(TOTAL_UPTIME_FILE, "w") as f:
            f.write(str(cur_uptime))
        last_saved_uptime = cur_uptime
    except Exception as e:
        log_sys_err(f"save_persistent_data uptime: {e}")
    try:
        with open(TOTAL_TRAFFIC_FILE, "w") as f:
            f.write(f"{cur_down},{cur_up}")
        last_saved_traffic = cur_down + cur_up
    except Exception as e:
        log_sys_err(f"save_persistent_data traffic: {e}")

def system_monitor_thread():
    global state
    try:
        ip_req = urllib.request.urlopen("https://api.ipify.org", timeout=3)
        ip_val = ip_req.read().decode()
        loc_req = urllib.request.urlopen("https://ipinfo.io/json", timeout=3)
        loc_val = json.loads(loc_req.read().decode()).get("country", "??")
        with state_lock:
            state["ip"] = ip_val
            state["loc"] = loc_val
    except Exception as e:
        log_sys_err(f"system_monitor_thread ip fetch: {e}")

    tick = 0
    while engine_running:
        tick += 1

        try:
            cpu_val = sample_cpu_pct()
            try:
                la = os.getloadavg()[0]
            except Exception:
                la = 0.0
            sys_cpu = f"{cpu_val:.0f}% (load {la:.2f})"
            
            used = 0
            tot = 0
            try:
                with open('/proc/meminfo') as f:
                    mem = {}
                    for line in f:
                        parts = line.split()
                        if len(parts) >= 2:
                            mem[parts[0].strip(':')] = int(parts[1])
                used = (mem.get('MemTotal', 0) - mem.get('MemAvailable', mem.get('MemFree', 0))) / 1024
                tot = mem.get('MemTotal', 0) / 1024
            except Exception as e:
                log_sys_err(f"system_monitor_thread meminfo: {e}")
                
            mem_pct = (used / tot) * 100 if tot else 0.0
            sys_mem = f"{used:.0f} / {tot:.0f} MB ({mem_pct:.0f}%)"
            
            with state_lock:
                state["cpu_pct"] = cpu_val
                state["sys_cpu"] = sys_cpu
                state["mem_pct"] = mem_pct
                state["sys_mem"] = sys_mem
                state["cpu_hist"].append(cpu_val)
                state["mem_hist"].append(mem_pct)
                if len(state["cpu_hist"]) > 240:
                    state["cpu_hist"].pop(0)
                if len(state["mem_hist"]) > 240:
                    state["mem_hist"].pop(0)
        except Exception as e:
            log_sys_err(f"system_monitor_thread resources: {e}")

        if wake_lock_active and tick % 60 == 0:
            ok_pulse = wake_lock_pulse()
            t_now = time.time()
            with state_lock:
                state["wake_ok"] = ok_pulse
                state["wake_last"] = t_now

        time.sleep(1)

def xray_monitor_thread():
    global state, last_saved_uptime, last_saved_traffic

    last_fd = None
    last_fu = None

    if os.path.exists(TOTAL_UPTIME_FILE):
        try:
            with open(TOTAL_UPTIME_FILE) as f:
                with state_lock:
                    state["uptime_sec"] = int(f.read().strip())
                    last_saved_uptime = state["uptime_sec"]
        except Exception as e:
            log_sys_err(f"xray_monitor_thread uptime init: {e}")

    if os.path.exists(TOTAL_TRAFFIC_FILE):
        try:
            with open(TOTAL_TRAFFIC_FILE) as f:
                parts = f.read().strip().split(",")
                with state_lock:
                    state["total_down"] = int(parts[0])
                    state["total_up"] = int(parts[1])
                    last_saved_traffic = state["total_down"] + state["total_up"]
        except Exception as e:
            log_sys_err(f"xray_monitor_thread traffic init: {e}")

    tick = 0
    while engine_running:
        tick += 1
        
        is_running = check_xray_running()
        with state_lock:
            state["is_xray_running"] = is_running

        if tick > 10 and tick % 30 == 0:
            if not is_running or not check_port_listening(XRAY_PORT):
                start_xray()
                build_subscription(force=True)
                with state_lock:
                    state["is_xray_running"] = check_xray_running()

        if tick % 120 == 0 and is_running:
            trigger_make_ports_public()

        with settings_lock:
            sub_interval = max(1, settings.get("sub_update_mins", 1)) * 60
        if tick % sub_interval == 0:
            build_subscription()

        conns = count_client_connections() if is_running else 0
        with state_lock:
            state["conns"] = conns

        if is_running:
            with state_lock:
                state["uptime_sec"] += 1
                cur_uptime = state["uptime_sec"]
                
            try:
                out = subprocess.check_output(
                    ["timeout", "2", XRAY_BIN, "api", "statsquery", f"-server=127.0.0.1:{API_PORT}"],
                    text=True, stderr=subprocess.DEVNULL)
                fd = fu = 0
                parsed = False
                try:
                    data = json.loads(out)
                    for s in data.get("stat", []) or []:
                        name = s.get("name", "") or ""
                        parts = name.split(">>>")
                        if len(parts) == 4 and parts[0] == "inbound" and parts[1] != "api":
                            try:
                                val = int(s.get("value", 0) or 0)
                            except Exception:
                                val = 0
                            if parts[3] == "downlink":
                                fd += val
                            elif parts[3] == "uplink":
                                fu += val
                    parsed = True
                except Exception as e:
                    log_sys_err(f"xray_monitor_thread stats parse json: {e}")
                    parsed = False

                if not parsed:
                    for m in re.finditer(r'name:\s*"([^"]+)".*?value:\s*(\d+)', out, re.S):
                        name, valstr = m.group(1), m.group(2)
                        parts = name.split(">>>")
                        if len(parts) == 4 and parts[0] == "inbound" and parts[1] != "api":
                            try:
                                val = int(valstr)
                            except Exception:
                                val = 0
                            if parts[3] == "downlink":
                                fd += val
                            elif parts[3] == "uplink":
                                fu += val

                dt_down = 0
                if last_fd is not None:
                    if fd >= last_fd:
                        dt_down = fd - last_fd
                    else:
                        dt_down = fd
                last_fd = fd

                dt_up = 0
                if last_fu is not None:
                    if fu >= last_fu:
                        dt_up = fu - last_fu
                    else:
                        dt_up = fu
                last_fu = fu

                with state_lock:
                    state["total_down"] += dt_down
                    state["total_up"] += dt_up
                    state["rx_hist"].append(dt_down)
                    state["tx_hist"].append(dt_up)
                    cur_down = state["total_down"]
                    cur_up = state["total_up"]

                cur_traffic = cur_down + cur_up
                if cur_uptime - last_saved_uptime >= 300 or cur_traffic - last_saved_traffic >= 52428800:
                    save_persistent_data()

            except Exception as e:
                log_sys_err(f"xray_monitor_thread stats fetch: {e}")
                with state_lock:
                    state["rx_hist"].append(0)
                    state["tx_hist"].append(0)
        else:
            with state_lock:
                state["rx_hist"].append(0)
                state["tx_hist"].append(0)

        with state_lock:
            for k in ("rx_hist", "tx_hist"):
                if len(state[k]) > 240:
                    state[k].pop(0)

        with state_lock:
            don_active = state["donate_active"]
            don_last = state["donate_last"]
            is_run = state["is_xray_running"]
            
        if don_active:
            if not is_run or donate_quota_about_to_end():
                donate_revoke()
                with state_lock:
                    state["donate_active"] = False
                    state["donate_msg"] = "auto-stopped"
            elif (time.time() - don_last) >= DONATE_HEARTBEAT_SEC:
                donate_heartbeat()

        if tick % 5 == 0:
            p_ok = check_port_listening(XRAY_PORT)
            with state_lock:
                state["ports_ok"] = p_ok

        time.sleep(1)

def start_xray():
    try:
        subprocess.run(f"setcap cap_net_bind_service=+ep {XRAY_BIN}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log_sys_err(f"start_xray setcap: {e}")

    for attempt in range(5):
        full_cleanup()
        uid = get_uuid()
        cfg = {
            "log": {"loglevel": "warning", "access": "none", "error": XRAY_LOG},
            "stats": {},
            "api": {"tag": "api", "services": ["StatsService"]},
            "routing": {
                "rules": [
                    {"inboundTag": ["api"], "outboundTag": "api", "type": "field"}
                ]
            },
            "policy": {
                "system": {"statsInboundDownlink": True, "statsInboundUplink": True},
                "levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True,
                                 "bufferSize": 4, "connIdle": 300, "handshake": 4}}
            },
            "inbounds": [
                {"tag": "vless-in", "port": XRAY_PORT, "listen": "0.0.0.0", "protocol": "vless",
                 "settings": {"clients": [{"id": uid, "flow": "", "level": 0, "email": "user@g2leafy"}], "decryption": "none"},
                 "streamSettings": {
                     "network": "xhttp", "security": "none",
                     "xhttpSettings": {"mode": "packet-up", "path": "/"},
                     "sockopt": {"tcpFastOpen": True, "tcpNoDelay": True,
                                 "tcpKeepAliveIdle": 30, "mark": 0}
                 },
                 "sniffing": {"enabled": False}},
                {"listen": "127.0.0.1", "port": API_PORT, "protocol": "dokodemo-door",
                 "settings": {"address": "127.0.0.1"}, "tag": "api"}
            ],
            "outbounds": [
                {"tag": "direct", "protocol": "freedom", "settings": {"domainStrategy": "UseIP"}},
                {"tag": "block", "protocol": "blackhole"}
            ]
        }
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception as e:
            log_sys_err(f"start_xray write config: {e}")
            
        try:
            subprocess.Popen([XRAY_BIN, "run", "-c", CONFIG_FILE],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            log_sys_err(f"start_xray Popen: {e}")
        
        ok = False
        for _ in range(40):
            if check_xray_running() and check_port_listening(XRAY_PORT):
                ok = True
                break
            time.sleep(0.2)
        
        if ok:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{XRAY_PORT}/", timeout=1)
            except Exception:
                pass
            trigger_make_ports_public()
            return
            
        stop_xray()
        time.sleep(1.5)

def stop_xray():
    try:
        subprocess.run(["pkill", "-9", "-x", "xray"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log_sys_err(f"stop_xray pkill: {e}")

def generate_link(ip, tag):
    uid = get_uuid()
    t = urllib.parse.quote(tag)
    ip = re.sub(r'^https?://', '', ip)
    ip = ip.split('/')[0]
    ip = ip.split(':')[0]
    return (f"vless://{uid}@{ip}:443?encryption=none&security=tls"
            f"&sni={PORT_DOMAIN}&fp=chrome&alpn=h2&insecure=1&allowInsecure=1"
            f"&type=xhttp&host={PORT_DOMAIN}&path=%2F&mode=packet-up#{t}")

def build_subscription(force=False):
    global last_sub_state
    with state_lock:
        used_val = state['total_down'] + state['total_up']
        u_sec = state['uptime_sec']
        
    with settings_lock:
        nodes = copy.deepcopy(settings.get("nodes", []))
        
    nodes_str = json.dumps(nodes)
    
    if not force:
        if (used_val - last_sub_state["traffic"]) < 52428800 and \
           (u_sec - last_sub_state["uptime"]) < 60 and \
           nodes_str == last_sub_state["nodes"]:
            return
            
    last_sub_state["traffic"] = used_val
    last_sub_state["uptime"] = u_sec
    last_sub_state["nodes"] = nodes_str

    used_str = fmt_auto(used_val)
    left_sec = max(0, 60 * 3600 - u_sec)
    left_str = f"{left_sec//3600}h {(left_sec%3600)//60}m"
    info_tag = f"\U0001F343 @G2Leafy | {left_str}-{used_str} | {GITHUB_USER}"
    info_link = f"trojan://{get_uuid()}@127.0.0.1:80?security=none#{urllib.parse.quote(info_tag)}"
    lines = [info_link]
    
    for nd in nodes:
        ip = nd.get("ip", "").strip()
        if ip:
            lines.append(generate_link(ip, nd.get("name") or ip))
    raw = "\n".join(lines)
    try:
        with open(os.path.join(WWW_DIR, "sub"), "w") as f:
            f.write(raw)
    except Exception as e:
        log_sys_err(f"build_subscription write: {e}")

def update_and_build():
    save_settings()
    build_subscription(force=True)

DONATED_FILE = os.path.join(DATA_DIR, "donated.txt")
donate_label = ""

def donate_id():
    return f"{CODESPACE_NAME}"[:48] or get_uuid()[:12]

def donate_link():
    tag = (donate_label or f"\U0001F343G2Leafy | {GITHUB_USER}").strip()
    return generate_link(DONATE_IP, tag)

def _webhook_configured():
    return DONATE_WEBHOOK_URL and "REPLACE_WITH_YOUR_DEPLOY_ID" not in DONATE_WEBHOOK_URL

def _post_webhook(payload, timeout=10):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(DONATE_WEBHOOK_URL, data=data,
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")

def donate_heartbeat():
    if not _webhook_configured():
        return False, "Donation webhook not configured."
    link = donate_link()
    try:
        with open(DONATED_FILE, "w") as f:
            f.write(link + "\n")
    except Exception as e:
        log_sys_err(f"donate_heartbeat file write: {e}")
    payload = {"action": "register", "id": donate_id(), "message": link,
               "label": (donate_label or GITHUB_USER)[:64],
               "ttl": DONATE_TTL_SEC, "secret": DONATE_SECRET}
    try:
        resp = _post_webhook(payload)
        ok = resp.startswith("OK")
        with state_lock:
            state["donate_last"] = time.time()
            state["donate_msg"] = resp.strip()[:48]
        return ok, resp.strip()
    except Exception as e:
        with state_lock:
            state["donate_msg"] = "send failed"
        log_sys_err(f"donate_heartbeat post: {e}")
        return False, f"Heartbeat failed: {e}"

def donate_revoke():
    if not _webhook_configured():
        return False, "not configured"
    try:
        resp = _post_webhook({"action": "revoke", "id": donate_id(),
                              "secret": DONATE_SECRET}, timeout=10)
        return resp.startswith("OK"), resp.strip()
    except Exception as e:
        log_sys_err(f"donate_revoke post: {e}")
        return False, str(e)

def donate_quota_about_to_end():
    with state_lock:
        u_sec = state["uptime_sec"]
    left = 60 * 3600 - u_sec
    return left <= DONATE_QUOTA_GRACE_SEC

def fetch_upstream_version():
    try:
        remote_content = urllib.request.urlopen(RAW_BASE + "g2leafy.py", timeout=5).read()
        with open(__file__, "rb") as f:
            local_content = f.read()
        r_str = remote_content.replace(b'\r\n', b'\n')
        l_str = local_content.replace(b'\r\n', b'\n')
        needs_update = r_str != l_str
        m = re.search(rb'LOCAL_VERSION\s*=\s*[\'"]([^\'"]+)[\'"]', remote_content)
        remote_ver = m.group(1).decode() if m else "Updated"
        return remote_ver, needs_update
    except Exception as e:
        log_sys_err(f"fetch_upstream_version: {e}")
        return None, False

def download_update(progress_cb=None):
    for name in UPDATE_CANDIDATES:
        url = RAW_BASE + name
        try:
            req = urllib.request.urlopen(url, timeout=20)
            total = int(req.headers.get("Content-Length", 0) or 0)
            buf = bytearray()
            while True:
                chunk = req.read(8192)
                if not chunk:
                    break
                buf += chunk
                if progress_cb:
                    progress_cb(len(buf), total)
            if len(buf) < 400:
                continue
            target = os.path.abspath(__file__)
            try:
                shutil.copyfile(target, target + ".bak")
            except Exception as e:
                log_sys_err(f"download_update copyfile: {e}")
            with open(target, "wb") as f:
                f.write(buf)
            try:
                os.chmod(target, 0o755)
            except Exception as e:
                log_sys_err(f"download_update chmod: {e}")
            interp = sys.executable if target.endswith(".py") else "/bin/bash"
            return target, interp
        except Exception as e:
            log_sys_err(f"download_update {name}: {e}")
            continue
    return None, None

def c_color(fg):
    return curses.color_pair(fg)

def safe_add(scr, y, x, text, attr=0):
    try:
        h, w = scr.getmaxyx()
        if 0 <= y < h and 0 <= x < w:
            max_len = w - x - 1
            s = str(text)
            if len(s) > max_len and max_len > 0:
                s = s[:max_len - 1] + "\u2026"
            elif len(s) > max_len:
                s = ""
            if s:
                scr.addstr(y, x, s, attr)
    except curses.error:
        pass

def draw_box(scr, y, x, h, w, title="", color=4):
    if h < 2 or w < 2: return
    scr.attron(color)
    safe_add(scr, y, x, "\u256d" + "\u2500" * (w - 2) + "\u256e")
    for i in range(1, h - 1):
        safe_add(scr, y + i, x, "\u2502" + " " * (w - 2) + "\u2502")
    safe_add(scr, y + h - 1, x, "\u2570" + "\u2500" * (w - 2) + "\u256f")
    scr.attroff(color)
    if title:
        safe_add(scr, y, x + 2, f" {title} "[:w-4], color | curses.A_BOLD)

def draw_card(scr, y, x, w, title, val, unit, icon, icon_color):
    if w < 10: return
    draw_box(scr, y, x, 5, w, "", c_color(4))
    safe_add(scr, y + 1, x + 2, title[:w - 6], c_color(4) | curses.A_BOLD)
    safe_add(scr, y + 1, x + w - 3, icon, icon_color | curses.A_BOLD)
    safe_add(scr, y + 2, x + 2, str(val)[:w - 4], c_color(1) | curses.A_BOLD)
    safe_add(scr, y + 2, x + 2 + len(str(val)) + 1, unit[:w - len(str(val)) - 4], c_color(4))

def kv(scr, y, x, w, label, val, val_color=None):
    if val_color is None:
        val_color = c_color(1)
    vs = str(val)
    safe_add(scr, y, x, str(label)[:max(1, w - len(vs) - 1)], c_color(4))
    safe_add(scr, y, x + w - len(vs), vs, val_color)

def progress_bar(scr, y, x, w, pct, color):
    pct = max(0.0, min(100.0, pct))
    inner = max(1, w - 2)
    filled = int(round((pct / 100.0) * inner))
    safe_add(scr, y, x, "[", c_color(4))
    safe_add(scr, y, x + 1, "\u2588" * filled + "\u2591" * (inner - filled), color | curses.A_BOLD)
    safe_add(scr, y, x + 1 + inner, "]", c_color(4))

def blocking_getch(scr):
    scr.nodelay(False)
    scr.timeout(-1)
    try:
        return scr.getch()
    finally:
        scr.nodelay(True)
        scr.timeout(500)

def render_spaced_kvs(scr, y_start, x, w, h_avail, kvs):
    if not kvs or h_avail < 1:
        return
    n = len(kvs)
    if n == 1:
        kv(scr, y_start + h_avail // 2, x, w, kvs[0][0], kvs[0][1], kvs[0][2])
        return
    step = max(1, (h_avail - 1) // (n - 1))
    for i, item in enumerate(kvs):
        y = y_start + i * step
        if y >= y_start + h_avail:
            y = y_start + h_avail - 1
        if len(item) == 4:
            label, val_lines, col, _ = item
            safe_add(scr, y, x, label, c_color(4))
            for j, line in enumerate(val_lines):
                if y + j + 1 < y_start + h_avail:
                    safe_add(scr, y + j + 1, x, line, col)
        else:
            label, val, col = item
            kv(scr, y, x, w, label, val, col)

def draw_dot_chart(scr, y, x, h, w, title, series, ymax=None, value_fmt=None):
    if h < 4 or w < 10:
        return
    draw_box(scr, y, x, h, w, title, c_color(3))
    ylab = 6
    pw = w - 4 - ylab - 1
    ph = h - 3
    if ph < 2 or pw < 6:
        return
    allvals = [v for data, _ in series for v in data[-pw:]]
    maxv = ymax if ymax else max(allvals + [1])
    if maxv <= 0:
        maxv = 1
    if value_fmt is None:
        value_fmt = lambda v: f"{v:.0f}"
    for row, frac in ((0, 1.0), (ph // 2, 0.5), (ph - 1, 0.0)):
        lab = value_fmt(maxv * frac)
        safe_add(scr, y + 1 + row, x + 2 + ylab - len(lab), lab, c_color(4))
    for row in range(ph):
        safe_add(scr, y + 1 + row, x + 2 + ylab, "\u2502", c_color(4))
    bx = x + 3 + ylab
    for data, color in series:
        dd = data[-pw:]
        prev_y = None
        for col in range(pw):
            idx = col - (pw - len(dd))
            if idx >= 0:
                v = max(0.0, min(maxv, dd[idx]))
                yp = ph - 1 - int(round((v / maxv) * (ph - 1)))
                if prev_y is not None and abs(yp - prev_y) > 1:
                    step = 1 if yp > prev_y else -1
                    for yy in range(prev_y + step, yp, step):
                        safe_add(scr, y + 1 + yy, bx + col, "\u25cf", color | curses.A_BOLD)
                safe_add(scr, y + 1 + yp, bx + col, "\u25cf", color | curses.A_BOLD)
                prev_y = yp
            else:
                prev_y = None
    safe_add(scr, y + 1 + ph, x + 2 + ylab, "\u2514" + "\u2500" * pw, c_color(4))

def keyhints_for_tab(tab):
    controls = {"Nav": [("\u2191\u2193", "tab"), ("1-4", "jump"), ("q", "quit")]}
    if tab == 1:
        controls["Power"] = [("s", "start"), ("x", "stop"), ("r", "restart")]
    elif tab == 2:
        controls["Settings"] = [("5", "cap"), ("6", "upd"), ("7", "wake"), ("8", "reset"), ("u", "UUID")]
    elif tab == 3:
        controls["Configs"] = [("\u2190\u2192", "sel"), ("a", "add"), ("d", "del"), ("e", "edit")]
        controls["Sub"] = [("b", "build"), ("g", "QR")]
        controls["Donate"] = [("t", "share/stop")]
    elif tab == 4:
        controls["Logs"] = [("c", "clear"), ("s", "search"), ("f", "follow"), ("\u2191\u2193", "scroll")]
    return controls

def popup_ask(scr, prompt, default=""):
    h, w = scr.getmaxyx()
    bw = max(20, min(66, w - 2))
    bh = max(5, min(7, h))
    win = curses.newwin(bh, bw, max(0, (h - bh) // 2), max(0, (w - bw) // 2))
    win.keypad(True)
    win.bkgd(' ', c_color(1))
    buf = str(default)
    curses.curs_set(1)
    res = None
    while True:
        win.erase()
        win.box()
        safe_add(win, 0, max(1, (bw - 7) // 2), " Input ", c_color(3) | curses.A_BOLD)
        safe_add(win, 2, 2, prompt[:bw - 4], c_color(1))
        if bh >= 7:
            safe_add(win, 4, 2, "Enter=save  Esc=cancel", c_color(4))
        safe_add(win, bh // 2, 2, "> ", c_color(3) | curses.A_BOLD)
        safe_add(win, bh // 2, 4, buf[-(bw - 6):], c_color(1) | curses.A_BOLD)
        win.refresh()
        ch = win.getch()
        if ch == 27:
            res = None
            break
        elif ch in (10, 13, curses.KEY_ENTER):
            res = buf
            break
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            buf = buf[:-1]
        elif 32 <= ch <= 126:
            buf += chr(ch)
    curses.curs_set(0)
    win.clear()
    win.refresh()
    del win
    scr.clear()
    scr.refresh()
    return res

def popup_msg(scr, title, msg):
    h, w = scr.getmaxyx()
    bw = max(20, min(66, w - 2))
    bh = max(5, min(7, h))
    win = curses.newwin(bh, bw, max(0, (h - bh) // 2), max(0, (w - bw) // 2))
    win.keypad(True)
    win.box()
    safe_add(win, 0, max(1, (bw - len(title) - 2) // 2), f" {title} ", c_color(3) | curses.A_BOLD)
    safe_add(win, 2, 2, msg[:bw - 4], c_color(1))
    if bh >= 6:
        safe_add(win, bh - 2, 2, "Press any key...", c_color(4))
    win.refresh()
    win.getch()
    win.clear()
    win.refresh()
    del win
    scr.clear()
    scr.refresh()

def popup_confirm(scr, title, msg):
    h, w = scr.getmaxyx()
    bw = max(20, min(66, w - 2))
    bh = max(5, min(7, h))
    win = curses.newwin(bh, bw, max(0, (h - bh) // 2), max(0, (w - bw) // 2))
    win.keypad(True)
    win.box()
    safe_add(win, 0, max(1, (bw - len(title) - 2) // 2), f" {title} ", c_color(3) | curses.A_BOLD)
    safe_add(win, 2, 2, msg[:bw - 4], c_color(1))
    if bh >= 6:
        safe_add(win, bh - 2, 2, "[y] yes      [n] no", c_color(4))
    win.refresh()
    res = False
    while True:
        ch = win.getch()
        if ch in (ord('y'), ord('Y')):
            res = True
            break
        if ch in (ord('n'), ord('N'), 27):
            res = False
            break
    win.clear()
    win.refresh()
    del win
    scr.clear()
    scr.refresh()
    return res

def splash_and_update(scr):
    h, w = scr.getmaxyx()
    cy = h // 2
    scr.nodelay(True)
    scr.timeout(120)

    def center(row, text, attr=0):
        safe_add(scr, row, max(0, (w - len(text)) // 2), text, attr)

    result = {"ver": None, "needs_update": False, "done": False}

    def worker():
        v, u = fetch_upstream_version()
        result["ver"] = v
        result["needs_update"] = u
        result["done"] = True
    threading.Thread(target=worker, daemon=True).start()

    spinner = "|/-\\"
    i = 0
    t0 = time.time()
    while not result["done"] and time.time() - t0 < 8:
        scr.erase()
        center(cy - 2, "G2Leafy", c_color(2) | curses.A_BOLD)
        center(cy, f"checking for updates {spinner[i % 4]}", c_color(4))
        center(cy + 2, f"current  v{LOCAL_VERSION}", c_color(4))
        scr.refresh()
        i += 1
        if scr.getch() == 27:
            return

    remote_ver = result["ver"]
    needs_update = result["needs_update"]

    if not remote_ver:
        scr.erase()
        center(cy - 1, "G2Leafy", c_color(2) | curses.A_BOLD)
        center(cy + 1, "update check skipped (offline/ratelimit)", c_color(5))
        scr.refresh()
        time.sleep(0.9)
        return

    if not needs_update:
        scr.erase()
        center(cy - 1, "G2Leafy", c_color(2) | curses.A_BOLD)
        center(cy + 1, "you're on the latest code", c_color(2))
        scr.refresh()
        time.sleep(1.0)
        return

    if not AUTO_UPDATE:
        if not _ask_update(scr, remote_ver):
            return

    _run_update_animation(scr, remote_ver)

def _ask_update(scr, remote):
    h, w = scr.getmaxyx()
    cy = h // 2

    def center(row, text, attr=0):
        safe_add(scr, row, max(0, (w - len(text)) // 2), text, attr)

    frames = [
        "     > ", "    -> ", "   --> ", "  ---> ", " ----> ",
        "-----> ", "=====> ", " ====> ", "  ===> ", "   ==> ", "    => "
    ]
    i = 0
    t0 = time.time()
    scr.nodelay(True)
    scr.timeout(150)
    while time.time() - t0 < 30:
        scr.erase()
        center(cy - 4, "UPDATE AVAILABLE", c_color(2) | curses.A_BOLD)
        cur = f"[ v{LOCAL_VERSION} ]"
        new = f"[ v{remote} ]"
        arrow = frames[i % len(frames)]
        start = max(0, (w - (len(cur) + 3 + 7 + 3 + len(new))) // 2)
        safe_add(scr, cy - 1, start, cur, c_color(4) | curses.A_BOLD)
        safe_add(scr, cy - 1, start + len(cur) + 3, arrow, c_color(2) | curses.A_BOLD)
        safe_add(scr, cy - 1, start + len(cur) + 3 + 7, new, c_color(2) | curses.A_BOLD)
        center(cy + 2, "Update now?   [y] yes     [n] no", c_color(1))
        scr.refresh()
        i += 1
        ch = scr.getch()
        if ch in (ord('y'), ord('Y')):
            return True
        if ch in (ord('n'), ord('N'), 27):
            return False
    return False

def _run_update_animation(scr, remote):
    h, w = scr.getmaxyx()
    cy = h // 2
    prog = {"cur": 0, "total": 0}

    def center(row, text, attr=0):
        safe_add(scr, row, max(0, (w - len(text)) // 2), text, attr)

    def cb(cur, total):
        prog["cur"] = cur
        prog["total"] = total

    done = {"ok": None, "target": None, "interp": None}

    def worker():
        tgt, interp = download_update(cb)
        done["target"] = tgt
        done["interp"] = interp
        done["ok"] = tgt is not None
    threading.Thread(target=worker, daemon=True).start()

    frames = [
        "     > ", "    -> ", "   --> ", "  ---> ", " ----> ",
        "-----> ", "=====> ", " ====> ", "  ===> ", "   ==> ", "    => "
    ]
    i = 0
    scr.nodelay(True)
    scr.timeout(120)
    while done["ok"] is None:
        scr.erase()
        center(cy - 3, "DOWNLOADING UPDATE", c_color(2) | curses.A_BOLD)
        block = f"v{LOCAL_VERSION}  {frames[i % len(frames)]}  v{remote}"
        center(cy - 1, block, c_color(2) | curses.A_BOLD)
        pct = (prog["cur"] / prog["total"] * 100) if prog["total"] else (i * 4 % 100)
        bw = min(40, w - 8)
        progress_bar(scr, cy + 1, max(0, (w - bw) // 2), bw, pct, c_color(2))
        center(cy + 2, f"{pct:.0f}%", c_color(4))
        scr.refresh()
        i += 1
        scr.getch()

    scr.erase()
    if done["ok"]:
        center(cy, f"updated to v{remote} \u2014 restarting\u2026", c_color(2) | curses.A_BOLD)
        scr.refresh()
        time.sleep(1.0)
        try:
            curses.endwin()
        except Exception:
            pass
        global engine_running
        engine_running = False
        os.execv(done["interp"], [done["interp"], done["target"]])
    else:
        center(cy, "update failed \u2014 continuing on current version", c_color(5))
        scr.refresh()
        time.sleep(1.2)

def render_sidebar(scr, h, w, mx):
    sbw = mx - 2
    iw = sbw - 1
    for r in range(h):
        safe_add(scr, r, sbw + 1, "\u2502", c_color(4))
    safe_add(scr, 1, 2, "G2Leafy", c_color(2) | curses.A_BOLD)
    safe_add(scr, 1, 11, f"v{LOCAL_VERSION}", c_color(4))
    safe_add(scr, 2, 2, "\u2500" * iw, c_color(4))
    menus = [(4, "1", "Dashboard", 1), (6, "2", "Settings", 2),
             (8, "3", "Config Gen", 3), (10, "4", "Logs", 4)]
    for y, num, txt, idx in menus:
        if nav_current == idx:
            safe_add(scr, y, 2, ("\u258c " + num + "  " + txt).ljust(iw), c_color(6) | curses.A_BOLD)
        else:
            safe_add(scr, y, 2, "  " + num + "  " + txt, c_color(1))
    sy = 12
    safe_add(scr, sy, 2, "\u2500" * iw, c_color(4))
    safe_add(scr, sy + 1, 2, "STATUS", c_color(4) | curses.A_BOLD)
    
    with state_lock:
        is_run = state["is_xray_running"]
        conns = state["conns"]
        u_sec = state["uptime_sec"]
        
    if is_run:
        safe_add(scr, sy + 2, 2, "\u25cf engine on", c_color(2) | curses.A_BOLD)
    else:
        safe_add(scr, sy + 2, 2, "\u25cb engine off", c_color(5) | curses.A_BOLD)
    safe_add(scr, sy + 3, 2, f"{conns} conns", c_color(4))
    safe_add(scr, sy + 4, 2, f"up {fmt_hms(u_sec)}", c_color(4))
    fy = sy + 6
    safe_add(scr, fy, 2, "\u2500" * iw, c_color(4))
    row = fy + 1
    hints = keyhints_for_tab(nav_current)
    for cat, items in hints.items():
        if row >= h - 3:  
            break
        safe_add(scr, row, 2, f"{cat}:", c_color(4) | curses.A_BOLD)
        row += 1
        x_offset = 2
        for k, d in items:
            item_len = len(k) + 1 + len(d) + 2
            if x_offset + item_len > 2 + iw:
                row += 1
                if row >= h - 3: 
                    break
                x_offset = 2
            safe_add(scr, row, x_offset, k, c_color(3) | curses.A_BOLD)
            safe_add(scr, row, x_offset + len(k) + 1, d, c_color(4))
            x_offset += item_len
        row += 1
    safe_add(scr, h - 2, 2, "Made By CodeLeafy \U0001F343", c_color(2) | curses.A_BOLD)

def render_bottombar(scr, h, w):
    menus = [(1, "Dash"), (2, "Set"), (3, "Cfg"), (4, "Log")]
    parts = []
    for idx, txt in menus:
        if idx == nav_current:
            parts.append(f"[{txt}]")
        else:
            parts.append(f" {idx}:{txt} ")
    bar = "".join(parts) + "  q:Quit"
    safe_add(scr, h - 1, 0, " " * w, c_color(6))
    safe_add(scr, h - 1, max(0, (w - len(bar)) // 2), bar[:w], c_color(6) | curses.A_BOLD)

def render_topbar(scr, mx, mw):
    with state_lock:
        dl = fmt_gb(state["total_down"])
        ul = fmt_gb(state["total_up"])
        is_run = state["is_xray_running"]
        
    if mw < 30:
        safe_add(scr, 1, mx, f"D:{dl} U:{ul}", c_color(1))
    else:
        safe_add(scr, 1, mx, f"\u2193 {dl} GB    \u2191 {ul} GB", c_color(1))
    badge = "\u25cf ON" if is_run else "\u25cb OFF"
    if mw >= 40:
        badge = "\u25cf ONLINE" if is_run else "\u25cb OFFLINE"
    bcol = c_color(2) if is_run else c_color(5)
    safe_add(scr, 1, mx + mw - len(badge) - 1, badge, bcol | curses.A_BOLD)

def screen_dashboard(scr, h, w, mx, mw):
    with state_lock:
        t_down = state["total_down"]
        t_up = state["total_up"]
        conns = state["conns"]
        u_sec = state["uptime_sec"]
        is_run = state["is_xray_running"]
        s_cpu = state["sys_cpu"]
        s_mem = state["sys_mem"]
        ip_v = state["ip"]
        p_ok = state["ports_ok"]
        rx_h = list(state["rx_hist"])
        tx_h = list(state["tx_hist"])
        
    gap = 2
    cols = max(1, mw // 18)
    cols = min(cols, 4)
    cw = max(1, (mw - (cols - 1) * gap) // cols)
    rem = (mw - (cols - 1) * gap) % cols
    cards = [
        ("DOWNLOAD", fmt_gb(t_down), "GB", "\u2193", c_color(2)),
        ("UPLOAD", fmt_gb(t_up), "GB", "\u2191", c_color(3)),
        ("CONNECTIONS", str(conns), "", "\u25c6", c_color(2)),
        ("UPTIME", fmt_hms(u_sec), "", "\u25f7", c_color(3)),
    ]
    y_offset = 3
    if h > 10 and mw > 20:
        for i, c in enumerate(cards):
            r = i // cols
            c_idx = i % cols
            ww = cw + (1 if c_idx < rem else 0)
            cx = mx + sum((cw + (1 if j < rem else 0) + gap) for j in range(c_idx))
            draw_card(scr, y_offset + r * 6, cx, ww, c[0], c[1], c[2], c[3], c[4])
        y_offset += ((len(cards) + cols - 1) // cols) * 6
    pr = y_offset
    ph = max(4, min(9, h - pr - 1))
    tot = 60 * 3600
    used = u_sec
    left = max(0, tot - used)
    pct = (used / tot) * 100 if tot else 0
    quota_color = c_color(5) if pct >= 90 else c_color(2)
    health_items = [
        ("Engine", " ONLINE " if is_run else " OFFLINE ", c_color(6) if is_run else c_color(5)),
        ("CPU", s_cpu, c_color(1)),
        ("RAM", s_mem, c_color(1)),
        (f"Quota 60h ({pct:.0f}%)", f"{fmt_hms(left)} left", quota_color)
    ]
    net_items = [
        ("Public IP", ip_v, c_color(1)),
        ("VLESS Port", f"{XRAY_PORT} {'open' if p_ok else 'closed'}", c_color(2) if p_ok else c_color(5)),
        ("Sub Port", str(SUB_PORT), c_color(1)),
        ("Wake Lock", "ON" if wake_lock_active else "OFF", c_color(2) if wake_lock_active else c_color(4))
    ]
    if mw < 50:
        if pr < h - 2:
            draw_box(scr, pr, mx, ph, mw, "Health & Quota", c_color(2))
            render_spaced_kvs(scr, pr + 1, mx + 2, mw - 4, ph - 2, health_items)
            pr += ph + 1
        if pr < h - 2:
            draw_box(scr, pr, mx, ph, mw, "Network", c_color(3))
            render_spaced_kvs(scr, pr + 1, mx + 2, mw - 4, ph - 2, net_items)
            pr += ph + 1
    else:
        pw1 = (mw - gap) // 2
        pw2 = max(1, mw - pw1 - gap)
        if pr < h - 2:
            draw_box(scr, pr, mx, ph, pw1, "Health & Quota", c_color(2))
            render_spaced_kvs(scr, pr + 1, mx + 2, pw1 - 4, ph - 2, health_items)
            draw_box(scr, pr, mx + pw1 + gap, ph, pw2, "Network", c_color(3))
            render_spaced_kvs(scr, pr + 1, mx + pw1 + gap + 2, pw2 - 4, ph - 2, net_items)
            pr += ph + 1
    chh = max(0, h - pr)
    if chh >= 4 and mw > 15:
        draw_dot_chart(scr, pr, mx, chh, mw, "Traffic Flow",
                       [(rx_h, c_color(2)), (tx_h, c_color(3))],
                       value_fmt=fmt_rate)

def screen_settings(scr, h, w, mx, mw):
    global wake_lock_active
    with settings_lock:
        cap_gb = settings.get("data_cap_gb", 999999)
        upd_m = settings.get("sub_update_mins", 1)
        
    with state_lock:
        u_sec = state["uptime_sec"]
        ip_v = state["ip"]
        w_last = state["wake_last"]
        c_pct = state["cpu_pct"]
        m_pct = state["mem_pct"]
        cpu_h = list(state["cpu_hist"])
        mem_h = list(state["mem_hist"])

    gap = 2
    pr = 3
    ph = max(4, min(9, h - pr - 1))
    d_s = "Unlimited" if is_unlimited(cap_gb) else f"{cap_gb} GB"
    left = max(0, 60 * 3600 - u_sec)
    id_items = [
        ("Server", SERVER_NAME, c_color(2)),
        ("5 \u00b7 Data Cap", d_s, c_color(2)),
        ("Time Left", fmt_hms(left), c_color(3))
    ]
    wl_status = "OFF"
    wl_color = c_color(4)
    if wake_lock_active:
        wl_color = c_color(2)
        if w_last and time.time() - w_last < 120:
            ago = int(time.time() - w_last)
            wl_status = f"ON ({ago}s ago)"
        else:
            wl_status = "ON (arming...)"
    conn_items = [
        ("Public IP", ip_v, c_color(3)),
        ("6 \u00b7 Sub Update", f"{upd_m} mins", c_color(1)),
        ("7 \u00b7 Wake Lock", wl_status, wl_color),
        ("8 \u00b7 Reset", "Defaults", c_color(5)),
        ("u \u00b7 Rotate UUID", "Regenerate", c_color(4))
    ]
    if mw < 50:
        if pr < h - 2:
            draw_box(scr, pr, mx, ph, mw, "Identity & Quota", c_color(2))
            render_spaced_kvs(scr, pr + 1, mx + 2, mw - 4, ph - 2, id_items)
            pr += ph + 1
        if pr < h - 2:
            draw_box(scr, pr, mx, ph, mw, "Connection", c_color(3))
            render_spaced_kvs(scr, pr + 1, mx + 2, mw - 4, ph - 2, conn_items)
            pr += ph + 1
        hr = pr
        hh = max(0, h - hr)
        if hh >= 8 and mw > 15:
            hh //= 2
            draw_dot_chart(scr, hr, mx, hh, mw, f"CPU  {c_pct:.0f}%", [(cpu_h, c_color(2))], ymax=100, value_fmt=lambda v: f"{v:.0f}%")
            draw_dot_chart(scr, hr + hh, mx, h - (hr + hh), mw, f"RAM  {m_pct:.0f}%", [(mem_h, c_color(3))], ymax=100, value_fmt=lambda v: f"{v:.0f}%")
        elif hh >= 4 and mw > 15:
            draw_dot_chart(scr, hr, mx, hh, mw, f"CPU  {c_pct:.0f}%", [(cpu_h, c_color(2))], ymax=100, value_fmt=lambda v: f"{v:.0f}%")
    else:
        pw1 = (mw - gap) // 2
        pw2 = max(1, mw - pw1 - gap)
        if pr < h - 2:
            draw_box(scr, pr, mx, ph, pw1, "Identity & Quota", c_color(2))
            render_spaced_kvs(scr, pr + 1, mx + 2, pw1 - 4, ph - 2, id_items)
            draw_box(scr, pr, mx + pw1 + gap, ph, pw2, "Connection", c_color(3))
            render_spaced_kvs(scr, pr + 1, mx + pw1 + gap + 2, pw2 - 4, ph - 2, conn_items)
        hr = pr + ph + 1
        hh = max(0, h - hr)
        if hh >= 4 and mw > 15:
            chw = (mw - gap) // 2
            chw2 = max(1, mw - chw - gap)
            draw_dot_chart(scr, hr, mx, hh, chw, f"CPU  {c_pct:.0f}%",
                           [(cpu_h, c_color(2))], ymax=100,
                           value_fmt=lambda v: f"{v:.0f}%")
            draw_dot_chart(scr, hr, mx + chw + gap, hh, chw2, f"RAM  {m_pct:.0f}%",
                           [(mem_h, c_color(3))], ymax=100,
                           value_fmt=lambda v: f"{v:.0f}%")

def screen_configgen(scr, h, w, mx, mw):
    global cfg_sel
    
    with settings_lock:
        nodes = copy.deepcopy(settings.get("nodes", []))
        
    with state_lock:
        ip_v = state["ip"]
        d_act = state["donate_active"]
        loc_v = state["loc"]
        
    if cfg_sel >= len(nodes) and nodes:
        cfg_sel = max(0, len(nodes) - 1)
    pr = 3
    if mw < 60:
        ph_server = max(4, min(7, h - pr - 2))
        listh = max(2, h - pr - ph_server - 1)
        if pr < h - 2:
            draw_box(scr, pr, mx, ph_server, mw, "Server", c_color(2))
            iw = max(1, mw - 4)
            server_items = [
                ("Port", str(XRAY_PORT), c_color(1)),
                ("Primary IP", ip_v, c_color(3)),
                ("Sub", "[g] View QR", c_color(2))
            ]
            if d_act:
                server_items.append(("Donate", "\u2665 Live [t] to stop", c_color(2)))
            else:
                server_items.append(("Donate", "[t] Share Config", c_color(4)))
            render_spaced_kvs(scr, pr + 1, mx + 2, iw, ph_server - 2, server_items)
        pr += ph_server
        if pr < h and listh >= 3:
            draw_box(scr, pr, mx, listh, mw, f"Config IPs ({len(nodes)})", c_color(3))
            if mw > 35:
                safe_add(scr, pr + listh - 2, mx + 2, " [\u2190\u2192] move [a] add [d] del [e] edit ", c_color(6))
            else:
                safe_add(scr, pr + listh - 2, mx + 1, " [\u2190\u2192] [a] [d] [e] ", c_color(6))
            row = pr + 1
            maxrows = listh - 3
            if maxrows > 0:
                start = max(0, cfg_sel - maxrows + 1)
                for i in range(start, min(len(nodes), start + maxrows)):
                    nd = nodes[i]
                    line = f"{i+1}. {nd.get('name','')[:15]} {nd.get('ip','')}"
                    if i == cfg_sel:
                        safe_add(scr, row, mx + 1, ("\u258c " + line).ljust(mw - 2)[:mw-2], c_color(6) | curses.A_BOLD)
                    else:
                        safe_add(scr, row, mx + 2, line[:mw-4], c_color(1))
                    row += 1
                if not nodes:
                    safe_add(scr, pr + 2, mx + 2, "No configs \u2014 [a]", c_color(4))
    else:
        pw1 = max(34, mw * 4 // 10)
        pw2 = max(1, mw - pw1 - 2)
        lc, rc = mx, mx + pw1 + 2
        listh = max(4, h - pr)
        ph_server = max(4, min(7, listh - 2))
        ph_notes = listh - ph_server
        if pr < h - 2:
            draw_box(scr, pr, lc, ph_server, pw1, "Server", c_color(2))
            iw = max(1, pw1 - 4)
            server_items = [
                ("Port", str(XRAY_PORT), c_color(1)),
                ("UUID", get_uuid()[:max(8, iw - 6)], c_color(1)),
                ("Primary IP", ip_v, c_color(3)),
                ("Sub", "Press [g] to view/QR", c_color(2))
            ]
            if d_act:
                server_items.append(("Donate", "\u2665 Live [t] to stop", c_color(2)))
            else:
                server_items.append(("Donate", "[t] Share Config", c_color(4)))
            render_spaced_kvs(scr, pr + 1, lc + 2, iw, ph_server - 2, server_items)
            notes_lines = []
            notes_lines.extend(textwrap.wrap("1. Make Sure To Join Our Telegram Channel : https://t.me/CodeLeafy \U0001F343", width=iw))
            notes_lines.append("")
            notes_lines.extend(textwrap.wrap("2. To find the best IPs, go to this link and scan for G2ray IPs:", width=iw))
            notes_lines.append("https://github.com/Code-Leafy/NetLeafyScanner")
            notes_lines.append("")
            notes_lines.extend(textwrap.wrap("3. The donated configs will all be placed inside of this sub:", width=iw))
            notes_lines.append("https://B2n.ir/G2LeafySub")
            notes_lines.append("")
            notes_lines.extend(textwrap.wrap("4. Make sure to star this project on GitHub:", width=iw))
            notes_lines.append("https://github.com/Code-Leafy/G2rayXCodeLeafy")
            eu_codes = ["NL", "DE", "FR", "GB", "IE", "SE", "CH", "AT", "BE", "DK", "NO", "FI", "ES", "IT"]
            if loc_v not in eu_codes and loc_v != "...":
                notes_lines.append("")
                notes_lines.extend(textwrap.wrap("Warning: For faster speed please change your codespace region to Europe West.", width=iw))
            while notes_lines and not notes_lines[0]:
                notes_lines.pop(0)
            if notes_lines and ph_notes >= 3:
                draw_box(scr, pr + ph_server, lc, ph_notes, pw1, "Notes", c_color(3))
                for i, line in enumerate(notes_lines):
                    if i >= ph_notes - 2:
                        break
                    col = c_color(5) if "Warning" in line else (c_color(3) if "http" in line else c_color(4))
                    safe_add(scr, pr + ph_server + 1 + i, lc + 2, line, col)
            elif ph_notes > 0:
                draw_box(scr, pr + ph_server, lc, ph_notes, pw1, "Notes", c_color(3))
            draw_box(scr, pr, rc, listh, pw2, f"Config IPs ({len(nodes)})", c_color(3))
            safe_add(scr, pr + listh - 2, rc + 2, " [\u2190\u2192] move  [a] add  [d] del  [e] edit ", c_color(6))
            row = pr + 1
            maxrows = listh - 3
            start = max(0, cfg_sel - maxrows + 1)
            if maxrows > 0:
                for i in range(start, min(len(nodes), start + maxrows)):
                    nd = nodes[i]
                    line = f"{i+1:>2}. {nd.get('name','')[:15]:<15} {nd.get('ip','')}"
                    if i == cfg_sel:
                        safe_add(scr, row, rc + 1, ("\u258c " + line).ljust(pw2 - 2)[:pw2-2], c_color(6) | curses.A_BOLD)
                    else:
                        safe_add(scr, row, rc + 3, line[:pw2-4], c_color(1))
                    row += 1
            if not nodes:
                safe_add(scr, pr + 2, rc + 3, "No configs yet \u2014 press [a]", c_color(4))

def screen_logs(scr, h, w, mx, mw):
    global log_scroll_offset, log_search_term
    bh = max(3, h - 3)
    draw_box(scr, 3, mx, bh, mw, "xray.log", c_color(2))
    if os.path.exists(XRAY_LOG):
        try:
            with open(XRAY_LOG) as f:
                lines = f.readlines()
            if log_search_term:
                lines = [l for l in lines if log_search_term.lower() in l.lower()]
            if not lines:
                safe_add(scr, 4, mx + 2, "(log is empty or no match)", c_color(4))
            else:
                max_scroll = max(0, len(lines) - (bh - 2))
                log_scroll_offset = min(max_scroll, log_scroll_offset)
                start_idx = len(lines) - (bh - 2) - log_scroll_offset
                if start_idx < 0:
                    start_idx = 0
                disp_lines = lines[start_idx : start_idx + (bh - 2)]
                for i, l in enumerate(disp_lines):
                    if 4 + i < 3 + bh - 1:
                        safe_add(scr, 4 + i, mx + 2, l.rstrip()[:mw - 4], c_color(4))
        except Exception as e:
            log_sys_err(f"screen_logs: {e}")

def screen_sub_qr(scr):
    sub_url = f"https://{SUB_DOMAIN}/"
    h, w = scr.getmaxyx()
    scr.erase()
    title = " Subscription Link & QR "
    safe_add(scr, max(0, h // 2 - 12), max(0, (w - len(title)) // 2), title, c_color(1) | curses.A_BOLD)
    url_y = max(1, h // 2 - 10)
    safe_add(scr, url_y, max(0, (w - len(sub_url)) // 2), sub_url, c_color(2) | curses.A_UNDERLINE)
    qr_start = url_y + 2
    try:
        out = subprocess.check_output(["qrencode", "-m", "1", "-t", "UTF8", sub_url], text=True, stderr=subprocess.DEVNULL)
        lines = out.splitlines()
        if len(lines) > (h - qr_start - 2):
            out = subprocess.check_output(["qrencode", "-m", "0", "-t", "UTF8", sub_url], text=True, stderr=subprocess.DEVNULL)
            lines = out.splitlines()
        if lines:
            qr_w = len(lines[0])
            start_x = max(2, (w - qr_w) // 2)
            safe_add(scr, qr_start, start_x, "\u2588" * qr_w, c_color(1))
            for i, line in enumerate(lines):
                if qr_start + 1 + i >= h - 2:
                    break
                safe_add(scr, qr_start + 1 + i, start_x, line, c_color(1))
    except FileNotFoundError:
        safe_add(scr, qr_start, max(0, (w - 45) // 2), "qrencode missing: sudo apt install qrencode", c_color(5))
    except Exception as e:
        log_sys_err(f"screen_sub_qr: {e}")
        safe_add(scr, qr_start, max(0, (w - 40) // 2), "(QR unavailable \u2014 copy the link above)", c_color(5))
    msg = " Press any key to return "
    safe_add(scr, h - 1, max(0, (w - len(msg)) // 2), msg, c_color(4))
    scr.refresh()
    blocking_getch(scr)
    scr.clear()

def main(scr):
    global nav_current, wake_lock_active, engine_running, cfg_sel, log_scroll_offset, log_search_term
    try:
        curses.set_escdelay(25)
    except Exception:
        pass
    curses.start_color()
    curses.use_default_colors()
    if curses.COLORS >= 256:
        curses.init_pair(1, curses.COLOR_WHITE, -1)
        curses.init_pair(2, 40, -1)
        curses.init_pair(3, 45, -1)
        curses.init_pair(4, 245, -1)
        curses.init_pair(5, 196, -1)
        curses.init_pair(6, curses.COLOR_WHITE, 22)
    else:
        curses.init_pair(1, curses.COLOR_WHITE, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        curses.init_pair(3, curses.COLOR_CYAN, -1)
        curses.init_pair(4, curses.COLOR_BLUE, -1)
        curses.init_pair(5, curses.COLOR_RED, -1)
        curses.init_pair(6, curses.COLOR_WHITE, curses.COLOR_GREEN)
    curses.curs_set(0)
    scr.keypad(True)
    splash_and_update(scr)
    scr.nodelay(True)
    scr.timeout(500)
    while True:
        h, w = scr.getmaxyx()
        
        if h < 12 or w < 40:
            scr.erase()
            msg = "Please enlarge terminal"
            try:
                scr.addstr(max(0, h//2), max(0, (w - len(msg))//2), msg)
            except Exception:
                pass
            scr.refresh()
            k = scr.getch()
            if k == curses.KEY_RESIZE:
                try:
                    curses.update_lines_cols()
                except Exception:
                    pass
                scr.clear()
            elif k in (ord('q'), ord('Q')):
                break
            continue

        h = max(1, h)
        w = max(1, w)
        sbw = 26 if w >= 80 else 0
        nav_h = 1 if sbw == 0 else 0
        mx = sbw + 2 if sbw > 0 else 1
        mw = max(1, w - mx - 1)
        content_h = max(1, h - nav_h)
        
        try:
            scr.erase()
            if sbw > 0:
                render_sidebar(scr, h, w, mx)
            else:
                render_bottombar(scr, h, w)
            render_topbar(scr, mx, mw)
            if nav_current == 1:
                screen_dashboard(scr, content_h, w, mx, mw)
            elif nav_current == 2:
                screen_settings(scr, content_h, w, mx, mw)
            elif nav_current == 3:
                screen_configgen(scr, content_h, w, mx, mw)
            elif nav_current == 4:
                screen_logs(scr, content_h, w, mx, mw)
            scr.refresh()
        except curses.error:
            pass

        k = scr.getch()
        
        if k == curses.KEY_RESIZE:
            try:
                curses.update_lines_cols()
            except Exception:
                pass
            scr.clear()
            continue

        if k == -1:
            continue
            
        with settings_lock:
            nodes_len = len(settings.get("nodes", []))
            
        if k in (curses.KEY_UP, 259):
            if nav_current == 4:
                log_scroll_offset += 1
            else:
                nav_current = (nav_current - 2) % 4 + 1
        elif k in (curses.KEY_DOWN, 258):
            if nav_current == 4:
                log_scroll_offset = max(0, log_scroll_offset - 1)
            else:
                nav_current = (nav_current % 4) + 1
        elif k == ord('1'):
            nav_current = 1
        elif k == ord('2'):
            nav_current = 2
        elif k == ord('3'):
            nav_current = 3
        elif k == ord('4'):
            nav_current = 4
        elif k in (curses.KEY_LEFT, 260):
            if nav_current == 3 and nodes_len > 0:
                cfg_sel = max(0, cfg_sel - 1)
        elif k in (curses.KEY_RIGHT, 9, 261):
            if nav_current == 3 and nodes_len > 0:
                cfg_sel = min(nodes_len - 1, cfg_sel + 1)
        elif k in (ord('q'), ord('Q')):
            break
        elif k in (ord('s'), ord('S')):
            if nav_current == 4:
                term = popup_ask(scr, "Search term (empty to clear)", log_search_term)
                if term is not None:
                    log_search_term = term.strip()
                    log_scroll_offset = 0
            else:
                start_xray()
                popup_msg(scr, "Engine", "Xray core has been started!")
        elif k in (ord('f'), ord('F')) and nav_current == 4:
            log_scroll_offset = 0
        elif k in (ord('x'), ord('X')):
            stop_xray()
            popup_msg(scr, "Engine", "Xray core has been stopped!")
        elif k in (ord('r'), ord('R')):
            start_xray()
            popup_msg(scr, "Engine", "Xray core has been restarted!")
            
        if nav_current == 2:
            if k == ord('5'):
                with settings_lock:
                    cur_cap = str(settings.get("data_cap_gb", 999999))
                ans = popup_ask(scr, "Data cap GB (999999 = Unlimited)", cur_cap)
                if ans is not None and ans.strip().isdigit():
                    with settings_lock:
                        settings["data_cap_gb"] = int(ans.strip())
                    save_settings()
            elif k == ord('6'):
                with settings_lock:
                    cur_upd = str(settings.get("sub_update_mins", 1))
                ans = popup_ask(scr, "Sub Update Interval (mins)", cur_upd)
                if ans is not None and ans.strip().isdigit():
                    with settings_lock:
                        settings["sub_update_mins"] = max(1, int(ans.strip()))
                    save_settings()
            elif k == ord('7'):
                wake_lock_active = not wake_lock_active
                save_settings()
                if wake_lock_active:
                    threading.Thread(target=lambda: state.update(
                        wake_ok=wake_lock_pulse(), wake_last=time.time()), daemon=True).start()
            elif k == ord('8'):
                if popup_confirm(scr, "Reset", "Reset settings to defaults?"):
                    with settings_lock:
                        settings["data_cap_gb"] = 999999
                        settings["sub_update_mins"] = 1
                    save_settings()
            elif k in (ord('u'), ord('U')):
                if popup_confirm(scr, "Rotate UUID", "Generate a new UUID and restart Xray?"):
                    try:
                        with open(UUID_FILE, "w") as f:
                            f.write(str(uuid.uuid4()))
                    except Exception as e:
                        log_sys_err(f"Rotate UUID write: {e}")
                    start_xray()
                    build_subscription(force=True)
                    popup_msg(scr, "UUID Rotated", "New UUID generated and Xray restarted.")
                    
        elif nav_current == 3:
            if k in (ord('a'), ord('A')):
                ip = popup_ask(scr, "New config IP / domain")
                if ip and ip.strip():
                    name = popup_ask(scr, "Config name (label)", f"Node-{nodes_len+1}")
                    with settings_lock:
                        settings["nodes"].append({"ip": ip.strip(), "name": (name or ip).strip()})
                        cfg_sel = len(settings["nodes"]) - 1
                    update_and_build()
            elif k in (ord('d'), ord('D')) and nodes_len > 0:
                with settings_lock:
                    target_name = settings["nodes"][cfg_sel].get("name", "")
                if popup_confirm(scr, "Delete", f"Delete config '{target_name}'?"):
                    with settings_lock:
                        settings["nodes"].pop(cfg_sel)
                        cfg_sel = max(0, cfg_sel - 1)
                    update_and_build()
            elif k in (ord('e'), ord('E')) and nodes_len > 0:
                with settings_lock:
                    nd = settings["nodes"][cfg_sel]
                    cur_ip = nd.get("ip", "")
                    cur_name = nd.get("name", "")
                ip = popup_ask(scr, "Edit IP / domain", cur_ip)
                if ip is not None and ip.strip():
                    name = popup_ask(scr, "Edit name", cur_name)
                    with settings_lock:
                        nd_ref = settings["nodes"][cfg_sel]
                        nd_ref["ip"] = ip.strip()
                        if name is not None:
                            nd_ref["name"] = name.strip() or ip.strip()
                    update_and_build()
            elif k in (ord('b'), ord('B')):
                build_subscription(force=True)
                popup_msg(scr, "Done", "Subscription rebuilt with latest configs.")
            elif k in (ord('t'), ord('T')):
                global donate_label
                with state_lock:
                    is_donating = state["donate_active"]
                    
                if is_donating:
                    if popup_confirm(scr, "Stop Donating", "Remove your config from the community list?"):
                        def _rev():
                            ok, msg = donate_revoke()
                            with state_lock:
                                state["donate_active"] = False
                                state["donate_msg"] = "stopped"
                        threading.Thread(target=_rev, daemon=True).start()
                        popup_msg(scr, "Stopped", "Your config is being removed from the list.")
                else:
                    if not _webhook_configured():
                        popup_msg(scr, "Not configured", "Set DONATE_WEBHOOK_URL in g2leafy.py first.")
                    elif popup_confirm(scr, "Donate Config", "Keep sharing a live config with the community?"):
                        donate_label = f"\U0001F343G2Leafy | {GITHUB_USER}"
                        popup_msg(scr, "Donating", "Registering with relay in background...")
                        def _don():
                            ok, msg = donate_heartbeat()
                            with state_lock:
                                if ok:
                                    state["donate_active"] = True
                                state["donate_msg"] = msg
                        threading.Thread(target=_don, daemon=True).start()
            elif k in (ord('g'), ord('G')):
                screen_sub_qr(scr)
                
        elif nav_current == 4:
            if k in (ord('c'), ord('C')):
                try:
                    open(XRAY_LOG, "w").close()
                    log_scroll_offset = 0
                except Exception as e:
                    log_sys_err(f"clear xray log: {e}")

if __name__ == "__main__":
    load_settings()
    full_cleanup()
    start_xray()
    build_subscription(force=True)
    threading.Thread(target=system_monitor_thread, daemon=True).start()
    threading.Thread(target=xray_monitor_thread, daemon=True).start()
    threading.Thread(target=sub_server_thread, daemon=True).start()
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    finally:
        engine_running = False
        save_persistent_data()
        if state.get("donate_active"):
            try:
                donate_revoke()
            except Exception:
                pass
