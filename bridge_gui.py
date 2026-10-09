"""
bridge_gui.py -- desktop GUI for the Jarvis<->Cursor Gmail-bus bridge.

Window with: connection status, all bridge settings (email, app password,
poll interval, device tag, workspace allowlist file, allowed senders), the
executor routing table (executor name -> inbox folder, with a folder picker,
'Open folder' and 'Send probe task' per executor, a live watcher status
line per inbox and the runner's executor slots, idle or running a task id;
the openclaw row shows its gateway process, run state, queue, last result
and gateway-log activity instead), the device registry (per-device IP display, an input box
and a Set button, plus add-device), the approved workspaces (the runner's
allowlist, edited in place), start/stop, a connection test, and a live
activity log at the bottom showing everything the bridge engine does --
whether it was driven from here or the CLI.

Usage:  pythonw bridge_gui.py [--config PATH] [--no-start]
This is the canonical way to run the bridge (Start-JarvisBridge.ps1 launches
it; the Windows logon autostart -- the "JarvisBridge Autostart" Scheduled
Task from Register-JarvisBridgeAutostart.ps1 -- runs that script, and the
"Start bridge when Windows starts" box adds/removes it). First run creates the
default config and every executor inbox. Only one bridge runs per config
file; a second launch exits at once. Polling starts on launch once the
Gmail address and app password are set (config "start_on_launch").

Minimizes to the system tray when closed (needs `pip install pystray pillow`;
without it, the window just minimizes to the taskbar). A "Minimize to
taskbar" button minimizes without closing; the bridge keeps running.

On Windows, console windows start hidden: the GUI's own console (when run
with python.exe rather than pythonw.exe) and the Jarvis task runner's
console (Invoke-JarvisTask.ps1 -AutoApprove). "Show console" / "Hide
console" toggles them; anything hidden is shown again when the GUI quits.

Standard library otherwise. Windows-first, but runs anywhere tkinter does.
"""
import argparse
import json
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from tkinter import filedialog, messagebox, scrolledtext

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_core  # noqa: E402
from bridge_core import Bridge, redact_config  # noqa: E402

_setting = bridge_core.bridge_setting
APP_TITLE = _setting("ui.app_title", "Jarvis Bridge")
CONFIG_NAME = _setting("paths.config_file_name", "gmail-bridge-config.json")
WINDOW_GEOMETRY = _setting("ui.window_geometry", "820x990")
QUEUE_PUMP_MS = _setting("timeouts.gui_queue_pump_ms", 200)
FIRST_ROUTE_REFRESH_MS = _setting("timeouts.gui_first_route_refresh_ms", 500)
ROUTE_REFRESH_MS = _setting("timeouts.gui_route_refresh_ms", 2000)
LOCK_ACQUIRE_SECONDS = _setting("timeouts.gui_lock_acquire_seconds", 3)
SCHTASKS_QUERY_SECONDS = _setting("timeouts.gui_schtasks_query_seconds", 15)
AUTOSTART_REGISTER_SECONDS = _setting(
    "timeouts.gui_autostart_register_seconds", 120)
START_ON_LAUNCH_DELAY_MS = _setting("timeouts.gui_start_on_launch_delay_ms",
                                    1000)
CONSOLE_POLL_SECONDS = _setting("timeouts.gui_console_poll_seconds", 3)
SCROLL_SYNC_MS = _setting("timeouts.gui_scroll_sync_ms", 250)

# Visual theme: flat, light, card-style sections. Appearance only.
FONT_UI = "Segoe UI"
FONT_UI_SEMIBOLD = "Segoe UI Semibold"
FONT_MONO = "Cascadia Mono"
COL_WINDOW = "#eef1f5"
COL_CARD = "#ffffff"
COL_BORDER = "#dce2ea"
COL_INPUT_BORDER = "#c7d0db"
COL_TEXT = "#1f2937"
COL_HEADING = "#0f172a"
COL_MUTED = "#64748b"
COL_FAINT = "#94a3b8"
COL_ACCENT = "#2563eb"
COL_ACCENT_ACTIVE = "#1d4ed8"
COL_OK = "#15803d"
COL_WARN = "#b45309"
COL_ERROR = "#dc2626"
COL_DOT_ON = "#22c55e"
COL_DOT_OFF = "#ef4444"
COL_BUTTON = "#e4e9f0"
COL_BUTTON_ACTIVE = "#d3dbe5"
COL_BUTTON_ON_WINDOW = "#ffffff"
COL_LOG_BG = "#0f172a"
COL_LOG_FG = "#d4dce7"


def apply_theme(root):
    """Option-database defaults, so rows rebuilt at runtime are styled too.
    Any option a widget sets itself still wins."""
    root.configure(bg=COL_WINDOW)
    opts = (
        (60, "*Background", COL_WINDOW),
        (60, "*Foreground", COL_TEXT),
        (61, "*Labelframe*Background", COL_CARD),
        (61, "*Labelframe.Foreground", COL_HEADING),
        (61, "*Labelframe.Font", "{%s} 10" % FONT_UI_SEMIBOLD),
        (61, "*Labelframe.Relief", "flat"),
        (61, "*Labelframe.BorderWidth", 0),
        (61, "*Labelframe.HighlightThickness", 1),
        (61, "*Labelframe.HighlightBackground", COL_BORDER),
        (61, "*Labelframe.HighlightColor", COL_BORDER),
        (62, "*Button.Background", COL_BUTTON_ON_WINDOW),
        (63, "*Labelframe*Button.Background", COL_BUTTON),
        (62, "*Button.Foreground", COL_TEXT),
        (62, "*Button.ActiveBackground", COL_BUTTON_ACTIVE),
        (62, "*Button.ActiveForeground", COL_HEADING),
        (62, "*Button.DisabledForeground", COL_FAINT),
        (62, "*Button.Relief", "flat"),
        (62, "*Button.OverRelief", "flat"),
        (62, "*Button.BorderWidth", 0),
        (62, "*Button.HighlightThickness", 0),
        (62, "*Button.PadX", 6),
        (62, "*Button.PadY", 4),
        (62, "*Button.Cursor", "hand2"),
        (62, "*Checkbutton.ActiveBackground", COL_CARD),
        (62, "*Checkbutton.SelectColor", COL_CARD),
        (62, "*Checkbutton.HighlightThickness", 0),
        (62, "*Checkbutton.Cursor", "hand2"),
        (62, "*Entry.Background", COL_CARD),
        (62, "*Entry.Foreground", COL_HEADING),
        (62, "*Entry.Relief", "flat"),
        (62, "*Entry.BorderWidth", 3),
        (62, "*Entry.HighlightThickness", 1),
        (62, "*Entry.HighlightBackground", COL_INPUT_BORDER),
        (62, "*Entry.HighlightColor", COL_ACCENT),
        (62, "*Entry.InsertBackground", COL_HEADING),
        (62, "*Entry.SelectBackground", "#bfdbfe"),
        (62, "*Entry.SelectForeground", COL_HEADING),
        (62, "*Spinbox.Background", COL_CARD),
        (62, "*Spinbox.Foreground", COL_HEADING),
        (62, "*Spinbox.Relief", "flat"),
        (62, "*Spinbox.BorderWidth", 3),
        (62, "*Spinbox.HighlightThickness", 1),
        (62, "*Spinbox.HighlightBackground", COL_INPUT_BORDER),
        (62, "*Spinbox.HighlightColor", COL_ACCENT),
        (62, "*Spinbox.ButtonBackground", COL_BUTTON),
        (62, "*Spinbox.InsertBackground", COL_HEADING),
        (62, "*Listbox.Background", COL_CARD),
        (62, "*Listbox.Foreground", COL_HEADING),
        (62, "*Listbox.Relief", "flat"),
        (62, "*Listbox.BorderWidth", 4),
        (62, "*Listbox.HighlightThickness", 1),
        (62, "*Listbox.HighlightBackground", COL_INPUT_BORDER),
        (62, "*Listbox.HighlightColor", COL_ACCENT),
        (62, "*Listbox.SelectBackground", COL_ACCENT),
        (62, "*Listbox.SelectForeground", "#ffffff"),
        (62, "*Text.Background", COL_LOG_BG),
        (62, "*Text.Foreground", COL_LOG_FG),
        (62, "*Text.Relief", "flat"),
        (62, "*Text.BorderWidth", 8),
        (62, "*Text.HighlightThickness", 0),
        (62, "*Text.InsertBackground", COL_LOG_FG),
        (62, "*Text.SelectBackground", "#334155"),
    )
    for priority, pattern, value in opts:
        root.option_add(pattern, value, priority)


try:
    import pystray  # noqa: F401
    from PIL import Image, ImageDraw
    HAVE_TRAY = True
except ImportError:
    HAVE_TRAY = False


def default_config_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_NAME)


# ----------------------------------------------------------------------
# Approved workspaces: allowed-workspaces.txt (config "allowed_workspaces",
# by default in the cursor inbox), the file Invoke-JarvisTask.ps1 enforces.
# The panel only edits its contents; every
# line it does not add or remove (comments, blanks, other entries) is kept
# byte-for-byte, as are the newline style, trailing newline and BOM.
# ----------------------------------------------------------------------
ALLOWLIST_NAME = bridge_core.ALLOWLIST_NAME
_UTF8_BOM = b"\xef\xbb\xbf"
# Windows PowerShell 5.1 Get-Content reads BOM-less files as the ANSI code
# page, so new lines must be written in it to compare equal in the runner.
_ANSI = "mbcs" if os.name == "nt" else "utf-8"


class AllowlistError(Exception):
    pass


def allowlist_path(inbox):
    inbox = (inbox or "").strip() or bridge_core.resolve_path(
        bridge_core.default_inboxes().get("cursor") or
        os.path.join(os.path.expanduser("~"), "cursor-inbox"))
    return os.path.join(inbox, ALLOWLIST_NAME)


def workspace_key(path):
    """Comparison key matching the runner: trimmed, trailing '\\' dropped,
    case-insensitive."""
    return path.strip().rstrip("\\").lower()


def workspace_allowed(entries, workspace):
    """Same rule as the runner's Test-WorkspaceAllowed: the folder itself or
    anything below an approved folder."""
    key = workspace_key(workspace)
    for e in entries:
        a = workspace_key(e)
        if key == a or key.startswith(a + "\\"):
            return True
    return False


