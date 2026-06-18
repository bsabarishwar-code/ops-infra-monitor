"""
OPS-Infra Agent — runs silently on the client machine.
Polls relay for a trigger, runs all health checks, posts the report back.
App ID is read from own filename (agent-{app_id}.exe).
Store ID / Name fetched from relay on startup.
RELAY_URL / RELAY_SECRET / DASHBOARD_TOKEN baked in at build time.
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
RELAY_URL       = "PLACEHOLDER_RELAY_URL"
RELAY_SECRET    = "PLACEHOLDER_RELAY_SECRET"
DASHBOARD_BASE  = "https://dashboard-api.tangoeye.ai"
DASHBOARD_TOKEN = "PLACEHOLDER_DASHBOARD_TOKEN"
# ── app monitoring constants ─────────────────────────────────────────────────
APP_EXE            = "TangoEyeStreamer.exe"
STREAM_FOLDER_ROOT = r"C:\ProgramData\Tango_IT\Tango_Eye_Streamer"
# ───────────────────────────────────────────────────────────────────────────

POLL_INTERVAL = 15
_NO_WIN = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
_HDR    = {"X-Secret": RELAY_SECRET}


def _get_app_id():
    exe  = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
    name = os.path.splitext(os.path.basename(exe))[0]   # "agent-Auk1eb4f78"
    if name.startswith("agent-"):
        return name[len("agent-"):]
    return name


APP_ID     = _get_app_id()
STORE_ID   = APP_ID   # overwritten in main() after relay lookup
STORE_NAME = APP_ID


def _fetch_store_config():
    try:
        r = requests.get(f"{RELAY_URL}/config/{APP_ID}", headers=_HDR, timeout=10)
        data = r.json()
        return data.get("store_id", APP_ID), data.get("store_name", APP_ID)
    except Exception:
        return APP_ID, APP_ID


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
        "stream_id":     str(_pick(c, "streamId", "stream_id", "streamName", "stream_name", default="")),
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
    for _ in range(2):
        ok, lat = False, None
        if ping3:
            try:
                r = ping3.ping("8.8.8.8", timeout=2)
                if r:
                    ok, lat = True, round(r * 1000, 2)
            except Exception:
                pass
        if not ok:
            try:
                t = time.perf_counter()
                with socket.create_connection(("8.8.8.8", 53), timeout=2):
                    ok, lat = True, round((time.perf_counter() - t) * 1000, 2)
            except Exception:
                pass
        results.append({"success": ok, "latency": lat})
        time.sleep(0.2)
    succ = sum(1 for r in results if r["success"])
    lats = [r["latency"] for r in results if r["latency"] is not None]
    return {"connected": succ >= 1,
            "packet_loss": round((2 - succ) * 100 / 2, 1),
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
                           capture_output=True, text=True, timeout=15, creationflags=_NO_WIN)
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

def _rtsp_check(url, timeout=3):
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


# ── app monitoring ────────────────────────────────────────────────────────────
def _check_process(exe_name):
    for proc in psutil.process_iter(['name', 'pid', 'status', 'create_time']):
        try:
            if proc.info['name'].lower() == exe_name.lower():
                started    = datetime.datetime.fromtimestamp(proc.info['create_time']).isoformat()
                uptime_h   = round((time.time() - proc.info['create_time']) / 3600, 1)
                return {'running': True, 'pid': proc.info['pid'],
                        'status': proc.info['status'],
                        'started_at': started, 'uptime_hours': uptime_h}
        except Exception:
            pass
    return {'running': False, 'pid': None, 'status': 'not found',
            'started_at': None, 'uptime_hours': None}


def _get_crash_events(exe_name, days=2):
    if platform.system() != "Windows":
        return []
    app = exe_name.replace(".exe", "")
    ps = (
        "$ErrorActionPreference='SilentlyContinue'\n"
        f"$start=(Get-Date).AddDays(-{days})\n"
        "$ev=Get-WinEvent -FilterHashtable @{LogName='Application';Id=1000,1001,1002;StartTime=$start}"
        " -ErrorAction SilentlyContinue\n"
        f"$filt=$ev|Where-Object{{$_.Message -like '*{app}*'}}\n"
        "$list=foreach($e in $filt){"
        "[PSCustomObject]@{time=$e.TimeCreated.ToString('o');id=$e.Id;level=$e.LevelDisplayName;"
        "msg=(($e.Message -split \"`r?`n\"|Where-Object{$_.Trim()}|Select-Object -First 3)-join' | ')}}\n"
        "$list|ConvertTo-Json -Compress"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=15, creationflags=_NO_WIN)
        data = json.loads((r.stdout or "").strip())
        if isinstance(data, dict):
            data = [data]
        events = [{"time": d.get("time", ""), "id": str(d.get("id", "")),
                   "level": d.get("level", ""), "msg": (d.get("msg") or "").strip()}
                  for d in data]
        events.sort(key=lambda e: e["time"], reverse=True)
        return events
    except Exception:
        return []


def _check_stream_folders(cameras):
    results = []
    seen   = set()
    cutoff = time.time() - 3600

    # List all subfolders once; folder names are {stream_id}_{date}_{time},{seq}
    try:
        all_folders = [e for e in os.listdir(STREAM_FOLDER_ROOT)
                       if os.path.isdir(os.path.join(STREAM_FOLDER_ROOT, e))]
    except Exception:
        all_folders = []

    def _mtime(fname):
        try:
            return os.path.getmtime(os.path.join(STREAM_FOLDER_ROOT, fname))
        except Exception:
            return 0

    for cam in cameras:
        sid = (cam.get("stream_id") or cam.get("stream_name") or "").strip()
        if not sid or sid in seen:
            continue
        seen.add(sid)

        # Match folders that start with this stream ID (handles timestamp suffix
        # and the API returning slightly truncated IDs)
        matching = [f for f in all_folders if f.startswith(sid)]

        if not matching:
            results.append({
                "stream_id":        sid,
                "camera_number":    cam.get("camera_number", "—"),
                "ip":               cam.get("ip", "—"),
                "folder_exists":    False,
                "last_modified":    None,
                "recent_images_1h": 0,
                "total_images":     0,
                "status":           "missing",
            })
            continue

        # Use the most recently modified folder among all windows
        best   = max(matching, key=_mtime)
        folder = os.path.join(STREAM_FOLDER_ROOT, best)

        last_modified = None
        recent        = 0
        total         = 0

        try:
            last_modified = datetime.datetime.fromtimestamp(
                os.path.getmtime(folder)).isoformat()
        except Exception:
            pass
        try:
            for fn in os.listdir(folder):
                if fn.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp', '.webp')):
                    total += 1
                    try:
                        if os.path.getmtime(os.path.join(folder, fn)) > cutoff:
                            recent += 1
                    except Exception:
                        pass
        except Exception:
            pass

        status = "active" if recent > 0 else ("stale" if total > 0 else "empty")

        results.append({
            "stream_id":        sid,
            "camera_number":    cam.get("camera_number", "—"),
            "ip":               cam.get("ip", "—"),
            "folder_exists":    True,
            "last_modified":    last_modified,
            "recent_images_1h": recent,
            "total_images":     total,
            "status":           status,
        })
    return results


def check_app_status(cameras):
    process        = _check_process(APP_EXE)
    crashes        = _get_crash_events(APP_EXE, days=2)
    stream_folders = _check_stream_folders(cameras)
    return {
        "app_exe":        APP_EXE,
        "process":        process,
        "crashes":        crashes,
        "stream_folders": stream_folders,
        "summary": {
            "process_running": process.get("running", False),
            "crash_count_2d":  len(crashes),
            "streams_total":   len(stream_folders),
            "streams_active":  sum(1 for s in stream_folders if s["status"] == "active"),
            "streams_stale":   sum(1 for s in stream_folders if s["status"] == "stale"),
            "streams_missing": sum(1 for s in stream_folders if s["status"] == "missing"),
        },
    }


# ── main run ──────────────────────────────────────────────────────────────────
def run_checks():
    cams = fetch_cameras()

    with ThreadPoolExecutor(max_workers=max(12, len(cams) + 8)) as ex:
        # Camera checks + all system checks run fully in parallel
        cam_futures  = [ex.submit(check_camera, c) for c in cams]
        f_internet   = ex.submit(check_internet)
        f_network    = ex.submit(get_network_info)
        f_system     = ex.submit(check_system)
        f_antivirus  = ex.submit(check_antivirus)
        f_av_list    = ex.submit(get_antivirus_details)
        f_wifi       = ex.submit(get_wifi_change_logs, 24)
        f_sleep      = ex.submit(get_sleep_wake_logs, 24)
        f_app        = ex.submit(check_app_status, cams)

        cam_results  = [f.result() for f in cam_futures]
        internet     = f_internet.result()
        network      = f_network.result()
        system       = f_system.result()
        antivirus    = f_antivirus.result()
        av_list      = f_av_list.result()
        wifi         = f_wifi.result()
        sleep_logs   = f_sleep.result()
        app_status   = f_app.result()

    return {
        "app_id":         APP_ID,
        "store_id":       STORE_ID,
        "store_name":     STORE_NAME,
        "timestamp":      datetime.datetime.now().isoformat(),
        "internet":       internet,
        "network":        network,
        "system":         system,
        "antivirus":      antivirus,
        "antivirus_list": av_list,
        "wifi_changes":   wifi,
        "sleep_logs":     sleep_logs,
        "cameras":        cam_results,
        "app_status":     app_status,
        "summary": {
            "total_cameras":   len(cam_results),
            "cameras_passing": sum(1 for c in cam_results if c["ping"] == "OK"),
            "rtsp_working":    sum(1 for c in cam_results if c.get("rtsp") in ("WORKING", "REACHABLE")),
        },
    }


def main():
    global STORE_ID, STORE_NAME
    _add_to_startup()
    STORE_ID, STORE_NAME = _fetch_store_config()
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
