"""
OPS-Infra v3 - Remote Infrastructure Monitoring via Agent
Enter an App ID -> triggers the agent on the client machine via relay ->
displays the full health report when the agent responds.
"""
import tkinter as tk
from tkinter import ttk, messagebox
import requests
import json
import threading
import datetime
import socket
import platform
import os
import sys
import time

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ===========================================================================
# CONFIG  (loaded from config.json next to the EXE)
# ===========================================================================
def _config_path():
    if getattr(sys, "frozen", False):
        user_cfg = os.path.join(os.path.dirname(sys.executable), "config.json")
        if os.path.exists(user_cfg):
            return user_cfg
        return os.path.join(sys._MEIPASS, "config.json")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

_DEFAULT_CONFIG = {
    "dashboard_base_url": "https://dashboard-api.tangoeye.ai",
    "dashboard_token":    "",
    "auth_style":         "bearer",
    "relay_url":          "",
    "relay_secret":       "",
}

def _load_config():
    path = _config_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
            merged = dict(_DEFAULT_CONFIG)
            merged.update({k: v for k, v in data.items() if k in _DEFAULT_CONFIG})
            return merged
        except Exception:
            pass
    if not getattr(sys, "frozen", False):
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(_DEFAULT_CONFIG, fh, indent=2)
        except Exception:
            pass
    return dict(_DEFAULT_CONFIG)

_cfg         = _load_config()
RELAY_URL    = _cfg["relay_url"].rstrip("/")
RELAY_SECRET = _cfg["relay_secret"]
_RELAY_HDR   = {"X-Secret": RELAY_SECRET}

POLL_TIMEOUT   = 300   # seconds to wait for agent response
POLL_INTERVAL  = 4     # seconds between relay polls

# ===========================================================================
# UI CONSTANTS
# ===========================================================================
COL_BG     = "#f4f6f8"
COL_HEADER = "#1f2a37"
COL_CARD   = "#ffffff"
COL_BORDER = "#e2e8f0"
COL_TEXT   = "#1f2937"
COL_MUTED  = "#6b7280"
COL_ACCENT = "#2563eb"
COL_GREEN  = "#16a34a"
COL_AMBER  = "#d97706"
COL_RED    = "#dc2626"
COL_DARK   = "#0f172a"

STATE_COLORS = {"ok": COL_GREEN, "warn": COL_AMBER, "bad": COL_RED, "muted": COL_MUTED}


def _camera_health(cam):
    ping = cam.get("ping", "")
    rtsp = cam.get("rtsp", "")
    if ping == "OK" and rtsp in ("WORKING", "REACHABLE"):
        return "ok",   "Online"
    if ping == "OK":
        return "warn", "Partial"
    return "bad", "Offline"


# ===========================================================================
# RELAY HELPERS
# ===========================================================================
def _relay_trigger(app_id):
    requests.post(f"{RELAY_URL}/trigger/{app_id}", headers=_RELAY_HDR, timeout=10)

def _relay_get_report(app_id):
    r = requests.get(f"{RELAY_URL}/report/{app_id}", headers=_RELAY_HDR, timeout=10)
    return r.json()


