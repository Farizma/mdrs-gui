#!/usr/bin/env python3
"""
mdrs GUI - a Windows front-end for mediafire_rs (`mdrs`)
https://github.com/NicKoehler/mediafire_rs

Run:   python mdrs_gui.py
Needs: Python 3.9+ (tkinter ships with the python.org installer) and `mdrs` installed
       (cargo install mediafire_rs).
Optional (auto-detected):  pip install tkinterdnd2 pystray pillow
       tkinterdnd2 -> real drag & drop    pystray+pillow -> system-tray mode

How it works
  mdrs only exposes: -i -o -m -t -r -p --proxy-download.  Everything else in the
  blueprint (queue, priorities, pause/resume, speed schedule, MD5/archive checks,
  extraction, stats...) is implemented by this GUI around those flags:
    * one mdrs process per link, downloading into a private staging folder
    * speed/progress = staging-folder growth, pause = suspending the process
    * throttling = duty-cycle suspend/resume, stall timeout = watchdog
"""
import os, sys, re, json, csv, time, shutil, queue, threading, subprocess
import hashlib, zipfile, datetime, html, collections, urllib.request, ctypes
import wave, array, math, io
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

try:
    import winsound
except ImportError:
    winsound = None
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAVE_DND = True
except Exception:
    HAVE_DND = False
try:
    import pystray
    from PIL import Image, ImageDraw
    HAVE_TRAY = True
except Exception:
    HAVE_TRAY = False

IS_WIN = os.name == "nt"
APP_NAME = "mdrs GUI"
DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "MdrsGUI"
DATA_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = DATA_DIR / "settings.json"
STATS_FILE = DATA_DIR / "stats.json"
HISTORY_FILE = DATA_DIR / "history.json"
QUEUE_FILE = DATA_DIR / "queue.json"
PROXY_ACTIVE = DATA_DIR / "proxies_active.txt"

URL_RE = re.compile(r"https?://(?:www\.)?mediafire\.com/[^\s\"'<>,;|]+", re.I)
PH_TEXT = ("Enter links here...\n"
           "  - One MediaFire link per line (recommended)\n"
           "  - Links separated by commas, semicolons or spaces also work\n"
           "  - Folder links and single-file links can be mixed\n"
           "  - Drag & drop .txt / .md / .csv files onto this box to load their links")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
PRIOS = ["Low", "Medium", "High"]
PRIO_RANK = {"High": 0, "Medium": 1, "Low": 2}
CLOSE_ACTIONS = ["Minimize to system tray", "Terminate immediately",
                 "Close after queue finishes"]
POST_ACTIONS = ["Do nothing", "Close application", "Sleep PC"]
SYSTEM_SOUNDS = {"System: Default": "SystemDefault", "System: Asterisk": "SystemAsterisk",
                 "System: Exclamation": "SystemExclamation", "System: Hand": "SystemHand",
                 "System: Exit": "SystemExit"}
# (frequency Hz, milliseconds); frequency 0 = silence. Played with winsound.Beep.
MELODIES = {
    "Chime (rising)": [(660, 120), (880, 120), (1175, 240)],
    "Chime (falling)": [(1175, 120), (880, 120), (660, 240)],
    "Double beep": [(1000, 110), (0, 70), (1000, 110)],
    "Triple beep": [(900, 90), (0, 60), (900, 90), (0, 60), (900, 90)],
    "Fanfare": [(523, 110), (659, 110), (784, 110), (1047, 380)],
    "Urgent alarm": [(1400, 140), (900, 140)] * 3,
    "Soft ding": [(1568, 350)],
    "Low tone": [(260, 450)],
    "Victory": [(784, 100), (784, 100), (784, 100), (622, 320), (698, 100), (698, 100),
                (698, 100), (587, 420)],
}
CUSTOM_WAV = "Custom WAV file…"
NO_TAG = "(No tag)"

TIPS = {
    "output_dir": "Folder where finished downloads are saved. Downloads are first staged in a hidden .mdrs_stage folder inside it.",
    "tag": "Label applied to links you add next. Pick (No tag) for none, or type a new name to create a tag. Tags only color/filter rows.",
    "prio": "Queue priority for links you add next. High links start before Medium, Medium before Low.",
    "search": "Filter the queue instantly by link, tag, status or note.",
    "txt_links": "Paste MediaFire links here (one per line, or separated by commas/spaces). You can also drop text files onto this box.",
    "drop_zone": "Drop .txt/.md files here to add every MediaFire link inside them straight to the queue.",
    "auto_cleanup": "Remove a link from the list as soon as it finishes successfully, keeping the queue tidy.",
    "low_resource": "Stop drawing the speed graph to save CPU on very large queues. Speed is still measured and counted.",
    "max_threads": "Simultaneous connections mdrs uses inside one job (mdrs -m). Higher is faster but more likely to get rate-limited.",
    "max_jobs": "How many mdrs processes (links) run at the same time.",
    "host_limit": "Hard cap on simultaneous jobs hitting mediafire.com. The effective limit is the lower of this and 'Max parallel jobs'. Keep low to avoid IP bans.",
    "timeout": "If a running link makes no progress for this many seconds it is killed and treated as failed (then retried if auto-retry is on). 0 disables.",
    "tries": "How many times mdrs itself retries a single file before giving up (mdrs -t).",
    "reverse": "Download the largest files first instead of the default order (mdrs -r).",
    "auto_retry": "Automatically re-queue a failed link right away instead of waiting for you to press Retry.",
    "auto_retries": "Maximum number of automatic re-queues per link before it is marked Failed.",
    "retry_502": "Auto-retry when the server answers 502 Bad Gateway (usually temporary).",
    "retry_503": "Auto-retry when the server answers 503 Service Unavailable (usually temporary).",
    "retry_504": "Auto-retry when the server answers 504 Gateway Timeout (usually temporary).",
    "retry_429": "Auto-retry on 429 Too Many Requests. Consider lowering connections if this keeps happening.",
    "retry_404": "Auto-retry on 404 Not Found. Normally off: a deleted file will never come back.",
    "mirror": "Keep MediaFire's folder structure. When off, every file is placed directly in the output folder (name clashes get ' (1)', ' (2)').",
    "md5": "After each download, check for empty or broken (.zip) files and record an MD5 hash of every file for later 'Force Check'.",
    "md5_policy": "What to do when a downloaded file fails the integrity check: delete it, or keep it so you can inspect it.",
    "extract": "Automatically unpack .zip, .rar and .7z files when their download finishes. .rar/.7z need 7-Zip installed.",
    "extract_delete": "Delete the archive file after it has been extracted successfully.",
    "post_action": "What the app does once the whole queue is finished: nothing, close itself, or put the PC to sleep.",
    "min_free_gb": "The queue is held and a warning shown when free space on the output drive drops below this.",
    "mdrs_path": "Full path to mdrs.exe. Leave blank to find it automatically (PATH or ~/.cargo/bin).",
    "check_updates": "On startup, compare your installed mdrs version with the latest release.",
    "theme": "Switch between light and dark appearance.",
    "close_action": "What the window's X button does: hide to tray, quit immediately, or quit once the queue finishes.",
    "wake_lock": "Prevent Windows from sleeping while downloads are running.",
    "daily_cap_gb": "Stop starting/continuing downloads after this much data today. Resets at midnight. 0 = no cap.",
    "sound": "Sound played when the queue finishes or a link fails.",
    "custom_wav": "Path to your own .wav file, used when 'Custom WAV file...' is selected as the sound.",
    "volume": "Loudness of completion sounds. Applies to built-in melodies and .wav sounds; 'System:' sounds follow the Windows volume.",
    "quiet_on": "Mute sounds and notifications during the hours painted as 'Quiet' in the grid below.",
    "quiet_grid": "Click or drag to paint hours of the week. Choose a brush above: 'Quiet' hours mute alerts.",
    "speed_grid": "Click or drag to paint hours of the week with a speed limit. Pick a brush: Unlimited, Limit A, Limit B or Paused.",
    "limit_a": "Download speed cap (MB/s) used by hours painted 'Limit A'.",
    "limit_b": "Download speed cap (MB/s) used by hours painted 'Limit B'.",
    "flare_url": "Address of your FlareSolverr server. Only the connection test uses it for now; mdrs has no FlareSolverr option yet.",
    "proxy_file": "Text file with one proxy per line (e.g. http://user:pass@host:port). Passed to mdrs with -p.",
    "proxy_download": "Also send the file downloads (not just page requests) through the proxies (--proxy-download).",
    "proxy_cycle_n": "Move to the next proxy in the list after this many completed links. 0 = never rotate.",
    "proxy_type": "Protocol of the single proxy configured below.",
    "proxy_host": "Hostname or IP of the single proxy. Added to the proxy list given to mdrs.",
    "proxy_port": "Port of the single proxy.",
    "proxy_user": "Username for the proxy, if it needs authentication.",
    "proxy_pass": "Password for the proxy, if it needs authentication.",
    "user_agent": "Custom User-Agent string. Saved in your profile; mdrs has no option for it yet.",
    "folder_password": "Password for protected MediaFire folders. Saved in your profile; mdrs has no option for it yet.",
}
# matched by "substring in button text", first match wins
BUTTON_TIPS = [
    ("Start queue", "Start downloading the queue."),
    ("Pause all", "Suspend every running download and stop starting new ones."),
    ("Add to queue", "Add all MediaFire links in the box to the queue."),
    ("Import .txt", "Choose text files and load the MediaFire links they contain into the box."),
    ("Clear finished", "Remove all completed links from the list."),
    ("Retry failed", "Put every failed link back in the queue."),
    ("Pause", "Pause the selected links. Double-click a row to toggle."),
    ("Resume", "Resume the selected links."),
    ("Remove", "Remove the selected links (running ones are stopped and their partial files deleted)."),
    ("Export CSV", "Save lifetime metrics and per-day totals as a CSV file."),
    ("Export HTML", "Save lifetime metrics and per-day totals as a web page."),
    ("Reset lifetime stats", "Zero the lifetime counters. This cannot be undone."),
    ("Force Check", "Recalculate the MD5 of every recorded file and report any that changed or went missing."),
    ("Test connection", "Check that the FlareSolverr server answers at the URL above."),
    ("Rotate now", "Switch to the next proxy for links started from now on."),
    ("Check for updates", "Look up the newest mediafire_rs release now."),
    ("Test sound", "Play the selected sound at the current volume (ignores Quiet Mode)."),
    ("Export log", "Save the console log to a text file."),
    ("Clear all", "Reset the whole grid to the default (first) brush."),
    ("Clear", "Empty the log console."),
    ("Export profile", "Save all settings to a JSON file you can back up or move to another PC."),
    ("Import profile", "Load settings from a previously exported JSON profile."),
    ("Reset to defaults", "Restore every setting to its original value. Queue, statistics and history are kept."),
    ("Browse", "Choose a file or folder."),
]


