#!/usr/bin/env python3
"""
MDRS GUI: Windows Front-End for mediafire_rs
https://github.com/NicKoehler/mediafire_rs

Run:    double-click mdrs_gui.pyw   (or:  python mdrs_gui.pyw)
Needs:  Python 3.9+ and `mdrs` installed (cargo install mediafire_rs).
        A Chromium browser (Microsoft Edge ships with Windows 10/11) is used as the window.
        No pip packages are required.

How it works
  * A tiny local web server (127.0.0.1 only, protected by a random per-launch token)
    runs the download engine and serves the interface.
  * The interface opens in a chrome-less Edge/Chrome "app" window, so it is GPU-composited:
    translucent materials, spring animations and a virtualised list.
  * mdrs only exposes -i -o -m -t -r -p --proxy-download.  Everything else (queue,
    priorities, pause/resume, speed schedule, MD5/archive checks, extraction, statistics...)
    is implemented here around those flags:
      - one mdrs process per link, downloading into a private staging folder
      - speed/progress = staging-folder growth, pause = suspending the process
      - throttling = duty-cycle suspend/resume, stall timeout = watchdog
  * Closing the window follows the "Close button" setting (tray / quit / quit after queue).

Debug:  python mdrs_gui.pyw --no-window     (serve only; prints the URL)
"""
import os, sys, re, json, csv, time, shutil, queue, threading, subprocess
import hashlib, zipfile, datetime, html, collections, ctypes
import urllib.request, urllib.parse, wave, array, math, io, secrets, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import winsound
except ImportError:
    winsound = None

IS_WIN = os.name == "nt"
APP_NAME = "MDRS GUI"
TITLE = "MDRS GUI: Windows Front-End for mediafire_rs"
DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "MdrsGUI"
DATA_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = DATA_DIR / "settings.json"
STATS_FILE = DATA_DIR / "stats.json"
HISTORY_FILE = DATA_DIR / "history.json"
QUEUE_FILE = DATA_DIR / "queue.json"
INSTANCE_FILE = DATA_DIR / "instance.json"
PROXY_ACTIVE = DATA_DIR / "proxies_active.txt"
BROWSER_PROFILE = DATA_DIR / "window_profile"
NO_WINDOW = "--no-window" in sys.argv
CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0

URL_RE = re.compile(r"https?://(?:www\.)?mediafire\.com/[^\s\"'<>,;|]+", re.I)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
PRIO_RANK = {"High": 0, "Medium": 1, "Low": 2}
ACTIVE = ("Queued", "Running", "Verifying", "Extracting")
NO_TAG = "(No tag)"
CUSTOM_WAV = "Custom WAV file…"

CLOSE_ACTIONS = ["Minimize to system tray", "Terminate immediately", "Close after queue finishes"]
POST_ACTIONS = ["Do nothing", "Close application", "Sleep PC"]
MD5_POLICIES = ["Keep for Manual Inspection", "Delete Corrupted Downloads"]
SYSTEM_SOUNDS = {"System: Default": "SystemDefault", "System: Asterisk": "SystemAsterisk",
                 "System: Exclamation": "SystemExclamation", "System: Hand": "SystemHand",
                 "System: Exit": "SystemExit"}
# (frequency Hz, milliseconds); frequency 0 = silence
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
    "custom_wav": "", "volume": 80, "theme": "System", "text_size": "Default",
    "close_action": CLOSE_ACTIONS[0], "wake_lock": False,
    "daily_cap_gb": 0.0, "sound": "Chime (rising)", "quiet_on": False,
    "quiet_grid": "0" * 168, "speed_grid": "0" * 168, "limit_a": 5.0, "limit_b": 1.0,
    "check_updates": True,
}
CHOICES = {
    "theme": ["System", "Light", "Dark"],
    "text_size": ["Small", "Default", "Large", "Extra Large"],
    "close_action": CLOSE_ACTIONS, "post_action": POST_ACTIONS, "md5_policy": MD5_POLICIES,
    "proxy_type": ["http", "https", "socks5", "socks5h"], "prio": list(PRIO_RANK),
}
RANGES = {
    "max_threads": (1, 50), "max_jobs": (1, 10), "host_limit": (1, 10), "timeout": (0, 600),
    "tries": (1, 20), "auto_retries": (1, 20), "min_free_gb": (0, 10000),
    "daily_cap_gb": (0, 100000), "limit_a": (0.1, 1000), "limit_b": (0.1, 1000),
    "proxy_cycle_n": (0, 1000), "volume": (0, 100),
}
NO_PROFILE = ("batch_text",)   # not exported / imported with profiles


# ------------------------------------------------------------------ helpers
def coerce(key, v):
    """Validate/convert a setting from the UI or a profile; fall back to the default."""
    d = DEFAULTS[key]
    try:
        if isinstance(d, bool):
            return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
        if isinstance(d, (int, float)):
            n = (int if isinstance(d, int) else float)(float(v))
            lo, hi = RANGES.get(key, (None, None))
            return n if lo is None else max(lo, min(hi, n))
        s = str(v)
    except (TypeError, ValueError):
        return d
    if key in CHOICES and s not in CHOICES[key]:
        return d
    if key.endswith("_grid") and not re.fullmatch(r"[0-3]{168}", s):
        return d
    return s


def human_size(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.2f} {u}"
        n /= 1024


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


def find_browser():
    """Edge ships with Windows; fall back to Chrome / Brave, else None (default browser)."""
    roots = [os.environ.get(k, "") for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    rel = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe",
           r"BraveSoftware\Brave-Browser\Application\brave.exe",
           r"Chromium\Application\chrome.exe"]
    for r in rel:
        for base in roots:
            p = os.path.join(base, r) if base else ""
            if p and os.path.isfile(p):
                return p
    for name in ("msedge", "chrome", "google-chrome", "chromium", "brave"):
        p = shutil.which(name)
        if p:
            return p
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


def media_sounds():
    """Every .wav in the Windows Media folder -> {'Windows: tada': path}"""
    out = {}
    try:
        for f in sorted((Path(os.environ.get("WINDIR", r"C:\Windows")) / "Media").glob("*.wav")):
            out["Windows: " + f.stem] = str(f)
    except OSError:
        pass
    return out


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
    """WAV bytes of `path` with amplitude scaled by volume, or None if the format is unsupported."""
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


def pick_dialog(kind, current=""):
    """Native file/folder chooser (tkinter is only used for these dialogs)."""
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass
    try:
        if kind == "dir":
            return filedialog.askdirectory(initialdir=current or None, parent=root) or ""
        types = {"exe": [("Executable", "*.exe"), ("All files", "*.*")],
                 "wav": [("WAV audio", "*.wav")], "file": [("All files", "*.*")]}.get(kind)
        return filedialog.askopenfilename(filetypes=types, parent=root) or ""
    finally:
        root.destroy()


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

    def row(self):
        return [self.id, self.url, self.tag, self.prio, self.status, self.note, self.tries,
                self.size_now, int(self.speed), int(self.user_paused), int(self.suspended)]


class Conn:
    """Per-browser-connection bookkeeping so each SSE message only carries changes."""

    def __init__(self):
        self.first = True
        self.sigs = {}
        self.log_seq = 0
        self.notice_seq = 0
        self.set_ver = 0
        self.meta_ver = 0


# --------------------------------------------------------------- tray icon
class Tray:
    """Notification-area icon built from plain Win32 calls (no extra packages needed)."""
    WM_TRAY = 0x8001

    def __init__(self, tip, on_show, on_quit):
        self.ok = False
        self.tip, self.on_show, self.on_quit = tip, on_show, on_quit
        self.hwnd = self.nid = None
        if IS_WIN:
            ready = threading.Event()
            threading.Thread(target=self._run, args=(ready,), daemon=True).start()
            ready.wait(3)

    def _run(self, ready):
        try:
            from ctypes import wintypes as w
            u32, s32, k32 = ctypes.windll.user32, ctypes.windll.shell32, ctypes.windll.kernel32
            LRESULT = ctypes.c_ssize_t
            WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

            class WNDCLASS(ctypes.Structure):
                _fields_ = [("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                            ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE),
                            ("hIcon", w.HICON), ("hCursor", w.HANDLE), ("hbrBackground", w.HANDLE),
                            ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR)]

            class NID(ctypes.Structure):   # NOTIFYICONDATAW (V2 layout)
                _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("uFlags", w.UINT),
                            ("uCallbackMessage", w.UINT), ("hIcon", w.HICON), ("szTip", w.WCHAR * 128),
                            ("dwState", w.DWORD), ("dwStateMask", w.DWORD), ("szInfo", w.WCHAR * 256),
                            ("uVersion", w.UINT), ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD)]

            u32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
            u32.DefWindowProcW.restype = LRESULT
            u32.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU,
                                            w.HINSTANCE, w.LPVOID]
            u32.CreateWindowExW.restype = w.HWND
            u32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
            u32.RegisterClassW.restype = w.ATOM
            u32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
            u32.GetMessageW.restype = w.BOOL
            u32.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
            u32.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
            u32.DispatchMessageW.restype = LRESULT
            u32.CreatePopupMenu.restype = w.HMENU
            u32.AppendMenuW.argtypes = [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]
            u32.TrackPopupMenu.argtypes = [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           w.HWND, ctypes.c_void_p]
            u32.TrackPopupMenu.restype = ctypes.c_int
            u32.DestroyMenu.argtypes = [w.HMENU]
            u32.SetForegroundWindow.argtypes = [w.HWND]
            u32.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
            u32.LoadIconW.argtypes = [w.HINSTANCE, ctypes.c_void_p]
            u32.LoadIconW.restype = w.HICON
            u32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
            u32.PostQuitMessage.argtypes = [ctypes.c_int]
            s32.Shell_NotifyIconW.argtypes = [w.DWORD, ctypes.POINTER(NID)]
            s32.Shell_NotifyIconW.restype = w.BOOL
            k32.GetModuleHandleW.argtypes = [w.LPCWSTR]
            k32.GetModuleHandleW.restype = w.HMODULE

            def menu(hwnd):
                m = u32.CreatePopupMenu()
                u32.AppendMenuW(m, 0, 1, "Show MDRS GUI")
                u32.AppendMenuW(m, 0, 2, "Quit")
                pt = w.POINT()
                u32.GetCursorPos(ctypes.byref(pt))
                u32.SetForegroundWindow(hwnd)
                cmd = u32.TrackPopupMenu(m, 0x182, pt.x, pt.y, 0, hwnd, None)  # RETURNCMD|NONOTIFY|RIGHTBUTTON
                u32.PostMessageW(hwnd, 0, 0, 0)
                u32.DestroyMenu(m)
                return cmd

            def wndproc(hwnd, msg, wp, lp):
                if msg == self.WM_TRAY:
                    if lp in (0x202, 0x203):          # left click / double-click
                        threading.Thread(target=self.on_show, daemon=True).start()
                    elif lp == 0x205:                 # right click
                        cmd = menu(hwnd)
                        if cmd == 1:
                            threading.Thread(target=self.on_show, daemon=True).start()
                        elif cmd == 2:
                            threading.Thread(target=self.on_quit, daemon=True).start()
                    return 0
                if msg == 0x0002:                     # WM_DESTROY
                    u32.PostQuitMessage(0)
                    return 0
                return u32.DefWindowProcW(hwnd, msg, wp, lp)

            self._proc = WNDPROC(wndproc)             # keep a reference alive
            hinst = k32.GetModuleHandleW(None)
            wc = WNDCLASS()
            wc.lpfnWndProc, wc.hInstance, wc.lpszClassName = self._proc, hinst, "MdrsGuiTray"
            u32.RegisterClassW(ctypes.byref(wc))
            self.hwnd = u32.CreateWindowExW(0, "MdrsGuiTray", APP_NAME, 0, 0, 0, 0, 0, None, None, hinst, None)
            nid = NID()
            nid.cbSize, nid.hWnd, nid.uID = ctypes.sizeof(NID), self.hwnd, 1
            nid.uFlags, nid.uCallbackMessage = 0x7, self.WM_TRAY   # MESSAGE|ICON|TIP
            nid.hIcon = u32.LoadIconW(None, ctypes.c_void_p(32512))  # IDI_APPLICATION
            nid.szTip = self.tip[:127]
            self.nid, self._s32, self._NID = nid, s32, NID
            self.ok = bool(s32.Shell_NotifyIconW(0, ctypes.byref(nid)))  # NIM_ADD
            ready.set()
            msg = w.MSG()
            while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u32.TranslateMessage(ctypes.byref(msg))
                u32.DispatchMessageW(ctypes.byref(msg))
            s32.Shell_NotifyIconW(2, ctypes.byref(nid))               # NIM_DELETE
        except Exception:
            self.ok = False
        finally:
            ready.set()

    def balloon(self, title, text):
        if not self.ok:
            return
        try:
            n = self.nid
            n.uFlags, n.szInfo, n.szInfoTitle, n.dwInfoFlags = 0x10, text[:255], title[:63], 1
            self._s32.Shell_NotifyIconW(1, ctypes.byref(n))           # NIM_MODIFY
            n.uFlags = 0x7
        except Exception:
            pass

    def remove(self):
        if self.ok and self.nid is not None:
            try:
                self._s32.Shell_NotifyIconW(2, ctypes.byref(self.nid))
                self.ok = False
            except Exception:
                pass