def read_allowlist(path):
    """Parse the file into a dict describing it exactly. Raises
    AllowlistError on anything unreadable or unexpected (fail closed)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as exc:
        raise AllowlistError("cannot read %s: %s" % (path, exc))
    bom = data.startswith(_UTF8_BOM)
    body = data[len(_UTF8_BOM):] if bom else data
    if body.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in body:
        raise AllowlistError("%s is not a plain-text (ANSI/UTF-8) file" % path)
    encoding = "utf-8" if bom else _ANSI
    try:
        text = body.decode(encoding, errors="strict")
    except UnicodeDecodeError as exc:
        raise AllowlistError("cannot decode %s: %s" % (path, exc))
    newline = "\n" if ("\n" in text and "\r\n" not in text) else "\r\n"
    trailing = text.endswith(newline)
    if not text:
        lines = []
    else:
        lines = (text[:-len(newline)] if trailing else text).split(newline)
    if any("\r" in ln or "\n" in ln for ln in lines):
        raise AllowlistError("%s has mixed line endings" % path)
    return {"path": path, "raw": data, "bom": bom, "encoding": encoding,
            "newline": newline, "trailing": trailing, "lines": lines}


def allowlist_entries(doc):
    """Entries exactly as the runner sees them: non-blank, non-comment lines,
    shown trimmed, in file order."""
    return [ln.strip() for ln in doc["lines"]
            if ln.strip() and not ln.strip().startswith("#")]


def render_allowlist(doc, lines):
    text = doc["newline"].join(lines)
    if lines and (doc["trailing"] or len(lines) > len(doc["lines"])):
        text += doc["newline"]
    data = text.encode(doc["encoding"], errors="strict")
    return (_UTF8_BOM + data) if doc["bom"] else data


def clean_new_workspace(raw, entries):
    """Validate a folder typed/browsed for approval. Returns the line to
    append; raises AllowlistError with a user-facing reason."""
    path = (raw or "").strip().replace("/", "\\")
    if not path:
        raise AllowlistError("Type or browse to a folder first.")
    if any(ord(ch) < 32 for ch in path):
        raise AllowlistError("Folder path contains control characters.")
    if not (re.match(r"^[A-Za-z]:\\", path) or
            re.match(r"^\\\\[^\\]+\\[^\\]+", path)):
        raise AllowlistError(
            "Folder must be an absolute path like C:\\Projects\\thing:\n%s"
            % raw)
    if len(path) > 3:
        path = path.rstrip("\\")
    key = workspace_key(path)
    if any(workspace_key(e) == key for e in entries):
        raise AllowlistError("That folder is already approved:\n%s" % path)
    return path


def _write_allowlist(doc, lines):
    data = render_allowlist(doc, lines)
    path = doc["path"]
    tmp = path + ".panel-tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return read_allowlist(path)


def add_allowlist_entry(path, raw):
    """Re-read the file, append one validated line, write it back."""
    doc = read_allowlist(path)
    line = clean_new_workspace(raw, allowlist_entries(doc))
    try:
        line.encode(doc["encoding"], errors="strict")
    except UnicodeEncodeError:
        raise AllowlistError("Folder name has characters the runner's "
                             "allowlist file cannot store:\n%s" % line)
    new = _write_allowlist(doc, doc["lines"] + [line])
    return line, new


def remove_allowlist_entry(path, entry):
    """Re-read the file and drop every line matching `entry` (same key the
    runner compares with); all other lines are untouched."""
    doc = read_allowlist(path)
    key = workspace_key(entry)
    keep = [ln for ln in doc["lines"]
            if not (ln.strip() and not ln.strip().startswith("#")
                    and workspace_key(ln) == key)]
    removed = len(doc["lines"]) - len(keep)
    if not removed:
        raise AllowlistError("Not in the allowlist (file changed?):\n%s"
                             % entry)
    new = _write_allowlist(doc, keep)
    return removed, new


# ----------------------------------------------------------------------
# Web Publishing: the "webgui" section of bridge.config.json, which the web
# dashboard (bridge-dashboard\server.py) re-reads every few seconds. Saving
# rewrites only that section; every other byte of the file is kept.
# ----------------------------------------------------------------------
WEBGUI_MODES = ("lan", "public")
WEBGUI_DEFAULTS = {"broadcast_mode": "lan", "web_address": "",
                   "cloudflare_hostname": "", "cloudflare_tunnel_token": ""}
_WEB_ADDRESS_RE = re.compile(r"^https?://[^\s/?#]+(?:[/?#]\S*)?$", re.I)
_TUNNEL_TOKEN_RE = re.compile(r"^[A-Za-z0-9+/=_.-]+$")


def read_webgui_settings(path):
    """({key: value} with defaults for missing keys, error text or "")."""
    web = dict(WEBGUI_DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return web, ""
    except (OSError, ValueError) as exc:
        return web, "%s: %s" % (type(exc).__name__, exc)
    section = doc.get("webgui") if isinstance(doc, dict) else None
    if isinstance(section, dict):
        for key in web:
            if isinstance(section.get(key), str):
                web[key] = section[key].strip()
    if web["broadcast_mode"].lower() not in WEBGUI_MODES:
        web["broadcast_mode"] = "lan"
    web["broadcast_mode"] = web["broadcast_mode"].lower()
    return web, ""


def validate_webgui_settings(web):
    """List of problems (empty = OK)."""
    problems = []
    if web["broadcast_mode"] not in WEBGUI_MODES:
        problems.append("Broadcast mode must be LAN only or Public.")
    if web["web_address"] and not _WEB_ADDRESS_RE.match(web["web_address"]):
        problems.append("Web address must start with http:// or https:// "
                        "(e.g. https://bridge.example.com/).")
    host = web["cloudflare_hostname"]
    if host and ("/" in host or ":" in host or not bridge_core.valid_host(host)):
        problems.append("Cloudflare hostname must be a bare hostname "
                        "(e.g. bridge.example.com), no https:// or path.")
    token = web["cloudflare_tunnel_token"]
    if token and not _TUNNEL_TOKEN_RE.match(token):
        problems.append("Cloudflare tunnel token has unexpected characters "
                        "(paste only the token, not the whole command).")
    return problems


def _top_level_value_span(text, key):
    """(start, end) of `key`'s value in the top-level JSON object `text`, or
    (None, end of the last value) when the key is absent."""
    dec = json.JSONDecoder()
    ws = re.compile(r"\s*")
    i = ws.match(text, 0).end()
    if text[i:i + 1] != "{":
        raise ValueError("not a JSON object")
    i = ws.match(text, i + 1).end()
    last_end = None
    while text[i:i + 1] != "}":
        name, j = dec.raw_decode(text, i)
        j = ws.match(text, j).end()
        if text[j:j + 1] != ":":
            raise ValueError("expected ':' at offset %d" % j)
        j = ws.match(text, j + 1).end()
        _value, end = dec.raw_decode(text, j)
        if name == key:
            return j, end
        last_end = end
        i = ws.match(text, end).end()
        if text[i:i + 1] == ",":
            i = ws.match(text, i + 1).end()
    return None, last_end


def write_webgui_settings(path, web):
    """Merge `web` into the top-level "webgui" object of bridge.config.json
    (other keys in that object, e.g. "_note", are kept). Returns False when
    nothing changed (file untouched). BOM and newline style are kept."""
    with open(path, "rb") as f:
        raw = f.read()
    bom = _UTF8_BOM if raw.startswith(_UTF8_BOM) else b""
    text = raw[len(bom):].decode("utf-8")
    doc = json.loads(text)
    if not isinstance(doc, dict):
        raise ValueError("%s is not a JSON object" % path)
    old = doc.get("webgui") if isinstance(doc.get("webgui"), dict) else {}
    section = dict(old)
    section.update({k: web[k] for k in WEBGUI_DEFAULTS})
    if section == old:
        return False
    nl = "\r\n" if "\r\n" in text else "\n"
    block = json.dumps(section, indent=2, ensure_ascii=False).replace(
        "\n", nl + "  ")
    start, end = _top_level_value_span(text, "webgui")
    if start is not None:
        new_text = text[:start] + block + text[end:]
    elif end is not None:
        new_text = text[:end] + "," + nl + nl + '  "webgui": ' + block + \
            text[end:]
    else:
        close = text.rindex("}")
        new_text = text[:close] + '  "webgui": ' + block + nl + text[close:]
    check = json.loads(new_text)
    doc["webgui"] = section
    if check != doc:
        raise ValueError("webgui update would change other settings")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(bom + new_text.encode("utf-8"))
    os.replace(tmp, path)
    return True


# ----------------------------------------------------------------------
# Console windows (Windows only). Under Windows Terminal, the handle
# GetConsoleWindow() returns is an invisible pseudo window; the window the
# user sees is its owner, so that is what gets hidden/shown. Runner
# consoles are found process-first (command line, then AttachConsole):
# a pseudo window does not reliably report its client process. A hidden
# window has no taskbar entry, so everything hidden is remembered and put
# back on Show or when the GUI quits.
# ----------------------------------------------------------------------
SW_HIDE = 0
SW_SHOW = 5
_GW_OWNER = 4
_SHELLS = ("powershell.exe", "pwsh.exe")
RUNNER_PATTERN = r"Invoke-JarvisTask\.ps1"
# Only a runner that never prompts is hidden automatically; without
# -AutoApprove it waits for a per-task Y/N at its console.
_AUTO_APPROVE = re.compile(r"-AutoApprove\b", re.I)

HAVE_WIN32 = False
if os.name == "nt":
    try:
        import ctypes
        from ctypes import wintypes
        _k32 = ctypes.WinDLL("kernel32")
        _u32 = ctypes.WinDLL("user32")
        _ntdll = ctypes.WinDLL("ntdll")
        _k32.GetConsoleWindow.restype = wintypes.HWND
        _k32.AttachConsole.argtypes = [wintypes.DWORD]
        _k32.OpenProcess.restype = wintypes.HANDLE
        _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                     wintypes.DWORD]
        _k32.CloseHandle.argtypes = [wintypes.HANDLE]
        _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD,
                                                  wintypes.DWORD]
        _ntdll.NtQueryInformationProcess.restype = ctypes.c_long
        _ntdll.NtQueryInformationProcess.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
            ctypes.POINTER(wintypes.ULONG)]
        _u32.GetWindow.restype = wintypes.HWND
        _u32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        _u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        _u32.IsWindowVisible.argtypes = [wintypes.HWND]
        _u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR,
                                       ctypes.c_int]
        _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                          wintypes.LPARAM)
        _u32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]

        class _PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD),
                        ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_wchar * 260)]

        class _UNICODE_STRING(ctypes.Structure):
            _fields_ = [("Length", ctypes.c_ushort),
                        ("MaximumLength", ctypes.c_ushort),
                        ("Buffer", ctypes.c_void_p)]

        _k32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                         ctypes.POINTER(_PROCESSENTRY32W)]
        _k32.Process32NextW.argtypes = [wintypes.HANDLE,
                                        ctypes.POINTER(_PROCESSENTRY32W)]
        HAVE_WIN32 = True
    except (ImportError, OSError, AttributeError):
        HAVE_WIN32 = False


def _shown_window(hwnd):
    return _u32.GetWindow(hwnd, _GW_OWNER) or hwnd


def own_console_window():
    """The window showing this process's console, or None (pythonw.exe,
    non-Windows)."""
    if not HAVE_WIN32:
        return None
    hwnd = _k32.GetConsoleWindow()
    return _shown_window(hwnd) if hwnd else None


def _shell_pids():
    """PIDs of running powershell.exe / pwsh.exe processes."""
    snap = _k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        return []
    pids = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() in _SHELLS:
                pids.append(entry.th32ProcessID)
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    return pids


def _command_line(pid):
    """The process's command line, or None if it can't be read."""
    proc = _k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not proc:
        return None
    try:
        size = wintypes.ULONG(0)
        _ntdll.NtQueryInformationProcess(proc, 60, None, 0,  # CommandLine
                                         ctypes.byref(size))
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if _ntdll.NtQueryInformationProcess(proc, 60, buf, size,
                                            ctypes.byref(size)) != 0:
            return None
        us = _UNICODE_STRING.from_buffer(buf)
        return ctypes.wstring_at(us.Buffer, us.Length // 2) if us.Buffer else ""
    finally:
        _k32.CloseHandle(proc)


def _console_of(pid):
    """The console window of another process. Only works while this process
    has no console of its own (pythonw.exe, as the GUI is launched)."""
    if not _k32.AttachConsole(pid):
        return None
    try:
        return _k32.GetConsoleWindow()
    finally:
        _k32.FreeConsole()


def _tabs_in(win):
    """Console pseudo windows owned by a Windows Terminal window."""
    tabs = []

    def cb(hwnd, _lparam):
        cls = ctypes.create_unicode_buffer(64)
        _u32.GetClassNameW(hwnd, cls, 64)
        if cls.value == "PseudoConsoleWindow" and \
                _u32.GetWindow(hwnd, _GW_OWNER) == win:
            tabs.append(hwnd)
        return True

    _u32.EnumWindows(_WNDENUMPROC(cb), 0)
    return tabs


class ConsoleWindows:
    """Hides/shows this process's console and the task runner's console."""

    def __init__(self, runner_pattern=RUNNER_PATTERN):
        self.pattern = re.compile(runner_pattern, re.I)
        self.own = own_console_window()
        self.hidden = set()
        self.lock = threading.Lock()
        self._left_visible = set()

    def _runners(self):
        """{shown hwnd: {console hwnd: auto-approve?}} for running task
        runners. One Windows Terminal window can hold several consoles."""
        wins = {}
        if self.own:
            return wins
        for pid in _shell_pids():
            cmd = _command_line(pid)
            if not cmd or not self.pattern.search(cmd):
                continue
            con = _console_of(pid)
            if con:
                auto = bool(_AUTO_APPROVE.search(cmd))
                cons = wins.setdefault(_shown_window(con), {})
                cons[con] = cons.get(con, True) and auto
        return wins

    def hide(self, wanted, log):
        """Hide the own console and visible runner consoles, if `wanted()`
        still holds. A window is only hidden if every console in it is a
        -AutoApprove runner."""
        if not HAVE_WIN32:
            return
        with self.lock:
            targets = set()
            for win, cons in self._runners().items():
                if not _u32.IsWindowVisible(win):
                    continue
                tabs = set(_tabs_in(win)) if win not in cons else {win}
                if all(cons.values()) and tabs <= set(cons):
                    targets.add(win)
                elif win not in self._left_visible:
                    self._left_visible.add(win)
                    log("Task runner console left visible: it runs without "
                        "-AutoApprove (asks for approval there) or shares "
                        "its window with other tabs.")
            if self.own:
                targets.add(self.own)
            if not wanted():
                return
            for win in targets:
                if _u32.IsWindowVisible(win):
                    _u32.ShowWindow(win, SW_HIDE)
                    self.hidden.add(win)
                    log("Console window hidden -- press 'Show console' "
                        "to see it.")

    def show(self, wanted, log):
        """Show everything hidden, including runner consoles hidden by an
        earlier GUI run."""
        if not HAVE_WIN32:
            return
        with self.lock:
            targets = set(self.hidden) | set(self._runners())
            if self.own:
                targets.add(self.own)
            if not wanted():
                return
            shown = [w for w in targets if not _u32.IsWindowVisible(w)]
            for win in targets:
                _u32.ShowWindow(win, SW_SHOW)
            self.hidden.clear()
            if shown:
                log("Console window shown.")
            else:
                log("No hidden console window to show (is the task runner "
                    "open?).")

    def hide_own(self):
        """Hide just this process's console -- no lookup, safe at startup."""
        if self.own and _u32.IsWindowVisible(self.own):
            with self.lock:
                _u32.ShowWindow(self.own, SW_HIDE)
                self.hidden.add(self.own)

    def restore(self):
        """Put back what this GUI hid; used on quit."""
        if not HAVE_WIN32:
            return
        got = self.lock.acquire(timeout=LOCK_ACQUIRE_SECONDS)
        try:
            for win in self.hidden:
                _u32.ShowWindow(win, SW_SHOW)
            self.hidden.clear()
        finally:
            if got:
                self.lock.release()