class Tooltip:
    def __init__(self, app, widget, text):
        self.app, self.w, self.text = app, widget, text
        self.tw = self.job = None
        widget.bind("<Enter>", self.schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def schedule(self, _e=None):
        self.hide()
        self.job = self.w.after(500, self.show)

    def hide(self, _e=None):
        if self.job:
            try:
                self.w.after_cancel(self.job)
            except tk.TclError:
                pass
            self.job = None
        if self.tw:
            try:
                self.tw.destroy()
            except tk.TclError:
                pass
            self.tw = None

    def show(self):
        self.job = None
        if self.tw or not self.app.app_is_foreground():
            return
        p = self.app.pal()
        tw = tk.Toplevel(self.w)
        tw.wm_overrideredirect(True)
        try:
            tw.attributes("-topmost", True)
        except tk.TclError:
            pass
        border = tk.Frame(tw, bg=p["accent"], padx=1, pady=1)
        border.pack()
        text = self.text() if callable(self.text) else self.text
        tk.Label(border, text=text, justify="left", wraplength=340, bg=p["field"], fg=p["fg"],
                 font=("Segoe UI", 9), padx=8, pady=5).pack()
        tw.update_idletasks()
        w, h = tw.winfo_reqwidth(), tw.winfo_reqheight()
        x, y = self.w.winfo_pointerx() + 14, self.w.winfo_pointery() + 20
        sw, sh = tw.winfo_screenwidth(), tw.winfo_screenheight()
        if x + w > sw - 8:
            x = sw - w - 8
        if y + h > sh - 8:
            y = self.w.winfo_pointery() - h - 10
        tw.geometry(f"+{x}+{y}")
        self.tw = tw


def synth_wav(melody, vol):
    """Render a (freq, ms) melody to WAV bytes at the given volume (0-100)."""
    rate = 22050
    amp = 32767 * 0.6 * (vol / 100.0) ** 2
    frames = array.array("h")
    for freq, ms in melody:
        n = int(rate * ms / 1000)
        for i in range(n):
            if freq < 37:
                frames.append(0)
            else:
                env = min(1.0, i / 180.0, (n - i) / 360.0)
                frames.append(int(amp * env * math.sin(2 * math.pi * freq * i / rate)))
        frames.extend([0] * int(rate * 0.015))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames.tobytes())
    return buf.getvalue()


def scaled_wav(path, vol):
    """Return WAV bytes of `path` with amplitude scaled by volume, or None if unsupported."""
    try:
        with wave.open(path, "rb") as r:
            params, data = r.getparams(), r.readframes(r.getnframes())
        g = (vol / 100.0) ** 2
        if params.sampwidth == 2:
            a = array.array("h")
            a.frombytes(data)
            data = array.array("h", (int(x * g) for x in a)).tobytes()
        elif params.sampwidth == 1:
            data = bytes(max(0, min(255, int((x - 128) * g) + 128)) for x in data)
        else:
            return None
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setparams(params)
            w.writeframes(data)
        return buf.getvalue()
    except Exception:
        return None


def media_sounds():
    """Every .wav in the Windows Media folder -> {'Windows: tada': path}"""
    out = {}
    d = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Media"
    try:
        for f in sorted(d.glob("*.wav")):
            out["Windows: " + f.stem] = str(f)
    except OSError:
        pass
    return out


MD5_POLICIES = ["Keep for Manual Inspection", "Delete Corrupted Downloads"]
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
STATUS_ICON = {"Queued": "⏳", "Running": "▶", "Paused": "⏸", "Done": "✔",
               "Failed": "✖", "Verifying": "🔍", "Extracting": "📦"}

DEFAULTS = {
    "mdrs_path": "", "output_dir": str(Path.home() / "Downloads" / "MediaFire"),
    "batch_text": "", "tags": "Games,Videos,Work", "tag": NO_TAG, "prio": "Medium",
    "auto_cleanup": False, "low_resource": False,
    "max_threads": 10, "max_jobs": 2, "host_limit": 2, "timeout": 120,
    "tries": 3, "reverse": False,
    "auto_retry": True, "auto_retries": 3, "retry_502": True, "retry_503": True,
    "retry_504": True, "retry_429": True, "retry_404": False,
    "mirror": True, "md5": False, "md5_policy": MD5_POLICIES[0],
    "extract": False, "extract_delete": False, "post_action": POST_ACTIONS[0],
    "min_free_gb": 2.0,
    "flare_url": "http://localhost:8191/v1", "proxy_cycle_n": 0, "proxy_file": "",
    "proxy_download": False, "proxy_type": "http", "proxy_host": "", "proxy_port": "",
    "proxy_user": "", "proxy_pass": "", "user_agent": "", "folder_password": "",
    "custom_wav": "", "volume": 80, "theme": "Light", "close_action": CLOSE_ACTIONS[0], "wake_lock": False,
    "daily_cap_gb": 0.0, "sound": "Chime (rising)", "quiet_on": False,
    "quiet_grid": "0" * 168, "speed_grid": "0" * 168, "limit_a": 5.0, "limit_b": 1.0,
    "check_updates": True, "geometry": "1150x760",
}

PAL = {
    "Light": dict(bg="#f3f3f3", fg="#1b1b1b", field="#ffffff", accent="#2563eb",
                  sub="#666666", grid="#d0d0d0", sel="#cfe0ff"),
    "Dark": dict(bg="#1e1f22", fg="#e6e6e6", field="#2b2d31", accent="#5b8cff",
                 sub="#9aa0a6", grid="#3a3d42", sel="#33415f"),
}
TAG_COLORS = [("#dbeafe", "#1e3a5f"), ("#fde2e2", "#5a2a2a"), ("#dcfce7", "#1f4d33"),
              ("#fef3c7", "#5a4a1a"), ("#ede9fe", "#3d2f66"), ("#cffafe", "#164e5a")]


# ----------------------------------------------------------------- helpers
def human_size(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.2f} {u}"
        n /= 1024


def human_speed(n):
    return human_size(n) + "/s"


def dir_size(path):
    total = 0
    try:
        for root, _d, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_mdrs(custom=""):
    if custom and os.path.isfile(custom):
        return custom
    p = shutil.which("mdrs")
    if p:
        return p
    cand = Path.home() / ".cargo" / "bin" / ("mdrs.exe" if IS_WIN else "mdrs")
    return str(cand) if cand.exists() else None


def find_7z():
    p = shutil.which("7z") or shutil.which("7za")
    if p:
        return p
    for base in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
        c = os.path.join(base, "7-Zip", "7z.exe")
        if base and os.path.isfile(c):
            return c
    return None


def proc_suspend(p, on):
    try:
        if IS_WIN:
            h = ctypes.windll.kernel32.OpenProcess(0x0800, False, p.pid)
            if h:
                fn = ctypes.windll.ntdll.NtSuspendProcess if on else ctypes.windll.ntdll.NtResumeProcess
                fn(h)
                ctypes.windll.kernel32.CloseHandle(h)
        else:
            import signal
            os.kill(p.pid, signal.SIGSTOP if on else signal.SIGCONT)
    except Exception:
        pass


def proc_kill(p):
    try:
        proc_suspend(p, False)
        p.kill()
    except Exception:
        pass


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    try:
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


class Job:
    _next = 1

    def __init__(self, url, tag, prio):
        self.id = Job._next
        Job._next += 1
        self.url, self.tag, self.prio = url, tag, prio
        self.status = "Queued"
        self.note = ""
        self.tries = 0
        self.proc = None
        self.out = ""
        self.stage = None
        self.size_now = self.size_prev = 0
        self.speed = 0.0
        self.last_act = time.time()
        self.user_paused = False
        self.suspended = False
        self.kill_reason = None
        self.log_tail = collections.deque(maxlen=60)


# ------------------------------------------------------------ grid picker
class GridPicker(ttk.Frame):
    """7x24 clickable/draggable matrix stored as a 168-char digit string."""

    def __init__(self, parent, app, key, names, colors):
        super().__init__(parent)
        self.app, self.key, self.names, self.colors = app, key, names, colors
        self.cw, self.ch, self.lx, self.ty = 21, 19, 36, 18
        self.brush = tk.IntVar(value=1 if len(names) > 1 else 0)
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, text="Brush:").pack(side="left")
        for i, n in enumerate(names):
            tk.Radiobutton(bar, text=n, variable=self.brush, value=i, indicatoron=False,
                           bg=colors[i], fg="#111", selectcolor=colors[i], padx=8,
                           relief="raised", bd=2).pack(side="left", padx=2)
        ttk.Button(bar, text="Clear all", command=self.clear).pack(side="right")
        self.cv = tk.Canvas(self, width=self.lx + 24 * self.cw + 2,
                            height=self.ty + 7 * self.ch + 2, highlightthickness=0)
        self.cv.pack(pady=4)
        app.themed.append(self.cv)
        app.tip(self.cv, key)
        self.cv.bind("<Button-1>", self.paint)
        self.cv.bind("<B1-Motion>", self.paint)
        self.draw()

    def data(self):
        s = str(self.app.S(self.key))
        return s if len(s) == 168 else "0" * 168

    def clear(self):
        self.app.setv(self.key, "0" * 168)
        self.draw()

    def paint(self, e):
        c, r = (e.x - self.lx) // self.cw, (e.y - self.ty) // self.ch
        if 0 <= c < 24 and 0 <= r < 7:
            d = list(self.data())
            d[r * 24 + c] = str(self.brush.get())
            self.app.setv(self.key, "".join(d))
            self.draw()

    def draw(self):
        p = self.app.pal()
        cv, d = self.cv, self.data()
        cv.delete("all")
        cv.configure(bg=p["bg"])
        for h in range(0, 24, 3):
            cv.create_text(self.lx + h * self.cw + 2, 8, text=f"{h:02d}", anchor="w",
                           fill=p["sub"], font=("Segoe UI", 7))
        for r, name in enumerate(DAYS):
            cv.create_text(4, self.ty + r * self.ch + self.ch // 2, text=name, anchor="w",
                           fill=p["fg"], font=("Segoe UI", 8))
            for c in range(24):
                x, y = self.lx + c * self.cw, self.ty + r * self.ch
                cv.create_rectangle(x, y, x + self.cw, y + self.ch, outline=p["grid"],
                                    fill=self.colors[int(d[r * 24 + c])])