# --------------------------------------------------------------------- core
class Core:
    """The download engine and all state. Every public `api_*` method is callable from the UI."""

    def __init__(self):
        self.lock = threading.RLock()
        self.cv = threading.Condition(self.lock)
        self.ver = 0
        self.stop = threading.Event()
        saved = load_json(SETTINGS_FILE, {})
        self.cfg = {k: coerce(k, saved.get(k, d)) for k, d in DEFAULTS.items()}
        self.set_ver = 1
        self.meta_ver = 1
        self._save_timer = None
        self.jobs, self.by_id = [], {}
        self.q = queue.Queue()
        self.log_lines = collections.deque(maxlen=5000)
        self.log_seq = 0
        self.notices = collections.deque(maxlen=60)
        self.notice_seq = 0
        self.speed_hist = collections.deque([0] * 120, maxlen=120)
        self.stats = load_json(STATS_FILE, {})
        for k, d in (("bytes", 0), ("seconds", 0.0), ("links", 0), ("days", {})):
            self.stats.setdefault(k, d)
        self.history = load_json(HISTORY_FILE, [])
        self.global_pause = True
        self.throttled = False
        self.warn = {"disk": False, "cap": False, "sched": False, "path": False}
        self.total_speed = 0.0
        self.active_session = False
        self.close_pending = False
        self.proxy_offset = 0
        self.done_since_rotate = 0
        self.ver_text = ""
        self.media = media_sounds()
        self.clients = set()
        self.win_state = "awaiting"          # awaiting -> open -> closed
        self.win_since = time.time()
        self.fallback_opened = False
        self.server = self.port = self.token = None
        self.tray = None
        if self.cfg["sound"] not in self.sound_names():
            self.cfg["sound"] = DEFAULTS["sound"]
        for d in load_json(QUEUE_FILE, []):
            if isinstance(d, dict) and d.get("url"):
                self._add_job(d["url"], d.get("tag", ""), d.get("prio", "Medium")
                              if d.get("prio") in PRIO_RANK else "Medium")
        threading.Thread(target=self._monitor_loop, daemon=True).start()
        threading.Thread(target=self._loop, daemon=True).start()
        if self.cfg["check_updates"]:
            threading.Thread(target=self.check_updates, daemon=True).start()

    # ------------------------------------------------------------ plumbing
    def s(self, k):
        return self.cfg[k]

    def notify(self):
        with self.cv:
            self.ver += 1
            self.cv.notify_all()

    def wait_change(self, ver, timeout):
        with self.cv:
            if self.ver == ver:
                self.cv.wait(timeout)
            return self.ver

    def log_add(self, line):
        self.log_seq += 1
        self.log_lines.append(f"{datetime.datetime.now():%H:%M:%S}  {line}")

    def say(self, msg, kind="toast", title=""):
        """Status message shown by the UI as a toast (or a sheet with a title)."""
        self.notice_seq += 1
        self.notices.append({"seq": self.notice_seq, "msg": msg, "kind": kind, "title": title})

    def call(self, name, data):
        fn = getattr(self, "api_" + name, None)
        if fn is None:
            raise KeyError(name)
        if name in ("pick", "test_flare"):        # blocking: run without holding the lock
            return fn(data)
        with self.lock:
            out = fn(data)
        self.notify()
        return out

    def sound_names(self):
        return ["None"] + list(MELODIES) + [CUSTOM_WAV] + list(SYSTEM_SOUNDS) + list(self.media)

    def tag_names(self):
        return [t.strip() for t in self.s("tags").split(",") if t.strip()]

    # ------------------------------------------------------------ settings
    def _set(self, k, v):
        nv = coerce(k, v)
        if self.cfg.get(k) != nv:
            self.cfg[k] = nv
            self.set_ver += 1
            if k == "mdrs_path":
                self.meta_ver += 1
            self._schedule_save()

    def _schedule_save(self):
        if self._save_timer:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(0.6, self.save_settings)
        self._save_timer.daemon = True
        self._save_timer.start()

    def save_settings(self):
        save_json(SETTINGS_FILE, self.cfg)

    def api_set(self, d):
        for k, v in d.items():
            if k in DEFAULTS:
                self._set(k, v)
        return {}

    def api_reset_defaults(self, _d):
        for k, v in DEFAULTS.items():
            if k not in NO_PROFILE:
                self._set(k, v)
        self.say("Settings reset to defaults.")
        return {}

    def api_export_profile(self, _d):
        prof = {k: v for k, v in self.cfg.items() if k not in NO_PROFILE}
        return {"name": "mdrs_gui_profile.json", "mime": "application/json",
                "data": json.dumps(prof, indent=2)}

    def api_import_profile(self, d):
        prof = d.get("profile")
        if not isinstance(prof, dict):
            raise ValueError("Not a valid profile file.")
        n = 0
        for k, v in prof.items():
            if k in DEFAULTS and k not in NO_PROFILE:
                self._set(k, v)
                n += 1
        self.say(f"Profile imported ({n} settings).")
        return {"count": n}

    # -------------------------------------------------------------- intake
    def _add_job(self, url, tag, prio):
        j = Job(url, tag, prio)
        self.jobs.append(j)
        self.by_id[j.id] = j
        return j

    def api_extract(self, d):
        links = [u.rstrip(".,);]") for u in URL_RE.findall(str(d.get("text", "")))]
        return {"links": list(dict.fromkeys(links))}

    def api_add(self, d):
        urls = d.get("urls") or self.api_extract(d)["links"]
        if not urls:
            raise ValueError("No MediaFire links found.")
        tag = str(d.get("tag", self.s("tag"))).strip()
        tag = "" if tag == NO_TAG else tag
        prio = d.get("prio", self.s("prio"))
        prio = prio if prio in PRIO_RANK else "Medium"
        if tag and tag not in self.tag_names():
            self._set("tags", ",".join(self.tag_names() + [tag]))
        have = {j.url for j in self.jobs}
        n = 0
        for u in urls:
            u = str(u).rstrip(".,);]")
            if u not in have and URL_RE.fullmatch(u):
                self._add_job(u, tag, prio)
                have.add(u)
                n += 1
        self.save_queue()
        self.say(f"Added {n} link{'s' if n != 1 else ''}." if n else "Those links are already in the queue.")
        return {"added": n}

    def save_queue(self):
        save_json(QUEUE_FILE, [{"url": j.url, "tag": j.tag, "prio": j.prio}
                               for j in self.jobs if j.status != "Done"])

    # ------------------------------------------------------- queue actions
    def api_toggle_global(self, _d):
        self.global_pause = not self.global_pause
        return {"paused": self.global_pause}

    def api_job(self, d):
        op, value = d.get("op"), d.get("value")
        jobs = [self.by_id[i] for i in d.get("ids", []) if i in self.by_id]
        for j in jobs:
            if op == "pause":
                j.user_paused = True
            elif op == "resume":
                j.user_paused = False
            elif op == "toggle" and j.status in ("Running", "Queued"):
                j.user_paused = not j.user_paused
            elif op == "retry" and j.status == "Failed":
                self._requeue(j)
            elif op == "prio" and value in PRIO_RANK:
                j.prio = value
            elif op == "tag":
                j.tag = "" if value in (None, NO_TAG) else str(value).strip()
            elif op == "remove":
                self.remove_job(j)
        if op in ("remove", "tag", "prio"):
            self.save_queue()
        return {}

    def remove_job(self, j):
        if j.proc:
            j.kill_reason = "removed"
            proc_kill(j.proc)
        if j.stage and j.status != "Done":
            shutil.rmtree(j.stage, ignore_errors=True)
        if j in self.jobs:
            self.jobs.remove(j)
        self.by_id.pop(j.id, None)

    def _requeue(self, j):
        j.status, j.note, j.tries, j.kill_reason = "Queued", "", 0, None

    def api_retry_failed(self, _d):
        n = 0
        for j in self.jobs:
            if j.status == "Failed":
                self._requeue(j)
                n += 1
        self.say(f"Re-queued {n} failed link{'s' if n != 1 else ''}.")
        return {}

    def api_clear_finished(self, _d):
        for j in [j for j in self.jobs if j.status == "Done"]:
            self.jobs.remove(j)
            self.by_id.pop(j.id, None)
        self.save_queue()
        return {}

    def api_open_out(self, _d):
        d = self.s("output_dir")
        os.makedirs(d, exist_ok=True)
        if IS_WIN:
            os.startfile(d)
        else:
            subprocess.Popen(["xdg-open", d])
        return {}

    def api_pick(self, d):
        try:
            return {"path": pick_dialog(d.get("kind", "file"), d.get("current", ""))}
        except Exception as e:
            return {"path": "", "error": f"File dialog unavailable: {e}"}

    # ----------------------------------------------------------- scheduler
    def current_limit(self):
        """-> (bytes_per_sec or None, paused) from the weekly grid."""
        now = datetime.datetime.now()
        lvl = int(self.s("speed_grid")[now.weekday() * 24 + now.hour])
        if lvl == 3:
            return None, True
        if lvl == 1:
            return self.s("limit_a") * 1048576, False
        if lvl == 2:
            return self.s("limit_b") * 1048576, False
        return None, False

    def is_quiet(self):
        if not self.s("quiet_on"):
            return False
        now = datetime.datetime.now()
        return self.s("quiet_grid")[now.weekday() * 24 + now.hour] == "1"

    def today_bytes(self):
        return self.stats["days"].get(datetime.date.today().isoformat(), 0)

    def has_pending(self):
        return any(j.status in ACTIVE for j in self.jobs)

    def _loop(self):
        last = time.time()
        while not self.stop.is_set():
            try:
                with self.lock:
                    changed = self.drain()
                    now = time.time()
                    if now - last >= 1.0:
                        dt, last, changed = now - last, now, True
                        self.tick(now, dt)
                    self.watchdog(now)
                if changed:
                    self.notify()
            except Exception as e:  # never let the loop die
                self.log_add(f"internal error: {e!r}")
            self.stop.wait(0.2)

    def tick(self, now, dt):
        running = [j for j in self.jobs if j.proc]
        limit, sched_pause = self.current_limit()
        cap = self.s("daily_cap_gb") * (1024 ** 3)
        cap_hit = cap > 0 and self.today_bytes() >= cap

        total, delta_total = 0.0, 0
        for j in running:                                   # sample speeds
            d = max(0, j.size_now - j.size_prev)
            j.size_prev = j.size_now
            j.speed = d / dt
            total += j.speed
            delta_total += d
            if d > 0 or j.suspended:
                j.last_act = now
            t = self.s("timeout")
            if t and now - j.last_act > t and not j.kill_reason:
                j.kill_reason, j.note = "timeout", f"stalled {t}s"
                proc_kill(j.proc)
        if delta_total:
            self.stats["bytes"] += delta_total
            self.stats["seconds"] += dt
            k = datetime.date.today().isoformat()
            self.stats["days"][k] = self.stats["days"].get(k, 0) + delta_total
        self.total_speed = total
        self.speed_hist.append(total)
        self.throttled = bool(limit and total > limit and not self.throttled)   # duty-cycle

        hold = self.global_pause or cap_hit or sched_pause
        for j in running:                                   # suspend / resume
            want = hold or self.throttled or j.user_paused
            if want != j.suspended:
                proc_suspend(j.proc, want)
                j.suspended = want

        low_disk = bad_path = False
        if not hold:
            try:
                free = shutil.disk_usage(self._ensure_out()).free
                low_disk = free < self.s("min_free_gb") * (1024 ** 3)
                if not low_disk:
                    self.start_jobs()
            except OSError:
                bad_path = True
        self.warn = {"disk": low_disk, "cap": cap_hit, "sched": sched_pause, "path": bad_path}
        self.set_wake_lock(self.s("wake_lock") and bool(running))
        self.check_queue_empty()
        if int(now) % 20 == 0:
            save_json(STATS_FILE, self.stats)

    def _ensure_out(self):
        d = self.s("output_dir")
        os.makedirs(d, exist_ok=True)
        return d

    def start_jobs(self):
        running = [j for j in self.jobs if j.proc or j.status in ("Verifying", "Extracting")]
        slots = min(self.s("max_jobs"), self.s("host_limit")) - len(running)
        exe = find_mdrs(self.s("mdrs_path"))
        if slots <= 0 or not exe:
            return
        pend = sorted([j for j in self.jobs if j.status == "Queued" and not j.user_paused],
                      key=lambda j: (PRIO_RANK[j.prio], j.id))
        for j in pend[:slots]:
            self.start_job(j, exe)

    def start_job(self, j, exe):
        j.out = self._ensure_out()
        j.stage = Path(j.out) / ".mdrs_stage" / hashlib.md5(j.url.encode()).hexdigest()[:10]
        j.stage.mkdir(parents=True, exist_ok=True)
        cmd = [exe, "-o", str(j.stage), "-m", str(self.s("max_threads")), "-t", str(self.s("tries"))]
        if self.s("reverse"):
            cmd.append("-r")
        pf = self.proxy_file()
        if pf:
            cmd += ["-p", pf]
            if self.s("proxy_download"):
                cmd.append("--proxy-download")
        cmd.append(j.url)
        try:
            j.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, bufsize=0, creationflags=CREATE_NO_WINDOW)
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
        self.q.put(("exit", j.id, proc.wait()))

    def _monitor_loop(self):
        while not self.stop.is_set():
            for j in list(self.jobs):
                if j.proc and j.stage:
                    j.size_now = dir_size(j.stage)
            time.sleep(1)

    def drain(self):
        n = 0
        for _ in range(500):
            try:
                kind, *a = self.q.get_nowait()
            except queue.Empty:
                break
            n += 1
            if kind == "log":
                j = self.by_id.get(a[0])
                if j:
                    j.log_tail.append(a[1])
                    j.last_act = time.time()
                self.log_add(f"[{a[0]}] {a[1]}" if a[0] else a[1])
            elif kind == "exit":
                self.on_exit(*a)
            elif kind == "fin":
                self.on_finalized(*a)
            elif kind == "status":
                j = self.by_id.get(a[0])
                if j:
                    j.status = a[1]
            elif kind == "say":
                self.say(*a)
        return n > 0

    def on_exit(self, jid, code):
        j = self.by_id.get(jid)
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
        retry = self.s("auto_retry") and j.tries < self.s("auto_retries")
        if retry and why != "timeout" and codes:
            retryable = {c for c in codes if c in ("429", "502", "503", "504") and self.s("retry_" + c)}
            if "404" in codes and self.s("retry_404"):
                retryable.add("404")
            if not retryable:
                retry = False
        if retry:
            j.tries += 1
            j.status, j.note = "Queued", f"retry {j.tries} ({why})"
        else:
            j.status, j.note = "Failed", why + (f" [{', '.join(sorted(codes))}]" if codes else "")
            self.play_sound()

    # ------------------------------------------------------ post-processing
    def finalize(self, j):
        """Runs in a worker thread: integrity check -> move -> history -> extract."""
        post = self.q.put
        try:
            stage = j.stage
            files = [Path(r) / f for r, _d, fs in os.walk(stage) for f in fs]
            note, bad, hashes = "", [], {}
            if self.s("md5"):
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
                if bad and self.s("md5_policy") == MD5_POLICIES[1]:
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
                    post(("log", j.id, f"integrity: {f.name}: {why}"))
            moved = self.move_out(j, files)
            if self.s("md5"):
                with self.lock:
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
            if self.s("extract"):
                post(("status", j.id, "Extracting"))
                n = self.extract_all([new for _o, new in moved])
                if n:
                    note = (note + f" 📦 {n} extracted").strip()
            post(("fin", j.id, True, note))
        except Exception as e:
            post(("fin", j.id, False, f"finalize error: {e}"))

    def move_out(self, j, files):
        out, stage, moved = Path(j.out), j.stage, []
        for f in files:
            dest = out / f.relative_to(stage) if self.s("mirror") else out / f.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not self.s("mirror") and dest.exists():
                i = 1
                while True:
                    cand = dest.with_name(f"{dest.stem} ({i}){dest.suffix}")
                    if not cand.exists():
                        dest = cand
                        break
                    i += 1
            shutil.move(str(f), str(dest))
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
                                       creationflags=CREATE_NO_WINDOW)
                    if r.returncode != 0:
                        self.q.put(("log", 0, f"extract failed ({p.name}) rc={r.returncode}"))
                        continue
                n += 1
                if self.s("extract_delete"):
                    p.unlink(missing_ok=True)
            except Exception as e:
                self.q.put(("log", 0, f"extract failed ({p.name}): {e}"))
        return n

    def on_finalized(self, jid, ok, note):
        j = self.by_id.get(jid)
        if not j:
            return
        if ok:
            j.status, j.note, j.speed = "Done", note, 0
            self.stats["links"] += 1
            save_json(STATS_FILE, self.stats)
            self.done_since_rotate += 1
            n = self.s("proxy_cycle_n")
            if n and self.done_since_rotate >= n:
                self.rotate_proxy()
            if self.s("auto_cleanup"):
                self.jobs.remove(j)
                self.by_id.pop(jid, None)
        else:
            j.status, j.note = "Failed", note
        self.save_queue()

    def check_queue_empty(self):
        if self.active_session and not self.has_pending() and not any(j.proc for j in self.jobs):
            self.active_session = False
            ok = sum(1 for j in self.jobs if j.status == "Done")
            self.say("Queue finished.", "success")
            self.play_sound()
            self.balloon("Queue finished", f"{ok} link(s) complete")
            act = self.s("post_action")
            if self.close_pending or act == POST_ACTIONS[1]:
                threading.Timer(2.5, self.quit_app).start()
            elif act == POST_ACTIONS[2]:
                threading.Timer(2.5, self.sleep_pc).start()

    def sleep_pc(self):
        if IS_WIN:
            subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])

    def set_wake_lock(self, on):
        if IS_WIN:
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 if on else 0x80000000)
            except Exception:
                pass

    # -------------------------------------------------------------- proxies
    def proxy_file(self):
        lines = []
        pf = self.s("proxy_file")
        if pf and os.path.isfile(pf):
            with open(pf, encoding="utf-8", errors="ignore") as f:
                lines += [l.strip() for l in f if l.strip() and not l.startswith("#")]
        host, port = self.s("proxy_host").strip(), str(self.s("proxy_port")).strip()
        if host and port:
            auth = f"{self.s('proxy_user')}:{self.s('proxy_pass')}@" if self.s("proxy_user") else ""
            lines.append(f"{self.s('proxy_type')}://{auth}{host}:{port}")
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

    def api_rotate_proxy(self, _d):
        self.rotate_proxy()
        self.say("Proxy rotated. It applies to the next link that starts.")
        return {}

    def api_test_flare(self, _d):
        url = self.s("flare_url").strip().rstrip("/")
        if not url.endswith("/v1"):
            url += "/v1"
        try:
            req = urllib.request.Request(url, data=json.dumps({"cmd": "sessions.list"}).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode())
            if data.get("status") == "ok":
                return {"ok": True, "msg": f"Connected ({data.get('version', 'FlareSolverr')})"}
            return {"ok": False, "msg": f"Unexpected reply: {str(data)[:80]}"}
        except Exception as e:
            return {"ok": False, "msg": f"Failed: {e}"}

    # ----------------------------------------------------- stats / exports
    def api_reset_stats(self, _d):
        self.stats.update({"bytes": 0, "seconds": 0.0, "links": 0, "days": {}})
        save_json(STATS_FILE, self.stats)
        self.say("Lifetime statistics reset.")
        return {}

    def api_export_stats(self, d):
        kind, s = d.get("kind", "csv"), self.stats
        rows = [("Total downloaded (GB)", f"{s['bytes'] / 1024 ** 3:.3f}"),
                ("Average speed (B/s)", f"{s['bytes'] / s['seconds']:.0f}" if s["seconds"] else "0"),
                ("Links processed", s["links"]),
                ("Exported", datetime.datetime.now().isoformat(timespec="seconds"))]
        daily = sorted(s["days"].items())
        if kind == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["metric", "value"])
            w.writerows(rows)
            w.writerow([])
            w.writerow(["date", "bytes"])
            w.writerows(daily)
            return {"name": "mdrs_metrics.csv", "mime": "text/csv", "data": buf.getvalue()}
        body = "".join(f"<tr><td>{html.escape(str(a))}</td><td>{html.escape(str(b))}</td></tr>" for a, b in rows)
        body2 = "".join(f"<tr><td>{a}</td><td>{human_size(b)}</td></tr>" for a, b in daily)
        page = ("<!doctype html><meta charset=utf-8><title>mdrs metrics</title>"
                "<style>body{font:15px Segoe UI,sans-serif;margin:2rem}td,th{padding:4px 14px;"
                "border-bottom:1px solid #ccc;text-align:left}</style><h2>MDRS GUI metrics</h2>"
                f"<table>{body}</table><h3>Per day</h3><table><tr><th>Date</th><th>Downloaded</th></tr>{body2}</table>")
        return {"name": "mdrs_metrics.html", "mime": "text/html", "data": page}

    def api_export_log(self, _d):
        return {"name": "mdrs_log.txt", "mime": "text/plain", "data": "\n".join(self.log_lines)}

    def api_clear_log(self, _d):
        self.log_lines.clear()
        self.log_seq += 1
        return {}

    def api_force_check(self, _d):
        hist = list(self.history)
        if not hist:
            self.say("History is empty. Enable the MD5 check first; completed files are recorded "
                     "automatically.", "sheet", "Force Check")
            return {}
        self.say("Force-checking history…")

        def work():
            ok = bad = miss = 0
            for h in hist:
                p = h["path"]
                if not os.path.exists(p):
                    miss += 1
                    self.q.put(("log", 0, f"force-check MISSING  {p}"))
                elif md5_file(p) == h["md5"]:
                    ok += 1
                else:
                    bad += 1
                    self.q.put(("log", 0, f"force-check MISMATCH {p}"))
            self.q.put(("say", f"{ok} OK, {bad} mismatched, {miss} missing.", "sheet", "Force Check"))
        threading.Thread(target=work, daemon=True).start()
        return {}

    # -------------------------------------------------------------- updates
    def api_check_updates(self, _d):
        threading.Thread(target=self.check_updates, args=(True,), daemon=True).start()
        return {}

    def check_updates(self, manual=False):
        local = "?"
        exe = find_mdrs(self.s("mdrs_path"))
        if exe:
            try:
                local = subprocess.run([exe, "-V"], capture_output=True, text=True, timeout=10,
                                       creationflags=CREATE_NO_WINDOW).stdout.strip().split()[-1]
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
        with self.lock:
            self.ver_text = msg
            self.meta_ver += 1
            if manual or "newer" in msg:
                self.say(msg)
        self.notify()

    # --------------------------------------------------------------- sounds
    def api_test_sound(self, _d):
        self.play_sound(force=True)
        return {}

    def play_sound(self, force=False):
        if self.is_quiet() and not force:
            return
        args = (self.s("sound"), self.s("custom_wav"), max(0, min(100, int(self.s("volume")))))
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
            if name == "None" or vol <= 0 or not winsound:
                return
            if name in MELODIES:
                winsound.PlaySound(synth_wav(MELODIES[name], vol), winsound.SND_MEMORY)
            elif name == CUSTOM_WAV:
                self._play_file(custom_wav, vol)
            elif name in self.media:
                self._play_file(self.media[name], vol)
            elif name in SYSTEM_SOUNDS:
                winsound.PlaySound(SYSTEM_SOUNDS[name], winsound.SND_ALIAS)
        except Exception:
            pass

    def balloon(self, title, msg):
        if self.tray and not self.is_quiet():
            self.tray.balloon(title, msg)

    # --------------------------------------------------- window / lifecycle
    def add_client(self, conn):
        with self.lock:
            self.clients.add(conn)
            self.win_state = "open"

    def remove_client(self, conn):
        with self.lock:
            self.clients.discard(conn)
            if not self.clients:
                self.win_since = time.time()

    def open_window(self):
        if self.port is None:
            return
        self.win_state, self.win_since, self.fallback_opened = "awaiting", time.time(), False
        if not NO_WINDOW:
            launch_window(self.port, self.token)

    def show_window(self):
        """Tray 'Show': open a window unless one is already connected."""
        with self.lock:
            if self.clients:
                return
        self.open_window()

    def watchdog(self, now):
        """Detects the window being closed (all event streams gone) and applies the close policy."""
        if NO_WINDOW or self.clients:
            return
        if self.win_state == "awaiting":
            waited = now - self.win_since
            if waited > 20 and not self.fallback_opened:
                self.fallback_opened = True
                webbrowser.open(f"http://127.0.0.1:{self.port}/?k={self.token}")
            if waited > 90 and not (self.tray and self.tray.ok):
                self.quit_app()
        elif self.win_state == "open" and now - self.win_since > 3.5:
            self.win_state = "closed"
            self.on_window_closed()

    def on_window_closed(self):
        act = self.s("close_action")
        if act == CLOSE_ACTIONS[1]:
            self.quit_app()
        elif act == CLOSE_ACTIONS[2] or not (self.tray and self.tray.ok):
            # "close after queue"; also the fallback when no tray icon is available
            if self.has_pending():
                self.close_pending, self.global_pause = True, False
                self.log_add("window closed: will quit when the queue finishes")
            else:
                self.quit_app()
        else:
            self.log_add("window closed: running in the system tray")

    def quit_app(self):
        try:
            self.save_settings()
            self.save_queue()
            save_json(STATS_FILE, self.stats)
            for j in self.jobs:
                if j.proc:
                    j.kill_reason = "removed"
                    proc_kill(j.proc)
            self.set_wake_lock(False)
            if self.tray:
                self.tray.remove()
            if INSTANCE_FILE.exists():
                INSTANCE_FILE.unlink()
        finally:
            os._exit(0)

    def api_quit(self, _d):
        threading.Timer(0.2, self.quit_app).start()
        return {}

    # ------------------------------------------------------------ snapshots
    def snapshot(self, conn):
        """One SSE message: everything for a new connection, otherwise only what changed."""
        with self.lock:
            m = {"t": "full" if conn.first else "tick", "speed": self.total_speed,
                 "paused": self.global_pause, "warn": self.warn,
                 "stats": {"bytes": self.stats["bytes"], "seconds": self.stats["seconds"],
                           "links": self.stats["links"], "today": self.today_bytes()}}
            if conn.first:
                m["hist"] = list(self.speed_hist)
            else:
                m["hist"] = self.speed_hist[-1]
            if conn.meta_ver != self.meta_ver or conn.first:
                conn.meta_ver = self.meta_ver
                m["meta"] = {"title": TITLE, "win": IS_WIN, "tray": bool(self.tray and self.tray.ok),
                             "mdrs": bool(find_mdrs(self.s("mdrs_path"))), "sounds": self.sound_names(),
                             "version": self.ver_text, "ps": os.path.basename(sys.argv[0])}
            if conn.set_ver != self.set_ver:
                conn.set_ver = self.set_ver
                m["settings"] = self.cfg
            live, changed = set(), []
            for j in self.jobs:
                live.add(j.id)
                sig = (j.status, j.note, j.tries, j.size_now, int(j.speed), j.user_paused,
                       j.suspended, j.tag, j.prio)
                if conn.sigs.get(j.id) != sig:
                    conn.sigs[j.id] = sig
                    changed.append(j.row())
            gone = [i for i in conn.sigs if i not in live]
            for i in gone:
                del conn.sigs[i]
            if changed:
                m["jobs"] = changed
            if gone:
                m["gone"] = gone
            if conn.first or conn.log_seq != self.log_seq:
                n_new = self.log_seq - conn.log_seq
                lines = list(self.log_lines)
                m["log"] = {"reset": conn.first or n_new < 0 or n_new > len(lines),
                            "lines": lines if (conn.first or n_new < 0 or n_new > len(lines))
                            else (lines[-n_new:] if n_new else [])}
                conn.log_seq = self.log_seq
            new = [n for n in self.notices if n["seq"] > conn.notice_seq]
            if new:
                conn.notice_seq = new[-1]["seq"]
            if not conn.first:
                if new:
                    m["notices"] = new
            else:
                conn.notice_seq = self.notice_seq
            conn.first = False
            return m, self.ver


