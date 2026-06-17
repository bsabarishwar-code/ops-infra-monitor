"""
OPS-Infra Agent — runs silently on the client machine.
Polls relay for a trigger, runs all health checks, posts the report back.
APP_ID/STORE_ID/RELAY_URL/RELAY_SECRET/DASHBOARD_TOKEN replaced at build time.
"""
import os, sys, json, time, socket, platform, subprocess, datetime, winreg
from concurrent.futures import ThreadPoolExecutor
import requests, psutil
import urllib3; urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    import ping3; ping3.DEBUG = False
except ImportError:
    ping3 = None

# ── baked-in at build time ──────────────────────────────────────────────────
APP_ID          = "PLACEHOLDER_APP_ID"
STORE_ID        = "PLACEHOLDER_STORE_ID"
STORE_NAME      = "PLACEHOLDER_STORE_NAME"
RELAY_URL       = "PLACEHOLDER_RELAY_URL"
RELAY_SECRET    = "PLACEHOLDER_RELAY_SECRET"
DASHBOARD_BASE  = "https://dashboard-api.tangoeye.ai"
DASHBOARD_TOKEN = "PLACEHOLDER_DASHBOARD_TOKEN"
# ───────────────────────────────────────────────────────────────────────────

POLL_INTERVAL   = 15
_NO_WIN = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
_HDR    = {"X-Secret": RELAY_SECRET}


# ── startup registration ─────────────────────────────────────────────────────
def _add_to_startup():
    if platform.system() != "Windows":
        return
    exe = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Run",
                           0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(k, "OPS-Infra-Agent", 0, winreg.REG_SZ, f'"{exe}"')
        winreg.CloseKey(k)
    except Exception:
        pass


# ── relay communication ───────────────────────────────────────────────────────
def _poll():
    try:
        return requests.get(f"{RELAY_URL}/poll/{APP_ID}", headers=_HDR, timeout=10).json().get("triggered", False)
    except Exception:
        return False

def _ack():
    try:
        requests.post(f"{RELAY_URL}/ack/{APP_ID}", headers=_HDR, timeout=10)
    except Exception:
        pass

def _post_report(report):
    try:
        requests.post(f"{RELAY_URL}/report/{APP_ID}",
                      data=json.dumps(report),
                      headers={**_HDR, "Content-Type": "application/json"},
                      timeout=60)
    except Exception:
        pass


# ── dashboard camera fetch ────────────────────────────────────────────────────
def _pick(d, *keys, default=None):
    if not isinstance(d, dict):
        return default
    low = {k.lower(): v for k, v in d.items()}
    for k in keys:
        v = low.get(k.lower())
        if v not in (None, "", []):
            return v
    return default

def _extract_cameras(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("cameras", "data", "result", "results", "items", "payload"):
            val = payload.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                r = _extract_cameras(val)
                if r:
                    return r
    return []

def _norm(c):
    active = _pick(c, "isActivated", "active", "isActive", "enabled", "status", default=True)
    up     = _pick(c, "isUp", "up", default=True)
    return {
        "ip":            _pick(c, "ip", "cameraIp", "camera_ip", "ipAddress"),
        "username":      _pick(c, "username", "user", "camUsername", "login", default="admin"),
        "password":      _pick(c, "password", "pass", "camPassword", "pwd", default=""),
        "rtsp":          _pick(c, "rtsp", "rtspUrl", "rtsp_url", "streamUrl", "url"),
        "camera_number": str(_pick(c, "cameraNumber", "camera_number", "cameraName", "name", "id", default="CAM")),
        "manufacturer":  _pick(c, "manufacturer", "make", "brand", "vendor", default="Unknown"),
        "stream_name":   str(_pick(c, "streamName", "stream_name", default="")),
        "active":        active not in (False, 0, "0", "false", "False", "inactive", "disabled", "DOWN"),
        "is_up":         up     not in (False, 0, "0", "false", "False", "DOWN", "down"),
    }

def fetch_cameras():
    today = datetime.date.today().isoformat()
    url   = (DASHBOARD_BASE.rstrip("/") +
             f"/v3/edgeapp/getAllCameraStreamData?storeId={STORE_ID}&date={today}"
             "&searchValue=&filterByStatus=&filterByProduct=&filterByZone=")
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {DASHBOARD_TOKEN}",
                                          "Accept": "application/json"},
                            timeout=30, verify=True)
        if resp.status_code != 200:
            return []
        cams = [_norm(c) for c in _extract_cameras(resp.json()) if isinstance(c, dict)]
        return [c for c in cams if c["active"] and (c["ip"] or c["rtsp"])]
    except Exception:
        return []