# Keep in sync with $TaskName / $LegacyRunValue in the script.
AUTOSTART_SCRIPT = "Register-JarvisBridgeAutostart.ps1"
AUTOSTART_TASK = _setting("scheduled_tasks.autostart.name",
                          "JarvisBridge Autostart")
LEGACY_RUN_VALUE = _setting("scheduled_tasks.autostart.legacy_run_value",
                            "JarvisBridgeGUI")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def autostart_registered():
    """True if the logon autostart task (or the legacy HKCU Run value)
    exists for the current user. Windows only."""
    try:
        r = subprocess.run(["schtasks.exe", "/Query", "/TN", AUTOSTART_TASK],
                           capture_output=True, timeout=SCHTASKS_QUERY_SECONDS,
                           creationflags=_NO_WINDOW)
        if r.returncode == 0:
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
                            ) as key:
            winreg.QueryValueEx(key, LEGACY_RUN_VALUE)
        return True
    except OSError:
        return False


# ----------------------------------------------------------------------
# OpenClaw executor readouts, all read live: the gateway is whatever
# process listens on gateway.port from OpenClaw's own config; its uptime
# comes from the system process table (NtQuerySystemInformation needs no
# process handle, so a LocalSystem gateway is visible to a normal user);
# liveness from its /health endpoint; the queue from the openclaw inbox;
# the last result from the result files Loop B mails as [JARVIS-RESULT]
# (checked against sent.json); activity from the gateway log.
# ----------------------------------------------------------------------
OPENCLAW_GATEWAY_PORT_DEFAULT = 18789  # OpenClaw's own default gateway.port
OPENCLAW_HEALTH_TIMEOUT_SECONDS = max(1.0, ROUTE_REFRESH_MS / 1000.0)
OPENCLAW_PLUGIN_TEXT_MAX = 44
_OC_VERDICT_RE = re.compile(r"\b(PASS|FAIL)\b")
_OC_UUID_RE = re.compile(r"\b([0-9a-f]{8})(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b")
_OC_PLUGIN_TAG = "[jarvis-bridge-priority] "
_UNSET = object()
_json_cache = {}

_iphlp = None
_PROC_TABLE_OK = False
if HAVE_WIN32:
    try:
        _iphlp = ctypes.WinDLL("iphlpapi")
        _iphlp.GetExtendedTcpTable.restype = wintypes.DWORD
        _iphlp.GetExtendedTcpTable.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
            wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
        _ntdll.NtQuerySystemInformation.restype = ctypes.c_long
        _ntdll.NtQuerySystemInformation.argtypes = [
            wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
            ctypes.POINTER(wintypes.ULONG)]
        # The SYSTEM_PROCESS_INFORMATION offsets below are the 64-bit layout.
        _PROC_TABLE_OK = ctypes.sizeof(ctypes.c_void_p) == 8
    except (OSError, AttributeError):
        _iphlp = None


def _read_json_cached(path):
    """Parsed JSON at path, re-read only when its size or mtime changes
    (the GUI asks every refresh); None when missing or unreadable."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_size, st.st_mtime)
    hit = _json_cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = None
    _json_cache[path] = (key, data)
    return data


def openclaw_config_path():
    """openclaw.json, found the way bridge_core.openclaw_workspace() does."""
    env = os.environ
    user_home = bridge_core.home_dir()
    home = env.get("OPENCLAW_HOME", "").strip()
    if home:
        home = bridge_core._openclaw_user_path(home, user_home)
    elif bridge_core.OPENCLAW_HOME_DEFAULT.upper() == bridge_core.HOME_TOKEN:
        home = user_home
    else:
        home = bridge_core.resolve_path(bridge_core.OPENCLAW_HOME_DEFAULT)
    state = env.get("OPENCLAW_STATE_DIR", "").strip()
    state_dir = bridge_core._openclaw_user_path(state, home) if state \
        else os.path.join(home, bridge_core.OPENCLAW_STATE_DIRNAME)
    override = env.get("OPENCLAW_CONFIG_PATH", "").strip()
    return bridge_core._openclaw_user_path(override, home) if override \
        else os.path.join(state_dir, bridge_core.OPENCLAW_CONFIG_NAME)


def openclaw_gateway_port(config_path=None):
    """(port, source): gateway.port from openclaw.json, else OpenClaw's
    default port."""
    path = config_path or openclaw_config_path()
    data = _read_json_cached(path)
    gateway = data.get("gateway") if isinstance(data, dict) else None
    port = gateway.get("port") if isinstance(gateway, dict) else None
    if isinstance(port, int) and not isinstance(port, bool) and \
            0 < port < 65536:
        return port, "gateway.port in %s" % os.path.basename(path)
    return OPENCLAW_GATEWAY_PORT_DEFAULT, "OpenClaw default port"


def tcp_listener_pids(port):
    """PIDs with a TCP socket listening on `port` (IPv4 and IPv6); None when
    the table cannot be read here."""
    if _iphlp is None:
        return None
    pids, read_any = [], False
    # (family, row size, local-port offset, PID offset) of
    # MIB_TCPROW_OWNER_PID and MIB_TCP6ROW_OWNER_PID.
    for family, row, port_off, pid_off in ((2, 24, 8, 20), (23, 56, 20, 52)):
        size = wintypes.DWORD(0)
        buf, rc = None, None
        for _ in range(4):
            buf = ctypes.create_string_buffer(max(size.value, 4))
            rc = _iphlp.GetExtendedTcpTable(  # 3 = OWNER_PID_LISTENER
                buf, ctypes.byref(size), False, family, 3, 0)
            if rc != 122:  # ERROR_INSUFFICIENT_BUFFER
                break
        if rc != 0:
            continue
        read_any = True
        raw = buf.raw
        for i in range(struct.unpack_from("<I", raw, 0)[0]):
            off = 4 + i * row
            if off + row > len(raw):
                break
            if struct.unpack_from(">H", raw, off + port_off)[0] == port:
                pid = struct.unpack_from("<I", raw, off + pid_off)[0]
                if pid not in pids:
                    pids.append(pid)
    return pids if read_any else None


def process_table():
    """{pid: (parent pid, image name, start time in unix seconds)} for every
    process on the machine, or None when it cannot be read here."""
    if not _PROC_TABLE_OK:
        return None
    size, rc, buf = 1 << 20, None, None
    for _ in range(6):
        buf = ctypes.create_string_buffer(size)
        need = wintypes.ULONG(0)
        rc = _ntdll.NtQuerySystemInformation(  # 5 = SystemProcessInformation
            5, buf, size, ctypes.byref(need)) & 0xFFFFFFFF
        if rc != 0xC0000004:  # STATUS_INFO_LENGTH_MISMATCH
            break
        size = max(size * 2, need.value + 65536)
    if rc != 0:
        return None
    raw, base, out, off = buf.raw, ctypes.addressof(buf), {}, 0
    while off + 96 <= len(raw):
        nxt = struct.unpack_from("<I", raw, off)[0]
        created = struct.unpack_from("<q", raw, off + 32)[0]
        name_len = struct.unpack_from("<H", raw, off + 56)[0]
        name_at = struct.unpack_from("<Q", raw, off + 64)[0] - base
        pid, ppid = struct.unpack_from("<QQ", raw, off + 80)
        name = ""
        if name_len and 0 <= name_at <= len(raw) - name_len:
            name = raw[name_at:name_at + name_len].decode("utf-16-le",
                                                          "replace")
        out[pid] = (ppid, name,
                    created / 1e7 - 11644473600 if created > 0 else None)
        if not nxt:
            break
        off += nxt
    return out


def openclaw_gateway_state(port, now=None, listeners=_UNSET,
                           processes=_UNSET):
    """The OpenClaw gateway as seen from its port: {"port", "running",
    "pid", "name", "parent", "uptime" (seconds), "error"}."""
    now = time.time() if now is None else now
    if listeners is _UNSET:
        listeners = tcp_listener_pids(port)
    out = {"port": port, "running": False, "pid": None, "name": "",
           "parent": None, "uptime": None, "error": ""}
    if listeners is None:
        out["error"] = "the TCP listener table is not readable here"
        return out
    if not listeners:
        return out
    if processes is _UNSET:
        processes = process_table()
    pid = listeners[0]
    info = (processes or {}).get(pid)
    out.update(running=True, pid=pid)
    if info:
        out["parent"], out["name"] = info[0], info[1]
        if info[2] is not None:
            out["uptime"] = max(0, int(now - info[2]))
    return out


_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def openclaw_gateway_health(port, timeout=OPENCLAW_HEALTH_TIMEOUT_SECONDS):
    """(ok, detail) from GET http://127.0.0.1:<port>/health."""
    try:
        with _NO_PROXY.open("http://127.0.0.1:%d/health" % port,
                            timeout=timeout) as r:
            code, body = r.status, r.read(4096)
    except urllib.error.HTTPError as exc:
        return False, "HTTP %s" % exc.code
    except Exception as exc:
        return False, "no answer (%s)" % (getattr(exc, "reason", None) or exc)
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get("ok") is True:
        return True, str(data.get("status") or "ok")
    return False, "HTTP %s, not ok" % code


def result_verdict(res):
    """(verdict, why) for a result body -- what its [JARVIS-RESULT] email
    tells Jarvis: "PASS", "FAIL" or "NO VERDICT"."""
    if not isinstance(res, dict):
        return "FAIL", "result file unreadable"
    if res.get("skipped") or res.get("status") == "skipped":
        return "FAIL", "skipped: %s" % (res.get("skip_reason_code") or
                                        "no reason given")
    if res.get("status") == "failed":
        return "FAIL", "status failed"
    if res.get("timed_out"):
        return "FAIL", "timed out"
    code = res.get("exit_code")
    if code not in (None, 0):
        return "FAIL", "exit code %s" % code
    passed = failed = 0
    for line in str(res.get("jarvis_report") or "").splitlines():
        if "PASS or FAIL" in line:
            continue
        found = set(_OC_VERDICT_RE.findall(line))
        if "FAIL" in found:
            failed += 1
        elif "PASS" in found:
            passed += 1
    if failed:
        return "FAIL", "%d of %d criteria FAIL" % (failed, failed + passed)
    if passed:
        return "PASS", "%d/%d criteria PASS" % (passed, passed)
    if code == 0:
        return "PASS", "exit code 0"
    return "NO VERDICT", "no PASS/FAIL or exit code in the report"


def openclaw_last_result(results_dir, sent_keys, key_for):
    """The newest result file in the executor's results folder -- the body
    Loop B mails as [JARVIS-RESULT] <task_id> -- as {"task_id", "verdict",
    "why", "when" (file time), "emailed"}, or None."""
    try:
        entries = list(os.scandir(results_dir))
    except OSError:
        return None
    best = None
    for e in entries:
        if not e.name.endswith(bridge_core.RESULT_FILE_SUFFIX):
            continue
        try:
            mtime = e.stat().st_mtime
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, e.name, e.path)
    if best is None:
        return None
    mtime, name, path = best
    res = _read_json_cached(path)
    verdict, why = result_verdict(res)
    task_id = str(res.get("task_id") or "") if isinstance(res, dict) else ""
    return {"task_id": task_id or name[:-len(bridge_core.RESULT_FILE_SUFFIX)],
            "verdict": verdict, "why": why, "when": mtime,
            "emailed": key_for(name) in sent_keys}