PAGE = r'''<!doctype html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>MDRS GUI: Windows Front-End for mediafire_rs</title>
<style>
/* ===== tokens ===== */
:root{
  --ease-out:cubic-bezier(.23,1,.32,1);--ease-in-out:cubic-bezier(.77,0,.175,1);--ease-drawer:cubic-bezier(.32,.72,0,1);
  /* JS replaces these with real spring curves (linear() easing) when supported */
  --sp-ui:var(--ease-drawer);--sp-ui-t:.34s;--sp-sheet:var(--ease-drawer);--sp-sheet-t:.45s;--sp-snap:var(--ease-drawer);--sp-snap-t:.42s;
  --hit:44px;--gutter:clamp(16px,3vw,32px);--ts:1;
  --font:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;
  --font-d:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;
  --mono:ui-monospace,"SF Mono","Cascadia Mono",Consolas,monospace;
}
:root[data-theme=light]{color-scheme:light;
  --blue:#007AFF;--green:#34C759;--red:#FF3B30;--orange:#FF9500;--purple:#AF52DE;--teal:#30B0C7;--gray:#8E8E93;
  --bg:#F2F2F7;--bg2:#FFFFFF;--label:#000;--label2:rgba(60,60,67,.62);--label3:rgba(60,60,67,.3);--sep:rgba(60,60,67,.2);
  --fill:rgba(120,120,128,.2);--fill2:rgba(120,120,128,.14);--fill3:rgba(118,118,128,.1);
  --mat:rgba(250,250,252,.72);--mat-thick:rgba(246,246,248,.88);--side:rgba(236,236,242,.62);
  --shadow:0 12px 40px rgba(0,0,0,.16),0 2px 6px rgba(0,0,0,.08),0 0 0 .5px rgba(0,0,0,.06);--scrim:rgba(0,0,0,.3);
  --wall:radial-gradient(900px 600px at 0% 0%,#cfe0ff 0,transparent 60%),radial-gradient(700px 500px at 0% 100%,#e6d4ff 0,transparent 60%),#eceef5}
:root[data-theme=dark]{color-scheme:dark;
  --blue:#0A84FF;--green:#30D158;--red:#FF453A;--orange:#FF9F0A;--purple:#BF5AF2;--teal:#40C8E0;--gray:#98989D;
  --bg:#000;--bg2:#1C1C1E;--label:#fff;--label2:rgba(235,235,245,.62);--label3:rgba(235,235,245,.3);--sep:rgba(84,84,88,.6);
  --fill:rgba(120,120,128,.36);--fill2:rgba(120,120,128,.28);--fill3:rgba(118,118,128,.22);
  --mat:rgba(44,44,46,.7);--mat-thick:rgba(40,40,43,.88);--side:rgba(30,30,34,.66);
  --shadow:0 14px 44px rgba(0,0,0,.65),0 0 0 .5px rgba(255,255,255,.1);--scrim:rgba(0,0,0,.55);
  --wall:radial-gradient(900px 600px at 0% 0%,#1a2a52 0,transparent 60%),radial-gradient(700px 500px at 0% 100%,#2f1c52 0,transparent 60%),#0b0b10}
@media (prefers-reduced-transparency:reduce){:root{--mat:var(--bg2);--mat-thick:var(--bg2);--side:var(--bg2)}
  *{backdrop-filter:none!important;-webkit-backdrop-filter:none!important}}
@media (prefers-contrast:more){:root{--sep:var(--label3)}.group,.tile,.card{outline:1px solid var(--sep)}}

/* ===== base ===== */
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{font-size:calc(100% * var(--ts));-webkit-text-size-adjust:100%}
html,body{margin:0;height:100%}
body{font:400 1.0625rem/1.35 var(--font);color:var(--label);background:var(--bg);overflow:hidden;overscroll-behavior:none;
  -webkit-font-smoothing:antialiased;user-select:none;-webkit-user-select:none}
button,input,textarea{font:inherit;color:inherit;letter-spacing:inherit}
button{touch-action:manipulation;cursor:default}
input,textarea{user-select:text;-webkit-user-select:text}
svg.ic{width:1.5em;height:1.5em;flex:none;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
svg.ic .f{fill:currentColor;stroke:none}
:focus-visible{outline:3px solid color-mix(in srgb,var(--blue) 55%,transparent);outline-offset:2px;border-radius:10px}
::selection{background:color-mix(in srgb,var(--blue) 30%,transparent)}
*::-webkit-scrollbar{width:12px;height:12px}
*::-webkit-scrollbar-thumb{background:var(--fill);border-radius:8px;border:3px solid transparent;background-clip:content-box}
*::-webkit-scrollbar-thumb:hover{background:var(--label3);background-clip:content-box;border:3px solid transparent}

[hidden]{display:none!important}

/* ===== shell ===== */
.app{display:grid;grid-template-columns:clamp(208px,22vw,252px) minmax(0,1fr);height:100dvh;background:var(--wall)}
.sidebar{display:flex;flex-direction:column;gap:6px;padding:calc(18px + env(safe-area-inset-top)) 12px calc(12px + env(safe-area-inset-bottom));
  background:var(--side);backdrop-filter:blur(40px) saturate(180%);-webkit-backdrop-filter:blur(40px) saturate(180%);
  border-right:.5px solid var(--sep);min-height:0}
.brand{display:flex;align-items:center;gap:10px;padding:4px 10px 14px;font:600 1.0625rem var(--font-d);letter-spacing:-.01em}
.brand .logo{width:32px;height:32px;border-radius:9px;background:linear-gradient(145deg,#5ac8fa,#007aff 55%,#5856d6);color:#fff;display:grid;place-items:center;box-shadow:0 2px 8px rgba(0,122,255,.35)}
.brand .logo svg{width:18px;height:18px}
.brand small{display:block;font:400 .75rem var(--font);color:var(--label2);letter-spacing:0}
.nav-list{position:relative;display:flex;flex-direction:column;gap:4px}
.nav-pill{position:absolute;left:0;top:0;width:100%;height:var(--hit);border-radius:10px;background:var(--fill);
  transform:translateY(calc(var(--i,0) * (var(--hit) + 4px)));transition:transform var(--sp-ui-t) var(--sp-ui)}
.nav-item{position:relative;display:flex;align-items:center;gap:12px;height:var(--hit);padding:0 12px;border:0;border-radius:10px;
  background:none;text-align:left;font-weight:500;font-size:1.0625rem;transition:color .18s,transform .16s var(--ease-out)}
.nav-item svg{color:var(--blue)}
.nav-item:active{transform:scale(.97)}
.nav-item .badge{margin-left:auto;font-size:.75rem;font-weight:600;color:var(--label2);font-variant-numeric:tabular-nums}
.side-foot{margin-top:auto;padding:10px;border-radius:12px;background:var(--fill3);font-size:.8125rem;color:var(--label2);display:grid;gap:4px}
.side-foot b{color:var(--label);font:600 1.125rem var(--font-d);font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--gray);margin-right:6px;vertical-align:1px}
.dot.ok{background:var(--green)}.dot.bad{background:var(--red)}
.main{position:relative;min-width:0;min-height:0;background:var(--bg);overflow:hidden}
.page{position:absolute;inset:0;overflow-y:auto;overflow-x:hidden;padding:0 var(--gutter) calc(40px + env(safe-area-inset-bottom));display:none}
.page.on{display:block;animation:page-in .2s var(--ease-out)}
@keyframes page-in{from{opacity:0}to{opacity:1}}
.wrap{max-width:960px;margin:0 auto}
.navbar{position:sticky;top:0;z-index:6;display:flex;align-items:center;gap:12px;min-height:calc(var(--hit) + 16px);
  margin:0 calc(-1 * var(--gutter));padding:calc(8px + env(safe-area-inset-top)) var(--gutter) 8px;
  transition:background-color .2s,box-shadow .2s}
.navbar.scrolled{background:var(--mat);backdrop-filter:blur(30px) saturate(180%);-webkit-backdrop-filter:blur(30px) saturate(180%);box-shadow:0 .5px 0 var(--sep)}
.nb-title{font:600 1.0625rem var(--font-d);opacity:0;transform:translateY(6px);transition:opacity .18s,transform .22s var(--ease-out)}
.navbar.scrolled .nb-title{opacity:1;transform:none}
.nb-actions{margin-left:auto;display:flex;gap:8px;align-items:center}
.large-title{font:700 2.125rem/1.12 var(--font-d);letter-spacing:-.022em;margin:2px 0 2px}
.sub{color:var(--label2);font-size:.9375rem;margin:0 0 12px;min-height:1.3em}

/* ===== controls ===== */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:var(--hit);min-width:var(--hit);padding:0 16px;border:0;border-radius:12px;
  background:var(--fill3);color:var(--blue);font-weight:600;font-size:1rem;white-space:nowrap;
  transition:transform .16s var(--ease-out),background-color .15s,opacity .15s,filter .15s}
.btn:active{transform:scale(.97)}
.btn svg{width:1.25em;height:1.25em}
.btn.filled{background:var(--blue);color:#fff}
.btn.red{color:var(--red)}.btn.filled.red{background:var(--red);color:#fff}
.btn.gray{color:var(--label)}
.btn.icon{padding:0}
.btn[disabled]{opacity:.35;pointer-events:none}
@media (hover:hover) and (pointer:fine){.btn:hover{background:var(--fill2)}.btn.filled:hover{background:var(--blue);filter:brightness(1.08)}.btn.filled.red:hover{background:var(--red)}}
.field{min-height:var(--hit);padding:0 12px;border:0;border-radius:10px;background:var(--fill3);font-size:1rem;min-width:0;
  transition:background-color .15s,box-shadow .15s}
.field:focus{outline:0;background:var(--bg2);box-shadow:0 0 0 2px var(--blue)}
.field::placeholder{color:var(--label3);transition:opacity .12s}
.field:focus::placeholder{opacity:0}
textarea.field{padding:12px;width:100%;min-height:136px;resize:vertical;line-height:1.45;font-size:1rem}
.row .field{width:min(320px,48vw);text-align:right}
.switch{position:relative;flex:none;width:51px;height:31px;padding:0;border:0;border-radius:16px;background:var(--fill);transition:background-color .25s var(--ease-out)}
.switch::before{content:"";position:absolute;inset:-7px -4px}
.switch::after{content:"";position:absolute;top:2px;left:2px;width:27px;height:27px;border-radius:50%;background:#fff;
  box-shadow:0 3px 8px rgba(0,0,0,.15),0 1px 1px rgba(0,0,0,.16);transition:transform var(--sp-ui-t) var(--sp-ui),width .18s var(--ease-out)}
.switch[aria-checked=true]{background:var(--green)}
.switch[aria-checked=true]::after{transform:translateX(20px)}
.switch:active::after{width:33px}.switch[aria-checked=true]:active::after{transform:translateX(14px)}
.stepper{display:inline-flex;align-items:center;background:var(--fill3);border-radius:10px;overflow:hidden}
.stepper button{width:var(--hit);height:var(--hit);border:0;background:none;color:var(--blue);display:grid;place-items:center;transition:background-color .12s,transform .16s var(--ease-out)}
.stepper button:active{background:var(--fill);transform:scale(.94)}
.stepper input{width:76px;height:var(--hit);border:0;background:transparent;text-align:center;font-size:1rem;font-variant-numeric:tabular-nums;border-radius:0}
.stepper input:focus{outline:0;background:var(--bg2);box-shadow:inset 0 0 0 2px var(--blue)}
.row .stepper .field{width:76px;padding:0;text-align:center;border-radius:0}
.unit{color:var(--label2);font-size:.9375rem;white-space:nowrap}
.slider-wrap{display:flex;align-items:center;gap:12px}
.slider-wrap output{min-width:3.4em;text-align:right;color:var(--label2);font-variant-numeric:tabular-nums}
input[type=range]{-webkit-appearance:none;appearance:none;width:min(260px,38vw);height:var(--hit);background:transparent;margin:0;--p:50%}
input[type=range]::-webkit-slider-runnable-track{height:4px;border-radius:2px;background:linear-gradient(to right,var(--blue) var(--p),var(--fill) var(--p))}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:28px;height:28px;margin-top:-12px;border-radius:50%;background:#fff;
  box-shadow:0 3px 8px rgba(0,0,0,.22),0 0 0 .5px rgba(0,0,0,.1);transition:transform .15s var(--ease-out)}
input[type=range]:active::-webkit-slider-thumb{transform:scale(1.1)}
.menu-btn{display:inline-flex;align-items:center;gap:6px;min-height:var(--hit);max-width:min(320px,52vw);padding:0 10px 0 14px;border:0;border-radius:10px;
  background:var(--fill3);color:var(--blue);font-weight:500;transition:transform .16s var(--ease-out),background-color .15s}
.menu-btn:active{transform:scale(.97)}
.menu-btn span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.menu-btn svg{width:1em;height:1em;opacity:.7}
.seg{position:relative;display:inline-flex;padding:2px;background:var(--fill3);border-radius:11px}
.seg button{position:relative;z-index:1;min-height:40px;padding:0 16px;border:0;border-radius:9px;background:none;font-weight:500;font-size:.9375rem;transition:color .15s}
.seg .thumb{position:absolute;top:2px;bottom:2px;left:0;border-radius:9px;background:var(--bg2);box-shadow:0 2px 6px rgba(0,0,0,.14),0 0 0 .5px rgba(0,0,0,.05);
  transition:transform var(--sp-ui-t) var(--sp-ui),width var(--sp-ui-t) var(--sp-ui)}
:root[data-theme=dark] .seg .thumb{background:#636366}
.seg button[aria-pressed=true]{font-weight:600}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{min-height:var(--hit);padding:0 16px;border:0;border-radius:22px;background:var(--fill3);font-weight:600;font-variant-numeric:tabular-nums;
  transition:background-color .2s,color .2s,transform .16s var(--ease-out)}
.chip:active{transform:scale(.95)}
.chip[aria-pressed=true]{background:var(--blue);color:#fff}
.pill{display:inline-flex;align-items:center;height:22px;padding:0 9px;border-radius:11px;font-size:.75rem;font-weight:600;max-width:120px;
  background:color-mix(in srgb,var(--c,var(--gray)) 18%,transparent);color:var(--c,var(--gray));white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* ===== grouped lists ===== */
.group-h{margin:26px 16px 7px;font-size:.8125rem;letter-spacing:.02em;text-transform:uppercase;color:var(--label2)}
.group-f{margin:7px 16px 0;font-size:.8125rem;line-height:1.4;color:var(--label2)}
.group{background:var(--bg2);border-radius:12px;overflow:hidden}
.card{background:var(--bg2);border-radius:14px;padding:16px}
.row{position:relative;display:flex;align-items:center;gap:12px;min-height:var(--hit);padding:7px 16px}
.row+.row::before{content:"";position:absolute;top:0;left:16px;right:0;height:.5px;background:var(--sep)}
.row .lbl{flex:1;min-width:0}
.row .ctl{display:flex;align-items:center;gap:8px;margin-left:auto;min-width:0}
.row.col{flex-direction:column;align-items:stretch;padding-block:12px}
.row .note{color:var(--label2);font-size:.9375rem;text-align:right}
.row.link{color:var(--blue);font-weight:500;width:100%;border:0;background:none;text-align:left;transition:background-color .12s}
.row.link.red{color:var(--red)}
.row.link:active{background:var(--fill3)}
.result{color:var(--label2);font-size:.9375rem}.result.ok{color:var(--green)}.result.bad{color:var(--red)}

/* ===== queue ===== */
.banner{display:flex;align-items:center;gap:10px;padding:10px 14px;margin:0 0 12px;border-radius:12px;font-weight:500;font-size:.9375rem;
  background:color-mix(in srgb,var(--orange) 16%,var(--bg2));color:color-mix(in srgb,var(--orange) 82%,var(--label));
  animation:banner-in .35s var(--sp-ui)}
.banner.bad{background:color-mix(in srgb,var(--red) 14%,var(--bg2));color:var(--red)}
@keyframes banner-in{from{opacity:0;transform:translateY(-8px) scale(.98)}to{opacity:1;transform:none}}
.add-grid{display:grid;grid-template-columns:minmax(0,1fr) 264px;gap:12px;align-items:stretch}
.add-grid textarea.field{height:100%;min-height:164px;resize:none}
.add-side{display:grid;gap:8px;min-width:0;align-content:start}
.add-side .two{display:grid;grid-template-columns:1.4fr 1fr;gap:8px}
.add-side .two .btn{padding:0 8px}
.disc{display:flex;align-items:center;gap:6px;width:calc(100% - 32px);margin:26px 16px 7px;padding:0;min-height:var(--hit);border:0;background:none;text-align:left;
  font-size:.8125rem;letter-spacing:.02em;text-transform:uppercase;color:var(--label2);margin-block:6px 0}
.disc svg{width:1.2em;height:1.2em;margin-left:auto;transition:transform var(--sp-ui-t) var(--sp-ui)}
.disc[aria-expanded=false] svg{transform:rotate(-90deg)}
.collapse{display:grid;grid-template-rows:1fr;transition:grid-template-rows var(--sp-sheet-t) var(--sp-sheet),opacity .2s;margin-bottom:0}
.collapse>div{overflow:hidden;min-height:0}
.collapse.closed{grid-template-rows:0fr;opacity:0}
.add-side .row-mini{display:flex;align-items:center;gap:10px;justify-content:space-between}
.drop{display:grid;place-items:center;gap:0;min-height:64px;padding:10px;border:1.5px dashed var(--label3);border-radius:12px;text-align:center;
  color:var(--label2);font-size:.875rem;background:none;transition:background-color .2s,border-color .2s,transform .2s var(--sp-ui),color .2s}
.drop svg{width:1.7em;height:1.7em;color:var(--blue)}
.drop.over,.field.over{border-color:var(--blue);background:color-mix(in srgb,var(--blue) 10%,transparent);transform:scale(1.015);color:var(--blue)}
textarea.field.over{box-shadow:0 0 0 2px var(--blue)}
.qtool{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:18px 0 10px}
.search{position:relative;flex:1 1 100%}
.qtool .row{margin-left:auto}
.search svg{position:absolute;left:12px;top:50%;transform:translateY(-50%);width:1.1em;height:1.1em;color:var(--label2);pointer-events:none}
.search .field{width:100%;padding-left:36px;border-radius:12px}
.vwrap{background:var(--bg2);border-radius:12px;overflow:hidden;position:relative}
.vspace{position:relative}
.qrow{position:absolute;left:0;right:0;top:0;height:64px;background:var(--bg2);transition:transform var(--sp-ui-t) var(--sp-ui),background-color .15s;will-change:transform;contain:layout paint}
.qrow::after{content:"";position:absolute;left:64px;right:0;bottom:0;height:.5px;background:var(--sep)}
.qrow.sel{background:color-mix(in srgb,var(--blue) 13%,var(--bg2))}
.qrow.noanim{transition:none}
.qin{position:relative;display:flex;align-items:center;gap:12px;height:100%;padding:0 8px 0 16px}
.qrow.enter .qin{animation:row-in .38s var(--ease-out)}
@keyframes row-in{from{opacity:0;transform:scale(.985) translateY(6px)}to{opacity:1;transform:none}}
.qi{width:36px;height:36px;border-radius:50%;display:grid;place-items:center;flex:none;color:var(--c);background:color-mix(in srgb,var(--c) 16%,transparent);transition:color .2s,background-color .2s}
.qi svg{width:20px;height:20px}
.qi.spin svg{animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{50%{transform:translateY(2px);opacity:.6}}
.qm{flex:1;min-width:0}
.qt,.qs{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.qt{font-weight:600;font-size:1rem;letter-spacing:-.005em}
.qs{font-size:.8125rem;color:var(--label2)}
.qr{display:flex;align-items:center;gap:10px;flex:none}
.qn{text-align:right;font-size:.8125rem;color:var(--label2);font-variant-numeric:tabular-nums;min-width:88px;line-height:1.3}
.qn b{display:block;color:var(--label);font-weight:600}
.prio{font-size:.75rem;font-weight:700;letter-spacing:.02em;width:2.4em;text-align:center}
.prio.High{color:var(--red)}.prio.Medium{color:var(--label2)}.prio.Low{color:var(--teal)}
.qa{display:flex;gap:0;margin-left:2px}
.qa .btn{background:none;color:var(--label2);padding:0}
.qa .btn.red:hover{color:var(--red)}
.qbar{position:absolute;left:64px;right:16px;bottom:3px;height:3px;border-radius:2px;background:var(--fill2);overflow:hidden;opacity:0;transition:opacity .2s}
.qrow.run .qbar{opacity:1}
.qbar i{position:absolute;inset:0;width:38%;border-radius:2px;background:var(--blue);animation:slide 1.3s var(--ease-in-out) infinite}
.qrow.held .qbar i,.qrow.pausd .qbar i{animation-play-state:paused;background:var(--orange)}
@keyframes slide{from{transform:translateX(-100%)}to{transform:translateX(265%)}}
.empty{display:none;place-items:center;gap:6px;padding:56px 20px;text-align:center;color:var(--label2)}
.empty.on{display:grid}
.empty svg{width:52px;height:52px;color:var(--label3);stroke-width:1.4}
.empty b{font:600 1.1875rem var(--font-d);color:var(--label)}

/* ===== statistics ===== */
.big{font:700 clamp(2.4rem,7vw,3.6rem)/1 var(--font-d);letter-spacing:-.035em;font-variant-numeric:tabular-nums}
.big small{font:500 1.0625rem var(--font);letter-spacing:0;color:var(--label2);margin-left:6px}
.graph{position:relative;margin-top:12px}
.graph canvas{width:100%;height:240px;display:block}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-top:12px}
.tile{background:var(--bg2);border-radius:14px;padding:16px;display:grid;gap:4px}
.tile span{font-size:.8125rem;color:var(--label2);text-transform:uppercase;letter-spacing:.02em}
.tile b{font:700 1.5rem/1.15 var(--font-d);letter-spacing:-.02em;font-variant-numeric:tabular-nums}

/* ===== log ===== */
.logbox{margin:0;padding:14px;height:calc(100dvh - 190px);min-height:240px;overflow:auto;background:var(--bg2);border-radius:12px;
  font:.75rem/1.55 var(--mono);white-space:pre-wrap;word-break:break-all;user-select:text;-webkit-user-select:text}

/* ===== schedule grids ===== */
.gp-scroll{overflow-x:auto;padding:4px 2px 10px}
.gp{display:grid;grid-template-columns:40px repeat(24,var(--cell,30px));gap:3px;width:max-content;touch-action:none}
.gp .h,.gp .d{font-size:.6875rem;color:var(--label2);display:grid;align-items:center;font-variant-numeric:tabular-nums}
.gp .d{font-size:.8125rem;color:var(--label)}
.gp .c{height:var(--cell,30px);border-radius:7px;background:var(--lv,var(--fill3));transition:background-color .12s}
@media (pointer:coarse){.gp{--cell:44px}}

/* ===== layer: popovers, sheets, toasts, tooltips ===== */
.pop{position:fixed;z-index:50;min-width:190px;max-width:min(360px,92vw);max-height:min(60vh,440px);overflow:auto;padding:6px;border-radius:14px;
  background:var(--mat-thick);backdrop-filter:blur(40px) saturate(180%);-webkit-backdrop-filter:blur(40px) saturate(180%);box-shadow:var(--shadow);
  transform-origin:var(--ox,50%) var(--oy,0%);opacity:0;transform:scale(.95);transition:opacity .2s var(--ease-out),transform .2s var(--ease-out)}
.pop.in{opacity:1;transform:none}
.pop-item{display:flex;align-items:center;gap:10px;width:100%;min-height:var(--hit);padding:0 12px;border:0;border-radius:8px;background:none;text-align:left;font-size:1rem}
.pop-item span{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pop-item svg{width:1.1em;height:1.1em;color:var(--blue)}
.pop-item.red{color:var(--red)}
.pop-item:active{background:var(--fill2)}
@media (hover:hover) and (pointer:fine){.pop-item:hover,.pop-item.kb{background:var(--fill3)}}
.pop-item[disabled]{opacity:.35;pointer-events:none}
.pop-sep{height:.5px;background:var(--sep);margin:5px 8px}
.scrim{position:fixed;inset:0;z-index:40;background:var(--scrim);opacity:0;transition:opacity .25s var(--ease-out)}
.scrim.in{opacity:1}
.sheet{position:fixed;z-index:41;inset:0;margin:auto;height:fit-content;width:min(420px,calc(100vw - 32px));padding:22px 20px 16px;border-radius:22px;
  background:var(--mat-thick);backdrop-filter:blur(50px) saturate(180%);-webkit-backdrop-filter:blur(50px) saturate(180%);box-shadow:var(--shadow);
  opacity:0;transform:translateY(14px) scale(.96);transition:opacity .2s var(--ease-out),transform var(--sp-sheet-t) var(--sp-sheet);touch-action:none}
.sheet.in{opacity:1;transform:none}
.sheet.drag{transition:none}
.sheet h3{margin:0 0 6px;font:600 1.1875rem var(--font-d);letter-spacing:-.01em;text-align:center}
.sheet p{margin:0 0 16px;text-align:center;color:var(--label2);font-size:.9375rem;line-height:1.4;white-space:pre-wrap;user-select:text}
.sheet .field{width:100%;text-align:left;margin-bottom:14px}
.sheet .acts{display:grid;gap:8px}
.sheet .acts .btn{width:100%}
.grab{display:none;width:36px;height:5px;border-radius:3px;background:var(--label3);margin:-12px auto 14px}
@media (max-width:640px){
  .sheet{inset:auto 8px calc(8px + env(safe-area-inset-bottom)) 8px;margin:0;width:auto;transform:translateY(110%);opacity:1}
  .sheet.in{transform:none}.grab{display:block}}
#toasts{position:fixed;z-index:70;top:calc(10px + env(safe-area-inset-top));left:0;right:0;display:flex;flex-direction:column;align-items:center;gap:8px;pointer-events:none}
.toast{display:flex;align-items:center;gap:10px;max-width:min(520px,calc(100vw - 24px));padding:11px 18px 11px 14px;border-radius:24px;font-weight:500;font-size:.9375rem;
  background:var(--mat-thick);backdrop-filter:blur(30px) saturate(180%);-webkit-backdrop-filter:blur(30px) saturate(180%);box-shadow:var(--shadow);
  transform:translateY(-160%);opacity:0;transition:transform var(--sp-sheet-t) var(--sp-sheet),opacity .22s var(--ease-out)}
.toast.in{transform:none;opacity:1}
.toast svg{width:1.3em;height:1.3em;color:var(--blue)}
.toast.success svg{color:var(--green)}.toast.error svg{color:var(--red)}
.tip{position:fixed;z-index:80;max-width:300px;padding:8px 12px;border-radius:10px;font-size:.8125rem;line-height:1.4;pointer-events:none;
  background:var(--mat-thick);backdrop-filter:blur(30px) saturate(180%);-webkit-backdrop-filter:blur(30px) saturate(180%);box-shadow:var(--shadow);
  opacity:0;transform:scale(.97);transform-origin:top left;transition:opacity .125s var(--ease-out),transform .125s var(--ease-out)}
.tip.in{opacity:1;transform:none}.tip.instant{transition-duration:0s}
.offline{position:fixed;inset:0;z-index:90;display:none;place-items:center;background:var(--scrim);backdrop-filter:blur(8px)}
.offline.on{display:grid}.offline .card{max-width:360px;text-align:center;box-shadow:var(--shadow)}

/* ===== narrow: bottom tab bar (iOS) ===== */
@media (max-width:800px){
  .app{grid-template-columns:1fr;grid-template-rows:minmax(0,1fr) auto}
  .sidebar{order:2;flex-direction:row;padding:6px 8px calc(6px + env(safe-area-inset-bottom));border-right:0;border-top:.5px solid var(--sep);gap:0}
  .brand,.side-foot{display:none}
  .nav-list{flex-direction:row;width:100%;gap:0;--n:5}
  .nav-pill{width:calc(100% / var(--n));height:100%;transform:translateX(calc(var(--i,0) * 100%));background:var(--fill2)}
  .nav-item{flex:1;flex-direction:column;justify-content:center;gap:2px;height:auto;min-height:var(--hit);padding:6px 2px;font-size:.6875rem;border-radius:10px}
  .nav-item svg{width:1.6em;height:1.6em}.nav-item .badge{display:none}
  .row .field,.menu-btn{max-width:56vw}
  .add-grid{grid-template-columns:1fr}.add-grid textarea.field{min-height:136px}
  .qr .qn{display:none}.prio,.qr .pill{display:none}
  .row{flex-wrap:wrap}
}
@media (max-width:420px){.qa .btn[data-act=trash]{display:none}.large-title{font-size:1.9rem}}

/* ===== reduced motion: gentler, not zero ===== */
@media (prefers-reduced-motion:reduce){
  :root{--sp-ui:ease;--sp-ui-t:.14s;--sp-sheet:ease;--sp-sheet-t:.16s;--sp-snap:ease;--sp-snap-t:.16s}
  .sheet,.toast,.pop,.banner,.qrow.enter .qin,.drop.over{transform:none!important}
  .qbar i,.qi.spin svg{animation:none}.page.on{animation:none}
  *{scroll-behavior:auto!important}
}

</style>
</head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
<symbol id="i-down" viewBox="0 0 24 24"><path d="M12 3.5v11m0 0l-4-4m4 4l4-4M4.5 16.5v2a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2v-2"/></symbol>
<symbol id="i-chart" viewBox="0 0 24 24"><path d="M4 4v16h16"/><path d="M8 15l3.5-4.5 3 3L20 7"/></symbol>
<symbol id="i-globe" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3c2.6 2.7 3.9 5.7 3.9 9s-1.3 6.3-3.9 9c-2.6-2.7-3.9-5.7-3.9-9S9.4 5.7 12 3z"/></symbol>
<symbol id="i-sliders" viewBox="0 0 24 24"><path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/></symbol>
<symbol id="i-term" viewBox="0 0 24 24"><rect x="3" y="4.5" width="18" height="15" rx="3"/><path d="M7.5 9.5l3 2.5-3 2.5M13 15h3.5"/></symbol>
<symbol id="i-play" viewBox="0 0 24 24"><path fill="currentColor" stroke="none" d="M8.2 5.3a1 1 0 0 0-1.5.86v11.68a1 1 0 0 0 1.5.86l9.9-5.84a1 1 0 0 0 0-1.72z"/></symbol>
<symbol id="i-pause" viewBox="0 0 24 24"><rect fill="currentColor" stroke="none" x="6.5" y="5" width="3.8" height="14" rx="1.2"/><rect fill="currentColor" stroke="none" x="13.7" y="5" width="3.8" height="14" rx="1.2"/></symbol>
<symbol id="i-trash" viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l.9 11.2a2 2 0 0 0 2 1.8h6.2a2 2 0 0 0 2-1.8L18 7M9 7V4.5h6V7"/></symbol>
<symbol id="i-retry" viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.9-6.2"/><path d="M20 4.5V9h-4.5"/></symbol>
<symbol id="i-plus" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></symbol>
<symbol id="i-minus" viewBox="0 0 24 24"><path d="M5 12h14"/></symbol>
<symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/></symbol>
<symbol id="i-folder" viewBox="0 0 24 24"><path d="M3 7.5a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></symbol>
<symbol id="i-doc" viewBox="0 0 24 24"><path d="M7 3.5h7l5 5V19a1.5 1.5 0 0 1-1.5 1.5h-10A1.5 1.5 0 0 1 6 19V5A1.5 1.5 0 0 1 7 3.5zM14 3.5v5h5"/></symbol>
<symbol id="i-x" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></symbol>
<symbol id="i-check" viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></symbol>
<symbol id="i-warn" viewBox="0 0 24 24"><path d="M12 4.2l9 15.6H3z"/><path d="M12 10v4.2M12 17.2v.3"/></symbol>
<symbol id="i-updown" viewBox="0 0 24 24"><path d="M8 9.5l4-4 4 4M8 14.5l4 4 4-4"/></symbol>
<symbol id="i-tag" viewBox="0 0 24 24"><path d="M3.5 12.2V5a1.5 1.5 0 0 1 1.5-1.5h7.2l8.3 8.3-8.7 8.7z"/><circle cx="8.2" cy="8.2" r="1.2" fill="currentColor" stroke="none"/></symbol>
<symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5.2l3.2 2"/></symbol>
<symbol id="i-export" viewBox="0 0 24 24"><path d="M12 15V3.5m0 0L8 7.5m4-4l4 4M5 12v6.5a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V12"/></symbol>
<symbol id="i-reset" viewBox="0 0 24 24"><path d="M4 12a8 8 0 1 0 2.9-6.2"/><path d="M4 4.5V9h4.5"/></symbol>
<symbol id="i-info" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.6v.3"/></symbol>
<symbol id="i-checkc" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M8 12.3l2.8 2.8 5.5-5.6"/></symbol>
<symbol id="i-xc" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M9.2 9.2l5.6 5.6M14.8 9.2l-5.6 5.6"/></symbol>
<symbol id="i-box" viewBox="0 0 24 24"><path d="M3.5 8L12 4l8.5 4v8L12 20l-8.5-4z"/><path d="M3.5 8L12 12l8.5-4M12 12v8"/></symbol>
<symbol id="i-shield" viewBox="0 0 24 24"><path d="M12 3.5l7 2.8v5.2c0 4.3-2.9 7.4-7 9-4.1-1.6-7-4.7-7-9V6.3z"/><path d="M9 12l2.2 2.2L15.5 10"/></symbol>
<symbol id="i-arrowdown" viewBox="0 0 24 24"><path d="M12 5v14m0 0l-5-5m5 5l5-5"/></symbol>
<symbol id="i-bolt" viewBox="0 0 24 24"><path d="M13 3L5 13.5h6L10 21l8-10.5h-6z"/></symbol>
<symbol id="i-chev" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></symbol>
</defs></svg>

<div class="app"><nav class="sidebar" id="nav" aria-label="Sections"></nav><main class="main" id="main"></main></div>
<script>
'use strict';
const TOKEN = "__TOKEN__";
const NS = 'http://www.w3.org/2000/svg';
const NO_TAG = '(No tag)';
const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const $ = (s, r = document) => r.querySelector(s);
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');

/* ---------- tiny view builder (one helper instead of hundreds of createElement calls) ---------- */
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style') typeof v === 'string' ? (el.style.cssText = v) : Object.assign(el.style, v);
    else if (k === 'tip') el.dataset.tip = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k in el && k !== 'list') el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  el.append(...kids.flat(Infinity).filter(x => x != null && x !== false));
  return el;
}
function icon(name, cls = '') {
  const s = document.createElementNS(NS, 'svg'), u = document.createElementNS(NS, 'use');
  s.setAttribute('class', 'ic ' + cls); s.setAttribute('aria-hidden', 'true');
  u.setAttribute('href', '#i-' + name); s.append(u);
  return s;
}
const setIcon = (svg, name) => svg.firstChild.setAttribute('href', '#i-' + name);

/* ---------- spring physics -> CSS linear() easing (Apple-style damping ratio + response) ---------- */
function spring(response, ratio) {
  const w = 2 * Math.PI / response, k = w * w, c = 2 * ratio * w, dt = 1 / 480;
  let x = 0, v = 0, t = 0, still = 0, next = 0; const pts = [];
  while (t < 3) {
    v += (-k * (x - 1) - c * v) * dt; x += v * dt; t += dt;
    if (t >= next) { pts.push(+x.toFixed(4)); next += 1 / 60; }
    if (Math.abs(x - 1) < .0015 && Math.abs(v) < .02) { if (++still > 20) break; } else still = 0;
  }
  return { ease: `linear(0, ${pts.join(', ')}, 1)`, dur: +t.toFixed(3) };
}
function installSprings() {
  const r = document.documentElement.style;
  const names = ['ui', 'sheet', 'snap'];
  if (reduceMotion.matches || !CSS.supports('transition-timing-function', 'linear(0, 1)')) {
    names.forEach(n => { r.removeProperty(`--sp-${n}`); r.removeProperty(`--sp-${n}-t`); });
    return;
  }
  // [name, response(s), damping ratio]: critically damped by default; bounce only for drag releases
  for (const [n, resp, z] of [['ui', .28, 1], ['sheet', .38, 1], ['snap', .32, .8]]) {
    const s = spring(resp, z); r.setProperty(`--sp-${n}`, s.ease); r.setProperty(`--sp-${n}-t`, s.dur + 's');
  }
}
installSprings(); reduceMotion.addEventListener('change', installSprings);
const haptic = (ms = 8) => { try { navigator.vibrate && navigator.vibrate(ms); } catch (e) { } };   // no-op on most desktops

/* ---------- state + server connection ---------- */
const st = {
  s: {}, jobs: new Map(), sel: new Set(), anchor: null, meta: {}, stats: { bytes: 0, seconds: 0, links: 0, today: 0 },
  speed: 0, hist: [], paused: true, warn: {}, log: [], page: 'queue', search: '', online: false,
};
async function api(name, data) {
  const r = await fetch('/api/' + name, { method: 'POST', headers: { 'X-Token': TOKEN, 'Content-Type': 'application/json' }, body: JSON.stringify(data || {}) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || 'Request failed');
  return j;
}
const act = async (name, data) => { try { return await api(name, data); } catch (e) { toast(e.message, 'error'); } };

/* settings: controls register a setter; commit() updates UI, then the server */
const ctrls = {}, hooks = {}, pending = {}, lastLocal = {};
const reg = (k, fn) => (ctrls[k] ||= []).push(fn);
let sendT = 0;
function commit(k, v, delay = 0) {
  st.s[k] = v; lastLocal[k] = Date.now(); pending[k] = v;
  (ctrls[k] || []).forEach(f => f(v, false)); hooks[k] && hooks[k](v);
  clearTimeout(sendT);
  sendT = setTimeout(() => { const d = { ...pending }; for (const x in pending) delete pending[x]; api('set', d).catch(() => { }); }, delay);
}
function applySettings(s) {
  for (const k in s) {
    if (k in pending || Date.now() - (lastLocal[k] || 0) < 900) continue;   // don't fight the user's own edits
    if (st.s[k] === s[k]) continue;
    st.s[k] = s[k]; (ctrls[k] || []).forEach(f => f(s[k], true)); hooks[k] && hooks[k](s[k]);
  }
}
hooks.theme = hooks.text_size = () => applyAppearance();
function applyAppearance() {
  const t = st.s.theme || 'System', dark = t === 'Dark' || (t === 'System' && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  document.documentElement.style.setProperty('--ts', { Small: .9, Default: 1, Large: 1.15, 'Extra Large': 1.3 }[st.s.text_size] || 1);
  segs.forEach(f => f()); drawGraph(true);
}
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyAppearance);
document.documentElement.dataset.theme = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';

/* ---------- formatting ---------- */
function size(n) {
  n = +n || 0; const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0;
  while (n >= 1024 && i < 4) { n /= 1024; i++; }
  return (i === 0 ? n.toFixed(0) : n.toFixed(n >= 100 ? 0 : n >= 10 ? 1 : 2)) + ' ' + u[i];
}
const speed = n => size(n) + '/s';
function niceName(url) {
  try {
    const seg = new URL(url).pathname.split('/').filter(Boolean);
    let n = seg[seg.length - 1] === 'file' ? seg[seg.length - 2] : seg[seg.length - 1];
    return decodeURIComponent(n || url);
  } catch (e) { return url; }
}
const TAG_COLORS = ['--blue', '--red', '--green', '--orange', '--purple', '--teal'];
function tagColor(tag) { let x = 0; for (const c of tag) x = (x * 31 + c.charCodeAt(0)) >>> 0; return `var(${TAG_COLORS[x % TAG_COLORS.length]})`; }

/* ---------- layer: popovers, sheets, toasts, tooltips ---------- */
const layer = h('div', { id: 'layer' }), toasts = h('div', { id: 'toasts' });
let curPop = null;
function closePop() {
  if (!curPop) return; const p = curPop; curPop = null;
  p.classList.remove('in'); setTimeout(() => p.remove(), 190);
  document.removeEventListener('pointerdown', popDown, true); document.removeEventListener('keydown', popKey, true);
}
const popDown = e => { if (curPop && !curPop.contains(e.target)) closePop(); };
function popKey(e) {
  if (e.key === 'Escape') { e.stopPropagation(); closePop(); }
  else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault(); const it = [...curPop.querySelectorAll('.pop-item:not([disabled])')]; if (!it.length) return;
    const i = it.indexOf(document.activeElement); it[(i + (e.key === 'ArrowDown' ? 1 : -1) + it.length) % it.length].focus();
  }
}
/* items: [{label, run, checked, red, disabled}] or '-' separators; anchored to an element or a {x,y} point */
function popMenu(anchor, items, point) {
  closePop(); hideTip();
  const pop = h('div', { class: 'pop', role: 'menu' }, items.map(it => it === '-' ? h('div', { class: 'pop-sep' }) :
    h('button', { class: 'pop-item' + (it.red ? ' red' : ''), type: 'button', role: 'menuitem', disabled: !!it.disabled, onclick: () => { closePop(); it.run && it.run(); } },
      h('span', {}, it.label), it.checked && icon('check'))));
  document.body.append(pop);
  const r = point ? { left: point.x, right: point.x, top: point.y, bottom: point.y } : anchor.getBoundingClientRect();
  const pw = pop.offsetWidth, ph = pop.offsetHeight, vw = innerWidth, vh = innerHeight;
  let x = r.left + pw > vw - 8 ? r.right - pw : r.left; x = clamp(x, 8, vw - pw - 8);
  let y = r.bottom + 6, flip = false; if (y + ph > vh - 8) { y = Math.max(8, r.top - ph - 6); flip = true; }
  pop.style.left = x + 'px'; pop.style.top = y + 'px';
  pop.style.setProperty('--ox', clamp((r.left + r.right) / 2 - x, 0, pw) + 'px'); pop.style.setProperty('--oy', flip ? '100%' : '0%');
  curPop = pop; requestAnimationFrame(() => pop.classList.add('in'));
  document.addEventListener('pointerdown', popDown, true); document.addEventListener('keydown', popKey, true);
  addEventListener('blur', closePop, { once: true });
}

/* modal sheet (centred on desktop, bottom sheet on narrow screens; drag down to dismiss) */
function sheet({ title, msg, input, actions }) {
  return new Promise(resolve => {
    closePop(); hideTip();
    const scrim = h('div', { class: 'scrim' }), inp = input ? h('input', { class: 'field', value: input.value || '', placeholder: input.placeholder || '', autocomplete: 'off' }) : null;
    const sh = h('div', { class: 'sheet', role: 'dialog', 'aria-modal': 'true' }, h('div', { class: 'grab' }), h('h3', {}, title), msg && h('p', {}, msg), inp,
      h('div', { class: 'acts' }, actions.map(a => h('button', { class: 'btn' + (a.primary ? ' filled' : '') + (a.red ? ' red' : ''), type: 'button',
        onclick: () => close(a.value === undefined ? (inp ? inp.value : true) : a.value) }, a.label))));
    const close = v => {
      document.removeEventListener('keydown', onKey, true); sh.classList.remove('in', 'drag'); sh.style.transform = ''; scrim.classList.remove('in');
      setTimeout(() => { sh.remove(); scrim.remove(); }, 480); resolve(v);
    };
    const def = actions.find(a => a.primary);
    const onKey = e => { if (e.key === 'Escape') { e.stopPropagation(); close(actions.find(a => a.cancel) ? false : null); } else if (e.key === 'Enter' && def) { e.preventDefault(); close(def.value === undefined ? (inp ? inp.value : true) : def.value); } };
    document.addEventListener('keydown', onKey, true);
    scrim.addEventListener('pointerdown', () => close(actions.find(a => a.cancel) ? false : null));
    // drag-to-dismiss: 1:1 tracking, rubber-banding upward, release velocity projected forward
    let y0 = 0, last = [], dragging = false;
    sh.addEventListener('pointerdown', e => {
      if (!matchMedia('(max-width:640px)').matches || e.target.closest('input,button')) return;
      dragging = true; y0 = e.clientY; last = [[e.clientY, performance.now()]]; sh.setPointerCapture(e.pointerId); sh.classList.add('drag');
    });
    sh.addEventListener('pointermove', e => {
      if (!dragging) return; const dy = e.clientY - y0; last.push([e.clientY, performance.now()]); if (last.length > 5) last.shift();
      sh.style.transform = `translateY(${dy < 0 ? -(Math.abs(dy) * 55) / (sh.offsetHeight + 55 * Math.abs(dy) / 100) : dy}px)`;
    });
    const end = e => {
      if (!dragging) return; dragging = false; const dy = e.clientY - y0;
      const [a, b] = [last[0], last[last.length - 1]], vel = b[1] > a[1] ? (b[0] - a[0]) / (b[1] - a[1]) * 1000 : 0;   // px/s
      const projected = dy + (vel / 1000) * .998 / (1 - .998);
      if (projected > sh.offsetHeight * .45) close(actions.find(a => a.cancel) ? false : null);
      else { sh.style.transition = 'transform var(--sp-snap-t) var(--sp-snap)'; sh.classList.remove('drag'); sh.style.transform = ''; setTimeout(() => sh.style.transition = '', 500); }
    };
    sh.addEventListener('pointerup', end); sh.addEventListener('pointercancel', end);
    document.body.append(scrim, sh);
    requestAnimationFrame(() => requestAnimationFrame(() => { scrim.classList.add('in'); sh.classList.add('in'); (inp || sh.querySelector('.btn.filled') || sh.querySelector('.btn')).focus({ preventScroll: true }); }));
  });
}
const confirmSheet = (title, msg, ok = 'OK', red = false) => sheet({ title, msg, actions: [{ label: ok, primary: !red, red, value: true }, { label: 'Cancel', value: false, cancel: true }] });
const promptSheet = (title, placeholder) => sheet({ title, input: { placeholder }, actions: [{ label: 'Add', primary: true }, { label: 'Cancel', value: null }] });
const alertSheet = (title, msg, buttons) => sheet({ title, msg, actions: buttons || [{ label: 'OK', primary: true, value: true }] });

function toast(msg, kind = '') {
  while (toasts.children.length > 2) toasts.firstChild.remove();
  const t = h('div', { class: 'toast ' + kind, role: 'status' }, icon(kind === 'success' ? 'checkc' : kind === 'error' ? 'xc' : 'info'), h('span', {}, msg));
  toasts.append(t); requestAnimationFrame(() => requestAnimationFrame(() => t.classList.add('in')));
  if (kind === 'success') haptic(14);
  setTimeout(() => { t.classList.remove('in'); setTimeout(() => t.remove(), 520); }, 2800 + msg.length * 25);   // leaves the way it came
}

/* tooltips: 500 ms intent delay, then neighbours appear instantly */
let tipEl = null, tipTarget = null, tipTimer = 0, lastTipShown = 0; const mouse = { x: 0, y: 0 };
function hideTip() { clearTimeout(tipTimer); if (tipEl) { const t = tipEl; tipEl = null; lastTipShown = Date.now(); t.classList.remove('in'); setTimeout(() => t.remove(), 150); } tipTarget = null; }
function showTip(target, instant) {
  if (!target.isConnected) return;
  const t = h('div', { class: 'tip' + (instant ? ' instant' : ''), role: 'tooltip' }, target.dataset.tip); document.body.append(t);
  const w = t.offsetWidth, hh = t.offsetHeight; let x = mouse.x + 14, y = mouse.y + 20;
  if (x + w > innerWidth - 8) x = innerWidth - w - 8; if (y + hh > innerHeight - 8) y = mouse.y - hh - 12;
  t.style.left = Math.max(8, x) + 'px'; t.style.top = Math.max(8, y) + 'px'; tipEl = t; requestAnimationFrame(() => t.classList.add('in'));
}
document.addEventListener('pointermove', e => { mouse.x = e.clientX; mouse.y = e.clientY; }, { passive: true });
document.addEventListener('pointerover', e => {
  if (e.pointerType !== 'mouse') return;
  const t = e.target.closest && e.target.closest('[data-tip]'); if (t === tipTarget) return;
  const was = !!tipEl; hideTip(); tipTarget = t; if (!t) return;
  const instant = was || Date.now() - lastTipShown < 400; tipTimer = setTimeout(() => showTip(t, instant), instant ? 0 : 500);
});
document.addEventListener('pointerdown', hideTip, true); document.addEventListener('scroll', hideTip, true); addEventListener('blur', hideTip);

/* ---------- reusable controls (each registers a setter so server/profile changes flow back in) ---------- */
const segs = [];
function Seg(options, onPick, get) {
  const thumb = h('i', { class: 'thumb' }), btns = options.map(o => h('button', { type: 'button', 'aria-pressed': 'false', onclick: () => { haptic(5); onPick(o); } }, o));
  const el = h('div', { class: 'seg', role: 'group' }, thumb, btns);
  const sync = () => {
    const cur = get(), b = btns.find(x => x.textContent === cur);
    btns.forEach(x => x.setAttribute('aria-pressed', x === b)); if (!b || !b.offsetWidth) return;
    thumb.style.width = b.offsetWidth + 'px'; thumb.style.transform = `translateX(${b.offsetLeft}px)`;
  };
  segs.push(sync); new ResizeObserver(sync).observe(el); return { el, sync };
}
function Switch(k) {
  const b = h('button', { class: 'switch', type: 'button', role: 'switch', 'aria-checked': 'false', onclick: () => { haptic(6); commit(k, !st.s[k]); } });
  reg(k, v => b.setAttribute('aria-checked', !!v)); return b;
}
function Stepper(k, { min, max, step = 1, unit = '' }) {
  const dec = (String(step).split('.')[1] || '').length, fmt = v => (+v).toFixed(dec);
  const inp = h('input', { class: 'field', type: 'text', inputMode: 'decimal', 'aria-label': k });
  const set = v => { v = clamp(Math.round(v / step) * step, min, max); commit(k, +v.toFixed(dec), 250); };
  inp.addEventListener('change', () => { const n = parseFloat(inp.value.replace(',', '.')); set(isNaN(n) ? st.s[k] : n); });
  inp.addEventListener('keydown', e => { if (e.key === 'ArrowUp') { e.preventDefault(); set(+st.s[k] + step); } if (e.key === 'ArrowDown') { e.preventDefault(); set(+st.s[k] - step); } });
  reg(k, (v, srv) => { if (!(srv && document.activeElement === inp)) inp.value = fmt(v); });
  const btn = (name, d) => h('button', { type: 'button', 'aria-label': d > 0 ? 'Increase' : 'Decrease', onclick: () => { haptic(4); set(+st.s[k] + d * step); } }, icon(name));
  return [h('div', { class: 'stepper' }, btn('minus', -1), inp, btn('plus', 1)), unit && h('span', { class: 'unit' }, unit)];
}
function Slider(k, { min, max, unit = '' }) {
  const out = h('output', {}), inp = h('input', { type: 'range', min, max, step: 1, 'aria-label': k });
  const paint = v => { inp.value = v; inp.style.setProperty('--p', ((v - min) / (max - min) * 100) + '%'); out.textContent = v + unit; };
  inp.addEventListener('input', () => { paint(+inp.value); commit(k, +inp.value, 200); });
  reg(k, v => paint(+v)); return h('div', { class: 'slider-wrap' }, inp, out);
}
function MenuBtn(k, options, fmt = v => v) {
  const lab = h('span'), b = h('button', { class: 'menu-btn', type: 'button', 'aria-haspopup': 'menu', onclick: () => {
    const list = typeof options === 'function' ? options() : options;
    popMenu(b, list.map(o => ({ label: fmt(o), checked: o === st.s[k], run: () => { haptic(5); commit(k, o); } })));
  } }, lab, icon('updown'));
  reg(k, v => lab.textContent = fmt(v)); return b;
}
function TextField(k, { secret, ph = '', w } = {}) {
  const inp = h('input', { class: 'field', type: secret ? 'password' : 'text', placeholder: ph, spellcheck: false, autocomplete: 'off', style: w ? `width:${w}` : '' });
  inp.addEventListener('input', () => commit(k, inp.value, 350));
  reg(k, (v, srv) => { if (!(srv && document.activeElement === inp) && inp.value !== v) inp.value = v; }); return inp;
}
function PathField(k, kind) {
  return [TextField(k, { ph: 'Not set' }), h('button', { class: 'btn', type: 'button', onclick: async () => {
    const r = await act('pick', { kind, current: st.s[k] }); if (r && r.error) toast(r.error, 'error'); else if (r && r.path) commit(k, r.path);
  } }, 'Choose…')];
}
function GridPicker(k, names, colors) {
  let brush = names.length > 1 ? 1 : 0, painting = false; const cells = [];
  const root = h('div', { class: 'gp' }, h('i'), Array.from({ length: 24 }, (_, i) => h('div', { class: 'h' }, i % 3 === 0 ? String(i).padStart(2, '0') : '')));
  DAYS.forEach((d, r) => { root.append(h('div', { class: 'd' }, d)); for (let c = 0; c < 24; c++) { const cell = h('div', { class: 'c', 'data-i': r * 24 + c }); cells.push(cell); root.append(cell); } });
  const data = () => { const s = String(st.s[k] || ''); return s.length === 168 ? s : '0'.repeat(168); };
  const paint = e => {
    const el = document.elementFromPoint(e.clientX, e.clientY), i = el && el.dataset && el.dataset.i; if (i == null) return;
    const d = data().split(''); if (d[i] !== String(brush)) { d[i] = String(brush); commit(k, d.join(''), 300); }
  };
  root.addEventListener('pointerdown', e => { painting = true; root.setPointerCapture(e.pointerId); paint(e); });
  root.addEventListener('pointermove', e => painting && paint(e));
  root.addEventListener('pointerup', () => painting = false); root.addEventListener('pointercancel', () => painting = false);
  reg(k, v => cells.forEach((c, i) => c.style.setProperty('--lv', colors[+String(v)[i] || 0])));
  const seg = Seg(names, o => { brush = names.indexOf(o); seg.sync(); }, () => names[brush]);
  return h('div', { class: 'row col' }, h('div', { class: 'ctl', style: 'margin:0;justify-content:space-between;flex-wrap:wrap' }, seg.el,
    h('button', { class: 'btn', type: 'button', onclick: () => commit(k, '0'.repeat(168), 0) }, 'Clear All')), h('div', { class: 'gp-scroll' }, root));
}

/* saving a file: native "Save as" where supported, otherwise a normal download */
async function saveFile({ name, mime, data }) {
  try {
    if (window.showSaveFilePicker) {
      const ext = name.slice(name.lastIndexOf('.')), fh = await showSaveFilePicker({ suggestedName: name, types: [{ description: name, accept: { [mime]: [ext] } }] });
      const w = await fh.createWritable(); await w.write(data); await w.close(); toast('Saved ' + fh.name, 'success'); return;
    }
  } catch (e) { if (e.name === 'AbortError') return; }
  const a = h('a', { href: URL.createObjectURL(new Blob([data], { type: mime })), download: name }); document.body.append(a); a.click(); a.remove();
  toast('Saved ' + name, 'success');
}
const readFiles = async files => { let out = ''; for (const f of files) if (f.size < 20e6) out += '\n' + await f.text(); return out; };

/* ---------- descriptions shown on hover (the one place they live) ---------- */
const TIP = {
  output_dir: "Folder where finished downloads are saved. Downloads are first staged in a hidden .mdrs_stage folder inside it.",
  tag: "Label applied to links you add next. Pick No Tag for none, or create a new one. Tags only colour and filter rows.",
  prio: "Queue priority for links you add next. High links start before Medium, Medium before Low.",
  search: "Filter the queue instantly by link, tag, status or note.",
  links: "Paste MediaFire links here, one per line (commas, semicolons or spaces also work). You can drop text files onto this box.",
  drop: "Drop .txt / .md files here (or click to choose) to add every MediaFire link inside them straight to the queue.",
  auto_cleanup: "Remove a link from the list as soon as it finishes successfully, keeping the queue tidy.",
  low_resource: "Stop drawing the speed graph to save CPU on very large queues. Speed is still measured and counted.",
  max_threads: "Simultaneous connections mdrs uses inside one job (mdrs -m). Higher is faster but more likely to get rate-limited.",
  max_jobs: "How many mdrs processes (links) run at the same time.",
  host_limit: "Hard cap on simultaneous jobs hitting mediafire.com. The effective limit is the lower of this and Parallel Jobs. Keep low to avoid IP bans.",
  timeout: "If a running link makes no progress for this many seconds it is stopped and treated as failed (then retried if auto-retry is on). 0 disables.",
  tries: "How many times mdrs itself retries a single file before giving up (mdrs -t).",
  reverse: "Download the largest files first instead of the default order (mdrs -r).",
  auto_retry: "Automatically re-queue a failed link right away instead of waiting for you to press Retry.",
  auto_retries: "Maximum number of automatic re-queues per link before it is marked Failed.",
  retry_codes: "Auto-retry only when the server answered with one of these HTTP codes. 404 is normally off: a deleted file will never come back. Network drops and timeouts always retry.",
  mirror: "Keep MediaFire's folder structure. When off, every file goes directly into the output folder (name clashes get ' (1)', ' (2)').",
  md5: "After each download, check for empty or broken (.zip) files and record an MD5 hash of every file for later Force Check.",
  md5_policy: "What to do when a downloaded file fails the integrity check: delete it, or keep it so you can inspect it.",
  force_check: "Recalculate the MD5 of every recorded file and report any that changed or went missing.",
  extract: "Automatically unpack .zip, .rar and .7z files when their download finishes. .rar/.7z need 7-Zip installed.",
  extract_delete: "Delete the archive after it has been extracted successfully.",
  post_action: "What happens once the whole queue has finished: nothing, close the app, or put the PC to sleep.",
  min_free_gb: "The queue is held and a warning shown when free space on the output drive drops below this.",
  mdrs_path: "Full path to mdrs.exe. Leave blank to find it automatically (PATH or ~/.cargo/bin).",
  check_updates_now: "Look up the newest mediafire_rs release now.",
  check_updates: "On startup, compare your installed mdrs version with the latest release.",
  theme: "Match Windows, or force a light or dark appearance.",
  text_size: "Scales all text and spacing, like Dynamic Type.",
  close_action: "What the window's close button does: keep running in the tray, quit immediately, or quit once the queue finishes.",
  wake_lock: "Prevent Windows from sleeping while downloads are running.",
  daily_cap_gb: "Stop starting and continuing downloads after this much data today. Resets at midnight. 0 = no cap.",
  sound: "Sound played when the queue finishes or a link fails.",
  custom_wav: "Path to your own .wav file, used when Custom WAV file… is selected.",
  volume: "Loudness of completion sounds. Applies to built-in melodies and .wav sounds; System sounds follow the Windows volume.",
  test_sound: "Play the selected sound at the current volume (ignores Quiet Mode).",
  quiet_on: "Mute sounds and notifications during the hours painted as Quiet in the grid below.",
  quiet_grid: "Click or drag to paint hours of the week. Choose a brush above: Quiet hours mute alerts.",
  speed_grid: "Click or drag to paint hours of the week with a speed limit. Pick a brush: Unlimited, Limit A, Limit B or Paused.",
  limit_a: "Download speed cap (MB/s) used by hours painted Limit A. Applied by briefly pausing mdrs, so it is approximate.",
  limit_b: "Download speed cap (MB/s) used by hours painted Limit B.",
  export_profile: "Save all settings to a JSON file you can back up or move to another PC.",
  import_profile: "Load settings from a previously exported JSON profile.",
  reset_defaults: "Restore every setting to its original value. Queue, statistics and history are kept.",
  flare_url: "Address of your FlareSolverr server. Only the connection test uses it for now; mdrs has no FlareSolverr option yet.",
  test_flare: "Check that the FlareSolverr server answers at this address.",
  proxy_file: "Text file with one proxy per line (e.g. http://user:pass@host:port). Passed to mdrs with -p.",
  proxy_download: "Also send the file downloads (not just page requests) through the proxies (--proxy-download).",
  proxy_cycle_n: "Move to the next proxy in the list after this many completed links. 0 = never rotate.",
  rotate_proxy: "Switch to the next proxy for links that start from now on.",
  proxy_type: "Protocol of the single proxy configured below.",
  proxy_host: "Hostname or IP of the single proxy. Added to the proxy list given to mdrs.",
  proxy_port: "Port of the single proxy.",
  proxy_user: "Username for the proxy, if it needs authentication.",
  proxy_pass: "Password for the proxy, if it needs authentication.",
  user_agent: "Custom User-Agent string. Saved in your profile; mdrs has no option for it yet.",
  folder_password: "Password for protected MediaFire folders. Saved in your profile; mdrs has no option for it yet.",
  start: "Start downloading the queue. While paused, nothing new starts and running downloads are suspended.",
  pause_all: "Suspend every running download and stop starting new ones.",
  add: "Add all MediaFire links in the box to the queue.",
  import_files: "Choose text files and add the MediaFire links they contain straight to the queue.",
  pause: "Pause the selected links. Double-click a row to toggle.",
  resume: "Resume the selected links.",
  remove: "Remove the selected links (running ones are stopped and their partial files deleted).",
  retry_failed: "Put every failed link back in the queue.",
  clear_finished: "Remove all completed links from the list.",
  export_csv: "Save lifetime metrics and per-day totals as a CSV file.",
  export_html: "Save lifetime metrics and per-day totals as a web page.",
  reset_stats: "Zero the lifetime counters. This cannot be undone.",
  export_log: "Save the console log to a text file.",
  clear_log: "Empty the log console.",
  autoscroll: "Keep the newest log line in view.",
  open_out: "Open the output folder in Explorer.",
  choose_out: "Choose the output folder.",
};

/* ---------- page declarations: Settings and Network are generated from this schema ---------- */
const ACT = {
  force_check: () => act('force_check'),
  rotate_proxy: () => act('rotate_proxy'),
  check_updates_now: () => act('check_updates'),
  test_sound: () => act('test_sound'),
  export_profile: async () => { const r = await act('export_profile'); r && saveFile(r); },
  import_profile: () => refs.profileInput.click(),
  reset_defaults: async () => { if (await confirmSheet('Reset all settings?', 'Every setting returns to its default. Your queue, statistics and download history are not affected.', 'Reset', true)) act('reset_defaults'); },
  test_flare: async (out) => {
    out.className = 'result ctl'; out.textContent = 'Testing…'; const r = await act('test_flare');
    if (r) { out.textContent = (r.ok ? '✓ ' : '✕ ') + r.msg; out.className = 'result ctl ' + (r.ok ? 'ok' : 'bad'); } else out.textContent = '';
  },
};
const PROXY_TYPES = ['http', 'https', 'socks5', 'socks5h'];
const SCHEMA = {
  network: [
    { title: 'FlareSolverr', foot: 'mdrs has no FlareSolverr option yet, so the address is saved and connection-tested only.', rows: [
      { t: 'text', k: 'flare_url', label: 'Server URL', ph: 'http://localhost:8191/v1' },
      { t: 'action', act: 'test_flare', label: 'Test Connection', result: true }] },
    { title: 'Proxy List', foot: 'One proxy per line, for example http://user:pass@host:port. Passed to mdrs with -p; rotation applies to links that start afterwards.', rows: [
      { t: 'path', k: 'proxy_file', pick: 'file', label: 'List File' },
      { t: 'switch', k: 'proxy_download', label: 'Proxy File Downloads' },
      { t: 'stepper', k: 'proxy_cycle_n', label: 'Rotate After', min: 0, max: 1000, unit: 'links (0 = never)' },
      { t: 'action', act: 'rotate_proxy', label: 'Rotate Now' }] },
    { title: 'Single Proxy', foot: 'HTTP or SOCKS5, with optional sign-in. Separate from FlareSolverr.', rows: [
      { t: 'menu', k: 'proxy_type', label: 'Type', opts: PROXY_TYPES },
      { t: 'text', k: 'proxy_host', label: 'Host', ph: 'proxy.example.com' },
      { t: 'text', k: 'proxy_port', label: 'Port', ph: '8080' },
      { t: 'text', k: 'proxy_user', label: 'Username' },
      { t: 'text', k: 'proxy_pass', label: 'Password', secret: true }] },
    { title: 'Identity & Protected Folders', foot: 'mdrs currently has no options for these two, so they are stored in your profile for when it does.', rows: [
      { t: 'text', k: 'user_agent', label: 'User-Agent', ph: 'Default' },
      { t: 'text', k: 'folder_password', label: 'Folder Password', secret: true }] },
  ],
  settings: [
    { title: 'Downloads', rows: [
      { t: 'slider', k: 'max_threads', label: 'Connections per Job', min: 1, max: 50 },
      { t: 'stepper', k: 'max_jobs', label: 'Parallel Jobs', min: 1, max: 10 },
      { t: 'stepper', k: 'host_limit', label: 'Jobs per Domain', min: 1, max: 10 },
      { t: 'slider', k: 'timeout', label: 'Stall Timeout', min: 0, max: 600, unit: ' s' },
      { t: 'stepper', k: 'tries', label: 'Tries per File', min: 1, max: 20 },
      { t: 'switch', k: 'reverse', label: 'Largest Files First' }] },
    { title: 'Error Handling', foot: 'Unchecked codes are passed over without retrying. Network drops and timeouts always retry.', rows: [
      { t: 'switch', k: 'auto_retry', label: 'Instant Auto-Retry' },
      { t: 'stepper', k: 'auto_retries', label: 'Max Auto-Retries', min: 1, max: 20 },
      { t: 'chips', tipKey: 'retry_codes', label: 'Retry on HTTP', keys: ['retry_502', 'retry_503', 'retry_504', 'retry_429', 'retry_404'] }] },
    { title: 'Files & Integrity', foot: 'The check looks for empty files and broken .zip archives, and records hashes so Force Check can detect later corruption.', rows: [
      { t: 'switch', k: 'mirror', label: 'Mirror Folder Structure' },
      { t: 'switch', k: 'md5', label: 'MD5 Integrity Check' },
      { t: 'menu', k: 'md5_policy', label: 'On Integrity Failure', opts: ['Keep for Manual Inspection', 'Delete Corrupted Downloads'] },
      { t: 'action', act: 'force_check', label: 'Force Check History…' }] },
    { title: 'After Download', foot: '.rar and .7z extraction needs 7-Zip; .zip works without it.', rows: [
      { t: 'switch', k: 'extract', label: 'Auto-Extract Archives' },
      { t: 'switch', k: 'extract_delete', label: 'Delete Archive After Extracting' },
      { t: 'menu', k: 'post_action', label: 'When the Queue Finishes', opts: ['Do nothing', 'Close application', 'Sleep PC'] }] },
    { title: 'Disk Space', foot: 'Total download size is not known in advance, so the queue holds when free space falls below this level.', rows: [
      { t: 'stepper', k: 'min_free_gb', label: 'Hold Below', min: 0, max: 10000, step: .5, unit: 'GB free' }] },
    { title: 'Engine', rows: [
      { t: 'path', k: 'mdrs_path', pick: 'exe', label: 'mdrs Executable' },
      { t: 'info', label: 'Version', ref: 'ver' },
      { t: 'action', act: 'check_updates_now', label: 'Check for Updates' },
      { t: 'switch', k: 'check_updates', label: 'Check on Startup' }] },
    { title: 'Window & System', rows: [
      { t: 'menu', k: 'theme', label: 'Appearance', opts: ['System', 'Light', 'Dark'] },
      { t: 'menu', k: 'text_size', label: 'Text Size', opts: ['Small', 'Default', 'Large', 'Extra Large'] },
      { t: 'menu', k: 'close_action', label: 'Close Button', opts: ['Minimize to system tray', 'Terminate immediately', 'Close after queue finishes'] },
      { t: 'switch', k: 'wake_lock', label: 'Keep PC Awake While Downloading' },
      { t: 'stepper', k: 'daily_cap_gb', label: 'Daily Data Cap', min: 0, max: 100000, step: 1, unit: 'GB (0 = none)' }] },
    { title: 'Alerts', foot: 'Volume applies to the built-in melodies and .wav sounds; System sounds use the Windows volume.', rows: [
      { t: 'menu', k: 'sound', label: 'Completion Sound', opts: () => st.meta.sounds || [] },
      { t: 'path', k: 'custom_wav', pick: 'wav', label: 'Custom WAV File' },
      { t: 'slider', k: 'volume', label: 'Volume', min: 0, max: 100, unit: '%' },
      { t: 'action', act: 'test_sound', label: 'Play Test Sound' },
      { t: 'switch', k: 'quiet_on', label: 'Quiet Mode' },
      { t: 'grid', k: 'quiet_grid', names: ['Sound On', 'Quiet'], colors: ['var(--fill3)', 'var(--blue)'] }] },
    { title: 'Speed Schedule', foot: 'Throttling works by briefly pausing mdrs, so the actual speed varies around the limit.', rows: [
      { t: 'stepper', k: 'limit_a', label: 'Limit A', min: .1, max: 1000, step: .5, unit: 'MB/s' },
      { t: 'stepper', k: 'limit_b', label: 'Limit B', min: .1, max: 1000, step: .5, unit: 'MB/s' },
      { t: 'grid', k: 'speed_grid', names: ['Unlimited', 'Limit A', 'Limit B', 'Paused'],
        colors: ['color-mix(in srgb,var(--green) 45%,transparent)', 'color-mix(in srgb,var(--orange) 45%,transparent)', 'var(--orange)', 'var(--red)'] }] },
    { title: 'Profiles', rows: [
      { t: 'action', act: 'export_profile', label: 'Export Profile…' },
      { t: 'action', act: 'import_profile', label: 'Import Profile…' }] },
    { title: '', rows: [{ t: 'action', act: 'reset_defaults', label: 'Reset to Defaults…', red: true }] },
  ],
};
const refs = {};

function SchemaRow(r) {
  const key = r.k || r.act, tip = TIP[r.tipKey || key];
  if (r.t === 'grid') { const g = GridPicker(r.k, r.names, r.colors); g.dataset.tip = TIP[r.k]; return g; }
  if (r.t === 'action') {
    const out = r.result ? h('span', { class: 'result ctl' }) : null;
    return h('button', { class: 'row link' + (r.red ? ' red' : ''), type: 'button', tip, onclick: () => { haptic(6); ACT[r.act](out); } }, h('span', { class: 'lbl' }, r.label), out);
  }
  let ctl;
  switch (r.t) {
    case 'switch': ctl = Switch(r.k); break;
    case 'stepper': ctl = Stepper(r.k, r); break;
    case 'slider': ctl = Slider(r.k, r); break;
    case 'menu': ctl = MenuBtn(r.k, r.opts); break;
    case 'text': ctl = TextField(r.k, r); break;
    case 'path': ctl = PathField(r.k, r.pick); break;
    case 'chips': ctl = h('div', { class: 'chips' }, r.keys.map(k => { const c = h('button', { class: 'chip', type: 'button', 'aria-pressed': 'false', onclick: () => { haptic(5); commit(k, !st.s[k]); } }, k.slice(-3)); reg(k, v => c.setAttribute('aria-pressed', !!v)); return c; })); break;
    case 'info': ctl = refs[r.ref] = h('span', { class: 'note' }, '—'); break;
  }
  return h('div', { class: 'row', tip }, h('div', { class: 'lbl' }, r.label), h('div', { class: 'ctl' }, ctl));
}
function renderSchema(wrap, groups) {
  for (const g of groups) {
    wrap.append(g.title ? h('div', { class: 'group-h' }, g.title) : h('div', { style: 'height:28px' }), h('div', { class: 'group' }, g.rows.map(SchemaRow)));
    if (g.foot) wrap.append(h('div', { class: 'group-f' }, g.foot));
  }
}

/* ---------- page shell ---------- */
const PAGES = [['queue', 'Queue', 'down'], ['stats', 'Statistics', 'chart'], ['network', 'Network', 'globe'], ['settings', 'Settings', 'sliders'], ['log', 'Log Console', 'term']];
const pages = {};
function makePage(id, title, sub, actions = []) {
  const nb = h('div', { class: 'navbar' }, h('div', { class: 'nb-title' }, title), h('div', { class: 'nb-actions' }, actions));
  const subEl = h('p', { class: 'sub' }, sub || ''), wrap = h('div', { class: 'wrap' }, h('h1', { class: 'large-title' }, title), subEl);
  const page = h('section', { class: 'page', id: 'p-' + id }, nb, wrap);
  page.addEventListener('scroll', () => nb.classList.toggle('scrolled', page.scrollTop > 24), { passive: true });
  return (pages[id] = { page, wrap, sub: subEl, nb });
}
const btn = (label, ic, tipKey, onclick, cls = '') => h('button', { class: 'btn ' + cls, type: 'button', tip: TIP[tipKey], onclick }, ic && icon(ic), label);

/* ---------- Statistics ---------- */
function buildStats() {
  const P = makePage('stats', 'Statistics', 'Live speed and lifetime totals.', [
    btn('CSV', 'export', 'export_csv', async () => { const r = await act('export_stats', { kind: 'csv' }); r && saveFile(r); }),
    btn('HTML', 'export', 'export_html', async () => { const r = await act('export_stats', { kind: 'html' }); r && saveFile(r); })]);
  refs.speedNum = h('span'); refs.speedUnit = h('small'); refs.graph = h('canvas', { 'aria-label': 'Download speed over the last two minutes', role: 'img' });
  P.wrap.append(h('div', { class: 'card' }, h('div', { class: 'big' }, refs.speedNum, refs.speedUnit), h('div', { class: 'graph' }, refs.graph)),
    h('div', { class: 'group', style: 'margin-top:12px' }, h('div', { class: 'row', tip: TIP.low_resource }, h('div', { class: 'lbl' }, 'Low Resource Mode'), h('div', { class: 'ctl' }, Switch('low_resource')))),
    h('div', { class: 'group-f' }, 'Stops drawing the graph to save CPU on very large queues. Speed is still measured.'));
  const tile = (label, key) => h('div', { class: 'tile' }, h('span', {}, label), refs[key] = h('b', {}, '—'));
  P.wrap.append(h('div', { class: 'tiles' }, tile('Total downloaded', 'tGb'), tile('Average speed', 'tAvg'), tile('Links processed', 'tLinks'), tile('Downloaded today', 'tToday')),
    h('div', { class: 'group', style: 'margin-top:26px' }, h('button', { class: 'row link red', type: 'button', tip: TIP.reset_stats, onclick: async () => {
      if (await confirmSheet('Reset lifetime statistics?', 'Total downloaded, average speed and links processed return to zero. This cannot be undone.', 'Reset', true)) act('reset_stats'); } }, h('span', { class: 'lbl' }, 'Reset Lifetime Stats…'))));
  new ResizeObserver(() => drawGraph(true)).observe(refs.graph);
  hooks.low_resource = () => drawGraph(true);
  return P.page;
}
function renderStats() {
  const [n, u] = speed(st.speed).split(' '); refs.speedNum.textContent = n; refs.speedUnit.textContent = u;
  const s = st.stats, cap = +st.s.daily_cap_gb || 0;
  refs.tGb.textContent = (s.bytes / 1024 ** 3).toFixed(3) + ' GB'; refs.tAvg.textContent = s.seconds ? speed(s.bytes / s.seconds) : '—';
  refs.tLinks.textContent = s.links; refs.tToday.textContent = size(s.today) + (cap ? ` / ${cap} GB cap` : '');
}
const cssVar = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
function drawGraph(force) {
  const cv = refs.graph; if (!cv || (st.page !== 'stats' && !force)) return;
  const dpr = devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight; if (!W || !H) return;
  if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
  const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, H);
  g.font = `11px ${cssVar('--font') || 'sans-serif'}`; g.textBaseline = 'middle';
  if (st.s.low_resource) { g.fillStyle = cssVar('--label2'); g.textAlign = 'center'; g.fillText('Low Resource Mode: graph paused', W / 2, H / 2); return; }
  const data = st.hist, L = 52, R = 10, T = 12, B = 22, w = W - L - R, hh = H - T - B;
  const steps = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000].map(x => x * 1048576), mx = Math.max(...data, 1);
  const peak = steps.find(x => x >= mx * 1.1) || mx * 1.1;
  g.textAlign = 'right'; g.fillStyle = cssVar('--label2'); g.strokeStyle = cssVar('--sep'); g.lineWidth = .5;
  for (let i = 0; i <= 4; i++) { const y = T + hh - hh * i / 4; g.beginPath(); g.moveTo(L, y); g.lineTo(W - R, y); g.stroke(); g.fillText(i ? size(peak * i / 4) : '0', L - 8, y); }
  g.textAlign = 'left'; g.fillText('2 min ago', L, H - 8); g.textAlign = 'right'; g.fillText('now', W - R, H - 8);
  const pts = data.map((v, i) => [L + w * i / (data.length - 1), T + hh - hh * Math.min(v, peak) / peak]);
  const path = () => { g.beginPath(); g.moveTo(...pts[0]); for (let i = 1; i < pts.length - 1; i++) { const mx2 = (pts[i][0] + pts[i + 1][0]) / 2, my = (pts[i][1] + pts[i + 1][1]) / 2; g.quadraticCurveTo(pts[i][0], pts[i][1], mx2, my); } g.lineTo(...pts[pts.length - 1]); };
  const blue = cssVar('--blue'), grad = g.createLinearGradient(0, T, 0, T + hh); grad.addColorStop(0, blue + '55'); grad.addColorStop(1, blue + '00');
  path(); g.lineTo(L + w, T + hh); g.lineTo(L, T + hh); g.closePath(); g.fillStyle = grad; g.fill();
  path(); g.strokeStyle = blue; g.lineWidth = 2.2; g.lineJoin = g.lineCap = 'round'; g.stroke();
  const last = pts[pts.length - 1]; g.beginPath(); g.arc(last[0], last[1], 4, 0, 7); g.fillStyle = blue; g.fill();
}

/* ---------- Log console ---------- */
let autoscroll = true, logDirty = false;
function buildLog() {
  const P = makePage('log', 'Log Console', 'Raw output from mdrs, plus what this app is doing.', [
    btn('Export', 'export', 'export_log', async () => { const r = await act('export_log'); r && saveFile(r); }),
    btn('Clear', 'trash', 'clear_log', () => act('clear_log'), 'red')]);
  refs.log = h('pre', { class: 'logbox', tabIndex: 0 });
  const sw = h('button', { class: 'switch', type: 'button', role: 'switch', 'aria-checked': 'true', onclick: () => { autoscroll = !autoscroll; sw.setAttribute('aria-checked', autoscroll); if (autoscroll) refs.log.scrollTop = refs.log.scrollHeight; } });
  P.wrap.append(h('div', { class: 'group', style: 'margin-bottom:12px' }, h('div', { class: 'row', tip: TIP.autoscroll }, h('div', { class: 'lbl' }, 'Auto-Scroll'), h('div', { class: 'ctl' }, sw))), refs.log);
  return P.page;
}
function renderLog() {
  if (st.page !== 'log') { logDirty = true; return; }
  logDirty = false; const el = refs.log, stick = autoscroll || el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  el.textContent = st.log.join('\n'); if (stick) el.scrollTop = el.scrollHeight;
}

/* ---------- Queue ---------- */
const ROW = 64, BUF = 6;
let view = [], listDirty = true; const rowEls = new Map(), fresh = new Set();
const present = j => {
  if (j.paused && (j.status === 'Running' || j.status === 'Queued')) return { label: 'Paused', icon: 'pause', c: '--orange', cls: j.status === 'Running' ? 'run pausd' : '' };
  switch (j.status) {
    case 'Running': return j.held ? { label: 'Held', icon: 'pause', c: '--orange', cls: 'run held' } : { label: 'Downloading', icon: 'arrowdown', c: '--blue', cls: 'run', spin: true };
    case 'Done': return { label: 'Done', icon: 'checkc', c: '--green', cls: '' };
    case 'Failed': return { label: 'Failed', icon: 'xc', c: '--red', cls: '' };
    case 'Verifying': return { label: 'Verifying', icon: 'shield', c: '--purple', cls: '' };
    case 'Extracting': return { label: 'Extracting', icon: 'box', c: '--teal', cls: '' };
    default: return { label: 'Queued', icon: 'clock', c: '--gray', cls: '' };
  }
};
const toJob = r => ({ id: r[0], url: r[1], tag: r[2], prio: r[3], status: r[4], note: r[5], tries: r[6], size: r[7], speed: r[8], paused: !!r[9], held: !!r[10], name: niceName(r[1]) });
const PR = { High: 0, Medium: 1, Low: 2 };

function makeRow(id) {
  const el = h('div', { class: 'qrow', 'data-id': id, role: 'row' });
  el.i = h('div', { class: 'qi' }, icon('clock')); el.t = h('div', { class: 'qt' }); el.s = h('div', { class: 'qs' });
  el.pill = h('span', { class: 'pill' }); el.prio = h('span', { class: 'prio' }); el.sz = h('b'); el.sp = h('i', { style: 'font-style:normal' });
  el.go = h('button', { class: 'btn icon', type: 'button', 'data-act': 'go' }, icon('pause')); el.rm = h('button', { class: 'btn icon red', type: 'button', 'data-act': 'trash', 'aria-label': 'Remove', tip: 'Remove this link' }, icon('trash'));
  el.append(h('div', { class: 'qin' }, el.i, h('div', { class: 'qm' }, el.t, el.s), h('div', { class: 'qr' }, el.pill, el.prio, h('div', { class: 'qn' }, el.sz, el.sp)),
    h('div', { class: 'qa' }, el.go, el.rm)), h('div', { class: 'qbar' }, h('i')));
  return el;
}
function fillRow(el, j, sel) {
  const sig = [j.status, j.note, j.tries, j.size, j.speed, j.paused, j.held, j.tag, j.prio, sel].join('|'); if (el.sig === sig) return; el.sig = sig;
  const p = present(j); el.i.style.setProperty('--c', `var(${p.c})`); el.i.classList.toggle('spin', !!p.spin); setIcon(el.i.firstChild, p.icon);
  el.t.textContent = j.name; el.t.dataset.tip = j.url;
  el.s.textContent = p.label + (j.note ? ' · ' + j.note : j.tries ? ` · attempt ${j.tries + 1}` : '');
  el.pill.textContent = j.tag; el.pill.hidden = !j.tag; el.pill.style.setProperty('--c', j.tag ? tagColor(j.tag) : '');
  el.prio.textContent = j.prio; el.prio.className = 'prio ' + j.prio;
  el.sz.textContent = j.size ? size(j.size) : ''; el.sp.textContent = j.status === 'Running' && j.speed && !j.paused && !j.held ? speed(j.speed) : '';
  const canGo = j.status === 'Running' || j.status === 'Queued' || j.status === 'Failed'; el.go.hidden = !canGo;
  if (canGo) { const retry = j.status === 'Failed'; el.go.dataset.act = retry ? 'retry' : 'toggle'; setIcon(el.go.firstChild, retry ? 'retry' : j.paused ? 'play' : 'pause');
    el.go.setAttribute('aria-label', retry ? 'Retry' : j.paused ? 'Resume' : 'Pause'); el.go.dataset.tip = retry ? 'Retry this link' : j.paused ? 'Resume this link' : 'Pause this link'; }
  el.classList.toggle('sel', sel); el.className = 'qrow' + (sel ? ' sel' : '') + (p.cls ? ' ' + p.cls : '') + (el.noanim ? ' noanim' : '') + (el.isNew ? ' enter' : '');
}
function rebuildView() {
  const q = st.search.trim().toLowerCase();
  view = [...st.jobs.values()].filter(j => !q || (j.url + ' ' + j.name + ' ' + j.tag + ' ' + j.status + ' ' + j.note).toLowerCase().includes(q))
    .sort((a, b) => PR[a.prio] - PR[b.prio] || a.id - b.id);
  refs.vspace.style.height = view.length * ROW + 'px'; listDirty = false;
  refs.empty.classList.toggle('on', !view.length); refs.vwrap.style.display = view.length ? '' : 'none';
  refs.empty.querySelector('b').textContent = st.jobs.size ? 'No matches' : 'Your queue is empty';
  refs.empty.querySelector('span').textContent = st.jobs.size ? 'Try a different search.' : 'Paste MediaFire links above, or drop a text file, then press Start Queue.';
}
function renderList() {
  if (st.page !== 'queue') return;
  if (listDirty) rebuildView();
  const sc = pages.queue.page, top = refs.vspace.getBoundingClientRect().top - sc.getBoundingClientRect().top;
  const first = clamp(Math.floor(-top / ROW) - BUF, 0, view.length), last = clamp(Math.ceil((sc.clientHeight - top) / ROW) + BUF, 0, view.length);
  const keep = new Set();
  for (let i = first; i < last; i++) {
    const j = view[i]; keep.add(j.id); let el = rowEls.get(j.id);
    if (!el) { el = makeRow(j.id); el.noanim = true; el.isNew = fresh.delete(j.id); rowEls.set(j.id, el); refs.vspace.append(el); el.style.transform = `translateY(${i * ROW}px)`; requestAnimationFrame(() => { el.noanim = false; el.sig = null; fillRow(el, j, st.sel.has(j.id)); }); }
    else el.style.transform = `translateY(${i * ROW}px)`;
    el.sig !== undefined && fillRow(el, j, st.sel.has(j.id));
    if (el.sig === undefined) { el.sig = null; fillRow(el, j, st.sel.has(j.id)); }
  }
  for (const [id, el] of rowEls) if (!keep.has(id)) { el.remove(); rowEls.delete(id); }
}
function renderHead() {
  const c = { Running: 0, Queued: 0, Done: 0, Failed: 0 }; for (const j of st.jobs.values()) if (j.status in c) c[j.status]++; else c.Running++;
  pages.queue.sub.textContent = st.jobs.size ? `${st.jobs.size} link${st.jobs.size > 1 ? 's' : ''} · ${c.Running} running · ${c.Queued} queued · ${c.Done} done · ${c.Failed} failed` : 'No links yet.';
  const b = refs.start; b.className = 'btn ' + (st.paused ? 'filled' : ''); b.dataset.tip = st.paused ? TIP.start : TIP.pause_all;
  setIcon(b.firstChild, st.paused ? 'play' : 'pause'); b.lastChild.textContent = st.paused ? 'Start Queue' : 'Pause All';
  const w = st.warn, msgs = [];
  if (w.disk) msgs.push(['Low disk space: queue held until space is freed.', true]); if (w.path) msgs.push(['The output folder is unavailable.', true]);
  if (w.cap) msgs.push(['Daily data cap reached: downloads resume tomorrow or when you raise the cap.']); if (w.sched) msgs.push(['Paused by the speed schedule.']);
  const sig = JSON.stringify(msgs); if (refs.bannerSig !== sig) { refs.bannerSig = sig; refs.banners.replaceChildren(...msgs.map(([t, bad]) => h('div', { class: 'banner' + (bad ? ' bad' : ''), role: 'alert' }, icon('warn'), t))); }
  const sel = st.sel.size > 0; refs.bPause.disabled = refs.bResume.disabled = refs.bRemove.disabled = !sel;
  refs.bRetry.disabled = ![...st.jobs.values()].some(j => j.status === 'Failed'); refs.bClear.disabled = !c.Done;
  const act2 = [...st.jobs.values()].filter(j => ['Queued', 'Running', 'Verifying', 'Extracting'].includes(j.status)).length;
  refs.badge.textContent = act2 || '';
}
function refreshSel() { for (const [id, el] of rowEls) { const j = st.jobs.get(id); j && fillRow(el, j, st.sel.has(id)); } renderHead(); }
const jobOp = (op, value) => act('job', { op, ids: [...st.sel], value });
function select(id, e) {
  const ids = view.map(j => j.id);
  if (e.shiftKey && st.anchor != null && ids.includes(st.anchor)) {
    const a = ids.indexOf(st.anchor), b = ids.indexOf(id); if (!(e.ctrlKey || e.metaKey)) st.sel.clear(); ids.slice(Math.min(a, b), Math.max(a, b) + 1).forEach(i => st.sel.add(i));
  } else if (e.ctrlKey || e.metaKey) { st.sel.has(id) ? st.sel.delete(id) : st.sel.add(id); st.anchor = id; }
  else { st.sel.clear(); st.sel.add(id); st.anchor = id; }
  refreshSel();
}

function buildQueue() {
  refs.start = h('button', { class: 'btn filled', type: 'button', onclick: () => { haptic(12); act('toggle_global'); } }, icon('play'), h('span', {}, 'Start Queue'));
  const P = makePage('queue', 'Queue', '', [refs.start]); const W = P.wrap;
  refs.banners = h('div'); W.append(refs.banners);

  /* output folder */
  const out = h('input', { class: 'field', value: '', spellcheck: false, 'aria-label': 'Output folder' });
  out.addEventListener('input', () => commit('output_dir', out.value, 400)); reg('output_dir', (v, srv) => { if (!(srv && document.activeElement === out)) out.value = v; });
  W.append(h('div', { class: 'group-h' }, 'Save To'), h('div', { class: 'group' }, h('div', { class: 'row', tip: TIP.output_dir }, h('div', { class: 'lbl', style: 'flex:1' }, out),
    h('div', { class: 'ctl' }, h('button', { class: 'btn', type: 'button', tip: TIP.choose_out, onclick: async () => { const r = await act('pick', { kind: 'dir', current: st.s.output_dir }); if (r && r.error) toast(r.error, 'error'); else if (r && r.path) commit('output_dir', r.path); } }, 'Choose…'),
      h('button', { class: 'btn icon', type: 'button', 'aria-label': 'Open output folder', tip: TIP.open_out, onclick: () => act('open_out') }, icon('folder'))))));
  out.style.cssText = 'width:100%;text-align:left';

  /* add links */
  const ta = h('textarea', { class: 'field', spellcheck: false, tip: TIP.links, 'aria-label': 'MediaFire links',
    placeholder: 'Enter links here...\n  • One MediaFire link per line (recommended)\n  • Commas, semicolons or spaces between links also work\n  • Folder links and single-file links can be mixed\n  • Drag & drop .txt / .md / .csv files onto this box to load their links' });
  ta.addEventListener('input', () => commit('batch_text', ta.value, 500)); reg('batch_text', (v, srv) => { if (!(srv && document.activeElement === ta) && ta.value !== v) ta.value = v; });
  const fileIn = h('input', { type: 'file', multiple: true, hidden: true, accept: '.txt,.md,.csv,.log,.json,.html,text/*' });
  const addText = async text => { const r = await act('add', { text }); if (r) haptic(10); return r; };
  fileIn.addEventListener('change', async () => { const t = await readFiles(fileIn.files); fileIn.value = ''; t.trim() ? addText(t).then(r => r && setTimeout(() => refs.setIntake(false), 350)) : toast('Those files are empty.', 'error'); });
  const tagBtn = h('button', { class: 'menu-btn', type: 'button', tip: TIP.tag, 'aria-haspopup': 'menu', style: 'width:100%;justify-content:space-between' }, h('span'), icon('updown'));
  const tagLabel = v => v === NO_TAG || !v ? 'No Tag' : v; reg('tag', v => tagBtn.firstChild.textContent = tagLabel(v));
  tagBtn.addEventListener('click', () => {
    const tags = (st.s.tags || '').split(',').map(x => x.trim()).filter(Boolean), cur = st.s.tag;
    popMenu(tagBtn, [{ label: 'No Tag', checked: cur === NO_TAG, run: () => commit('tag', NO_TAG) }, ...tags.map(t => ({ label: t, checked: cur === t, run: () => commit('tag', t) })), '-',
      { label: 'New Tag…', run: async () => { const n = await promptSheet('New Tag', 'Tag name'); if (n && n.trim()) commit('tag', n.trim().replace(/,/g, ' ')); } }]);
  });
  const prio = Seg(['Low', 'Medium', 'High'], o => commit('prio', o), () => st.s.prio); reg('prio', prio.sync); prio.el.dataset.tip = TIP.prio;
  const drop = h('button', { class: 'drop', type: 'button', tip: TIP.drop, onclick: () => fileIn.click() }, icon('doc'), h('span', {}, 'Drop text files here'), h('span', { style: 'font-size:.75rem;opacity:.8' }, 'or click to choose'));
  const hasFiles = e => e.dataTransfer && [...e.dataTransfer.types].includes('Files');
  for (const [el, onDrop] of [[ta, async files => {
    const r = await act('extract', { text: await readFiles(files) }); if (!r) return;
    if (!r.links.length) return toast('No MediaFire links found in the dropped file(s).', 'error');
    ta.value = (ta.value.trim() ? ta.value.replace(/\s+$/, '') + '\n' : '') + r.links.join('\n'); commit('batch_text', ta.value, 200);
    toast(`Loaded ${r.links.length} link${r.links.length > 1 ? 's' : ''} into the box. Press Add to Queue.`);
  }], [drop, async files => { const t = await readFiles(files); t.trim() ? addText(t) : toast('Those files are empty.', 'error'); }]]) {
    el.addEventListener('dragover', e => { if (hasFiles(e)) { e.preventDefault(); el.classList.add('over'); } });
    el.addEventListener('dragleave', () => el.classList.remove('over'));
    el.addEventListener('drop', e => { if (!hasFiles(e)) return; e.preventDefault(); el.classList.remove('over'); onDrop(e.dataTransfer.files); });
  }
  const add = h('button', { class: 'btn filled', type: 'button', tip: TIP.add, onclick: async () => {
    if (!ta.value.trim()) return toast('Paste or type some MediaFire links first.', 'error');
    if (await addText(ta.value)) { ta.value = ''; commit('batch_text', '', 0); setTimeout(() => setIntake(false), 350); }
  } }, 'Add to Queue');
  const intake = h('div', { class: 'collapse' }, h('div', {}, h('div', { class: 'card', style: 'margin-top:6px' }, h('div', { class: 'add-grid' }, ta,
    h('div', { class: 'add-side' }, tagBtn, prio.el, h('div', { class: 'two' }, add, h('button', { class: 'btn', type: 'button', tip: TIP.import_files, onclick: () => fileIn.click() }, icon('doc'), 'Import')), drop)), fileIn)));
  const disc = h('button', { class: 'disc', type: 'button', 'aria-expanded': 'true', onclick: () => setIntake(!refs.intakeOpen) }, 'Add Links', icon('chev'));
  refs.intakeOpen = true; refs.intake = intake;
  const setIntake = open => { refs.intakeOpen = open; intake.classList.toggle('closed', !open); disc.setAttribute('aria-expanded', open); if (open) setTimeout(() => ta.focus({ preventScroll: true }), 180); };
  refs.setIntake = setIntake;
  W.append(disc, intake);

  /* list toolbar */
  const search = h('input', { class: 'field', type: 'search', placeholder: 'Search queue', 'aria-label': 'Search queue', tip: TIP.search, spellcheck: false });
  search.addEventListener('input', () => { st.search = search.value; listDirty = true; scrollTo0(); mark('list'); });
  const scrollTo0 = () => { };
  refs.bPause = btn('Pause', 'pause', 'pause', () => jobOp('pause')); refs.bResume = btn('Resume', 'play', 'resume', () => jobOp('resume'));
  refs.bRemove = btn('Remove', 'trash', 'remove', () => { haptic(10); jobOp('remove'); st.sel.clear(); }, 'red');
  refs.bRetry = btn('Retry Failed', 'retry', 'retry_failed', () => act('retry_failed')); refs.bClear = btn('Clear Finished', 'check', 'clear_finished', () => act('clear_finished'));
  const clean = h('div', { class: 'row', style: 'padding:0 4px 0 8px;gap:10px;min-height:var(--hit)', tip: TIP.auto_cleanup }, h('span', { class: 'unit' }, 'Auto-clean'), Switch('auto_cleanup'));
  W.append(h('div', { class: 'qtool' }, h('div', { class: 'search' }, icon('search'), search), refs.bPause, refs.bResume, refs.bRemove, refs.bRetry, refs.bClear, clean));

  /* virtualised list */
  refs.vspace = h('div', { class: 'vspace', role: 'rowgroup' }); refs.vwrap = h('div', { class: 'vwrap', role: 'table' }, refs.vspace);
  refs.empty = h('div', { class: 'empty' }, icon('down'), h('b'), h('span'));
  W.append(refs.vwrap, refs.empty);
  refs.vspace.addEventListener('click', e => {
    const row = e.target.closest('.qrow'); if (!row) return; const id = +row.dataset.id, b = e.target.closest('[data-act]');
    if (b) { haptic(6); const a = b.dataset.act; if (a === 'trash') { st.sel.delete(id); act('job', { op: 'remove', ids: [id] }); } else act('job', { op: a === 'go' ? 'toggle' : a, ids: [id] }); return; }
    select(id, e);
  });
  refs.vspace.addEventListener('dblclick', e => { const row = e.target.closest('.qrow'); if (row && !e.target.closest('[data-act]')) act('job', { op: 'toggle', ids: [+row.dataset.id] }); });
  refs.vspace.addEventListener('contextmenu', e => {
    const row = e.target.closest('.qrow'); if (!row) return; e.preventDefault(); const id = +row.dataset.id;
    if (!st.sel.has(id)) { st.sel.clear(); st.sel.add(id); st.anchor = id; refreshSel(); }
    const prios = new Set([...st.sel].map(i => st.jobs.get(i)?.prio));
    popMenu(null, [{ label: 'Pause', run: () => jobOp('pause') }, { label: 'Resume', run: () => jobOp('resume') }, { label: 'Retry', run: () => jobOp('retry') }, '-',
      ...['High', 'Medium', 'Low'].map(p => ({ label: 'Priority: ' + p, checked: prios.size === 1 && prios.has(p), run: () => jobOp('prio', p) })),
      { label: 'Apply Current Tag', run: () => jobOp('tag', st.s.tag) }, '-', { label: 'Open Output Folder', run: () => act('open_out') },
      { label: 'Remove', red: true, run: () => { jobOp('remove'); st.sel.clear(); } }], { x: e.clientX, y: e.clientY });
  });
  P.page.addEventListener('scroll', () => mark('list'), { passive: true }); new ResizeObserver(() => mark('list')).observe(P.page);
  return P.page;
}

/* ---------- render scheduling ---------- */
const dirty = new Set(); let raf = 0;
function mark(...k) { k.forEach(x => dirty.add(x)); if (!raf) raf = requestAnimationFrame(flush); }
function flush() {
  raf = 0; const d = [...dirty]; dirty.clear();
  if (d.includes('head')) renderHead();
  if (d.includes('list')) renderList();
  if (d.includes('stats') && st.page === 'stats') { renderStats(); drawGraph(); }
  if (d.includes('log')) renderLog();
  if (d.includes('side')) renderSide();
}
function renderSide() {
  refs.sideSpeed.textContent = speed(st.speed); refs.sideDot.className = 'dot ' + (st.meta.mdrs ? 'ok' : 'bad'); refs.sideEngine.textContent = st.meta.mdrs ? 'mdrs ready' : 'mdrs not found';
}

/* ---------- navigation ---------- */
function buildShell() {
  const items = PAGES.map(([id, label, ic], i) => { const b = h('button', { class: 'nav-item', type: 'button', 'data-page': id, onclick: () => { haptic(4); setPage(id); } }, icon(ic), label, id === 'queue' && (refs.badge = h('span', { class: 'badge' }))); return b; });
  const pill = h('i', { class: 'nav-pill' }); refs.pill = pill;
  refs.sideSpeed = h('b', {}, '0 B/s'); refs.sideDot = h('i', { class: 'dot' }); refs.sideEngine = h('span');
  $('#nav').append(h('div', { class: 'brand' }, h('div', { class: 'logo' }, icon('down')), h('div', {}, 'MDRS GUI', h('small', {}, 'for mediafire_rs'))),
    h('div', { class: 'nav-list' }, pill, items), h('div', { class: 'side-foot' }, h('span', {}, 'Current speed'), refs.sideSpeed, h('span', {}, refs.sideDot, refs.sideEngine)));
  const main = $('#main');
  main.append(buildQueue(), buildStats(), (() => { const P = makePage('network', 'Network', 'Proxies and anti-block tools.'); renderSchema(P.wrap, SCHEMA.network); return P.page; })(),
    (() => { const P = makePage('settings', 'Settings', 'Everything is saved automatically.'); renderSchema(P.wrap, SCHEMA.settings); return P.page; })(), buildLog());
  refs.profileInput = h('input', { type: 'file', accept: '.json,application/json', hidden: true });
  refs.profileInput.addEventListener('change', async () => {
    const f = refs.profileInput.files[0]; refs.profileInput.value = ''; if (!f) return;
    try { await act('import_profile', { profile: JSON.parse(await f.text()) }); } catch (e) { toast('That is not a valid profile file.', 'error'); }
  });
  document.body.append(refs.profileInput, toasts);
}
function setPage(id) {
  if (st.page === id && pages[id].page.classList.contains('on')) return;
  closePop(); st.page = id; const i = PAGES.findIndex(p => p[0] === id);
  document.querySelectorAll('.nav-item').forEach((b, n) => { b.classList.toggle('on', n === i); b.setAttribute('aria-current', n === i ? 'page' : 'false'); b.style.color = n === i ? 'var(--blue)' : ''; });
  refs.pill.style.setProperty('--i', i); $('#nav .nav-list').style.setProperty('--n', PAGES.length);
  for (const k in pages) pages[k].page.classList.toggle('on', k === id);
  segs.forEach(f => requestAnimationFrame(f));
  if (id === 'queue') { listDirty = true; mark('list'); } if (id === 'stats') { mark('stats'); requestAnimationFrame(() => drawGraph(true)); } if (id === 'log') { logDirty = true; renderLog(); }
  location.hash = id;
}

/* ---------- server events ---------- */
let firstLoad = true, warnedMdrs = false;
function onMsg(m) {
  if (m.t === 'full') { st.jobs.clear(); st.sel.clear(); rowEls.forEach(e => e.remove()); rowEls.clear(); fresh.clear(); st.log = []; firstLoad = true; }
  if (m.meta) {
    st.meta = m.meta; refs.ver && (refs.ver.textContent = m.meta.version || 'Not checked yet'); mark('side');
    if (!m.meta.mdrs && !warnedMdrs) { warnedMdrs = true; alertSheet('mdrs was not found', 'Install it with:\n  cargo install mediafire_rs\nor choose its location in Settings.', [{ label: 'Open Settings', primary: true, value: true }, { label: 'Dismiss', value: false }]).then(v => v && setPage('settings')); }
  }
  if (m.settings) applySettings(m.settings);
  if (m.jobs) { for (const r of m.jobs) { if (!st.jobs.has(r[0]) && !firstLoad) fresh.add(r[0]); st.jobs.set(r[0], toJob(r)); } listDirty = true; }
  if (m.gone) { m.gone.forEach(id => { st.jobs.delete(id); st.sel.delete(id); }); listDirty = true; }
  if (m.t === 'full') st.hist = m.hist.slice(); else { st.hist.push(m.hist); st.hist.shift(); }
  st.speed = m.speed; st.paused = m.paused; st.warn = m.warn; st.stats = m.stats;
  if (m.log) { m.log.reset ? st.log = m.log.lines.slice() : st.log.push(...m.log.lines); if (st.log.length > 5000) st.log.splice(0, st.log.length - 5000); mark('log'); }
  (m.notices || []).forEach(n => n.kind === 'sheet' ? alertSheet(n.title, n.msg) : toast(n.msg, n.kind));
  firstLoad = false; mark('head', 'list', 'stats', 'side');
  if (!st.online) { st.online = true; $('.offline').classList.remove('on'); }
}
function connect() {
  const es = new EventSource('/events?k=' + TOKEN);
  es.onmessage = e => onMsg(JSON.parse(e.data));
  es.onerror = () => { st.online = false; setTimeout(() => { if (!st.online) fetch('/ping?k=' + TOKEN).then(() => { }, () => $('.offline').classList.add('on')); }, 3500); };
}

/* ---------- keyboard + boot ---------- */
addEventListener('dragover', e => e.preventDefault()); addEventListener('drop', e => e.preventDefault());     // never navigate to a dropped file
addEventListener('keydown', e => {
  if (e.target.closest && e.target.closest('input,textarea') || st.page !== 'queue' || document.querySelector('.sheet')) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') { e.preventDefault(); view.forEach(j => st.sel.add(j.id)); refreshSel(); }
  else if (e.key === 'Delete' && st.sel.size) { jobOp('remove'); st.sel.clear(); }
  else if (e.key === 'Escape' && st.sel.size) { st.sel.clear(); refreshSel(); }
});
document.addEventListener('DOMContentLoaded', () => {
  document.body.append(layer, h('div', { class: 'offline' }, h('div', { class: 'card' }, h('h3', { style: 'margin:0 0 6px' }, 'MDRS GUI has quit'), h('p', { style: 'margin:0 0 14px;color:var(--label2)' }, 'The background app is no longer running. You can close this window, or start it again.'), h('button', { class: 'btn filled', onclick: () => location.reload() }, 'Try Again'))));
  buildShell(); const start = location.hash.slice(1); setPage(PAGES.some(p => p[0] === start) ? start : 'queue'); connect();
});

</script>
</body>
</html>
'''