# ── health checks ─────────────────────────────────────────────────────────────
def check_internet():
    results = []
    for _ in range(3):
        ok, lat = False, None
        if ping3:
            try:
                r = ping3.ping("8.8.8.8", timeout=3)
                if r:
                    ok, lat = True, round(r * 1000, 2)
            except Exception:
                pass
        else:
            try:
                t = time.perf_counter()
                with socket.create_connection(("8.8.8.8", 53), timeout=3):
                    ok, lat = True, round((time.perf_counter() - t) * 1000, 2)
            except Exception:
                pass
        results.append({"success": ok, "latency": lat})
        time.sleep(0.5)
    succ = sum(1 for r in results if r["success"])
    lats = [r["latency"] for r in results if r["latency"] is not None]
    return {"connected": succ >= 2,
            "packet_loss": round((3 - succ) * 100 / 3, 1),
            "avg_latency_ms": round(sum(lats) / len(lats), 2) if lats else None}

def check_system():
    try:
        return {"cpu_percent":  psutil.cpu_percent(interval=1),
                "ram_percent":  psutil.virtual_memory().percent,
                "disk_percent": psutil.disk_usage("C:\\" if platform.system() == "Windows" else "/").percent,
                "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
                "high_utilization": False}
    except Exception as e:
        return {"error": str(e)}

def check_antivirus():
    try:
        if platform.system() == "Windows":
            r = subprocess.run(["wmic", "/namespace:\\\\root\\SecurityCenter2", "path",
                                 "AntivirusProduct", "get", "displayName"],
                               capture_output=True, text=True, timeout=10, creationflags=_NO_WIN)
            lines = [l.strip() for l in r.stdout.strip().split("\n")[1:] if l.strip()]
            return {"antivirus_name": lines[0] if lines else "None detected"}
    except Exception:
        pass
    return {"antivirus_name": "Unable to detect"}

def _decode_av(state):
    try:
        h = format(int(state) & 0xFFFFFF, "06x")
        return h[2:4] in ("10", "11"), h[4:6] == "00"
    except Exception:
        return None, None

def get_antivirus_details():
    if platform.system() != "Windows":
        return []
    ps = ("$ErrorActionPreference='SilentlyContinue'\n"
          "$av=Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntivirusProduct\n"
          "$list=foreach($a in $av){[PSCustomObject]@{name=$a.displayName;state=$a.productState;ts=$a.timestamp}}\n"
          "$list|ConvertTo-Json -Compress")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=20, creationflags=_NO_WIN)
        data = json.loads((r.stdout or "").strip())
        if isinstance(data, dict):
            data = [data]
        out = []
        for d in data:
            en, utd = _decode_av(d.get("state"))
            out.append({"name": (d.get("name") or "Unknown").strip(),
                        "enabled": en, "up_to_date": utd, "updated": (d.get("ts") or "").strip()})
        return out
    except Exception:
        return []

def get_network_info():
    net = "Unknown"
    try:
        if platform.system() == "Windows":
            r = subprocess.run(["netsh", "wlan", "show", "interfaces"],
                               capture_output=True, text=True, timeout=5, creationflags=_NO_WIN)
            import re
            m = re.search(r"^\s*SSID\s*:\s*(.+)$", r.stdout, re.MULTILINE)
            net = m.group(1).strip() if m else "Ethernet/Wired"
    except Exception:
        pass
    try:
        h = socket.gethostname(); ip = socket.gethostbyname(h)
    except Exception:
        h, ip = "Unknown", "Unknown"
    return {"network_name": net, "hostname": h, "local_ip": ip}