def openclaw_queue(inbox):
    """[(task_id, mtime)] oldest first: packages in the inbox with no result
    file yet, i.e. still to be run (or running) by the OpenClaw watcher."""
    try:
        entries = list(os.scandir(inbox))
    except OSError:
        return []
    names = {e.name for e in entries}
    out = []
    for e in entries:
        if not e.name.endswith(bridge_core.TASK_FILE_SUFFIX):
            continue
        task_id = e.name[:-len(bridge_core.TASK_FILE_SUFFIX)]
        if task_id + bridge_core.RESULT_FILE_SUFFIX in names:
            continue
        try:
            out.append((task_id, e.stat().st_mtime))
        except OSError:
            continue
    return sorted(out, key=lambda t: t[1])


def openclaw_log_activity(log_dir=None):
    """{"log_dir", "log_time" (newest gateway log last written),
    "plugin_time", "plugin_text" (newest jarvis-bridge-priority line)}."""
    log_dir = log_dir or bridge_core.openclaw_log_dir()
    out = {"log_dir": log_dir, "log_time": None, "plugin_time": None,
           "plugin_text": ""}
    try:
        names = sorted(n for n in os.listdir(log_dir)
                       if n.startswith("openclaw-") and n.endswith(".log"))
    except OSError:
        return out
    if names:
        try:
            out["log_time"] = os.path.getmtime(os.path.join(log_dir,
                                                            names[-1]))
        except OSError:
            pass
    for name in reversed(names[-2:]):
        lines = bridge_core._openclaw_plugin_lines(
            os.path.join(log_dir, name), bridge_core.OPENCLAW_LOG_TAIL_BYTES)
        for line in reversed(lines):
            m = bridge_core._OC_TIME_RE.search(line)
            try:
                when = datetime.fromisoformat(m.group(1)).timestamp()
            except (AttributeError, ValueError):
                continue
            try:
                text = str(json.loads(line).get("1") or "")
            except (ValueError, AttributeError):
                text = line
            text = text[text.find(_OC_PLUGIN_TAG) + len(_OC_PLUGIN_TAG):] \
                if _OC_PLUGIN_TAG in text else text
            text = _OC_UUID_RE.sub(
                r"\1", text.split(";")[0].split(" (")[0].strip())
            if len(text) > OPENCLAW_PLUGIN_TEXT_MAX:
                text = text[:OPENCLAW_PLUGIN_TEXT_MAX - 3].rstrip() + "..."
            out["plugin_time"], out["plugin_text"] = when, text
            return out
    return out


def _fmt_duration(seconds):
    days, rest = divmod(max(0, int(seconds)), 86400)
    hours, rest = divmod(rest, 3600)
    text = "%d:%02d:%02d" % ((hours,) + divmod(rest, 60))
    return "%dd %s" % (days, text) if days else text


def _fmt_when(ts, now):
    """HH:MM:SS for today, else YYYY-MM-DD HH:MM."""
    local = time.localtime(ts)
    if local[:3] == time.localtime(now)[:3]:
        return time.strftime("%H:%M:%S", local)
    return time.strftime("%Y-%m-%d %H:%M", local)


def openclaw_readout_lines(snap, run_lines=(), now=None):
    """[(text, colour)] for the openclaw executor row, in the cursor row's
    executor-pool style: a grey header (gateway process + health), then
    indented state lines. run_lines is the green watcher RUNNING line, if
    a run is active."""
    now = time.time() if now is None else now
    gw, health = snap["gateway"], snap.get("health")
    where = "127.0.0.1:%d" % gw["port"]
    if gw["error"]:
        lines = [("gateway: state unreadable -- %s" % gw["error"], COL_ERROR)]
    elif not gw["running"]:
        lines = [("gateway: NOT RUNNING -- nothing listening on %s (%s)"
                  % (where, snap.get("port_source") or "gateway port"),
                  COL_ERROR)]
    else:
        head = "gateway: running  PID %s" % gw["pid"]
        if gw["name"]:
            head += " %s" % gw["name"]
        if gw["uptime"] is not None:
            head += "  up %s" % _fmt_duration(gw["uptime"])
        head += "  |  %s" % where
        colour = COL_MUTED
        if health:
            head += "/health: %s" % health[1]
            if not health[0]:
                colour = COL_WARN
        lines = [(head, colour)]
    lines += list(run_lines) or [("  OpenClaw watcher: idle", COL_FAINT)]
    queue_ = snap.get("queue") or []
    if queue_:
        running = 1 if run_lines else 0
        lines.append(("  queue: %d pending (%d running, %d waiting)  oldest "
                      "%s since %s" % (len(queue_), running,
                                       len(queue_) - running, queue_[0][0],
                                       _fmt_when(queue_[0][1], now)),
                      COL_MUTED))
    else:
        lines.append(("  queue: empty", COL_FAINT))
    last = snap.get("last_result")
    if last:
        lines.append(("  last result: %s %s (%s)  at %s, %s"
                      % (last["task_id"], last["verdict"], last["why"],
                         _fmt_when(last["when"], now),
                         "emailed" if last["emailed"] else "email pending"),
                      {"PASS": COL_OK, "FAIL": COL_ERROR}.get(last["verdict"],
                                                           COL_WARN)))
    else:
        lines.append(("  last result: none yet", COL_FAINT))
    d_id, d_time = snap.get("dispatch") or (None, None)
    lines.append(("  last dispatch: %s at %s" % (d_id, _fmt_when(d_time, now))
                  if d_id else "  last dispatch: none yet", COL_MUTED))
    log = snap["log"]
    if log["log_time"] is None:
        lines.append(("  gateway log: none found in %s" % log["log_dir"],
                      COL_ERROR))
    else:
        text = "  gateway log: written %s (%ds ago)" % (
            _fmt_when(log["log_time"], now),
            max(0, int(now - log["log_time"])))
        if log["plugin_time"] is not None:
            text += "  |  plugin %s %s" % (_fmt_when(log["plugin_time"], now),
                                           log["plugin_text"])
        lines.append((text, COL_MUTED))
    return lines