# --------------------------------------------------------------------- app
class App:
    def __init__(self):
        self.root = TkinterDnD.Tk() if HAVE_DND else tk.Tk()
        self.root.title("MDRS GUI: Windows Front-End for mediafire_rs")
        self.vars, self.themed = {}, []
        self._save_after = None
        self._init_settings()
        self.root.geometry(self.S("geometry"))
        self.jobs = []
        self.q = queue.Queue()
        self.log_lines = collections.deque(maxlen=20000)
        self.speed_hist = collections.deque([0] * 120, maxlen=120)
        self.stats = load_json(STATS_FILE, {"bytes": 0, "seconds": 0.0, "links": 0, "days": {}})
        self.history = load_json(HISTORY_FILE, [])
        self.global_pause = True
        self.throttled = False
        self.hold_reasons = set()
        self.active_session = False
        self.close_pending = False
        self.proxy_offset = 0
        self.done_since_rotate = 0
        self.tray = None
        self.last_tick = time.time()
        self.disk_flash = False
        self.latest_ver = None

        self.style = ttk.Style()
        self.style.theme_use("clam")
        self._build_ui()
        self._tipped = set()
        self.key_by_var = {str(v): k for k, v in self.vars.items()}
        self.key_by_var[str(self.search)] = "search"
        self._attach_tips(self.root)
        self.tip(self.txt_links, "txt_links")
        self.tip(self.drop, "drop_zone")
        self.apply_theme()
        self._restore_queue()
        self.refresh_tree()
        self.root.bind("<FocusOut>", self._on_focus_out, add="+")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        threading.Thread(target=self._monitor_loop, daemon=True).start()
        self.root.after(1000, self.tick)
        if self.S("check_updates"):
            threading.Thread(target=self.check_updates, daemon=True).start()
        if not find_mdrs(self.S("mdrs_path")):
            self.root.after(500, lambda: messagebox.showwarning(
                APP_NAME, "mdrs was not found.\nInstall it with:  cargo install mediafire_rs\n"
                          "or set its path in the Global tab."))

    # ------------------------------------------------------------ settings
    def _init_settings(self):
        saved = load_json(SETTINGS_FILE, {})
        for k, d in DEFAULTS.items():
            v = saved.get(k, d)
            if isinstance(d, bool):
                var = tk.BooleanVar(value=bool(v))
            elif isinstance(d, int):
                var = tk.IntVar(value=int(v))
            elif isinstance(d, float):
                var = tk.DoubleVar(value=float(v))
            else:
                var = tk.StringVar(value=str(v))
            self.vars[k] = var
            var.trace_add("write", lambda *_: self.schedule_save())

    def S(self, k):
        try:
            return self.vars[k].get()
        except tk.TclError:
            return DEFAULTS[k]

    def setv(self, k, v):
        self.vars[k].set(v)

    def schedule_save(self):
        if self._save_after:
            self.root.after_cancel(self._save_after)
        self._save_after = self.root.after(600, self.save_settings)

    def save_settings(self):
        self._save_after = None
        if hasattr(self, "txt_links"):
            self.vars["batch_text"].set(self.links_text().strip())
        try:
            if self.root.state() == "normal":
                self.vars["geometry"].set(self.root.geometry())
        except tk.TclError:
            pass
        save_json(SETTINGS_FILE, {k: self.S(k) for k in DEFAULTS})

    def export_profile(self):
        p = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")],
                                         initialfile="mdrs_gui_profile.json")
        if p:
            save_json(p, {k: self.S(k) for k in DEFAULTS if k not in ("batch_text", "geometry")})
            messagebox.showinfo(APP_NAME, "Profile exported.")

    def import_profile(self):
        p = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not p:
            return
        data = load_json(p, None)
        if not isinstance(data, dict):
            return messagebox.showerror(APP_NAME, "Not a valid profile file.")
        for k, v in data.items():
            if k in DEFAULTS and k not in ("batch_text", "geometry"):
                try:
                    self.setv(k, v)
                except Exception:
                    pass
        self.apply_theme()
        self.refresh_tags()
        messagebox.showinfo(APP_NAME, "Profile imported.")

    # --------------------------------------------------------------- theme
    def pal(self):
        return PAL.get(self.S("theme"), PAL["Light"])

    def apply_theme(self):
        p, s = self.pal(), self.style
        self.root.configure(bg=p["bg"])
        s.configure(".", background=p["bg"], foreground=p["fg"], fieldbackground=p["field"],
                    bordercolor=p["grid"], lightcolor=p["bg"], darkcolor=p["bg"])
        s.configure("TLabelframe", background=p["bg"], bordercolor=p["grid"])
        s.configure("TLabelframe.Label", background=p["bg"], foreground=p["accent"])
        s.configure("TButton", background=p["field"], foreground=p["fg"], padding=4)
        s.map("TButton", background=[("active", p["sel"])])
        s.configure("TNotebook", background=p["bg"])
        s.configure("TNotebook.Tab", background=p["field"], foreground=p["fg"], padding=(12, 5))
        s.map("TNotebook.Tab", background=[("selected", p["accent"])],
              foreground=[("selected", "#ffffff")])
        s.configure("Treeview", background=p["field"], fieldbackground=p["field"],
                    foreground=p["fg"], rowheight=24)
        s.configure("Treeview.Heading", background=p["bg"], foreground=p["fg"])
        s.map("Treeview", background=[("selected", p["sel"])], foreground=[("selected", p["fg"])])
        s.configure("TCombobox", fieldbackground=p["field"], background=p["field"],
                    foreground=p["fg"], arrowcolor=p["fg"], selectbackground=p["field"],
                    selectforeground=p["fg"])
        s.map("TCombobox",
              fieldbackground=[("readonly", p["field"]), ("disabled", p["bg"])],
              background=[("readonly", p["field"]), ("active", p["sel"])],
              foreground=[("readonly", p["fg"])],
              selectbackground=[("readonly", p["field"])],
              selectforeground=[("readonly", p["fg"])])
        hover = "#2b2d31" if self.S("theme") == "Dark" else "#e6e6e6"
        for w in ("TCheckbutton", "TRadiobutton"):
            s.configure(w, background=p["bg"], foreground=p["fg"])
            s.map(w, background=[("active", hover)], foreground=[("active", p["fg"])],
                  indicatorbackground=[("active", hover), ("pressed", hover)])
        s.configure("TEntry", fieldbackground=p["field"], foreground=p["fg"])
        s.configure("TSpinbox", fieldbackground=p["field"], foreground=p["fg"],
                    background=p["field"], arrowcolor=p["fg"])
        # dropdown popup lists (plain Tk listboxes)
        self.root.option_add("*TCombobox*Listbox.background", p["field"])
        self.root.option_add("*TCombobox*Listbox.foreground", p["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", p["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self._restyle_popdowns(self.root, p)
        s.configure("Sub.TLabel", foreground=p["sub"])
        s.configure("Warn.TLabel", foreground="#d33")
        for w in self.themed:
            try:
                if isinstance(w, tk.Canvas):
                    w.configure(bg=p["field"] if w is getattr(self, "graph", None) else p["bg"])
                elif isinstance(w, tk.Text):
                    w.configure(bg=p["field"], fg=p["fg"], insertbackground=p["fg"])
                    w.tag_configure("ph", foreground=p["sub"])
                elif isinstance(w, tk.Menu):
                    w.configure(bg=p["field"], fg=p["fg"])
            except tk.TclError:
                pass
        self.refresh_tags()
        for g in getattr(self, "grids", []):
            g.draw()
        self.draw_graph(force=True)

    def close_popdowns(self, widget=None):
        for w in (widget or self.root).winfo_children():
            if isinstance(w, ttk.Combobox):
                try:
                    if self.root.tk.call("winfo", "ismapped", str(w) + ".popdown"):
                        self.root.tk.call("ttk::combobox::Unpost", str(w))
                except tk.TclError:
                    pass
            self.close_popdowns(w)

    def _on_focus_out(self, _e=None):
        self.root.after(120, self._check_focus)

    def app_is_foreground(self):
        """True while one of this app's windows (incl. dropdown popups) is the active window."""
        if IS_WIN:
            try:
                hwnd = ctypes.windll.user32.GetForegroundWindow()
                if not hwnd:
                    return True  # in transition - don't guess
                pid = ctypes.c_ulong()
                ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                return pid.value == os.getpid()
            except Exception:
                return True
        try:
            return self.root.focus_displayof() is not None
        except (tk.TclError, KeyError):
            return True

    def _check_focus(self):
        if not self.app_is_foreground():
            self.close_popdowns()

    def _restyle_popdowns(self, widget, p):
        """option_add only affects popups created later; fix existing ones too."""
        for w in widget.winfo_children():
            if isinstance(w, ttk.Combobox):
                try:
                    pop = self.root.tk.call("ttk::combobox::PopdownWindow", str(w))
                    self.root.tk.call(f"{pop}.f.l", "configure", "-background", p["field"],
                                      "-foreground", p["fg"], "-selectbackground", p["accent"],
                                      "-selectforeground", "#ffffff")
                except tk.TclError:
                    pass
            self._restyle_popdowns(w, p)

    def refresh_tags(self):
        names = self.tag_names()
        if hasattr(self, "cb_tag"):
            self.cb_tag["values"] = [NO_TAG] + names
        dark = self.S("theme") == "Dark"
        for i, n in enumerate(names):
            bg = TAG_COLORS[i % len(TAG_COLORS)][1 if dark else 0]
            self.tree.tag_configure(self.tkey(n), background=bg)
        fail_fg = "#ff7b7b" if dark else "#b00020"
        self.tree.tag_configure("st_Failed", foreground=fail_fg)
        self.tree.tag_configure("st_Done", foreground="#3a9d5d")
        self.tree.tag_configure("st_Paused", foreground=self.pal()["sub"])

    @staticmethod
    def tkey(name):
        return "t_" + re.sub(r"\W", "_", name)

    def tag_names(self):
        return [t.strip() for t in self.S("tags").split(",") if t.strip()]

    # ------------------------------------------------------------ UI build
    def _scroll_frame(self, parent):
        outer = ttk.Frame(parent)
        cv = tk.Canvas(outer, highlightthickness=0)
        self.themed.append(cv)
        sb = ttk.Scrollbar(outer, orient="vertical", command=cv.yview)
        inner = ttk.Frame(cv)
        inner.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        win = cv.create_window((0, 0), window=inner, anchor="nw")
        cv.bind("<Configure>", lambda e: cv.itemconfigure(win, width=e.width))
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        cv.bind("<Enter>", lambda e: cv.bind_all("<MouseWheel>", lambda ev: cv.yview_scroll(-1 * (ev.delta // 120), "units")))
        cv.bind("<Leave>", lambda e: cv.unbind_all("<MouseWheel>"))
        return outer, inner

    def _row(self, parent, text):
        f = ttk.Frame(parent)
        f.pack(fill="x", padx=8, pady=2)
        if text:
            ttk.Label(f, text=text).pack(side="left", padx=(0, 14))
        return f

    def chk(self, parent, text, key):
        c = ttk.Checkbutton(parent, text=text, variable=self.vars[key])
        c.pack(anchor="w", padx=8, pady=1)
        return c

    def spin(self, parent, text, key, lo, hi, inc=1, w=8):
        f = self._row(parent, text)
        ttk.Spinbox(f, from_=lo, to=hi, increment=inc, textvariable=self.vars[key], width=w).pack(side="left")

    def scale(self, parent, text, key, lo, hi):
        f = self._row(parent, text)
        var = self.vars[key]
        ttk.Scale(f, from_=lo, to=hi, orient="horizontal", length=260, variable=var).pack(side="left")
        lbl = ttk.Label(f, width=6, text=str(self.S(key)))
        lbl.pack(side="left", padx=6)
        var.trace_add("write", lambda *_: lbl.configure(text=str(self.S(key))))

    def combo(self, parent, text, key, values, w=32):
        f = self._row(parent, text)
        cb = ttk.Combobox(f, values=values, textvariable=self.vars[key], state="readonly", width=w)
        cb.pack(side="left")
        return cb

    def entry(self, parent, text, key, show=None, w=44):
        f = self._row(parent, text)
        e = ttk.Entry(f, textvariable=self.vars[key], show=show, width=w)
        e.pack(side="left")
        return f

    # ---- tooltips
    def tip(self, widget, key_or_text):
        Tooltip(self, widget, TIPS.get(key_or_text, key_or_text))

    def _attach_tips(self, widget):
        for w in widget.winfo_children():
            self._tip_one(w)
            self._attach_tips(w)

    def _button_tip(self, w):
        try:
            text = str(w.cget("text"))
        except tk.TclError:
            return ""
        for sub, tip in BUTTON_TIPS:
            if sub in text:
                return tip
        return text

    def _tip_once(self, w, key_or_text):
        if str(w) not in self._tipped:
            self._tipped.add(str(w))
            self.tip(w, key_or_text)

    def _tip_one(self, w):
        key = None
        for opt in ("variable", "textvariable"):
            try:
                v = str(w.cget(opt))
            except tk.TclError:
                continue
            if v in self.key_by_var:
                key = self.key_by_var[v]
                break
        if key and key in TIPS:
            self._tip_once(w, key)
            if not isinstance(w, ttk.Checkbutton):   # also tip the label next to the control
                gi = w.grid_info()
                for sib in w.master.winfo_children():
                    if not isinstance(sib, ttk.Label) or not str(sib.cget("text")).strip():
                        continue
                    if gi and sib.grid_info().get("row") != gi.get("row"):
                        continue
                    self._tip_once(sib, key)
            return
        if isinstance(w, ttk.Button):
            self._tip_once(w, lambda w=w: self._button_tip(w))
        elif isinstance(w, ttk.Checkbutton):
            text = str(w.cget("text"))
            for sub, tip in BUTTON_TIPS:
                if sub in text:
                    self._tip_once(w, tip)
                    break

    def reset_defaults(self):
        if not messagebox.askyesno(APP_NAME, "Reset all settings to their defaults?\n\n"
                                   "Your queue, statistics and download history are not affected."):
            return
        for k, d in DEFAULTS.items():
            if k not in ("batch_text", "geometry"):
                self.setv(k, d)
        self.apply_theme()
        self.refresh_tags()
        self.refresh_tree()
        self.status.set("Settings reset to defaults.")

    def _build_ui(self):
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=6, pady=(6, 0))
        self.tabs = {}
        for name in ("Queue", "Statistics", "Network", "Settings", "Log Console"):
            fr = ttk.Frame(self.nb)
            self.nb.add(fr, text=f"  {name}  ")
            self.tabs[name] = fr
        self.grids = []
        self._build_queue(self.tabs["Queue"])
        self._build_stats(self.tabs["Statistics"])
        self._build_settings(self.tabs["Settings"])
        self._build_network(self.tabs["Network"])
        self._build_log(self.tabs["Log Console"])
        self.status = tk.StringVar(value="Ready.")
        ttk.Label(self.root, textvariable=self.status, style="Sub.TLabel", anchor="w").pack(fill="x", padx=8, pady=3)

    # ---- Tab 1
    def _build_queue(self, tab):
        top = ttk.Frame(tab)
        top.pack(fill="x", padx=8, pady=6)
        ttk.Label(top, text="Output folder:").pack(side="left")
        ttk.Entry(top, textvariable=self.vars["output_dir"], width=50).pack(side="left", padx=4)
        ttk.Button(top, text="Browse…", command=self.browse_out).pack(side="left")
        self.btn_start = ttk.Button(top, text="▶  Start queue", command=self.toggle_global)
        self.btn_start.pack(side="right")
        self.lbl_warn = ttk.Label(top, text="", style="Warn.TLabel")
        self.lbl_warn.pack(side="right", padx=10)

        box = ttk.LabelFrame(tab, text="Link intake")
        box.pack(fill="x", padx=8, pady=4)
        left = ttk.Frame(box)
        left.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        self.txt_links = tk.Text(left, height=7, wrap="none", relief="flat", bd=1)
        self.txt_links.pack(fill="both", expand=True)
        self.txt_links.tag_configure("ph", foreground=self.pal()["sub"])
        self.ph_active = False
        if self.S("batch_text").strip():
            self.txt_links.insert("1.0", self.S("batch_text"))
        else:
            self._ph_show()
        self.txt_links.bind("<FocusIn>", self._ph_hide)
        self.txt_links.bind("<FocusOut>", self._ph_focus_out)
        self.txt_links.bind("<KeyRelease>", lambda e: self.schedule_save())
        self.themed.append(self.txt_links)
        if HAVE_DND:
            self.txt_links.drop_target_register(DND_FILES)
            self.txt_links.dnd_bind("<<Drop>>", self.on_drop_box)
        right = ttk.Frame(box)
        right.pack(side="right", padx=6, pady=6)
        r1 = ttk.Frame(right)
        r1.pack(fill="x")
        ttk.Label(r1, text="Tag").grid(row=0, column=0, sticky="w")
        self.cb_tag = ttk.Combobox(r1, textvariable=self.vars["tag"], values=[NO_TAG] + self.tag_names(), width=14)
        self.cb_tag.grid(row=0, column=1, padx=4, pady=1)
        ttk.Label(r1, text="Priority").grid(row=1, column=0, sticky="w")
        ttk.Combobox(r1, textvariable=self.vars["prio"], values=PRIOS, state="readonly",
                     width=14).grid(row=1, column=1, padx=4, pady=1)
        ttk.Button(right, text="➕ Add to queue", command=self.add_from_box).pack(fill="x", pady=(6, 2))
        ttk.Button(right, text="📄 Import .txt file…", command=self.import_txt).pack(fill="x")
        label = "⬇ Drop .txt files here" if HAVE_DND else "Drop zone (pip install tkinterdnd2)\nclick to browse"
        self.drop = tk.Label(right, text=label, relief="groove", bd=2, width=24, height=3,
                             cursor="hand2")
        self.drop.pack(fill="x", pady=(6, 0))
        self.drop.bind("<Button-1>", lambda e: self.import_txt())
        if HAVE_DND:
            self.drop.drop_target_register(DND_FILES)
            self.drop.dnd_bind("<<Drop>>", self.on_drop)

        tb = ttk.Frame(tab)
        tb.pack(fill="x", padx=8, pady=4)
        ttk.Label(tb, text="🔎").pack(side="left")
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *_: self.refresh_tree())
        ttk.Entry(tb, textvariable=self.search, width=26).pack(side="left", padx=4)
        for text, cmd in (("⏸ Pause", self.pause_sel), ("▶ Resume", self.resume_sel),
                          ("🗑 Remove", self.remove_sel), ("↻ Retry failed", self.retry_failed),
                          ("🧹 Clear finished", self.clear_finished)):
            ttk.Button(tb, text=text, command=cmd).pack(side="left", padx=2)
        ttk.Checkbutton(tb, text="Auto-clean finished", variable=self.vars["auto_cleanup"]).pack(side="right")

        cols = ("id", "status", "tag", "prio", "size", "speed", "tries", "url")
        wrap = ttk.Frame(tab)
        wrap.pack(fill="both", expand=True, padx=8, pady=4)
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="extended")
        for c, w, t in (("id", 40, "#"), ("status", 110, "Status"), ("tag", 80, "Tag"),
                        ("prio", 70, "Priority"), ("size", 90, "Downloaded"), ("speed", 90, "Speed"),
                        ("tries", 50, "Tries"), ("url", 500, "Link / note")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w", stretch=(c == "url"))
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.toggle_pause_dbl)
        self.tree.bind("<Delete>", lambda e: self.remove_sel())
        self.tree.bind("<Button-3>", self.ctx_menu)
        self.menu = tk.Menu(self.root, tearoff=0)
        self.themed.append(self.menu)
        for lab, cmd in (("Pause", self.pause_sel), ("Resume", self.resume_sel),
                         ("Retry", self.retry_sel), ("Remove", self.remove_sel),
                         ("Open output folder", self.open_out)):
            self.menu.add_command(label=lab, command=cmd)
        self.menu.add_separator()
        for p in PRIOS:
            self.menu.add_command(label=f"Priority: {p}", command=lambda p=p: self.set_prio_sel(p))
        self.menu.add_command(label="Apply current tag", command=self.set_tag_sel)
        self.lbl_summary = ttk.Label(tab, text="", style="Sub.TLabel")
        self.lbl_summary.pack(anchor="w", padx=10, pady=(0, 4))

    # ---- Tab 2
    def _build_stats(self, tab):
        top = ttk.Frame(tab)
        top.pack(fill="x", padx=8, pady=8)
        ttk.Checkbutton(top, text="Low Resource Mode (stop drawing the graph)",
                        variable=self.vars["low_resource"]).pack(side="left")
        ttk.Button(top, text="Export CSV", command=lambda: self.export_stats("csv")).pack(side="right", padx=2)
        ttk.Button(top, text="Export HTML", command=lambda: self.export_stats("html")).pack(side="right", padx=2)
        self.lbl_now = ttk.Label(tab, text="Current speed: 0 B/s", font=("Segoe UI", 12, "bold"))
        self.lbl_now.pack(anchor="w", padx=10)
        self.graph = tk.Canvas(tab, height=240, highlightthickness=1)
        self.graph.pack(fill="x", padx=10, pady=6)
        self.themed.append(self.graph)
        board = ttk.LabelFrame(tab, text="Lifetime metrics")
        board.pack(fill="x", padx=10, pady=8)
        self.stat_vars = {k: tk.StringVar() for k in ("gb", "avg", "links", "today")}
        for i, (k, t) in enumerate((("gb", "Total downloaded"), ("avg", "Average speed"),
                                    ("links", "Links processed"), ("today", "Downloaded today"))):
            ttk.Label(board, text=t + ":").grid(row=i, column=0, sticky="w", padx=10, pady=3)
            ttk.Label(board, textvariable=self.stat_vars[k], font=("Segoe UI", 10, "bold")).grid(
                row=i, column=1, sticky="w", padx=10)
        ttk.Button(board, text="Reset lifetime stats", command=self.reset_stats).grid(
            row=0, column=2, padx=30)

    # ---- Tab 3
    def _build_settings(self, tab):
        outer, f = self._scroll_frame(tab)
        outer.pack(fill="both", expand=True)

        lf = ttk.LabelFrame(f, text="Concurrency & rate controls")
        lf.pack(fill="x", padx=8, pady=6)
        self.scale(lf, "Max connections per job (mdrs -m)", "max_threads", 1, 50)
        self.spin(lf, "Max parallel jobs (mdrs processes)", "max_jobs", 1, 10)
        self.spin(lf, "Per-domain concurrent jobs (mediafire.com)", "host_limit", 1, 10)
        self.scale(lf, "Stall timeout, seconds (0 = off)", "timeout", 0, 600)
        self.spin(lf, "mdrs internal tries per file (-t)", "tries", 1, 20)
        self.chk(lf, "Download largest files first (mdrs -r)", "reverse")

        lf = ttk.LabelFrame(f, text="Error filtering & resiliency")
        lf.pack(fill="x", padx=8, pady=6)
        self.chk(lf, "Instant auto-retry failed links", "auto_retry")
        self.spin(lf, "Max GUI auto-retries per link", "auto_retries", 1, 20)
        rr = ttk.Frame(lf)
        rr.pack(fill="x", padx=8)
        ttk.Label(rr, text="Retry on HTTP:").pack(side="left")
        for c in ("502", "503", "504", "429", "404"):
            ttk.Checkbutton(rr, text=c, variable=self.vars["retry_" + c]).pack(side="left", padx=6)
        ttk.Label(lf, text="Unchecked codes (e.g. 404) are passed over without retrying. "
                           "Network drops/timeouts always retry.", style="Sub.TLabel").pack(anchor="w", padx=8)

        lf = ttk.LabelFrame(f, text="Directory & integrity")
        lf.pack(fill="x", padx=8, pady=6)
        self.chk(lf, "Mirror MediaFire folder hierarchy (off = flatten into output folder)", "mirror")
        self.chk(lf, "Post-download MD5 integrity check", "md5")
        self.combo(lf, "On integrity failure", "md5_policy", MD5_POLICIES)
        ttk.Label(lf, text="Checks: empty files, broken .zip archives; hashes are recorded so "
                           "'Force Check' can detect later corruption.", style="Sub.TLabel").pack(anchor="w", padx=8)
        ttk.Button(lf, text="Force Check history (recalculate MD5)", command=self.force_check).pack(
            anchor="w", padx=8, pady=4)

        lf = ttk.LabelFrame(f, text="Post-download automation")
        lf.pack(fill="x", padx=8, pady=6)
        self.chk(lf, "Auto-extract .zip / .rar / .7z (rar & 7z need 7-Zip installed)", "extract")
        self.chk(lf, "Delete archive after successful extraction", "extract_delete")
        self.combo(lf, "When the queue finishes", "post_action", POST_ACTIONS)

        lf = ttk.LabelFrame(f, text="Pre-execution disk verification")
        lf.pack(fill="x", padx=8, pady=6)
        self.spin(lf, "Warn / hold when free space below (GB)", "min_free_gb", 0, 10000, 0.5)

        self._build_global(f)

        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=8, pady=16)
        ttk.Button(bar, text="↺ Reset to defaults", command=self.reset_defaults).pack(side="left")

    # ---- Tab 4
    def _build_network(self, tab):
        outer, f = self._scroll_frame(tab)
        outer.pack(fill="both", expand=True)
        lf = ttk.LabelFrame(f, text="FlareSolverr tunnel")
        lf.pack(fill="x", padx=8, pady=6)
        r = self.entry(lf, "FlareSolverr URL", "flare_url")
        ttk.Button(r, text="Test connection", command=self.test_flare).pack(side="left", padx=6)
        self.lbl_flare = ttk.Label(lf, text="", style="Sub.TLabel")
        self.lbl_flare.pack(anchor="w", padx=8)
        ttk.Label(lf, text="Note: mdrs has no FlareSolverr flag yet - the URL is saved and tested only.",
                  style="Sub.TLabel").pack(anchor="w", padx=8)

        lf = ttk.LabelFrame(f, text="Proxies (passed to mdrs with -p)")
        lf.pack(fill="x", padx=8, pady=6)
        r = self.entry(lf, "Proxy list file (one proxy per line)", "proxy_file")
        ttk.Button(r, text="Browse…", command=lambda: self._pick_file("proxy_file")).pack(side="left", padx=4)
        self.chk(lf, "Download files through proxies too (--proxy-download)", "proxy_download")
        self.spin(lf, "Rotate proxy every N completed links (0=off)", "proxy_cycle_n", 0, 1000)
        ttk.Button(lf, text="Rotate now", command=self.rotate_proxy).pack(anchor="w", padx=8, pady=2)

        lf = ttk.LabelFrame(f, text="Standalone HTTP / SOCKS5 proxy (separate from FlareSolverr)")
        lf.pack(fill="x", padx=8, pady=6)
        self.combo(lf, "Type", "proxy_type", ["http", "https", "socks5", "socks5h"], 12)
        self.entry(lf, "Host", "proxy_host", w=30)
        self.entry(lf, "Port", "proxy_port", w=10)
        self.entry(lf, "Username", "proxy_user", w=30)
        self.entry(lf, "Password", "proxy_pass", show="•", w=30)

        lf = ttk.LabelFrame(f, text="Request identity & protected folders")
        lf.pack(fill="x", padx=8, pady=6)
        self.entry(lf, "Custom User-Agent", "user_agent", w=60)
        self.entry(lf, "Protected folder password", "folder_password", show="•", w=30)
        ttk.Label(lf, text="Note: mdrs currently exposes no flags for these two - they are stored "
                           "in your profile for when it does.", style="Sub.TLabel").pack(anchor="w", padx=8)

    # ---- Tab 5
    def _build_log(self, tab):
        bar = ttk.Frame(tab)
        bar.pack(fill="x", padx=8, pady=6)
        ttk.Button(bar, text="Export log…", command=self.export_log).pack(side="left")
        ttk.Button(bar, text="Clear", command=self.clear_log).pack(side="left", padx=4)
        self.autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Auto-scroll", variable=self.autoscroll).pack(side="right")
        wrap = ttk.Frame(tab)
        wrap.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log = tk.Text(wrap, wrap="none", state="disabled", font=("Consolas", 9), relief="flat")
        sb = ttk.Scrollbar(wrap, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.themed.append(self.log)

    # ---- Global
    def _build_global(self, f):
        """Global options, placed at the bottom of the Settings tab."""
        lf = ttk.LabelFrame(f, text="Engine")
        lf.pack(fill="x", padx=8, pady=6)
        r = self.entry(lf, "mdrs executable (blank = auto)", "mdrs_path", w=50)
        ttk.Button(r, text="Browse…", command=lambda: self._pick_file("mdrs_path")).pack(side="left", padx=4)
        r = self._row(lf, "")
        self.lbl_ver = ttk.Label(r, text="", style="Sub.TLabel")
        self.lbl_ver.pack(side="left")
        ttk.Button(r, text="Check for updates", command=lambda: threading.Thread(
            target=self.check_updates, args=(True,), daemon=True).start()).pack(side="right")
        self.chk(lf, "Check for updates on startup", "check_updates")

        lf = ttk.LabelFrame(f, text="Window & system")
        lf.pack(fill="x", padx=8, pady=6)
        cb = self.combo(lf, "Theme", "theme", ["Light", "Dark"], 12)
        cb.bind("<<ComboboxSelected>>", lambda e: self.apply_theme())
        self.combo(lf, "Close (X) button action", "close_action", CLOSE_ACTIONS)
        self.chk(lf, "System wake lock while downloading", "wake_lock")
        self.spin(lf, "Daily download cap in GB (0 = none)", "daily_cap_gb", 0, 100000, 1)

        lf = ttk.LabelFrame(f, text="Alerts & Quiet Mode")
        lf.pack(fill="x", padx=8, pady=6)
        self.media = media_sounds()
        self.sound_names = (["None"] + list(MELODIES) + [CUSTOM_WAV] + list(SYSTEM_SOUNDS)
                            + list(self.media))
        if self.S("sound") not in self.sound_names:   # e.g. old saved "Default"
            self.setv("sound", "Chime (rising)")
        self.combo(lf, "Completion sound", "sound", self.sound_names)
        r = self.entry(lf, "Custom .wav file", "custom_wav", w=50)
        ttk.Button(r, text="Browse…", command=self.pick_wav).pack(side="left", padx=4)
        self.scale(lf, "Volume (%)", "volume", 0, 100)
        ttk.Label(lf, text="Volume applies to the built-in melodies and .wav sounds; "
                           "'System:' sounds use the Windows volume.", style="Sub.TLabel").pack(anchor="w", padx=8)
        ttk.Button(self._row(lf, ""), text="▶ Test sound", command=lambda: self.play_sound(force=True)).pack(side="left")
        self.chk(lf, "Enable Quiet Mode (mute sounds/notifications in painted hours)", "quiet_on")
        g = GridPicker(lf, self, "quiet_grid", ["Sound on", "Quiet"], ["#e6e6e6", "#7a8cff"])
        g.pack(padx=8, pady=4, anchor="w")
        self.grids.append(g)

        lf = ttk.LabelFrame(f, text="Weekly speed-limit schedule")
        lf.pack(fill="x", padx=8, pady=6)
        self.spin(lf, "Limit A (MB/s)", "limit_a", 0.1, 1000, 0.5)
        self.spin(lf, "Limit B (MB/s)", "limit_b", 0.1, 1000, 0.5)
        g = GridPicker(lf, self, "speed_grid", ["Unlimited", "Limit A", "Limit B", "Paused"],
                       ["#cfe8cf", "#ffe9a8", "#ffc27a", "#f08a8a"])
        g.pack(padx=8, pady=4, anchor="w")
        self.grids.append(g)
        ttk.Label(lf, text="Throttling works by briefly pausing mdrs, so it is approximate.",
                  style="Sub.TLabel").pack(anchor="w", padx=8)

        lf = ttk.LabelFrame(f, text="Profiles")
        lf.pack(fill="x", padx=8, pady=6)
        r = self._row(lf, "")
        ttk.Button(r, text="Export profile (JSON)…", command=self.export_profile).pack(side="left")
        ttk.Button(r, text="Import profile…", command=self.import_profile).pack(side="left", padx=6)

    # ---------------------------------------------------- small UI actions
    def browse_out(self):
        d = filedialog.askdirectory(initialdir=self.S("output_dir"))
        if d:
            self.setv("output_dir", d)

    def _pick_file(self, key):
        p = filedialog.askopenfilename()
        if p:
            self.setv(key, p)

    def selected(self):
        return [j for j in self.jobs if str(j.id) in self.tree.selection()]

    def ctx_menu(self, e):
        iid = self.tree.identify_row(e.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self.menu.tk_popup(e.x_root, e.y_root)

    def toggle_pause_dbl(self, e):
        for j in self.selected():
            if j.status in ("Running", "Queued", "Paused"):
                j.user_paused = not j.user_paused
        self.update_rows()

    # ---------------------------------------------------------- intake
    def _ph_show(self):
        t = self.txt_links
        t.delete("1.0", "end")
        text = PH_TEXT if HAVE_DND else PH_TEXT.rsplit("\n", 1)[0] + \
            "\n  - (Drag & drop needs:  pip install tkinterdnd2)"
        t.insert("1.0", text, "ph")
        self.ph_active = True

    def _ph_hide(self, _e=None):
        if self.ph_active:
            self.txt_links.delete("1.0", "end")
            self.ph_active = False

    def _ph_focus_out(self, _e=None):
        if not self.ph_active and not self.txt_links.get("1.0", "end").strip():
            self._ph_show()

    def links_text(self):
        return "" if self.ph_active else self.txt_links.get("1.0", "end")

    def set_links_text(self, text):
        self.txt_links.delete("1.0", "end")
        self.ph_active = False
        if text.strip():
            self.txt_links.insert("1.0", text)
        elif self.root.focus_get() is not self.txt_links:
            self._ph_show()

    def on_drop_box(self, ev):
        """Text-like files dropped on the box: load the MediaFire links they contain."""
        links = []
        for p in self.root.tk.splitlist(ev.data):
            try:
                if os.path.isdir(p) or os.path.getsize(p) > 20 * 1024 * 1024:
                    continue
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    links += URL_RE.findall(f.read())
            except OSError:
                continue
        links = list(dict.fromkeys(u.rstrip(".,);]") for u in links))
        if not links:
            self.status.set("No MediaFire links found in the dropped file(s).")
            return ev.action
        self._ph_hide()
        have = self.txt_links.get("1.0", "end").rstrip("\n")
        self.txt_links.insert("end", ("\n" if have else "") + "\n".join(links))
        self.schedule_save()
        self.status.set(f"Loaded {len(links)} link(s) into the box - press 'Add to queue'.")
        return ev.action

    def add_from_box(self):
        text = self.links_text()
        urls = URL_RE.findall(text)
        if not urls:
            return messagebox.showinfo(APP_NAME, "No MediaFire links found in the box.")
        n = self.add_urls(urls)
        self.set_links_text("")
        self.save_settings()
        self.status.set(f"Added {n} link(s).")

    def import_txt(self):
        for p in filedialog.askopenfilenames(filetypes=[("Text / Markdown", "*.txt *.md *.csv *.log"), ("All files", "*.*")]):
            self.ingest_file(p)

    def on_drop(self, ev):
        for p in self.root.tk.splitlist(ev.data):
            self.ingest_file(p)

    def ingest_file(self, p):
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                urls = URL_RE.findall(f.read())
        except OSError as e:
            return messagebox.showerror(APP_NAME, str(e))
        self.status.set(f"Imported {self.add_urls(urls)} link(s) from {os.path.basename(p)}.")

    def add_urls(self, urls):
        tag = self.S("tag").strip()
        if tag == NO_TAG:
            tag = ""
        if tag and tag not in self.tag_names():
            self.setv("tags", ",".join(self.tag_names() + [tag]))
        have = {j.url for j in self.jobs}
        n = 0
        for u in urls:
            u = u.rstrip(".,);]")
            if u not in have:
                self.jobs.append(Job(u, tag, self.S("prio")))
                have.add(u)
                n += 1
        self.refresh_tags()
        self.refresh_tree()
        self.save_queue()
        return n

    def save_queue(self):
        save_json(QUEUE_FILE, [{"url": j.url, "tag": j.tag, "prio": j.prio}
                               for j in self.jobs if j.status != "Done"])

    def _restore_queue(self):
        for d in load_json(QUEUE_FILE, []):
            self.jobs.append(Job(d["url"], d.get("tag", ""), d.get("prio", "Medium")))
        self.refresh_tags()

    # ------------------------------------------------------ queue actions
    def toggle_global(self):
        self.global_pause = not self.global_pause
        self.btn_start.configure(text="▶  Start queue" if self.global_pause else "⏸  Pause all")

    def pause_sel(self):
        for j in self.selected():
            j.user_paused = True
        self.update_rows()

    def resume_sel(self):
        for j in self.selected():
            j.user_paused = False
        self.update_rows()

    def remove_sel(self):
        for j in self.selected():
            self.remove_job(j)
        self.refresh_tree()
        self.save_queue()

    def remove_job(self, j, keep_stage=False):
        if j.proc:
            j.kill_reason = "removed"
            proc_kill(j.proc)
        if j.stage and j.status != "Done" and not keep_stage:
            shutil.rmtree(j.stage, ignore_errors=True)
        if j in self.jobs:
            self.jobs.remove(j)

    def retry_sel(self):
        for j in self.selected():
            if j.status == "Failed":
                self._requeue(j)
        self.refresh_tree()

    def retry_failed(self):
        n = 0
        for j in self.jobs:
            if j.status == "Failed":
                self._requeue(j)
                n += 1
        self.status.set(f"Re-queued {n} failed link(s).")
        self.refresh_tree()

    def _requeue(self, j):
        j.status, j.note, j.tries, j.kill_reason = "Queued", "", 0, None

    def clear_finished(self):
        for j in [j for j in self.jobs if j.status == "Done"]:
            self.jobs.remove(j)
        self.refresh_tree()
        self.save_queue()

    def set_prio_sel(self, p):
        for j in self.selected():
            j.prio = p
        self.refresh_tree()

    def set_tag_sel(self):
        for j in self.selected():
            j.tag = "" if self.S("tag").strip() == NO_TAG else self.S("tag").strip()
        self.refresh_tree()

    def open_out(self):
        d = self.S("output_dir")
        os.makedirs(d, exist_ok=True)
        if IS_WIN:
            os.startfile(d)
        else:
            subprocess.Popen(["xdg-open", d])

    # --------------------------------------------------------- tree render
    def _row_values(self, j):
        st = STATUS_ICON.get(j.status, "") + " " + j.status
        if j.user_paused and j.status in ("Queued", "Running"):
            st = "⏸ Paused"
        if j.status == "Running" and j.suspended and not j.user_paused:
            st = "⏸ Held"
        return (j.id, st, j.tag, j.prio, human_size(j.size_now) if j.size_now else "",
                human_speed(j.speed) if j.status == "Running" and j.speed else "",
                j.tries or "", (j.note + "  " if j.note else "") + j.url)

    def _row_tags(self, j):
        st = "Paused" if j.user_paused else j.status
        return (self.tkey(j.tag), "st_" + st)

    def refresh_tree(self):
        q = self.search.get().strip().lower() if hasattr(self, "search") else ""
        sel = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        for j in sorted(self.jobs, key=lambda j: (PRIO_RANK[j.prio], j.id)):
            if q and q not in (j.url + j.tag + j.status + j.note).lower():
                continue
            self.tree.insert("", "end", iid=str(j.id), values=self._row_values(j), tags=self._row_tags(j))
        keep = [i for i in sel if self.tree.exists(i)]
        if keep:
            self.tree.selection_set(keep)
        self.update_summary()

    def update_rows(self):
        for j in self.jobs:
            iid = str(j.id)
            if self.tree.exists(iid):
                self.tree.item(iid, values=self._row_values(j), tags=self._row_tags(j))
        self.update_summary()

    def update_summary(self):
        c = collections.Counter(j.status for j in self.jobs)
        self.lbl_summary.configure(
            text=f"{len(self.jobs)} links  |  running {c['Running']}  queued {c['Queued']}  "
                 f"done {c['Done']}  failed {c['Failed']}")

    # ----------------------------------------------------------- scheduler
    def current_limit(self):
        """-> (bytes_per_sec or None, paused:bool) from the weekly grid."""
        now = datetime.datetime.now()
        g = str(self.S("speed_grid"))
        lvl = int(g[now.weekday() * 24 + now.hour]) if len(g) == 168 else 0
        if lvl == 3:
            return None, True
        if lvl == 1:
            return self.S("limit_a") * 1048576, False
        if lvl == 2:
            return self.S("limit_b") * 1048576, False
        return None, False

    def is_quiet(self):
        if not self.S("quiet_on"):
            return False
        now = datetime.datetime.now()
        g = str(self.S("quiet_grid"))
        return len(g) == 168 and g[now.weekday() * 24 + now.hour] == "1"

    def pick_wav(self):
        p = filedialog.askopenfilename(filetypes=[("WAV audio", "*.wav")])
        if p:
            self.setv("custom_wav", p)
            self.setv("sound", CUSTOM_WAV)

    def play_sound(self, force=False):
        if self.is_quiet() and not force:
            return
        args = (self.S("sound"), self.S("custom_wav"), max(0, min(100, int(self.S("volume")))))
        threading.Thread(target=self._play, args=args, daemon=True).start()

    def _play_file(self, path, vol):
        if not os.path.isfile(path):
            return
        if vol >= 100:
            return winsound.PlaySound(path, winsound.SND_FILENAME)
        data = scaled_wav(path, vol)
        if data:
            winsound.PlaySound(data, winsound.SND_MEMORY)
        else:  # unsupported wav encoding: play a chime so the volume setting is still honoured
            winsound.PlaySound(synth_wav(MELODIES["Chime (rising)"], vol), winsound.SND_MEMORY)

    def _play(self, name, custom_wav, vol):
        try:
            if name == "None" or vol <= 0:
                return
            if not winsound:
                self.root.bell()
            elif name in MELODIES:
                winsound.PlaySound(synth_wav(MELODIES[name], vol), winsound.SND_MEMORY)
            elif name == CUSTOM_WAV:
                self._play_file(custom_wav, vol)
            elif name in getattr(self, "media", {}):
                self._play_file(self.media[name], vol)
            elif name in SYSTEM_SOUNDS:
                winsound.PlaySound(SYSTEM_SOUNDS[name], winsound.SND_ALIAS)
        except Exception:
            pass

    def today_bytes(self):
        return self.stats["days"].get(datetime.date.today().isoformat(), 0)

    def tick(self):
        now = time.time()
        dt = max(0.2, now - self.last_tick)
        self.last_tick = now
        try:
            self.drain()
            self._check_focus()
            running = [j for j in self.jobs if j.proc]
            limit, sched_pause = self.current_limit()
            cap = self.S("daily_cap_gb") * (1024 ** 3)
            cap_hit = cap > 0 and self.today_bytes() >= cap

            # sample speeds
            total = 0.0
            delta_total = 0
            for j in running:
                d = max(0, j.size_now - j.size_prev)
                j.size_prev = j.size_now
                j.speed = d / dt
                total += j.speed
                delta_total += d
                if d > 0:
                    j.last_act = now
                elif j.suspended:
                    j.last_act = now
                t = self.S("timeout")
                if t and now - j.last_act > t and not j.kill_reason:
                    j.kill_reason = "timeout"
                    j.note = f"stalled {t}s"
                    proc_kill(j.proc)
            if delta_total:
                self.stats["bytes"] += delta_total
                self.stats["seconds"] += dt
                k = datetime.date.today().isoformat()
                self.stats["days"][k] = self.stats["days"].get(k, 0) + delta_total
            self.speed_hist.append(total)

            # throttle (duty-cycle)
            self.throttled = bool(limit and total > limit and not self.throttled)

            # global hold reasons
            reasons = set()
            if self.global_pause:
                reasons.add("paused")
            if cap_hit:
                reasons.add("daily cap")
            if sched_pause:
                reasons.add("schedule")
            self.hold_reasons = reasons

            # suspend/resume
            for j in running:
                want = bool(reasons) or self.throttled or j.user_paused
                if want != j.suspended:
                    proc_suspend(j.proc, want)
                    j.suspended = want

            # start jobs
            low_disk = False
            if not reasons:
                free = shutil.disk_usage(self._ensure_out()).free
                low_disk = free < self.S("min_free_gb") * (1024 ** 3)
                if not low_disk:
                    self.start_jobs()
            self.disk_flash = low_disk and not self.disk_flash
            if low_disk:
                self.lbl_warn.configure(text="⚠ LOW DISK SPACE - queue held" if self.disk_flash
                                        else "⚠ low disk space")
            elif cap_hit:
                self.lbl_warn.configure(text="⏹ Daily data cap reached")
            elif sched_pause:
                self.lbl_warn.configure(text="⏸ Paused by schedule")
            else:
                self.lbl_warn.configure(text="")

            self.set_wake_lock(self.S("wake_lock") and bool(running))
            self.check_queue_empty()
            self.update_rows()
            self.update_stats_ui(total)
            if int(now) % 20 == 0:
                save_json(STATS_FILE, self.stats)
        except Exception as e:  # never let the loop die
            self.status.set(f"internal error: {e}")
        self.root.after(1000, self.tick)

    def _ensure_out(self):
        d = self.S("output_dir")
        os.makedirs(d, exist_ok=True)
        return d

    def start_jobs(self):
        running = [j for j in self.jobs if j.proc or j.status in ("Verifying", "Extracting")]
        slots = min(self.S("max_jobs"), self.S("host_limit")) - len(running)
        if slots <= 0:
            return
        exe = find_mdrs(self.S("mdrs_path"))
        if not exe:
            return
        pend = sorted([j for j in self.jobs if j.status == "Queued" and not j.user_paused],
                      key=lambda j: (PRIO_RANK[j.prio], j.id))
        for j in pend[:slots]:
            self.start_job(j, exe)

    def start_job(self, j, exe):
        j.out = self._ensure_out()
        h = hashlib.md5(j.url.encode()).hexdigest()[:10]
        j.stage = Path(j.out) / ".mdrs_stage" / h
        j.stage.mkdir(parents=True, exist_ok=True)
        cmd = [exe, "-o", str(j.stage), "-m", str(self.S("max_threads")), "-t", str(self.S("tries"))]
        if self.S("reverse"):
            cmd.append("-r")
        pf = self.proxy_file()
        if pf:
            cmd += ["-p", pf]
            if self.S("proxy_download"):
                cmd.append("--proxy-download")
        cmd.append(j.url)
        try:
            j.proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=0, creationflags=0x08000000 if IS_WIN else 0)
        except OSError as e:
            j.status, j.note = "Failed", f"launch error: {e}"
            return
        j.status, j.kill_reason, j.suspended = "Running", None, False
        j.size_now = j.size_prev = dir_size(j.stage)
        j.last_act = time.time()
        self.active_session = True
        self.log_add(f"[{j.id}] $ {' '.join(cmd)}")
        threading.Thread(target=self._reader, args=(j, j.proc), daemon=True).start()

    def _reader(self, j, proc):
        buf = ""
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                buf += ANSI_RE.sub("", chunk.decode("utf-8", "replace"))
                parts = re.split(r"[\r\n]+", buf)
                buf = parts.pop()
                for line in parts:
                    if line.strip():
                        self.q.put(("log", j.id, line))
            if buf.strip():
                self.q.put(("log", j.id, buf))
        except Exception:
            pass
        code = proc.wait()
        self.q.put(("exit", j.id, code))

    def _monitor_loop(self):
        while True:
            for j in list(self.jobs):
                if j.proc and j.stage:
                    j.size_now = dir_size(j.stage)
            time.sleep(1)

    def job_by_id(self, jid):
        return next((j for j in self.jobs if j.id == jid), None)

    def drain(self):
        for _ in range(500):
            try:
                kind, *a = self.q.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                j = self.job_by_id(a[0])
                if j:
                    j.log_tail.append(a[1])
                    j.last_act = time.time()
                self.log_add(f"[{a[0]}] {a[1]}")
            elif kind == "exit":
                self.on_exit(*a)
            elif kind == "fin":
                self.on_finalized(*a)
            elif kind == "call":
                a[0]()

    def on_exit(self, jid, code):
        j = self.job_by_id(jid)
        if not j:
            return
        j.proc, j.speed, j.suspended = None, 0, False
        self.log_add(f"[{jid}] exited with code {code}")
        if j.kill_reason == "removed":
            return
        if j.kill_reason == "timeout":
            return self.fail(j, "timeout")
        if code == 0:
            j.status = "Verifying"
            threading.Thread(target=self.finalize, args=(j,), daemon=True).start()
        else:
            self.fail(j, f"exit code {code}")

    def fail(self, j, why):
        text = " ".join(j.log_tail).lower()
        codes = set(re.findall(r"\b(404|403|429|502|503|504)\b", text))
        retry = self.S("auto_retry") and j.tries < self.S("auto_retries")
        if retry and why != "timeout" and codes:
            retryable = {c for c in codes if c in ("429", "502", "503", "504") and self.S("retry_" + c)}
            if "404" in codes and self.S("retry_404"):
                retryable.add("404")
            if not retryable:
                retry = False
        if retry:
            j.tries += 1
            j.status, j.note = "Queued", f"retry {j.tries} ({why})"
        else:
            j.status, j.note = "Failed", why + (f" [{', '.join(sorted(codes))}]" if codes else "")
            self.play_sound()
        self.refresh_tree()

    # ------------------------------------------------ post-processing
    def finalize(self, j):
        try:
            stage = j.stage
            files = [Path(r) / f for r, _d, fs in os.walk(stage) for f in fs]
            note, bad = "", []
            hashes = {}
            if self.S("md5"):
                for f in files:
                    try:
                        if f.stat().st_size == 0:
                            bad.append((f, "empty file"))
                            continue
                        if f.suffix.lower() == ".zip":
                            try:
                                with zipfile.ZipFile(f) as z:
                                    if z.testzip():
                                        bad.append((f, "corrupt zip"))
                                        continue
                            except zipfile.BadZipFile:
                                bad.append((f, "bad zip"))
                                continue
                        hashes[f] = md5_file(f)
                    except OSError as e:
                        bad.append((f, str(e)))
                if bad and self.S("md5_policy") == MD5_POLICIES[1]:
                    for f, _ in bad:
                        try:
                            os.remove(f)
                        except OSError:
                            pass
                    files = [f for f in files if f.exists()]
                    note = f"⚠ {len(bad)} corrupt file(s) deleted"
                elif bad:
                    note = f"⚠ {len(bad)} suspect file(s) kept"
                for f, why in bad:
                    self.q.put(("log", j.id, f"integrity: {f.name}: {why}"))
            moved = self.move_out(j, files)
            if self.S("md5"):
                for old, new in moved:
                    if old in hashes:
                        self.history.append({"path": str(new), "md5": hashes[old],
                                             "size": new.stat().st_size, "url": j.url,
                                             "time": datetime.datetime.now().isoformat(timespec="seconds")})
                save_json(HISTORY_FILE, self.history)
            shutil.rmtree(stage, ignore_errors=True)
            try:
                os.rmdir(stage.parent)  # only succeeds when empty
            except OSError:
                pass
            if self.S("extract"):
                self.q.put(("call", lambda: self._set_status(j, "Extracting")))
                n = self.extract_all([new for _o, new in moved])
                if n:
                    note = (note + f" 📦 {n} extracted").strip()
            self.q.put(("fin", j.id, True, note))
        except Exception as e:
            self.q.put(("fin", j.id, False, f"finalize error: {e}"))

    def _set_status(self, j, s):
        j.status = s

    def move_out(self, j, files):
        out, stage, moved = Path(j.out), j.stage, []
        for f in files:
            if self.S("mirror"):
                dest = out / f.relative_to(stage)
            else:
                dest = out / f.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not self.S("mirror") and dest.exists():
                i = 1
                while True:
                    cand = dest.with_name(f"{dest.stem} ({i}){dest.suffix}")
                    if not cand.exists():
                        dest = cand
                        break
                    i += 1
            os.replace(f, dest) if os.path.splitdrive(str(f))[0] == os.path.splitdrive(str(dest))[0] \
                else shutil.move(str(f), str(dest))
            moved.append((f, dest))
        return moved

    def extract_all(self, paths):
        n = 0
        for p in paths:
            ext = p.suffix.lower()
            if ext not in (".zip", ".rar", ".7z"):
                continue
            target = p.with_suffix("")
            try:
                if ext == ".zip":
                    with zipfile.ZipFile(p) as z:
                        z.extractall(target)
                else:
                    exe = find_7z()
                    if not exe:
                        self.q.put(("log", 0, f"extract skipped ({p.name}): 7-Zip not found"))
                        continue
                    r = subprocess.run([exe, "x", "-y", f"-o{target}", str(p)], capture_output=True,
                                       creationflags=0x08000000 if IS_WIN else 0)
                    if r.returncode != 0:
                        self.q.put(("log", 0, f"extract failed ({p.name}) rc={r.returncode}"))
                        continue
                n += 1
                if self.S("extract_delete"):
                    p.unlink(missing_ok=True)
            except Exception as e:
                self.q.put(("log", 0, f"extract failed ({p.name}): {e}"))
        return n

    def on_finalized(self, jid, ok, note):
        j = self.job_by_id(jid)
        if not j:
            return
        if ok:
            j.status, j.note, j.speed = "Done", note, 0
            self.stats["links"] += 1
            save_json(STATS_FILE, self.stats)
            self.done_since_rotate += 1
            n = self.S("proxy_cycle_n")
            if n and self.done_since_rotate >= n:
                self.rotate_proxy()
            if self.S("auto_cleanup"):
                self.jobs.remove(j)
        else:
            j.status, j.note = "Failed", note
        self.refresh_tree()
        self.save_queue()

    def check_queue_empty(self):
        busy = any(j.status in ("Queued", "Running", "Verifying", "Extracting") or
                   (j.status == "Queued") for j in self.jobs)
        if self.active_session and not busy and not any(j.proc for j in self.jobs):
            self.active_session = False
            ok = sum(1 for j in self.jobs if j.status == "Done")
            self.status.set("Queue finished.")
            self.play_sound()
            self.notify("Queue finished", f"{ok} link(s) complete")
            act = self.S("post_action")
            if self.close_pending or act == POST_ACTIONS[1]:
                self.root.after(2500, self.quit_app)
            elif act == POST_ACTIONS[2]:
                self.root.after(2500, self.sleep_pc)

    def sleep_pc(self):
        if IS_WIN:
            subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])

    def set_wake_lock(self, on):
        if IS_WIN:
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(
                    0x80000000 | 0x00000001 if on else 0x80000000)
            except Exception:
                pass

    # ----------------------------------------------------------- proxies
    def proxy_file(self):
        lines = []
        pf = self.S("proxy_file")
        if pf and os.path.isfile(pf):
            with open(pf, encoding="utf-8", errors="ignore") as f:
                lines += [l.strip() for l in f if l.strip() and not l.startswith("#")]
        host, port = self.S("proxy_host").strip(), str(self.S("proxy_port")).strip()
        if host and port:
            auth = f"{self.S('proxy_user')}:{self.S('proxy_pass')}@" if self.S("proxy_user") else ""
            lines.append(f"{self.S('proxy_type')}://{auth}{host}:{port}")
        if not lines:
            return None
        k = self.proxy_offset % len(lines)
        lines = lines[k:] + lines[:k]
        try:
            PROXY_ACTIVE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            return None
        return str(PROXY_ACTIVE)

    def rotate_proxy(self):
        self.proxy_offset += 1
        self.done_since_rotate = 0
        self.log_add(f"proxy rotated (offset {self.proxy_offset}) - applies to next launched link")

    def test_flare(self):
        url = self.S("flare_url").strip().rstrip("/")
        if not url.endswith("/v1"):
            url += "/v1"
        self.lbl_flare.configure(text="Testing…")

        def work():
            try:
                req = urllib.request.Request(url, data=json.dumps({"cmd": "sessions.list"}).encode(),
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    data = json.loads(r.read().decode())
                msg = f"✔ Connected ({data.get('version', 'FlareSolverr')})" if data.get("status") == "ok" \
                    else f"✖ Unexpected reply: {str(data)[:80]}"
            except Exception as e:
                msg = f"✖ Failed: {e}"
            self.q.put(("call", lambda: self.lbl_flare.configure(text=msg)))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------ stats / graph
    def update_stats_ui(self, speed):
        s = self.stats
        self.lbl_now.configure(text=f"Current speed: {human_speed(speed)}")
        self.stat_vars["gb"].set(f"{s['bytes'] / 1024 ** 3:.3f} GB")
        self.stat_vars["avg"].set(human_speed(s["bytes"] / s["seconds"]) if s["seconds"] else "-")
        self.stat_vars["links"].set(str(s["links"]))
        cap = self.S("daily_cap_gb")
        self.stat_vars["today"].set(human_size(self.today_bytes()) + (f" / {cap:g} GB cap" if cap else ""))
        self.draw_graph()

    def draw_graph(self, force=False):
        if self.S("low_resource") and not force:
            return
        if not force and self.nb.index(self.nb.select()) != 1:
            return
        cv, p = self.graph, self.pal()
        cv.delete("all")
        cv.configure(bg=p["field"], highlightbackground=p["grid"])
        w, h = max(cv.winfo_width(), 300), max(cv.winfo_height(), 120)
        if self.S("low_resource"):
            cv.create_text(w // 2, h // 2, text="Low Resource Mode - graph paused", fill=p["sub"])
            return
        data = list(self.speed_hist)
        peak = max(max(data), 1024)
        for i in range(1, 5):
            y = h - 20 - (h - 40) * i / 4
            cv.create_line(40, y, w - 10, y, fill=p["grid"])
            cv.create_text(36, y, text=human_size(peak * i / 4), anchor="e", fill=p["sub"], font=("Segoe UI", 7))
        step = (w - 50) / (len(data) - 1)
        pts = []
        for i, v in enumerate(data):
            pts += [40 + i * step, h - 20 - (h - 40) * v / peak]
        cv.create_line(*pts, fill=p["accent"], width=2, smooth=True)
        cv.create_text(w - 10, 10, text="last 2 minutes", anchor="e", fill=p["sub"], font=("Segoe UI", 8))

    def reset_stats(self):
        if messagebox.askyesno(APP_NAME, "Reset all lifetime statistics?"):
            self.stats = {"bytes": 0, "seconds": 0.0, "links": 0, "days": {}}
            save_json(STATS_FILE, self.stats)

    def export_stats(self, kind):
        p = filedialog.asksaveasfilename(defaultextension="." + kind, initialfile=f"mdrs_metrics.{kind}")
        if not p:
            return
        s = self.stats
        rows = [("Total downloaded (GB)", f"{s['bytes'] / 1024 ** 3:.3f}"),
                ("Average speed (B/s)", f"{s['bytes'] / s['seconds']:.0f}" if s["seconds"] else "0"),
                ("Links processed", s["links"]),
                ("Exported", datetime.datetime.now().isoformat(timespec="seconds"))]
        daily = sorted(s["days"].items())
        if kind == "csv":
            with open(p, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["metric", "value"])
                w.writerows(rows)
                w.writerow([])
                w.writerow(["date", "bytes"])
                w.writerows(daily)
        else:
            body = "".join(f"<tr><td>{html.escape(str(a))}</td><td>{html.escape(str(b))}</td></tr>" for a, b in rows)
            body2 = "".join(f"<tr><td>{a}</td><td>{human_size(b)}</td></tr>" for a, b in daily)
            Path(p).write_text(
                "<!doctype html><meta charset=utf-8><title>mdrs metrics</title>"
                "<style>body{font:15px Segoe UI,sans-serif;margin:2rem}td,th{padding:4px 14px;"
                "border-bottom:1px solid #ccc;text-align:left}</style><h2>mdrs GUI metrics</h2>"
                f"<table>{body}</table><h3>Per day</h3><table><tr><th>Date</th><th>Downloaded</th></tr>{body2}</table>",
                encoding="utf-8")
        self.status.set(f"Metrics exported to {p}")

    # --------------------------------------------------------------- log
    def log_add(self, line):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"{stamp}  {line}"
        self.log_lines.append(line)
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 5000:
            self.log.delete("1.0", "1000.0")
        self.log.configure(state="disabled")
        if self.autoscroll.get():
            self.log.see("end")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.log_lines.clear()

    def export_log(self):
        p = filedialog.asksaveasfilename(defaultextension=".txt", initialfile="mdrs_log.txt")
        if p:
            Path(p).write_text("\n".join(self.log_lines), encoding="utf-8")
            self.status.set(f"Log saved to {p}")

    # ----------------------------------------------------- integrity tool
    def force_check(self):
        hist = list(self.history)
        if not hist:
            return messagebox.showinfo(APP_NAME, "History is empty. Enable the MD5 check first; "
                                                 "completed files are recorded automatically.")

        def work():
            ok = bad = miss = 0
            for h in hist:
                p = h["path"]
                if not os.path.exists(p):
                    miss += 1
                    self.q.put(("log", 0, f"force-check MISSING  {p}"))
                    continue
                if md5_file(p) == h["md5"]:
                    ok += 1
                else:
                    bad += 1
                    self.q.put(("log", 0, f"force-check MISMATCH {p}"))
            msg = f"Force check: {ok} OK, {bad} mismatched, {miss} missing."
            self.q.put(("call", lambda: (self.status.set(msg), messagebox.showinfo(APP_NAME, msg))))
        self.status.set("Force-checking history…")
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ updates
    def check_updates(self, manual=False):
        local = "?"
        exe = find_mdrs(self.S("mdrs_path"))
        if exe:
            try:
                local = subprocess.run([exe, "-V"], capture_output=True, text=True, timeout=10,
                                       creationflags=0x08000000 if IS_WIN else 0).stdout.strip().split()[-1]
            except Exception:
                pass
        latest = None
        for url, getter in (
            ("https://api.github.com/repos/NicKoehler/mediafire_rs/releases/latest",
             lambda d: d.get("tag_name", "").lstrip("v")),
            ("https://crates.io/api/v1/crates/mediafire_rs",
             lambda d: d.get("crate", {}).get("max_stable_version", ""))):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "mdrs-gui"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    latest = getter(json.loads(r.read().decode())) or None
                if latest:
                    break
            except Exception:
                continue
        if latest and local != "?" and latest != local:
            msg = f"mdrs {local} installed - newer version {latest} available (cargo install mediafire_rs)"
        elif latest:
            msg = f"mdrs {local} - up to date (latest {latest})"
        else:
            msg = f"mdrs {local} - could not check for updates"
        self.q.put(("call", lambda: (self.lbl_ver.configure(text=msg),
                                     self.status.set(msg) if (manual or "newer" in msg) else None)))

    # ------------------------------------------------- tray / closing
    def notify(self, title, msg):
        if self.is_quiet():
            return
        if self.tray:
            try:
                self.tray.notify(msg, title)
            except Exception:
                pass

    def on_close(self):
        act = self.S("close_action")
        if act == CLOSE_ACTIONS[1]:
            self.quit_app()
        elif act == CLOSE_ACTIONS[2]:
            if any(j.status in ("Queued", "Running", "Verifying", "Extracting") for j in self.jobs):
                self.close_pending = True
                self.global_pause = False
                self.btn_start.configure(text="⏸  Pause all")
                self.status.set("Will close when the queue finishes…")
                self.root.iconify()
            else:
                self.quit_app()
        else:
            self.to_tray()

    def to_tray(self):
        if not HAVE_TRAY:
            self.root.iconify()
            return
        self.root.withdraw()
        if self.tray:
            return
        img = Image.new("RGB", (64, 64), "#2563eb")
        ImageDraw.Draw(img).polygon([(18, 16), (46, 16), (32, 48)], fill="white")
        menu = pystray.Menu(
            pystray.MenuItem("Show", lambda: self.q.put(("call", self.from_tray)), default=True),
            pystray.MenuItem("Quit", lambda: self.q.put(("call", self.quit_app))))
        self.tray = pystray.Icon("mdrs_gui", img, APP_NAME, menu)
        self.tray.run_detached()

    def from_tray(self):
        self.root.deiconify()
        self.root.lift()
        if self.tray:
            self.tray.stop()
            self.tray = None

    def quit_app(self):
        self.save_settings()
        self.save_queue()
        save_json(STATS_FILE, self.stats)
        for j in self.jobs:
            if j.proc:
                j.kill_reason = "removed"
                proc_kill(j.proc)
        self.set_wake_lock(False)
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()
        os._exit(0)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