def _ps_events(ps_script, hours, mapping):
    if platform.system() != "Windows":
        return []
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            ps_script.replace("HOURS", str(int(hours)))],
                           capture_output=True, text=True, timeout=40, creationflags=_NO_WIN)
        data = json.loads((r.stdout or "").strip())
        if isinstance(data, dict):
            data = [data]
        return data
    except Exception:
        return []

def get_wifi_change_logs(hours=24):
    ps = ("$ErrorActionPreference='SilentlyContinue'\n"
          "$start=(Get-Date).AddHours(-HOURS)\n"
          "$events=Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-WLAN-AutoConfig/Operational';Id=8001,8003;StartTime=$start}\n"
          "$list=foreach($e in $events){$ssid='';if($e.Message -match '\\bSSID:\\s*(.+)'){$ssid=$Matches[1].Trim()}\n"
          "[PSCustomObject]@{time=$e.TimeCreated.ToString('o');id=$e.Id;ssid=$ssid}}\n"
          "$list|ConvertTo-Json -Compress")
    data = _ps_events(ps, hours, {})
    events = []
    for d in data:
        try:
            evid = int(d.get("id", 0))
            events.append({"time": d.get("time", ""),
                           "event": "Connected" if evid == 8001 else "Disconnected",
                           "ssid": (d.get("ssid") or "").strip() or "(unknown)"})
        except Exception:
            pass
    events.sort(key=lambda e: e["time"], reverse=True)
    return events

def get_sleep_wake_logs(hours=24):
    ps = ("$ErrorActionPreference='SilentlyContinue'\n"
          "$start=(Get-Date).AddHours(-HOURS)\n"
          "$f1=@{LogName='System';ProviderName='Microsoft-Windows-Kernel-Power';Id=42,107,109,41;StartTime=$start}\n"
          "$f2=@{LogName='System';ProviderName='Microsoft-Windows-Power-Troubleshooter';Id=1;StartTime=$start}\n"
          "$events=@(Get-WinEvent -FilterHashtable $f1)+@(Get-WinEvent -FilterHashtable $f2)\n"
          "$list=foreach($e in $events){$line=($e.Message -split \"`r?`n\"|Where-Object{$_.Trim()}|Select-Object -First 1)\n"
          "[PSCustomObject]@{time=$e.TimeCreated.ToString('o');id=$e.Id;msg=$line}}\n"
          "$list|ConvertTo-Json -Compress")
    mapping = {42: ("Sleep","System entering sleep"), 107: ("Wake","System resumed from sleep"),
               1: ("Wake","System returned from a low-power state"),
               41: ("Power loss","Rebooted without a clean shutdown"),
               109: ("Shutdown","Kernel initiated a power transition")}
    data = _ps_events(ps, hours, mapping)
    events = []
    for d in data:
        try:
            evid = int(d.get("id", 0))
            ev, dflt = mapping.get(evid, ("Power event", ""))
            events.append({"time": d.get("time", ""), "event": ev,
                           "detail": (d.get("msg") or "").strip() or dflt})
        except Exception:
            pass
    events.sort(key=lambda e: e["time"], reverse=True)
    return events

def _diagnose(ip):
    for port, label in [(554, "RTSP/554"), (80, "HTTP/80"), (8080, "HTTP/8080")]:
        try:
            with socket.create_connection((ip, port), timeout=2):
                return f"ICMP disabled — host IS reachable on {label}"
        except ConnectionRefusedError:
            return f"Host up — TCP {label} refused"
        except Exception:
            continue
    return "All TCP ports timed out — camera offline or firewalled"