# ===========================================================================
# MAIN APP
# ===========================================================================
class OPSInfraApp:
    def __init__(self, root):
        self.root           = root
        self.current_app_id = None
        self.current_results= None

        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        raw = min(sw / 1920, sh / 1080)
        self.S = max(0.75, min(round(raw, 2), 2.0))
        ww = int(sw * 0.88); wh = int(sh * 0.88)
        root.geometry(f"{ww}x{wh}+{(sw-ww)//2}+{(sh-wh)//2}")
        root.minsize(self._s(900), self._s(560))
        root.title("OPS-Infra v3 · Remote Infrastructure Monitoring")
        root.configure(bg=COL_HEADER)

        self._build_ui()
        self._tick_clock()

    def _s(self, n):
        return max(1, int(n * self.S))

    def _f(self, n):
        if n <= 9:
            return n
        return max(9, int(n * self.S))

    # ------------------------------------------------------------------
    def _build_ui(self):
        self._build_header()
        self._build_controls()
        self._build_info_strip()
        self._build_tiles()
        self._build_tabs()
        self._build_footer()

    def _build_header(self):
        hdr = tk.Frame(self.root, bg=COL_HEADER, height=self._s(54))
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="OPS-Infra v3", bg=COL_HEADER, fg="white",
                 font=("Segoe UI", self._f(18), "bold")).pack(side="left", padx=self._s(18), pady=self._s(10))
        tk.Label(hdr, text="Remote Infrastructure Monitoring", bg=COL_HEADER,
                 fg="#94a3b8", font=("Segoe UI", self._f(10))).pack(side="left", pady=self._s(10))
        self.clock_label = tk.Label(hdr, text="", bg=COL_HEADER, fg="#94a3b8",
                                     font=("Segoe UI", self._f(10)))
        self.clock_label.pack(side="right", padx=self._s(18))

    def _build_controls(self):
        bar = tk.Frame(self.root, bg=COL_BG, pady=self._s(8))
        bar.pack(fill="x", padx=self._s(18), pady=(self._s(6), 0))
        tk.Label(bar, text="App ID", bg=COL_BG, fg=COL_TEXT,
                 font=("Segoe UI", self._f(10), "bold")).pack(side="left")
        self.app_entry = tk.Entry(bar, font=("Segoe UI", self._f(11)),
                                   width=18, relief="solid", bd=1)
        self.app_entry.pack(side="left", padx=(self._s(8), self._s(14)), ipady=self._s(4))
        self.app_entry.bind("<Return>", lambda _: self.run_monitoring())
        self.run_btn = tk.Button(bar, text="▶  Run Monitoring",
                                  font=("Segoe UI", self._f(10), "bold"),
                                  bg=COL_ACCENT, fg="white",
                                  activebackground="#1d4ed8", activeforeground="white",
                                  relief="flat", bd=0,
                                  padx=self._s(16), pady=self._s(6),
                                  command=self.run_monitoring)
        self.run_btn.pack(side="left")
        self.controls_status = tk.Label(bar, text="", bg=COL_BG, fg=COL_MUTED,
                                         font=("Segoe UI", self._f(9)))
        self.controls_status.pack(side="right")

    def _build_info_strip(self):
        strip = tk.Frame(self.root, bg=COL_BG)
        strip.pack(fill="x", padx=self._s(18), pady=(self._s(4), 0))
        specs = [("store","STORE ID"),("brand","BRAND / CLIENT"),("cameras","CAMERAS"),
                 ("host","MONITOR HOST"),("network","NETWORK"),("checked","LAST CHECKED")]
        self.info = {}
        for i, (key, caption) in enumerate(specs):
            col = tk.Frame(strip, bg=COL_BG)
            col.grid(row=0, column=i, padx=self._s(8), sticky="w")
            tk.Label(col, text=caption, bg=COL_BG, fg=COL_MUTED,
                     font=("Segoe UI", self._f(7), "bold")).pack(anchor="w")
            v = tk.Label(col, text="—", bg=COL_BG, fg=COL_TEXT,
                         font=("Segoe UI", self._f(10), "bold"))
            v.pack(anchor="w")
            self.info[key] = v

    def _build_tiles(self):
        row = tk.Frame(self.root, bg=COL_BG)
        row.pack(fill="x", padx=self._s(18), pady=self._s(6))
        specs = [("internet","INTERNET",""),("cpu","CPU",""),("ram","RAM",""),
                 ("disk","DISK",""),("cameras_ok","CAMERAS ONLINE",""),("rtsp_ok","RTSP WORKING","")]
        self.tiles = {}
        for i, (key, label, _) in enumerate(specs):
            card = tk.Frame(row, bg=COL_CARD, relief="flat",
                            highlightbackground=COL_BORDER, highlightthickness=1)
            card.grid(row=0, column=i, padx=self._s(4), sticky="nsew")
            row.columnconfigure(i, weight=1)
            tk.Label(card, text=label, bg=COL_CARD, fg=COL_MUTED,
                     font=("Segoe UI", self._f(7), "bold")).pack(anchor="w", padx=self._s(10), pady=(self._s(6),0))
            val = tk.Label(card, text="—", bg=COL_CARD, fg=COL_MUTED,
                           font=("Segoe UI", self._f(18), "bold"))
            val.pack(anchor="w", padx=self._s(10))
            sub = tk.Label(card, text="", bg=COL_CARD, fg=COL_MUTED,
                           font=("Segoe UI", self._f(8)))
            sub.pack(anchor="w", padx=self._s(10), pady=(0, self._s(6)))
            self.tiles[key] = {"val": val, "sub": sub}

    def _build_tabs(self):
        outer = tk.Frame(self.root, bg=COL_BG)
        outer.pack(fill="both", expand=True, padx=self._s(18), pady=(0, self._s(4)))
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TNotebook", background=COL_BG, borderwidth=0)
        style.configure("TNotebook.Tab", font=("Segoe UI", self._f(9), "bold"),
                         padding=[self._s(12), self._s(5)])
        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)

        self._build_tab_camera()
        self._build_tab_rtsp()
        self._build_tab_wifi()
        self._build_tab_sleep()
        self._build_tab_av()

    def _make_tree(self, parent, cols, headings, widths, anchors):
        style = ttk.Style()
        style.configure("OPS.Treeview", font=("Segoe UI", self._f(9)),
                         rowheight=self._s(26), background=COL_CARD,
                         fieldbackground=COL_CARD, foreground=COL_TEXT)
        style.configure("OPS.Treeview.Heading", font=("Segoe UI", self._f(9), "bold"),
                         background=COL_BG, foreground=COL_TEXT, relief="flat")
        frame = tk.Frame(parent, bg=COL_CARD)
        frame.pack(fill="both", expand=True)
        sb = ttk.Scrollbar(frame, orient="vertical")
        sb.pack(side="right", fill="y")
        tree = ttk.Treeview(frame, columns=cols, show="headings",
                             yscrollcommand=sb.set, style="OPS.Treeview")
        sb.config(command=tree.yview)
        tree.pack(fill="both", expand=True)
        for c, h, w, a in zip(cols, headings, widths, anchors):
            aa = "w" if a == "w" else ("center" if a == "center" else "w")
            tree.heading(c, text=h, anchor=aa)
            tree.column(c, width=self._s(w), anchor=aa, minwidth=self._s(40), stretch=True)
        for tag, bg, fg in [("ok","#f0fdf4","#15803d"),("warn","#fffbeb","#92400e"),("bad","#fef2f2","#b91c1c")]:
            tree.tag_configure(tag, background=bg, foreground=fg)
        return tree

    def _bind_col_resize(self, tree, proportions):
        def _resize(event):
            w = max(100, event.width - 4)
            for col, pct in proportions.items():
                tree.column(col, width=max(40, int(w * pct)))
        tree.bind("<Configure>", _resize)

    def _build_tab_camera(self):
        tab = tk.Frame(self.notebook, bg=COL_CARD)
        self.notebook.add(tab, text="Camera Status")
        cols = ("status","camera","ip","mfr","ping","lat","reason")
        hdrs = ("Status","Camera","IP Address","Manufacturer","Ping","Latency","Offline Reason")
        wids = (110,90,130,150,70,80,250)
        anch = ("w","w","w","w","center","center","w")
        self.tree = self._make_tree(tab, cols, hdrs, wids, anch)
        self._bind_col_resize(self.tree, {"status":0.13,"camera":0.09,"ip":0.15,
                                           "mfr":0.17,"ping":0.07,"lat":0.09,"reason":0.30})
        self.tree.insert("", "end", values=("","","Enter an App ID and click Run Monitoring","","","",""))

    def _build_tab_rtsp(self):
        tab = tk.Frame(self.notebook, bg=COL_CARD)
        self.notebook.add(tab, text="RTSP Status")
        # No live streaming — cameras are on the remote client network.
        # Shows the RTSP check results from the agent report.
        info = tk.Frame(tab, bg="#f8fafc", pady=self._s(10))
        info.pack(fill="x", padx=self._s(12), pady=self._s(8))
        tk.Label(info, text="Camera streams are on the client network — live preview is not available here.",
                 bg="#f8fafc", fg=COL_MUTED, font=("Segoe UI", self._f(9))).pack(anchor="w")
        tk.Label(info, text="RTSP check results from the agent are shown below.",
                 bg="#f8fafc", fg=COL_MUTED, font=("Segoe UI", self._f(9))).pack(anchor="w")
        cols = ("camera","ip","rtsp","rtsp_url")
        hdrs = ("Camera","IP Address","RTSP Status","RTSP URL")
        wids = (100,130,120,400)
        anch = ("w","w","center","w")
        self.rtsp_tree = self._make_tree(tab, cols, hdrs, wids, anch)
        self._bind_col_resize(self.rtsp_tree, {"camera":0.10,"ip":0.14,"rtsp":0.12,"rtsp_url":0.64})
        self.rtsp_tree.insert("", "end", values=("","","","Waiting for agent report..."))

    def _build_tab_wifi(self):
        tab = tk.Frame(self.notebook, bg=COL_CARD)
        self.notebook.add(tab, text="Wi-Fi History 24h")
        cols = ("time","event","ssid")
        self.wifi_tree = self._make_tree(tab, cols, ("Time","Event","SSID"),
                                          (160,110,300), ("w","w","w"))
        self._bind_col_resize(self.wifi_tree, {"time":0.22,"event":0.18,"ssid":0.60})

    def _build_tab_sleep(self):
        tab = tk.Frame(self.notebook, bg=COL_CARD)
        self.notebook.add(tab, text="Sleep / Power Logs")
        cols = ("time","event","detail")
        self.sleep_tree = self._make_tree(tab, cols, ("Time","Event","Detail"),
                                           (160,110,400), ("w","w","w"))
        self._bind_col_resize(self.sleep_tree, {"time":0.20,"event":0.15,"detail":0.65})

    def _build_tab_av(self):
        tab = tk.Frame(self.notebook, bg=COL_CARD)
        self.notebook.add(tab, text="Antivirus")
        cols = ("name","status","defs","updated")
        self.av_tree = self._make_tree(tab, cols, ("Product","Status","Definitions","Last Updated"),
                                        (200,120,120,200), ("w","center","center","w"))
        self._bind_col_resize(self.av_tree, {"name":0.35,"status":0.18,"defs":0.18,"updated":0.29})

    def _build_footer(self):
        foot = tk.Frame(self.root, bg=COL_DARK, height=self._s(36))
        foot.pack(fill="x", side="bottom")
        foot.pack_propagate(False)
        self.status_label = tk.Label(foot, text="● Ready — enter an App ID to begin",
                                      bg=COL_DARK, fg=COL_MUTED,
                                      font=("Segoe UI", self._f(9)))
        self.status_label.pack(side="left", padx=self._s(16), pady=self._s(8))
        note_frame = tk.Frame(foot, bg=COL_DARK)
        note_frame.pack(side="right", padx=self._s(16))
        tk.Label(note_frame, text="Note:", bg=COL_DARK, fg=COL_MUTED,
                 font=("Segoe UI", self._f(9))).pack(side="left")
        self.note_entry = tk.Entry(note_frame, font=("Segoe UI", self._f(9)),
                                    width=40, relief="flat", bg="#1e293b", fg="white",
                                    insertbackground="white")
        self.note_entry.pack(side="left", padx=self._s(6), ipady=self._s(3))
        tk.Button(note_frame, text="Save Note",
                  font=("Segoe UI", self._f(9)), bg="#334155", fg="white",
                  relief="flat", bd=0, padx=self._s(8), pady=self._s(3),
                  command=self.save_comment).pack(side="left")
        self.controls_status2 = tk.Label(note_frame, text="", bg=COL_DARK, fg=COL_MUTED,
                                          font=("Segoe UI", self._f(8)))
        self.controls_status2.pack(side="left", padx=self._s(6))

    # ------------------------------------------------------------------
    def _tick_clock(self):
        self.clock_label.config(
            text=datetime.datetime.now().strftime("%a %d %b %Y · %H:%M:%S"))
        self.root.after(1000, self._tick_clock)

    def _set_busy(self, busy, message=""):
        if busy:
            self.run_btn.config(state="disabled", bg="#64748b")
            self.controls_status.config(text=message, fg=COL_MUTED)
        else:
            self.run_btn.config(state="normal", bg=COL_ACCENT)
            self.controls_status.config(text=message, fg=COL_MUTED)

    def _set_tile(self, key, value, state, sub=""):
        t = self.tiles[key]
        t["val"].config(text=value, fg=STATE_COLORS.get(state, COL_TEXT))
        t["sub"].config(text=sub)

    def _show_error(self, msg):
        self._set_busy(False, "Error")
        self.controls_status.config(fg=COL_RED)
        messagebox.showerror("Error", msg)

    # ------------------------------------------------------------------
    def run_monitoring(self):
        app_id = self.app_entry.get().strip()
        if not app_id:
            messagebox.showwarning("App ID required", "Please enter an App ID (e.g. Auk1eb4f78).")
            return
        if not RELAY_URL:
            messagebox.showerror("Relay not configured",
                                  "relay_url is missing from config.json.")
            return
        self.current_app_id = app_id
        self._set_busy(True, "Sending trigger to agent …")
        threading.Thread(target=self._worker, args=(app_id,), daemon=True).start()

    def _worker(self, app_id):
        # 1. Send trigger
        try:
            _relay_trigger(app_id)
        except Exception as e:
            self.root.after(0, self._show_error, f"Cannot reach relay server:\n{e}")
            return

        trigger_time = datetime.datetime.now(datetime.timezone.utc)
        deadline     = time.time() + POLL_TIMEOUT

        # 2. Poll for a fresh report (reported_at > trigger_time)
        while time.time() < deadline:
            elapsed = int(time.time() - (deadline - POLL_TIMEOUT))
            self.root.after(0, lambda e=elapsed: self._set_busy(
                True, f"Waiting for agent… {e}s  (agent runs checks in ~60–90 s)"))
            time.sleep(POLL_INTERVAL)
            try:
                data = _relay_get_report(app_id)
            except Exception:
                continue
            report      = data.get("report")
            reported_at = data.get("reported_at")
            if not report or not reported_at:
                continue
            try:
                rep_dt = datetime.datetime.fromisoformat(reported_at.replace("Z", "+00:00"))
                if rep_dt <= trigger_time:
                    continue   # stale report from a previous run
            except Exception:
                continue
            self.root.after(0, self._display_results, report)
            return

        self.root.after(0, self._show_error,
                        "Agent did not respond within 5 minutes.\n"
                        "Make sure the agent is running on the client machine.")

    # ------------------------------------------------------------------
    def _display_results(self, results):
        self._set_busy(False, "Completed")
        self.current_results = results

        cams      = results.get("cameras", [])
        sysd      = results.get("system", {})
        inet      = results.get("internet", {})
        net       = results.get("network", {})
        summ      = results.get("summary", {})

        # Info strip
        ts = results.get("timestamp", "")
        try:
            ts_disp = datetime.datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            ts_disp = ts
        store_name = results.get("store_name", results.get("store_id", "—"))
        self.info["store"].config(text=results.get("app_id", results.get("store_id", "—")))
        self.info["brand"].config(text=store_name)
        self.info["cameras"].config(text=str(len(cams)))
        self.info["host"].config(text=net.get("hostname", "—"))
        self.info["network"].config(text=net.get("network_name", "—"))
        self.info["checked"].config(text=ts_disp or "—")

        # Tiles
        if inet.get("connected"):
            self._set_tile("internet", "Online", "ok",
                           f"{inet.get('avg_latency_ms','?')} ms · {inet.get('packet_loss',0)}% loss")
        else:
            self._set_tile("internet", "Down", "bad",
                           f"{inet.get('packet_loss',0)}% packet loss")

        if "error" in sysd:
            for k in ("cpu","ram","disk"):
                self._set_tile(k, "N/A", "muted")
        else:
            cpu  = sysd.get("cpu_percent", 0)
            ram  = sysd.get("ram_percent", 0)
            disk = sysd.get("disk_percent", 0)
            self._set_tile("cpu",  f"{cpu}%",
                           "bad" if cpu>80 else ("warn" if cpu>60 else "ok"))
            self._set_tile("ram",  f"{ram}%",
                           "bad" if ram>85 else ("warn" if ram>70 else "ok"))
            self._set_tile("disk", f"{disk}%",
                           "bad" if disk>90 else ("warn" if disk>75 else "ok"),
                           f"uptime {sysd.get('uptime_hours',0)} h")

        total   = summ.get("total_cameras", len(cams)) or 0
        ping_ok = summ.get("cameras_passing", 0)
        rtsp_ok = summ.get("rtsp_working", 0)
        self._set_tile("cameras_ok", f"{ping_ok}/{total}",
                       "ok" if total and ping_ok==total else ("warn" if ping_ok else "bad"))
        self._set_tile("rtsp_ok", f"{rtsp_ok}/{total}",
                       "ok" if total and rtsp_ok==total else ("warn" if rtsp_ok else "bad"))

        # Tab 0: Camera Status
        self.tree.delete(*self.tree.get_children())
        status_text = {"ok":"● Online","warn":"◑ Partial","bad":"○ Offline"}
        for cam in cams:
            state, _ = _camera_health(cam)
            lat    = f"{cam['ping_latency_ms']} ms" if cam.get("ping_latency_ms") else "—"
            reason = cam.get("ping_reason","") if cam.get("ping") != "OK" else ""
            self.tree.insert("","end",tags=(state,), values=(
                status_text.get(state,"○ Unknown"),
                cam.get("camera_number","—"), cam.get("ip","—"),
                cam.get("manufacturer","—"), cam.get("ping","—"), lat, reason))
        self.notebook.tab(0, text=f"Camera Status ({len(cams)})")

        # Tab 1: RTSP Status (from agent report — no live video)
        self.rtsp_tree.delete(*self.rtsp_tree.get_children())
        for cam in cams:
            rtsp  = cam.get("rtsp","—")
            tag   = "ok" if rtsp in ("WORKING","REACHABLE") else ("warn" if rtsp=="SKIPPED" else "bad")
            url   = cam.get("rtsp_url","—")
            masked = url.replace(cam.get("password",""), "****") if cam.get("password") else url
            self.rtsp_tree.insert("","end",tags=(tag,), values=(
                cam.get("camera_number","—"), cam.get("ip","—"), rtsp, masked[:80]))
        self.notebook.tab(1, text=f"RTSP Status ({len(cams)})")

        # Tab 2: Wi-Fi History
        wifi = results.get("wifi_changes",[])
        self.wifi_tree.delete(*self.wifi_tree.get_children())
        if wifi:
            for w in wifi:
                t   = (w.get("time","") or "")[:19].replace("T"," ")
                tag = "ok" if w.get("event")=="Connected" else "warn"
                self.wifi_tree.insert("","end",tags=(tag,),
                                      values=(t, w.get("event",""), w.get("ssid","")))
        else:
            self.wifi_tree.insert("","end",
                                  values=("","","No Wi-Fi changes in the last 24h"))
        self.notebook.tab(2, text=f"Wi-Fi History 24h ({len(wifi)})")

        # Tab 3: Sleep / Power
        sleep_logs = results.get("sleep_logs",[])
        self.sleep_tree.delete(*self.sleep_tree.get_children())
        if sleep_logs:
            for s in sleep_logs:
                t  = (s.get("time","") or "")[:19].replace("T"," ")
                ev = s.get("event","")
                tag= "bad" if ev=="Power loss" else ("warn" if ev in ("Sleep","Shutdown") else "ok")
                self.sleep_tree.insert("","end",tags=(tag,),
                                       values=(t, ev, s.get("detail","")))
        else:
            self.sleep_tree.insert("","end",
                                   values=("","","No sleep / power events in the last 24h"))
        self.notebook.tab(3, text=f"Sleep / Power Logs ({len(sleep_logs)})")

        # Tab 4: Antivirus
        av_list = results.get("antivirus_list",[])
        self.av_tree.delete(*self.av_tree.get_children())
        if av_list:
            for a in av_list:
                en, up = a.get("enabled"), a.get("up_to_date")
                if en is None:
                    st, defs, tag = "Unknown","Unknown","warn"
                else:
                    st   = "● Enabled" if en else "○ Disabled"
                    defs = "Up to date" if up else "Out of date"
                    tag  = "ok" if (en and up) else ("warn" if en else "bad")
                self.av_tree.insert("","end",tags=(tag,),
                                    values=(a.get("name","—"), st, defs,
                                            (a.get("updated","") or "")[:25] or "—"))
        else:
            fb = results.get("antivirus",{}).get("antivirus_name","")
            self.av_tree.insert("","end",
                                values=(fb or "No antivirus product detected","","",""))
        self.notebook.tab(4, text=f"Antivirus ({len(av_list)})")

        # Footer
        if total and inet.get("connected") and ping_ok==total and rtsp_ok==total:
            self.status_label.config(text="● All systems healthy",  fg=COL_GREEN)
        elif total and ping_ok==0:
            self.status_label.config(text="● Cameras unreachable",  fg=COL_RED)
        else:
            self.status_label.config(text="● Issues detected",      fg=COL_AMBER)

    # ------------------------------------------------------------------
    def save_comment(self):
        comment = self.note_entry.get().strip()
        if not comment:
            messagebox.showwarning("Empty note","Please type a note before saving.")
            return
        entry = {"timestamp": datetime.datetime.now().isoformat(),
                 "app_id": self.current_app_id or "Unknown",
                 "comment": comment}
        try:
            with open("ops_infra_v3_comments.json","a",encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            self.note_entry.delete(0, tk.END)
            self.controls_status2.config(text="Note saved ✓", fg=COL_GREEN)
            self.root.after(3000, lambda: self.controls_status2.config(text=""))
        except Exception as e:
            self.controls_status2.config(text=f"Save failed: {e}", fg=COL_RED)


# ===========================================================================
if __name__ == "__main__":
    if platform.system() == "Windows":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    root = tk.Tk()
    app  = OPSInfraApp(root)
    root.mainloop()