# ------------------------------------------------------------ window launch
def launch_window(port, token):
    """Open the UI in a chrome-less Edge/Chrome app window (default browser as a fallback)."""
    url = f"http://127.0.0.1:{port}/?k={token}"
    browser = find_browser()
    try:
        if browser:
            BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
            subprocess.Popen([browser, f"--app={url}", f"--user-data-dir={BROWSER_PROFILE}",
                              "--no-first-run", "--no-default-browser-check",
                              "--window-size=1240,820", "--disable-features=Translate",
                              "--disable-background-mode"], creationflags=CREATE_NO_WINDOW)
            return
    except Exception:
        pass
    webbrowser.open(url)


# -------------------------------------------------------------- HTTP server
def make_handler(core):
    class Handler(BaseHTTPRequestHandler):
        server_version = "mdrs-gui"

        def log_message(self, *args):
            pass

        def _host_ok(self):
            return self.headers.get("Host", "").rsplit(":", 1)[0] in ("127.0.0.1", "localhost")

        def _token_ok(self, query):
            tok = (query.get("k") or [""])[0] or self.headers.get("X-Token", "")
            return secrets.compare_digest(tok, core.token or "-")

        def _send(self, code, body, ctype="application/json"):
            body = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            if ctype == "text/html":
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                 "img-src data:; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if not self._host_ok() or not self._token_ok(q):
                return self._send(403, "forbidden", "text/plain")
            if u.path == "/ping":
                self._send(200, "mdrs-gui", "text/plain")
            elif u.path == "/":
                self._send(200, PAGE.replace("__TOKEN__", core.token), "text/html")
            elif u.path == "/events":
                self._events()
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):
            origin = self.headers.get("Origin")
            if (not self._host_ok() or not self._token_ok({})
                    or (origin and origin not in (f"http://127.0.0.1:{core.port}", f"http://localhost:{core.port}"))):
                return self._send(403, '{"error":"forbidden"}')
            path = urllib.parse.urlparse(self.path).path
            if not path.startswith("/api/"):
                return self._send(404, '{"error":"not found"}')
            try:
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(n) or b"{}")
                self._send(200, json.dumps(core.call(path[5:], data)))
            except KeyError:
                self._send(404, '{"error":"unknown action"}')
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            conn = Conn()
            core.add_client(conn)
            try:
                while not core.stop.is_set():
                    msg, ver = core.snapshot(conn)
                    self.wfile.write(b"data: " + json.dumps(msg, separators=(",", ":")).encode() + b"\n\n")
                    self.wfile.flush()
                    time.sleep(0.03)                     # coalesce bursts
                    core.wait_change(ver, 1.0)
            except OSError:
                pass
            finally:
                core.remove_client(conn)
    return Handler


def main():
    inst = load_json(INSTANCE_FILE, None)
    if isinstance(inst, dict) and inst.get("port"):          # already running -> just open a window
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{inst['port']}/ping?k={inst['token']}",
                                        timeout=1.5) as r:
                if r.read() == b"mdrs-gui":
                    if not NO_WINDOW:
                        launch_window(inst["port"], inst["token"])
                    return
        except Exception:
            pass
    core = Core()
    core.token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(core))
    server.daemon_threads = True
    core.server, core.port = server, server.server_address[1]
    save_json(INSTANCE_FILE, {"port": core.port, "token": core.token, "pid": os.getpid()})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if NO_WINDOW:
        if sys.stdout:
            print(f"http://127.0.0.1:{core.port}/?k={core.token}", flush=True)
    else:
        core.tray = Tray(APP_NAME, core.show_window, core.quit_app)
    core.open_window()
    try:
        core.stop.wait()
    except KeyboardInterrupt:
        core.quit_app()


if __name__ == "__main__":
    main()