class BridgeGui:
    def __init__(self, root, config_path, bridge_factory=None,
                 manage_consoles=True):
        """bridge_factory(config_path, log) builds the engine (tests pass
        one with fake mail transports); manage_consoles=False leaves console
        windows alone."""
        self.root = root
        self.config_path = config_path
        self._bridge_factory = bridge_factory or (
            lambda path, log: Bridge(path, log=log))
        self.log_queue = queue.Queue()
        self.bridge = None
        self.worker = None
        self.stop_event = None
        self.tray_icon = None
        self._cfg = bridge_core.default_config()
        self.manage_consoles = manage_consoles and HAVE_WIN32
        self.consoles = ConsoleWindows()
        if not self.manage_consoles:
            self.consoles.own = None
        self.console_hidden = self.manage_consoles
        if self.manage_consoles:
            self.consoles.hide_own()
        self._console_stop = threading.Event()

        apply_theme(root)
        root.title(APP_TITLE)
        root.geometry(WINDOW_GEOMETRY)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_scroll_area()
        self._build_status()
        self._build_settings()
        self._build_routing()
        self._build_devices()
        self._build_workspaces()
        self._build_web_publishing()
        self._build_buttons()
        self._build_log()

        self.load_settings_to_fields()
        self.refresh_autostart_checkbox()
        self.log("Bridge GUI ready. Press Start to begin polling.")
        if self.consoles.own:
            self.log("Console window hidden -- press 'Show console' to see it.")
        if not HAVE_TRAY:
            self.log("Tip: 'pip install pystray pillow' enables minimize-to-tray.")
        self.root.after(QUEUE_PUMP_MS, self._pump_queue)
        self.root.after(FIRST_ROUTE_REFRESH_MS, self._refresh_route_status)
        if self.manage_consoles:
            threading.Thread(target=self._console_watch, name="console-watch",
                             daemon=True).start()

    # ------------------------------------------------------------------
    # layout
    # ------------------------------------------------------------------
    def _build_scroll_area(self):
        # Every section lives in self.body, a frame inside a canvas with a
        # vertical scrollbar, so content taller than the window stays
        # reachable. The body is stretched to at least the window height so
        # the Activity log still fills spare room on a tall window.
        self.scroll_bar = tk.Scrollbar(self.root, orient="vertical")
        self.scroll_bar.pack(side="right", fill="y")
        self.scroll_canvas = tk.Canvas(self.root, highlightthickness=0,
                                       borderwidth=0,
                                       yscrollcommand=self.scroll_bar.set)
        self.scroll_canvas.pack(side="left", fill="both", expand=True)
        self.scroll_bar.config(command=self.scroll_canvas.yview)
        self.body = tk.Frame(self.scroll_canvas)
        self._body_item = self.scroll_canvas.create_window(
            0, 0, window=self.body, anchor="nw")
        self._body_size = None
        self.scroll_canvas.bind("<Configure>", lambda _e: self._sync_scroll())
        self.root.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self.root.after(SCROLL_SYNC_MS, self._scroll_watch)

    def _sync_scroll(self):
        """Size the body to the canvas width and max(content, canvas)
        height, and set the scroll region to match."""
        c = self.scroll_canvas
        size = (c.winfo_width(),
                max(self.body.winfo_reqheight(), c.winfo_height()))
        if size != self._body_size:
            self._body_size = size
            c.itemconfigure(self._body_item, width=size[0], height=size[1])
            c.configure(scrollregion=(0, 0, size[0], size[1]))

    def _scroll_watch(self):
        # Sections grow and shrink at runtime (executor slots, device and
        # route rows); Tk raises no event for a change in requested height.
        self._sync_scroll()
        self.root.after(SCROLL_SYNC_MS, self._scroll_watch)

    def _on_mousewheel(self, event):
        w = event.widget
        if not isinstance(w, tk.Misc) or w.winfo_toplevel() is not self.root:
            return
        if w.winfo_class() in ("Text", "Listbox", "Scrollbar"):
            return
        self.scroll_canvas.yview_scroll(int(-event.delta / 120) or
                                        (-1 if event.delta > 0 else 1),
                                        "units")

    def _build_status(self):
        frame = tk.LabelFrame(self.body, text="Status", padx=12, pady=8)
        frame.pack(fill="x", padx=12, pady=(12, 5))

        self.status_dot = tk.Label(frame, text="\u25cf", fg=COL_DOT_OFF,
                                   font=(FONT_UI, 18))
        self.status_dot.grid(row=0, column=0, rowspan=2, padx=(0, 10))
        self.status_text = tk.Label(frame, text="Stopped",
                                    font=(FONT_UI_SEMIBOLD, 13),
                                    fg=COL_HEADING)
        self.status_text.grid(row=0, column=1, sticky="w")
        self.counters_text = tk.Label(frame, text="tasks: 0   results sent: 0",
                                      fg=COL_MUTED)
        self.counters_text.grid(row=1, column=1, sticky="w")
        self.poll_text = tk.Label(frame, text="last poll: never",
                                  fg=COL_MUTED)
        self.poll_text.grid(row=0, column=2, sticky="e", padx=(20, 0))
        self.error_text = tk.Label(frame, text="", fg=COL_ERROR,
                                   wraplength=320, justify="left")
        self.error_text.grid(row=1, column=2, sticky="e", padx=(20, 0))
        frame.columnconfigure(2, weight=1)

    def _build_settings(self):
        frame = tk.LabelFrame(self.body, text="Bridge settings", padx=12,
                              pady=8)
        frame.pack(fill="x", padx=12, pady=5)

        self.var_email = tk.StringVar()
        self.var_password = tk.StringVar()
        self.var_poll = tk.IntVar(value=bridge_core.POLL_SECONDS_DEFAULT)
        self.var_device_tag = tk.StringVar()
        self.var_allowfile = tk.StringVar()
        self.var_senders = tk.StringVar()
        self.var_autostart = tk.BooleanVar(value=False)
        self.var_show_pw = tk.BooleanVar(value=False)

        r = 0
        tk.Label(frame, text="Bridge email:").grid(row=r, column=0, sticky="e",
                                                   padx=(0, 8))
        tk.Entry(frame, textvariable=self.var_email, width=42).grid(
            row=r, column=1, columnspan=2, sticky="we", pady=3)
        r += 1
        tk.Label(frame, text="App password:").grid(row=r, column=0, sticky="e",
                                                   padx=(0, 8))
        self.pw_entry = tk.Entry(frame, textvariable=self.var_password,
                                 width=42, show="\u2022")
        self.pw_entry.grid(row=r, column=1, sticky="we", pady=3)
        tk.Checkbutton(frame, text="show", variable=self.var_show_pw,
                       command=self._toggle_pw).grid(row=r, column=2, sticky="w",
                                                     padx=(6, 0))
        r += 1
        tk.Label(frame, text="Poll every (sec):").grid(row=r, column=0,
                                                       sticky="e", padx=(0, 8))
        pollf = tk.Frame(frame)
        pollf.grid(row=r, column=1, columnspan=2, sticky="w", pady=3)
        tk.Spinbox(pollf, from_=bridge_core.POLL_SECONDS_MIN, to=3600,
                   textvariable=self.var_poll,
                   width=8).pack(side="left")
        tk.Label(pollf, text="   Device tag:").pack(side="left", padx=(0, 6))
        tk.Entry(pollf, textvariable=self.var_device_tag, width=14).pack(
            side="left", padx=2)
        tk.Label(pollf, text="blank = this computer's name",
                 fg=COL_MUTED).pack(side="left", padx=6)
        r += 1
        tk.Label(frame, text="Workspace allowlist:").grid(row=r, column=0,
                                                         sticky="e",
                                                         padx=(0, 8))
        tk.Entry(frame, textvariable=self.var_allowfile, width=42).grid(
            row=r, column=1, sticky="we", pady=3)
        tk.Button(frame, text="Browse",
                  command=self._browse_allowfile).grid(row=r, column=2,
                                                       sticky="w", padx=(6, 0))
        r += 1
        tk.Label(frame, text="Allowed senders:").grid(row=r, column=0,
                                                      sticky="e", padx=(0, 8))
        tk.Entry(frame, textvariable=self.var_senders, width=42).grid(
            row=r, column=1, columnspan=2, sticky="we", pady=3)
        tk.Label(frame, text="comma-separated email addresses",
                 fg=COL_MUTED).grid(row=r + 1, column=1, columnspan=2,
                                    sticky="w")
        r += 2
        tk.Checkbutton(frame, text="Start bridge when Windows starts",
                       variable=self.var_autostart,
                       command=self._apply_autostart).grid(
                           row=r, column=0, columnspan=3, sticky="w", pady=(6, 0))
        frame.columnconfigure(1, weight=1)

    def _build_routing(self):
        """Executor routing: one row per executor --
        cursor  [inbox folder     ] [Browse] [Open folder] [Send probe task]
                watcher: alive ... | last task seen: ...
        A task's "executor" field picks the row ("cursor" when missing);
        an executor with no row is refused with a skip-notice email."""
        wrap = tk.LabelFrame(
            self.body, text="Executor routing (task \"executor\" -> inbox "
                            "folder; unknown executors get a skip notice)",
            padx=12, pady=8)
        wrap.pack(fill="x", padx=12, pady=5)
        self.routing_wrap = wrap
        self.routes = {}
        self.route_vars = {}
        self.route_status = {}
        self.route_slots = {}
        self._slot_seen = {}
        self._oc_runs = {}
        self._oc_health = {}  # port -> (ok, detail, unix time of the probe)
        self._oc_health_busy = False
        self._routes_box = None
        self._route_add_frame = None
        self._refresh_route_rows()
        addf = tk.Frame(wrap)
        addf.pack(fill="x", pady=(8, 0))
        self._route_add_frame = addf
        tk.Label(addf, text="Add executor:").pack(side="left", padx=(0, 6))
        self.var_new_exec = tk.StringVar()
        tk.Entry(addf, textvariable=self.var_new_exec, width=14).pack(
            side="left", padx=2)
        tk.Button(addf, text="Add", width=6,
                  command=self._add_executor).pack(side="left", padx=4)
        tk.Label(addf, text="lowercase name; inbox defaults to "
                            "<profile>\\<name>-inbox",
                 fg=COL_MUTED).pack(side="left", padx=6)

    def _refresh_route_rows(self):
        if self._routes_box is not None:
            self._routes_box.destroy()
        box = tk.Frame(self.routing_wrap)
        if self._route_add_frame is not None:
            box.pack(fill="x", before=self._route_add_frame)
        else:
            box.pack(fill="x")
        self._routes_box = box
        self.route_vars = {}
        self.route_status = {}
        self.route_slots = {}
        default_ex = self._cfg.get("default_executor",
                                   bridge_core.DEFAULT_EXECUTOR)
        for executor, path in self.routes.items():
            row = tk.Frame(box)
            row.pack(fill="x", pady=(4, 0))
            label = executor + (" *" if executor == default_ex else "")
            tk.Label(row, text=label, width=10, anchor="w",
                     font=(FONT_UI_SEMIBOLD, 10),
                     fg=COL_HEADING).pack(side="left")
            var = tk.StringVar(value=path)
            self.route_vars[executor] = var
            tk.Entry(row, textvariable=var, width=34).pack(
                side="left", padx=2, fill="x", expand=True)
            tk.Button(row, text="Browse",
                      command=lambda v=var: self._browse(v)).pack(
                          side="left", padx=2)
            tk.Button(row, text="Open folder",
                      command=lambda e=executor: self._open_route(e)).pack(
                          side="left", padx=2)
            tk.Button(row, text="Send probe task",
                      command=lambda e=executor: self._send_probe(e)).pack(
                          side="left", padx=2)
            if executor != default_ex:
                tk.Button(row, text="Remove",
                          command=lambda e=executor: self._remove_executor(e)
                          ).pack(side="left", padx=2)
            status = tk.Label(box, text="watcher: ...", fg=COL_MUTED,
                              anchor="w", justify="left")
            status.pack(fill="x", padx=(84, 0))
            self.route_status[executor] = status
            slots = tk.Frame(box)
            slots.pack(fill="x", padx=(84, 0))
            self.route_slots[executor] = {"frame": slots, "labels": []}

    def _staged_routes(self):
        """Routing rows as {executor: absolute path}; raises ValueError."""
        out = {}
        for executor, var in self.route_vars.items():
            raw = var.get().strip()
            if not raw:
                raise ValueError("Inbox folder for '%s' is empty." % executor)
            out[executor] = bridge_core.resolve_path(raw)
        if not out:
            raise ValueError("At least one executor inbox is required.")
        return out

    def _add_executor(self):
        name = self.var_new_exec.get().strip().lower()
        if not bridge_core.valid_executor(name):
            messagebox.showerror(
                APP_TITLE, "Executor names are lowercase letters, digits, "
                           "'.', '_' or '-' (max 32), e.g. openclaw.")
            return
        if name in self.route_vars:
            messagebox.showerror(APP_TITLE,
                                 "Executor '%s' already has a row." % name)
            return
        try:
            self.routes = self._staged_routes()
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self.routes[name] = bridge_core.resolve_path(
            os.path.join(bridge_core.HOME_TOKEN, "%s-inbox" % name))
        self._refresh_route_rows()
        self.var_new_exec.set("")
        if self.save_settings(quiet=True):
            self.log("Executor '%s' added -> %s" % (name, self.routes[name]))

    def _remove_executor(self, executor):
        if not messagebox.askyesno(
                APP_TITLE, "Remove executor '%s'?\nTasks for it will then be "
                           "refused with a skip notice. Its folder is not "
                           "deleted." % executor):
            return
        try:
            routes = self._staged_routes()
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        routes.pop(executor, None)
        self.routes = routes
        self._refresh_route_rows()
        if self.save_settings(quiet=True):
            self.log("Executor '%s' removed from routing." % executor)

    def _open_route(self, executor):
        var = self.route_vars.get(executor)
        path = bridge_core.resolve_path(var.get()) if var else ""
        if path and not os.path.isdir(path):
            messagebox.showerror(APP_TITLE, "Folder does not exist yet "
                                            "(Save settings creates it):\n%s"
                                 % path)
            return
        self._open_folder(path)

    def _send_probe(self, executor):
        if not self.save_settings(quiet=True):
            return
        try:
            cfg = bridge_core.load_config(self.config_path)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, "Config error:\n%s" % exc)
            return
        if executor not in cfg["inboxes"]:
            return
        workspace = bridge_core.BRIDGE_DIR
        if executor == bridge_core.DEFAULT_EXECUTOR and \
                not workspace_allowed(self.ws_entries, workspace):
            messagebox.showerror(
                APP_TITLE, "The cursor runner only runs tasks in approved "
                           "workspaces. Approve this folder first:\n%s"
                % workspace)
            return
        if not messagebox.askyesno(
                APP_TITLE,
                "Drop a probe task into the '%s' inbox?\n\n%s\n\nThe executor "
                "runs it (a short read-only request) and the bridge emails "
                "the result as %s probe-%s-..."
                % (executor, cfg["inboxes"][executor],
                   bridge_core.RESULT_SUBJECT_PREFIX, executor)):
            return
        try:
            task_id, path = bridge_core.write_probe_task(cfg, executor,
                                                         workspace)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, "Could not write probe:\n%s" % exc)
            return
        self.log("Probe task %s -> %s" % (task_id, path))

    def _route_status_text(self, executor, path, quick):
        """(text, colour) for one inbox's watcher line."""
        running = bool(self.worker and self.worker.is_alive())
        st = None
        if self.bridge is not None:
            st = self.bridge.stats.get("inboxes", {}).get(executor)
        colour = COL_MUTED
        if not path or not os.path.isdir(path):
            watcher, colour = "watcher: FOLDER MISSING", COL_ERROR
        elif not running:
            watcher, colour = ("watcher: stopped (bridge not running)",
                               COL_ERROR)
        elif st and st.get("last_scan") and \
                time.time() - st["last_scan"] <= max(20, 3 * quick):
            watcher = "watcher: alive (checked %ds ago" % max(
                0, int(time.time() - st["last_scan"]))
            if st.get("pending"):
                watcher += ", %d result(s) to send" % st["pending"]
            if st.get("undeliverable"):
                watcher += (", %d result(s) refused by Gmail -- see %s"
                            % (st["undeliverable"],
                               bridge_core.UNDELIVERABLE_TRACKER_NAME))
            watcher += ")"
            colour = COL_OK
        else:
            watcher = "watcher: starting..."
        disk_id, disk_time = bridge_core.newest_task_file(path) \
            if path else (None, None)
        last_id, last_time = disk_id, disk_time
        if st and st.get("last_task_time") and \
                (last_time is None or st["last_task_time"] >= last_time):
            last_id, last_time = st["last_task"], st["last_task_time"]
        if last_id:
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_time))
            last = "last task seen: %s (%s)" % (last_id, when)
        else:
            last = "last task seen: none"
        waiting = 0
        try:
            waiting = sum(1 for n in os.listdir(path)
                          if n.endswith(bridge_core.TASK_FILE_SUFFIX))
        except OSError:
            pass
        pool = bridge_core.read_executor_slots(path) if path else None
        oc_run = None
        if pool and pool["live"]:
            # Packages stay in the inbox while their slot runs them.
            waiting = max(0, waiting - pool["running"])
        elif waiting and executor == "openclaw":
            # The watcher runs one package at a time and moves it to done\
            # only when it finishes.
            oc_run = bridge_core.openclaw_bridge_run()
            if oc_run:
                waiting -= 1
                oc_run["task_id"] = bridge_core.oldest_pending_task(path)
        self._oc_runs[executor] = oc_run
        text = "%s  |  %s" % (watcher, last)
        if waiting:
            text += "  |  %d waiting for the executor" % waiting
        return text, colour

    def _refresh_route_status(self):
        try:
            quick = int(self._cfg.get(
                "results_poll_seconds",
                bridge_core.RESULTS_POLL_SECONDS_DEFAULT))
            for executor, label in self.route_status.items():
                text, colour = self._route_status_text(
                    executor, self.routes.get(executor, ""), quick)
                label.config(text=text, fg=colour)
                self._refresh_slots(executor, self.routes.get(executor, ""))
        except Exception as exc:  # status is cosmetic; never kill the timer
            self.log("Routing status: %s" % exc)
        self.root.after(ROUTE_REFRESH_MS, self._refresh_route_status)

    @staticmethod
    def _slot_lines(pool):
        """[(text, colour)] for the executor-pool rows; [] when the inbox's
        runner has never reported slots (e.g. an openclaw watcher)."""
        if not pool:
            return []
        if pool["live"]:
            head = ("executor pool: %d/%d running, %d queued  (runner PID %s)"
                    % (pool["running"], pool["max_slots"], pool["queued"],
                       pool["runner_pid"]))
            out = [(head, COL_MUTED)]
        else:
            age = pool["age"]
            when = "%ds ago" % age if age is not None else "at an unknown time"
            out = [("executor pool: runner not reporting (last update %s, "
                    "PID %s) -- slots unknown" % (when, pool["runner_pid"]),
                    COL_ERROR)]
        if pool.get("heartbeat_stale_seconds"):
            out[0] = (out[0][0] + "  |  heartbeat limit %ds, %d stale "
                      "worker(s) failed" % (pool["heartbeat_stale_seconds"],
                                            pool.get("heartbeat_failures", 0)),
                      out[0][1])
        for s in pool["slots"]:
            if s["state"] == "running":
                el = s["elapsed"]
                took = " (%d:%02d)" % divmod(el, 60) if el is not None else ""
                hb, colour = s.get("heartbeat_age"), COL_OK
                limit = pool.get("heartbeat_stale_seconds")
                if hb is not None:
                    took += "  heartbeat %ds ago" % hb
                    if limit and hb > min(120, limit // 2):
                        colour = COL_WARN if hb <= limit else COL_ERROR
                elif limit:
                    took += "  heartbeat: none yet"
                out.append(("  slot %s: RUNNING %s%s"
                            % (s["slot"], s["task_id"], took), colour))
            else:
                out.append(("  slot %s: idle" % s["slot"], COL_FAINT))
        return out

    def _refresh_slots(self, executor, path):
        entry = self.route_slots.get(executor)
        if entry is None:
            return
        pool = bridge_core.read_executor_slots(path) if path else None
        lines = self._slot_lines(pool)
        oc_run = self._oc_runs.get(executor)
        if not lines and oc_run:
            lines = [("  OpenClaw watcher: RUNNING %s (%d:%02d)  run started "
                      "%s" % ((oc_run.get("task_id") or "a package",)
                              + divmod(oc_run["elapsed"], 60)
                              + (time.strftime("%H:%M:%S", time.localtime(
                                  oc_run["started_unix"])),)), COL_OK)]
        if not pool and executor == "openclaw":
            try:
                lines = self._openclaw_lines(executor, path, oc_run, lines)
            except Exception as exc:  # never take the other rows down
                lines = [("openclaw readouts failed: %s: %s"
                          % (type(exc).__name__, exc), COL_ERROR)] + lines
        labels = entry["labels"]
        while len(labels) < len(lines):
            lbl = tk.Label(entry["frame"], anchor="w", justify="left",
                           font=(FONT_UI, 9))
            lbl.pack(fill="x")
            labels.append(lbl)
        while len(labels) > len(lines):
            labels.pop().destroy()
        for lbl, (text, colour) in zip(labels, lines):
            lbl.config(text=text, fg=colour)
        # Slot transitions go to the activity log as well.
        now = {}
        if pool and pool["live"]:
            now = {s["slot"]: s["task_id"] for s in pool["slots"]}
        seen = self._slot_seen.get(executor)
        if seen is not None:
            for slot, task_id in sorted(now.items(), key=lambda kv: str(kv[0])):
                if task_id and seen.get(slot) != task_id:
                    self.log("Executor %s slot %s: running %s"
                             % (executor, slot, task_id))
                elif not task_id and seen.get(slot):
                    self.log("Executor %s slot %s: idle (%s finished)"
                             % (executor, slot, seen[slot]))
        self._slot_seen[executor] = now

    def _openclaw_lines(self, executor, path, oc_run, run_lines):
        """Live readouts for the openclaw row (refreshed with the rest of
        the routing status)."""
        port, source = openclaw_gateway_port()
        self._probe_gateway_health(port)
        cfg = self._cfg
        results_dir = cfg.get("results_dirs", {}).get(executor) or path
        sent = _read_json_cached(os.path.join(
            os.path.dirname(self.config_path), bridge_core.SENT_TRACKER_NAME))
        sent = set(sent) if isinstance(sent, list) else set()
        sent |= set(getattr(self.bridge, "_sent_mem", None) or ())
        if executor in cfg.get("inboxes", {}):
            key_for = lambda name: bridge_core.sent_key(cfg, executor, name)
        else:
            key_for = lambda name: "%s/%s" % (executor, name)
        snap = {
            "gateway": openclaw_gateway_state(port),
            "port_source": source,
            "health": self._oc_health.get(port),
            "run": oc_run,
            "queue": openclaw_queue(path) if path else [],
            "last_result": openclaw_last_result(results_dir, sent, key_for)
            if results_dir else None,
            "dispatch": bridge_core.newest_task_file(path) if path
            else (None, None),
            "log": openclaw_log_activity(),
        }
        return openclaw_readout_lines(snap, run_lines)

    def _probe_gateway_health(self, port):
        """GET /health off the Tk thread; the next refresh shows it."""
        if self._oc_health_busy:
            return
        self._oc_health_busy = True

        def probe():
            try:
                ok, detail = openclaw_gateway_health(port)
                self._oc_health[port] = (ok, detail, time.time())
            finally:
                self._oc_health_busy = False
        threading.Thread(target=probe, name="openclaw-health",
                         daemon=True).start()

    def _build_devices(self):
        """Device registry: one compact row per device --
        D12 = [10.0.0.12         ] [Set]  name
        The input box shows the current IP; edit it and press Set to validate
        and save (empty + Set clears it). An add-device row registers further
        machines."""
        wrap = tk.LabelFrame(self.body, text="Devices", padx=12, pady=8)
        wrap.pack(fill="x", padx=12, pady=5)
        self.devices_wrap = wrap
        self.devices = {}
        self.device_ip_vars = {}
        self._rows_box = None
        self._add_frame = None
        self._refresh_device_rows()

        addf = tk.Frame(wrap)
        addf.pack(fill="x", pady=(8, 0))
        self._add_frame = addf
        tk.Label(addf, text="Add:").pack(side="left", padx=(0, 6))
        self.var_new_id = tk.StringVar()
        tk.Entry(addf, textvariable=self.var_new_id, width=6).pack(
            side="left", padx=2)
        self.var_new_name = tk.StringVar()
        tk.Entry(addf, textvariable=self.var_new_name, width=12).pack(
            side="left", padx=2)
        self.var_new_ip = tk.StringVar()
        tk.Entry(addf, textvariable=self.var_new_ip, width=16).pack(
            side="left", padx=2)
        tk.Button(addf, text="Add device",
                  command=self._add_device).pack(side="left", padx=4)
        tk.Label(addf, text="new: ID, name, IP",
                 fg=COL_MUTED).pack(side="left", padx=6)

    def _refresh_device_rows(self):
        # Compact rows:  D12 = [10.0.0.12         ] [Set]  name
        # The input box shows the current IP; editing it and pressing Set
        # validates and saves. Empty input + Set clears the IP (unset).
        if self._rows_box is not None:
            self._rows_box.destroy()
        box = tk.Frame(self.devices_wrap)
        if self._add_frame is not None:
            box.pack(fill="x", before=self._add_frame)
        else:
            box.pack(fill="x")
        self._rows_box = box
        self.device_ip_vars = {}
        for dev_id in sorted(self.devices.keys()):
            dev = self.devices[dev_id]
            row = tk.Frame(box)
            row.pack(fill="x", pady=2)
            tk.Label(row, text="%s =" % dev_id, width=6, anchor="w",
                     font=(FONT_UI_SEMIBOLD, 10),
                     fg=COL_HEADING).pack(side="left")
            var = tk.StringVar(value=dev.get("ip", ""))
            self.device_ip_vars[dev_id] = var
            tk.Entry(row, textvariable=var, width=18,
                     font=(FONT_UI, 10)).pack(side="left", padx=2)
            tk.Button(row, text="Set", width=6,
                      command=lambda d=dev_id: self._set_device_ip(d)
                      ).pack(side="left", padx=4)
            name = dev.get("name", "")
            if name and name != dev_id:
                tk.Label(row, text=name, fg=COL_FAINT).pack(side="left",
                                                            padx=6)

    def _set_device_ip(self, dev_id):
        raw = self.device_ip_vars[dev_id].get().strip()
        if raw and not bridge_core.valid_host(raw):
            messagebox.showerror(
                APP_TITLE,
                "Not a valid IP address or hostname:\n%s" % raw)
            return
        self.devices[dev_id]["ip"] = raw
        if not self.save_settings(quiet=True):
            return
        self.log("Device %s IP set to %s."
                 % (dev_id, raw if raw else "(not set)"))
        self._refresh_device_rows()

    def _add_device(self):
        dev_id = self.var_new_id.get().strip().upper()
        name = self.var_new_name.get().strip()
        ip = self.var_new_ip.get().strip()
        if not bridge_core.valid_device_id(dev_id):
            messagebox.showerror(
                APP_TITLE,
                "Device ID must look like D12 (a D followed by digits).")
            return
        if dev_id in self.devices:
            messagebox.showerror(
                APP_TITLE, "Device %s is already registered." % dev_id)
            return
        if ip and not bridge_core.valid_host(ip):
            messagebox.showerror(
                APP_TITLE,
                "Not a valid IP address or hostname:\n%s" % ip)
            return
        self.devices[dev_id] = {"name": name or dev_id, "ip": ip}
        self.var_new_id.set("")
        self.var_new_name.set("")
        self.var_new_ip.set("")
        if not self.save_settings(quiet=True):
            return
        self.log("Device %s (%s) added."
                 % (dev_id, self.devices[dev_id]["name"]))
        self._refresh_device_rows()

    def _stage_device_entries(self):
        """Fold any typed-but-unset device IPs into the registry before a
        save, so typing an IP and hitting 'Save settings' doesn't lose it.
        Invalid entries are left alone and logged."""
        for dev_id, var in self.device_ip_vars.items():
            raw = var.get().strip()
            if raw == self.devices[dev_id].get("ip", ""):
                continue
            if raw and not bridge_core.valid_host(raw):
                self.log("Device %s: ignoring invalid IP %r "
                         "(fix it and press Set)." % (dev_id, raw))
                continue
            self.devices[dev_id]["ip"] = raw

    def _build_workspaces(self):
        """Approved Workspaces: the entries of allowed-workspaces.txt (in
        the inbox folder), the allowlist Invoke-JarvisTask.ps1 enforces.
        Add/Remove write the file immediately. If the file can't be read
        the list stays empty and editing is disabled (fail closed)."""
        wrap = tk.LabelFrame(self.body, text="Approved Workspaces",
                             padx=12, pady=8)
        wrap.pack(fill="x", padx=12, pady=5)
        self.ws_path = None
        self.ws_entries = []
        self.var_ws_file = tk.StringVar(value="")
        tk.Label(wrap, textvariable=self.var_ws_file, fg=COL_MUTED,
                 anchor="w", justify="left").pack(fill="x")
        self.ws_error = tk.Label(wrap, text="", fg=COL_ERROR, anchor="w",
                                 justify="left", wraplength=660)
        self.ws_error.pack(fill="x")

        listf = tk.Frame(wrap)
        listf.pack(fill="x", pady=(2, 0))
        self.ws_list = tk.Listbox(listf, height=4, activestyle="dotbox",
                                  exportselection=False,
                                  font=(FONT_UI, 10))
        sb = tk.Scrollbar(listf, orient="vertical",
                          command=self.ws_list.yview)
        self.ws_list.config(yscrollcommand=sb.set)
        self.ws_list.pack(side="left", fill="x", expand=True)
        sb.pack(side="left", fill="y")

        addf = tk.Frame(wrap)
        addf.pack(fill="x", pady=(8, 0))
        self.var_new_ws = tk.StringVar()
        tk.Entry(addf, textvariable=self.var_new_ws, width=40,
                 font=(FONT_UI, 10)).pack(side="left", padx=2)
        tk.Button(addf, text="Browse",
                  command=self._browse_workspace).pack(side="left", padx=3)
        self.btn_ws_add = tk.Button(addf, text="Add",
                                    command=self._add_workspace, width=6)
        self.btn_ws_add.pack(side="left", padx=3)
        self.btn_ws_remove = tk.Button(addf, text="Remove selected",
                                       command=self._remove_workspace)
        self.btn_ws_remove.pack(side="left", padx=3)
        tk.Button(addf, text="Reload",
                  command=self._load_workspaces).pack(side="left", padx=3)

    def _show_workspaces(self, entries, error=""):
        self.ws_entries = list(entries)
        self.ws_list.delete(0, "end")
        for e in self.ws_entries:
            self.ws_list.insert("end", e)
        self.ws_error.config(text=error)
        state = "disabled" if error else "normal"
        self.btn_ws_add.config(state=state)
        self.btn_ws_remove.config(state=state)

    def _load_workspaces(self):
        raw = self.var_allowfile.get().strip()
        self.ws_path = bridge_core.resolve_path(raw) if raw else \
            self._cfg.get("allowed_workspaces") or allowlist_path("")
        self.var_ws_file.set("%s  --  tasks may only run in these folders "
                             "(and their subfolders)" % self.ws_path)
        try:
            doc = read_allowlist(self.ws_path)
        except AllowlistError as exc:
            self._show_workspaces(
                [], "Allowlist unavailable -- editing disabled, nothing "
                    "changed on disk. %s" % exc)
            self.log("Approved workspaces: %s" % exc)
            return
        self._show_workspaces(allowlist_entries(doc))

    def _add_workspace(self):
        try:
            line, doc = add_allowlist_entry(self.ws_path,
                                            self.var_new_ws.get())
        except (AllowlistError, OSError) as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            self._load_workspaces()
            return
        self.var_new_ws.set("")
        self._show_workspaces(allowlist_entries(doc))
        self.log("Workspace approved: %s" % line)

    def _remove_workspace(self):
        sel = self.ws_list.curselection()
        if not sel:
            messagebox.showerror(APP_TITLE, "Select a workspace to remove.")
            return
        entry = self.ws_entries[sel[0]]
        try:
            removed, doc = remove_allowlist_entry(self.ws_path, entry)
        except (AllowlistError, OSError) as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            self._load_workspaces()
            return
        self._show_workspaces(allowlist_entries(doc))
        self.log("Workspace removed: %s%s" % (
            entry, "" if removed == 1 else " (%d lines)" % removed))

    def _browse_workspace(self):
        d = filedialog.askdirectory(
            initialdir=self.var_new_ws.get() or os.path.expanduser("~"))
        if d:
            self.var_new_ws.set(d.replace("/", "\\"))

    def _build_web_publishing(self):
        """Web Publishing: the "webgui" section of bridge.config.json. The web
        dashboard re-reads it every few seconds: LAN only binds its LAN
        address for home-network clients; Public binds every interface for
        any client and, with a tunnel token, runs cloudflared. Written by
        'Save settings'."""
        wrap = tk.LabelFrame(self.body, text="Web Publishing", padx=12,
                             pady=8)
        wrap.pack(fill="x", padx=12, pady=5)
        self.web_settings_path = bridge_core.SETTINGS_PATH
        self._web_loaded = dict(WEBGUI_DEFAULTS)
        self.var_web_mode = tk.StringVar(value="lan")
        self.var_web_address = tk.StringVar()
        self.var_cf_hostname = tk.StringVar()
        self.var_cf_token = tk.StringVar()
        self.var_show_token = tk.BooleanVar(value=False)

        r = 0
        tk.Label(wrap, text="Broadcast mode:").grid(row=r, column=0,
                                                    sticky="e", padx=(0, 8))
        seg = tk.Frame(wrap, bg=COL_BUTTON)
        seg.grid(row=r, column=1, columnspan=2, sticky="w", pady=3)
        self.web_mode_buttons = {}
        for mode, label in (("lan", "LAN only"), ("public", "Public")):
            b = tk.Radiobutton(
                seg, text=label, value=mode, variable=self.var_web_mode,
                command=self._sync_web_mode, indicatoron=0, width=11,
                relief="flat", offrelief="flat", overrelief="flat",
                borderwidth=0, highlightthickness=0, padx=6, pady=4,
                bg=COL_BUTTON, activebackground=COL_BUTTON_ACTIVE,
                selectcolor=COL_ACCENT, font=(FONT_UI_SEMIBOLD, 9),
                cursor="hand2")
            b.pack(side="left", padx=2, pady=2)
            self.web_mode_buttons[mode] = b
        r += 1
        self.web_mode_hint = tk.Label(wrap, text="", anchor="w",
                                      justify="left", wraplength=600)
        self.web_mode_hint.grid(row=r, column=1, columnspan=2, sticky="w",
                                pady=(0, 4))
        r += 1
        tk.Label(wrap, text="Web address:").grid(row=r, column=0, sticky="e",
                                                 padx=(0, 8))
        tk.Entry(wrap, textvariable=self.var_web_address, width=42).grid(
            row=r, column=1, sticky="we", pady=3)
        tk.Button(wrap, text="Open", width=6,
                  command=self._open_web_address).grid(row=r, column=2,
                                                       sticky="w", padx=(6, 0))
        r += 1
        tk.Label(wrap, text="Cloudflare hostname:").grid(row=r, column=0,
                                                         sticky="e",
                                                         padx=(0, 8))
        tk.Entry(wrap, textvariable=self.var_cf_hostname, width=42).grid(
            row=r, column=1, columnspan=2, sticky="we", pady=3)
        r += 1
        tk.Label(wrap, text="Tunnel token:").grid(row=r, column=0, sticky="e",
                                                  padx=(0, 8))
        self.cf_token_entry = tk.Entry(wrap, textvariable=self.var_cf_token,
                                       width=42, show="\u2022")
        self.cf_token_entry.grid(row=r, column=1, sticky="we", pady=3)
        tk.Checkbutton(wrap, text="show", variable=self.var_show_token,
                       command=self._toggle_token).grid(row=r, column=2,
                                                        sticky="w",
                                                        padx=(6, 0))
        r += 1
        tk.Label(wrap, text="Public + token runs cloudflared on this PC; "
                            "route the hostname to http://localhost:8787 in "
                            "Cloudflare Zero Trust. Saved by 'Save settings'; "
                            "the web dashboard applies it within ~5 s.",
                 fg=COL_MUTED, anchor="w", justify="left",
                 wraplength=600).grid(row=r, column=1, columnspan=2,
                                      sticky="w")
        self.web_error = tk.Label(wrap, text="", fg=COL_ERROR, anchor="w",
                                  justify="left", wraplength=600)
        self.web_error.grid(row=r + 1, column=1, columnspan=2, sticky="w")
        wrap.columnconfigure(1, weight=1)
        self._sync_web_mode()

    def _sync_web_mode(self):
        mode = self.var_web_mode.get()
        for value, b in self.web_mode_buttons.items():
            on = value == mode
            b.config(fg="#ffffff" if on else COL_TEXT,
                     activeforeground="#ffffff" if on else COL_HEADING,
                     activebackground=COL_ACCENT_ACTIVE if on
                     else COL_BUTTON_ACTIVE)
        if mode == "public":
            self.web_mode_hint.config(
                fg=COL_WARN,
                text="Public: the web dashboard listens on every network "
                     "interface and answers any client. It has no login -- "
                     "put Cloudflare Access in front of the hostname.")
        else:
            self.web_mode_hint.config(
                fg=COL_MUTED,
                text="LAN only: the web dashboard listens on this PC's "
                     "home-network address and drops clients outside the "
                     "home network. Nothing is exposed from this PC.")

    def _toggle_token(self):
        self.cf_token_entry.config(
            show="" if self.var_show_token.get() else "\u2022")

    def _open_web_address(self):
        url = self.var_web_address.get().strip()
        if not url:
            messagebox.showinfo(APP_TITLE, "Enter a web address first.")
            return
        webbrowser.open(url)

    def _web_fields(self):
        return {"broadcast_mode": self.var_web_mode.get().strip().lower(),
                "web_address": self.var_web_address.get().strip(),
                "cloudflare_hostname": self.var_cf_hostname.get().strip(),
                "cloudflare_tunnel_token": self.var_cf_token.get().strip()}

    def _load_web_settings(self):
        web, err = read_webgui_settings(self.web_settings_path)
        self._web_loaded = web
        self.var_web_mode.set(web["broadcast_mode"])
        self.var_web_address.set(web["web_address"])
        self.var_cf_hostname.set(web["cloudflare_hostname"])
        self.var_cf_token.set(web["cloudflare_tunnel_token"])
        self._sync_web_mode()
        self.web_error.config(
            text="Could not read %s (%s) -- showing defaults."
                 % (self.web_settings_path, err) if err else "")
        if err:
            self.log("Web publishing: %s not readable (%s)."
                     % (self.web_settings_path, err))

    def _save_web_settings(self):
        """Write the webgui section if it changed. False on error (shown)."""
        web = self._web_fields()
        if web == self._web_loaded:
            return True
        try:
            write_webgui_settings(self.web_settings_path, web)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, "Could not save web publishing "
                                 "settings to %s:\n%s"
                                 % (self.web_settings_path, exc))
            return False
        self._web_loaded = web
        self.web_error.config(text="")
        # the tunnel token is never echoed
        self.log("Web publishing saved: %s%s. The web dashboard applies it "
                 "within ~5 s." % (
                     "Public" if web["broadcast_mode"] == "public"
                     else "LAN only",
                     ", tunnel token set" if web["cloudflare_tunnel_token"]
                     else ""))
        return True

    def _build_buttons(self):
        # Packed ahead of every section and anchored to the bottom: the packer
        # sizes slaves in packing order, so a row packed after the sections is
        # pushed off-screen once they (status text, executor slots) outgrow
        # the fixed window height.
        frame = tk.Frame(self.body)
        frame.pack(side="bottom", fill="x", padx=12, pady=(8, 6),
                   before=self.body.pack_slaves()[0])
        self.btn_start = tk.Button(frame, text="Start bridge",
                                   command=self.start_bridge, width=14,
                                   bg="#dcfce7", fg="#166534",
                                   activebackground="#bbf7d0",
                                   activeforeground="#14532d",
                                   disabledforeground="#86b796",
                                   font=(FONT_UI_SEMIBOLD, 9))
        self.btn_start.pack(side="left", padx=3)
        self.btn_stop = tk.Button(frame, text="Stop", command=self.stop_bridge,
                                  width=10, state="disabled",
                                  bg="#fee2e2", fg="#b91c1c",
                                  activebackground="#fecaca",
                                  activeforeground="#991b1b",
                                  disabledforeground="#d9a3a3",
                                  font=(FONT_UI_SEMIBOLD, 9))
        self.btn_stop.pack(side="left", padx=3)
        tk.Button(frame, text="Test connection",
                  command=self.test_connection, width=14).pack(side="left",
                                                               padx=3)
        tk.Button(frame, text="Save settings",
                  command=self.save_settings, width=14,
                  bg=COL_ACCENT, fg="#ffffff",
                  activebackground=COL_ACCENT_ACTIVE,
                  activeforeground="#ffffff",
                  font=(FONT_UI_SEMIBOLD, 9)).pack(side="left", padx=3)
        tk.Button(frame, text="Open inbox folder",
                  command=self.open_inbox, width=14).pack(side="left", padx=3)

        frame2 = tk.Frame(self.body)
        frame2.pack(side="bottom", fill="x", padx=12, pady=(0, 12),
                    before=frame)
        tk.Button(frame2, text="Minimize to taskbar",
                  command=self.minimize_window, width=18).pack(side="left",
                                                               padx=3)
        self.btn_console = tk.Button(
            frame2, command=self.toggle_console, width=14,
            state="normal" if HAVE_WIN32 else "disabled")
        self.btn_console.pack(side="left", padx=3)
        self._sync_console_button()

    def _build_log(self):
        frame = tk.LabelFrame(self.body, text="Activity", padx=12, pady=8)
        frame.pack(fill="both", expand=True, padx=12, pady=(5, 6))
        self.log_widget = scrolledtext.ScrolledText(
            frame, height=8, state="disabled", wrap="word",
            font=(FONT_MONO, 9))
        self.log_widget.pack(fill="both", expand=True)

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------
    def load_settings_to_fields(self):
        try:
            cfg = bridge_core.load_config(self.config_path)
        except FileNotFoundError:
            cfg = bridge_core.default_config()
            cfg["inboxes"] = {k: bridge_core.resolve_path(v)
                              for k, v in cfg["inboxes"].items()}
            cfg["allowed_workspaces"] = bridge_core.resolve_path(
                cfg["allowed_workspaces"])
        self._cfg = cfg
        self.var_email.set(cfg.get("email", ""))
        self.var_password.set(cfg.get("app_password", ""))
        self.var_poll.set(cfg.get("poll_seconds",
                                  bridge_core.POLL_SECONDS_DEFAULT))
        self.var_device_tag.set(cfg.get("device_tag", ""))
        self.var_allowfile.set(cfg.get("allowed_workspaces", ""))
        self.var_senders.set(", ".join(cfg.get("allowed_senders", [])))
        self.root.title("%s -- %s" % (APP_TITLE, bridge_core.device_tag(cfg)))
        self.routes = dict(cfg["inboxes"])
        self._refresh_route_rows()
        self.devices = bridge_core.normalize_devices(cfg.get("devices"))
        self._refresh_device_rows()
        self._load_workspaces()
        self._load_web_settings()

    def fields_to_config(self):
        """Settings as saved: the loaded config with the GUI fields laid over
        it, so config-only settings (runner templates, overrides) survive."""
        senders = [s.strip() for s in self.var_senders.get().split(",") if s.strip()]
        inboxes = self._staged_routes()
        cfg = dict(self._cfg)
        cfg.update({
            "email": self.var_email.get().strip(),
            "app_password": self.var_password.get(),
            "poll_seconds": int(self.var_poll.get() or
                                bridge_core.POLL_SECONDS_DEFAULT),
            "device_tag": self.var_device_tag.get().strip(),
            "allowed_workspaces": bridge_core.resolve_path(
                self.var_allowfile.get()) or os.path.join(
                    inboxes.get(bridge_core.DEFAULT_EXECUTOR) or
                    next(iter(inboxes.values())), ALLOWLIST_NAME),
            "inboxes": inboxes,
            "results_dirs": {k: v for k, v in
                             self._cfg.get("results_dirs", {}).items()
                             if k in inboxes},
            "done_dirs": {k: v for k, v in
                          self._cfg.get("done_dirs", {}).items()
                          if k in inboxes},
            "allowed_senders": senders or [self.var_email.get().strip()],
            "devices": self.devices,
        })
        return cfg

    def save_settings(self, quiet=False):
        problems = validate_webgui_settings(self._web_fields())
        if problems:
            messagebox.showerror(APP_TITLE, "Web Publishing:\n" +
                                 "\n".join(problems))
            return False
        self._stage_device_entries()
        try:
            bridge_core.save_config(self.config_path, self.fields_to_config())
            cfg = bridge_core.load_config(self.config_path)
            created = bridge_core.ensure_layout(cfg)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, "Could not save settings:\n%s" % exc)
            return False
        self._cfg = cfg
        self.routes = dict(cfg["inboxes"])
        for executor, var in self.route_vars.items():
            var.set(self.routes.get(executor, var.get()))
        for path in created:
            self.log("Created %s" % path)
        self._load_workspaces()
        if not self._save_web_settings():
            return False
        if not quiet:
            # password is never echoed -- even masked, we don't repeat it
            self.log("Settings saved. They apply on the next poll cycle.")
        return True

    def _browse(self, var):
        d = filedialog.askdirectory(initialdir=var.get() or os.path.expanduser("~"))
        if d:
            var.set(os.path.normpath(d))

    def _browse_allowfile(self):
        current = self.var_allowfile.get().strip()
        f = filedialog.asksaveasfilename(
            initialdir=os.path.dirname(current) or os.path.expanduser("~"),
            initialfile=os.path.basename(current) or ALLOWLIST_NAME,
            confirmoverwrite=False, defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if f:
            self.var_allowfile.set(os.path.normpath(f))

    def _toggle_pw(self):
        self.pw_entry.config(show="" if self.var_show_pw.get() else "\u2022")

    def open_inbox(self):
        """Opens the default executor's inbox."""
        ex = self._cfg.get("default_executor", bridge_core.DEFAULT_EXECUTOR)
        self._open_route(ex if ex in self.route_vars
                         else next(iter(self.route_vars), ""))

    def _open_folder(self, path):
        if not path:
            return
        try:
            if os.name == "nt":
                os.startfile(path)  # noqa: PGH121
            elif sys.platform == "darwin":
                os.system("open %s" % _shq(path))
            else:
                os.system("xdg-open %s" % _shq(path))
        except Exception as exc:
            messagebox.showerror(APP_TITLE, "Could not open folder:\n%s" % exc)

    # ------------------------------------------------------------------
    # autostart (Windows logon): the per-user Scheduled Task that runs
    # Start-JarvisBridge.ps1 -IfNotRunning, managed only through
    # Register-JarvisBridgeAutostart.ps1. The older HKCU Run value still
    # counts as "on" so it can be switched off (the script deletes it).
    # ------------------------------------------------------------------
    def refresh_autostart_checkbox(self):
        if os.name != "nt":
            return
        self.var_autostart.set(autostart_registered())

    def _apply_autostart(self):
        if os.name != "nt":
            messagebox.showinfo(APP_TITLE, "Autostart is a Windows feature.")
            self.var_autostart.set(False)
            return
        threading.Thread(target=self._apply_autostart_bg,
                         args=(self.var_autostart.get(),),
                         daemon=True).start()

    def _apply_autostart_bg(self, enable):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              AUTOSTART_SCRIPT)
        cmd = ["powershell.exe", "-NoProfile", "-NonInteractive",
               "-ExecutionPolicy", "Bypass", "-File", script]
        if not enable:
            cmd.append("-Unregister")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=AUTOSTART_REGISTER_SECONDS,
                               creationflags=_NO_WINDOW)
            ok, out = r.returncode == 0, (r.stdout + r.stderr).strip()
        except (OSError, subprocess.SubprocessError) as exc:
            ok, out = False, str(exc)
        for line in out.splitlines():
            self.log("Autostart: " + line.rstrip())
        if ok:
            self.log("Autostart ON: bridge starts at Windows logon."
                     if enable else "Autostart OFF.")
        self.root.after(0, self._autostart_done, ok, out)

    def _autostart_done(self, ok, out):
        if not ok:
            messagebox.showerror(APP_TITLE,
                                 "Could not update autostart:\n%s" % out[-800:])
        self.refresh_autostart_checkbox()

    # ------------------------------------------------------------------
    # bridge control
    # ------------------------------------------------------------------
    def start_bridge(self):
        if self.worker and self.worker.is_alive():
            return
        if not self.save_settings(quiet=True):
            return
        self.stop_event = threading.Event()
        self.bridge = self._bridge_factory(self.config_path,
                                           self.log_queue.put)
        self.worker = threading.Thread(target=self.bridge.run_forever,
                                       args=(self.stop_event,),
                                       name="bridge-worker", daemon=True)
        self.worker.start()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self._set_status(True)
        self._sync_tray_menu()

    def stop_bridge(self):
        if self.stop_event:
            self.stop_event.set()
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self._set_status(False)
        self.log("Stopping bridge (finishes the current poll first)...")
        self._sync_tray_menu()

    def test_connection(self):
        if not self.save_settings(quiet=True):
            return
        self.log("Testing connection...")
        threading.Thread(target=self._test_connection_bg, daemon=True).start()

    def _test_connection_bg(self):
        ok, msg = bridge_core.test_connection(self.config_path)
        self.log_queue.put("[test] %s: %s" % ("OK" if ok else "FAILED", msg))

    def _set_status(self, running):
        self.status_dot.config(fg=COL_DOT_ON if running else COL_DOT_OFF)
        self.status_text.config(text="Running" if running else "Stopped")

    # ------------------------------------------------------------------
    # log pump (runs on the GUI thread)
    # ------------------------------------------------------------------
    def log(self, line):
        self.log_queue.put(line)

    def _pump_queue(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.log_widget.config(state="normal")
                self.log_widget.insert("end", line.rstrip() + "\n")
                self.log_widget.see("end")
                self.log_widget.config(state="disabled")
        except queue.Empty:
            pass
        if self.bridge is not None:
            s = self.bridge.stats
            self.counters_text.config(
                text="tasks: %d   results sent: %d" % (s["tasks_received"],
                                                       s["results_sent"]))
            if s["last_poll"]:
                self.poll_text.config(text="last poll: %s   IMAP IDLE: %s" % (
                    s["last_poll"], s.get("idle", "off")))
            self.error_text.config(text=s["last_error"] or "")
            if not (self.worker and self.worker.is_alive()) and \
                    self.btn_stop["state"] == "normal":
                # worker died on its own (e.g. bad config); reflect it
                self.btn_start.config(state="normal")
                self.btn_stop.config(state="disabled")
                self._set_status(False)
        self.root.after(QUEUE_PUMP_MS, self._pump_queue)

    # ------------------------------------------------------------------
    # window / console controls
    # ------------------------------------------------------------------
    def minimize_window(self):
        """Taskbar minimize only; the bridge worker thread keeps polling."""
        self.log("Window minimized to the taskbar -- bridge keeps running.")
        self.root.iconify()

    def _sync_console_button(self):
        self.btn_console.config(
            text="Show console" if self.console_hidden else "Hide console")

    def toggle_console(self):
        self.console_hidden = not self.console_hidden
        self._sync_console_button()
        if self.console_hidden:
            work = self.consoles.hide
            wanted = lambda: self.console_hidden  # noqa: E731
        else:
            work = self.consoles.show
            wanted = lambda: not self.console_hidden  # noqa: E731
        threading.Thread(target=work, args=(wanted, self.log),
                         daemon=True).start()

    def _console_watch(self):
        """While 'hidden' is chosen, hide task runner consoles as they
        appear (the runner is usually opened after the GUI)."""
        while not self._console_stop.is_set():
            if self.console_hidden:
                try:
                    self.consoles.hide(lambda: self.console_hidden, self.log)
                except Exception as exc:
                    self.log("Console watch: %s" % exc)
            self._console_stop.wait(CONSOLE_POLL_SECONDS)

    # ------------------------------------------------------------------
    # tray
    # ------------------------------------------------------------------
    def _tray_image(self):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([4, 4, 60, 60], radius=12, fill=(30, 120, 200))
        d.ellipse([22, 22, 42, 42], fill=(255, 255, 255))
        d.ellipse([27, 27, 37, 37], fill=(30, 120, 200))
        return img

    def _tray_menu(self):
        if not HAVE_TRAY:
            return None
        running = bool(self.worker and self.worker.is_alive())
        return pystray.Menu(
            pystray.MenuItem("Show window", self._tray_show, default=True),
            pystray.MenuItem("Stop bridge" if running else "Start bridge",
                             self._tray_toggle),
            pystray.MenuItem("Quit", self._quit_from_tray),
        )

    def _sync_tray_menu(self):
        if self.tray_icon:
            self.tray_icon.menu = self._tray_menu()

    def _ensure_tray(self):
        if not HAVE_TRAY or self.tray_icon:
            return
        self.tray_icon = pystray.Icon("jarvis-bridge", self._tray_image(),
                                      APP_TITLE, menu=self._tray_menu())
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def _tray_show(self, _icon=None, _item=None):
        self.root.after(0, self._show_window)

    def _tray_toggle(self, _icon=None, _item=None):
        self.root.after(0, self.stop_bridge if
                        (self.worker and self.worker.is_alive())
                        else self.start_bridge)

    def _quit_from_tray(self, _icon=None, _item=None):
        self.root.after(0, self._quit)

    def _show_window(self):
        self.root.deiconify()
        self.root.lift()

    def on_close(self):
        """X button: hide to tray if the bridge is running there; otherwise
        quit (stopping the bridge first)."""
        running = bool(self.worker and self.worker.is_alive())
        if HAVE_TRAY and running:
            self._ensure_tray()
            self.root.withdraw()
            self.log("Minimized to tray -- bridge keeps running.")
        else:
            self._quit()

    def _quit(self):
        if self.stop_event:
            self.stop_event.set()
        self._console_stop.set()
        self.consoles.restore()
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.destroy()


def _shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


EXIT_ALREADY_RUNNING = 3


def main(argv=None):
    ap = argparse.ArgumentParser(description="Jarvis bridge GUI")
    ap.add_argument("--config", default=default_config_path())
    ap.add_argument("--no-start", action="store_true",
                    help="open the window without starting to poll")
    args = ap.parse_args(argv)
    config_path = os.path.abspath(args.config)
    lock = bridge_core.SingleInstance(config_path)
    if not lock.acquire():
        if sys.stderr:
            sys.stderr.write("Jarvis bridge already running for %s\n"
                             % config_path)
        return EXIT_ALREADY_RUNNING
    try:
        created, init_error = [], None
        try:
            created = bridge_core.init_install(config_path)
        except Exception as exc:
            init_error = exc
        root = tk.Tk()
        app = BridgeGui(root, config_path)
        for path in created:
            app.log("First-run setup created %s" % path)
        if init_error is not None:
            app.log("First-run setup failed: %s" % init_error)
        cfg = app._cfg
        if not args.no_start and cfg.get("start_on_launch") and \
                cfg.get("email") and cfg.get("app_password"):
            app.log("Starting the bridge (start_on_launch).")
            root.after(START_ON_LAUNCH_DELAY_MS, app.start_bridge)
        root.mainloop()
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