def _rtsp_check(url, timeout=5):
    import re
    m = re.match(r"rtsp://(?:[^@/]+@)?([^:/]+)(?::(\d+))?", url)
    if not m:
        return "INVALID_URL"
    host, port = m.group(1), int(m.group(2)) if m.group(2) else 554
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(f"OPTIONS {url} RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: OPS-Agent\r\n\r\n".encode())
            data = s.recv(1024).decode(errors="ignore")
        return "REACHABLE" if ("200" in data or "401" in data or data) else "NO_RESPONSE"
    except socket.timeout:
        return "TIMEOUT"
    except OSError:
        return "FAILED"

def _rtsp_url(cam):
    if cam.get("rtsp"):
        return cam["rtsp"]
    u = cam.get("username") or ""; p = cam.get("password") or ""
    creds = f"{u}:{p}@" if u else ""
    return f"rtsp://{creds}{cam['ip']}:554/cam/realmonitor?channel=1&subtype=0"

def check_camera(cam):
    ip = cam.get("ip")
    if not ip:
        return {**cam, "ping": "NO_IP", "ping_latency_ms": None,
                "ping_reason": "No IP", "rtsp": "SKIPPED", "rtsp_url": ""}
    ping_status, ping_latency, ping_reason = "NOT_CHECKED", None, ""
    if ping3:
        _tok, _tlat = False, None
        try:
            t = time.perf_counter()
            with socket.create_connection((ip, 554), timeout=1.0):
                _tlat, _tok = round((time.perf_counter() - t) * 1000, 2), True
        except Exception:
            pass
        try:
            r = ping3.ping(ip, timeout=2)
            if r:
                ping_status, ping_latency = "OK", round(r * 1000, 2)
            elif _tok:
                ping_status, ping_latency = "OK", _tlat
            else:
                ping_status, ping_reason = "FAILED", _diagnose(ip)
        except PermissionError:
            if _tok:
                ping_status, ping_latency = "OK", _tlat
            else:
                ping_status, ping_reason = "FAILED", _diagnose(ip)
        except Exception:
            ping_status, ping_reason = "FAILED", _diagnose(ip)
    else:
        try:
            t = time.perf_counter()
            with socket.create_connection((ip, 554), timeout=2):
                ping_status, ping_latency = "OK", round((time.perf_counter() - t) * 1000, 2)
        except Exception:
            ping_status, ping_reason = "FAILED", _diagnose(ip)

    url = _rtsp_url(cam)
    return {
        "camera_number":   cam.get("camera_number", "CAM"),
        "stream_name":     cam.get("stream_name", ""),
        "ip":              ip,
        "manufacturer":    cam.get("manufacturer", "Unknown"),
        "is_up":           cam.get("is_up", True),
        "ping":            ping_status,
        "ping_latency_ms": ping_latency,
        "ping_reason":     ping_reason,
        "rtsp":            _rtsp_check(url) if ping_status == "OK" else "SKIPPED",
        "rtsp_url":        url,
    }


# ── main run ──────────────────────────────────────────────────────────────────
def run_checks():
    cams = fetch_cameras()
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(cams)))) as ex:
        cam_results = list(ex.map(check_camera, cams)) if cams else []
    return {
        "app_id":         APP_ID,
        "store_id":       STORE_ID,
        "store_name":     STORE_NAME,
        "timestamp":      datetime.datetime.now().isoformat(),
        "internet":       check_internet(),
        "network":        get_network_info(),
        "system":         check_system(),
        "antivirus":      check_antivirus(),
        "antivirus_list": get_antivirus_details(),
        "wifi_changes":   get_wifi_change_logs(24),
        "sleep_logs":     get_sleep_wake_logs(24),
        "cameras":        cam_results,
        "summary": {
            "total_cameras":   len(cam_results),
            "cameras_passing": sum(1 for c in cam_results if c["ping"] == "OK"),
            "rtsp_working":    sum(1 for c in cam_results if c.get("rtsp") in ("WORKING", "REACHABLE")),
        },
    }


def main():
    _add_to_startup()
    while True:
        try:
            if _poll():
                _ack()
                _post_report(run_checks())
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
