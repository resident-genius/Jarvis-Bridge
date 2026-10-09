"""
bridge_core.py -- Jarvis<->executor Gmail-bus bridge engine.
Faithful rebuild of the live bridge contract (Gmail bus + DKIM auth):
  * Loop A (tasks in):  IMAP watch for UNSEEN "[JARVIS-TASK]" mail, validate
    From allowlist + Gmail DKIM signature, then route the task package by its
    "executor" field ("cursor" when missing) into that executor's inbox as
    <task-id>.jarvis-task.json (written to .tmp, then renamed, so a watcher
    never sees a partial file). A skipped "[JARVIS-TASK]" message -- including
    one naming an executor with no configured inbox, or re-sending a task_id
    the bridge already handled (never re-executed) -- gets a "skipped" result
    file that Loop B mails back like any other; nothing is dropped silently.
    A task whose JSON has no positive "timeout_minutes" is REJECTED at intake
    (never routed, no worker started) with a "failed" result naming the
    field -- there is no silent default timeout any more.
    New mail is noticed at once through IMAP IDLE; the plain poll every
    poll_seconds keeps running as the fallback.
  * Loop B (results out): SMTP *.jarvis-result.json files from EVERY
    configured inbox back to Jarvis as "[JARVIS-RESULT] <task-id>" mail. A
    result is only marked sent after the SMTP call succeeds -- results are
    never lost -- and once sent it is never mailed again, even when sent.json
    cannot be written. A temporary failure (network down, machine asleep,
    SMTP 4xx) is retried with a growing delay, not on every poll. A message
    Gmail refuses outright (SMTP 5xx after DATA, e.g. 552 "blocked for
    security" for a script attachment) is recorded in undeliverable.json and
    not retried until the result file changes.
    Before every send attempt (first try and each retry) the mailbox is
    searched over IMAP: Gmail can file a message in the mailbox and still
    answer DATA with an error, so a "[JARVIS-RESULT] <task-id>" already there
    means the result is delivered -- it is marked sent and never mailed
    again. When the mailbox cannot be searched the send waits like a
    temporary SMTP failure. Attachments Gmail blocks (script/executable
    types, archives holding them, password-protected or uninspectable
    archives) are left off with a reason in "attachments_skipped", so one
    bad file cannot get the whole result email refused.
    Every SMTP DATA attempt is first recorded in send-state.json (attempt
    count + file hash), so a result gets at most RESULT_SEND_MAX_ATTEMPTS
    sends even across crashes and restarts. After a 552 refusal or the last
    failed attempt the bridge stops retrying and, unless the mailbox shows
    Gmail filed the mail anyway, queues a small text-only
    "<task-id>.send-failed-<hash>" failure result so Jarvis is told.
Standard library only. No secrets are ever written to logs.
Config file (gmail-bridge-config.json). Paths may start with %USERPROFILE%
or ~, and relative paths are taken from the user's profile folder, so one
config works under any Windows user name:
  {
    "email": "bridge mailbox address",
    "app_password": "16-char Google app password",
    "poll_seconds": 60,              # IMAP poll (fallback when IDLE is quiet)
    "results_poll_seconds": 5,       # how often new result files are checked
    "idle": true,                    # use IMAP IDLE for instant task pickup
    "inboxes": {"cursor": "%USERPROFILE%\\cursor-inbox",
                "openclaw": "%USERPROFILE%\\openclaw-inbox"},
    "default_executor": "cursor",    # used when a task has no "executor"
    "results_dirs": {},              # per-executor override (default: inbox)
    "done_dirs": {},                 # per-executor override (default: the
                                     #   sibling <executor>-done folder)
    "allowed_workspaces": "%USERPROFILE%\\cursor-inbox\\allowed-workspaces.txt",
    "runners": {"cursor": "<command template>", "openclaw": ""},
    "allowed_senders": ["bridge mailbox address"],
    "device_tag": "",                # this machine's ID ("" = computer name)
    "devices": {},                   # device ID -> {"name", "ip"}
    "start_on_launch": true,         # GUI starts polling when it opens
    "work_dirs": {"openclaw":        # per-executor root for task workspaces
        "%OPENCLAW_WORKSPACE%\\jarvis-bridge-tasks"}
  }
Runner templates may use {inbox}, {bridge_dir}, {python},
{allowed_workspaces} and {executor}; "" means the executor is started
outside the bridge (it just watches its inbox).
"work_dirs": a task routed to an executor listed here gets its "workspace"
replaced by a fresh <root>\\<task-id> folder (created by the bridge) before
the package is written, so that executor's working files land there and
nowhere else. %OPENCLAW_WORKSPACE% is OpenClaw's own agent workspace, read
from OpenClaw's config and checked on disk; when it cannot be found the task
keeps the workspace Jarvis sent. {} turns rerouting off.
Legacy single-inbox configs ("inbox", "results_dir", "done_dir") are
migrated on load to the cursor entry of the map. Legacy "bridge_secret"
fields are ignored silently (DKIM replaced it).
The "devices" registry maps device IDs (a D followed by two or more digits)
to the machines the hub reaches over the LAN. An empty "ip" means unset.
"""
import email
import email.header
import email.message
import email.utils
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import glob
import hashlib
import imaplib
import io
import json
import os
import re
import smtplib
import socket
import ssl
import sys
import tarfile
import threading
import time
import zipfile
from datetime import datetime
# --------------------------------------------------------------------------
# Settings: bridge.config.json next to this file (or JARVIS_BRIDGE_SETTINGS),
# read once at import. Every value below falls back to the production literal
# when the file, the key, or a value of the same type is missing.
# --------------------------------------------------------------------------
SETTINGS_FILE_NAME = "bridge.config.json"
def _load_settings():
    path = os.environ.get("JARVIS_BRIDGE_SETTINGS", "").strip() or \
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     SETTINGS_FILE_NAME)
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        return {}, path, "%s: %s" % (type(exc).__name__, exc)
    if not isinstance(data, dict):
        return {}, path, "not a JSON object"
    return data, path, None
SETTINGS, SETTINGS_PATH, SETTINGS_ERROR = _load_settings()
SETTINGS_FALLBACKS = set()  # keys that used the built-in value
def _setting_kind_ok(value, default):
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, (tuple, list)):
        return isinstance(value, list) and all(
            isinstance(v, str) for v in value)
    if isinstance(default, dict):
        return isinstance(value, dict)
    return False
def settings_status():
    """One line for the bridge log: where the settings came from."""
    if SETTINGS_ERROR:
        return "Settings: %s not used (%s); built-in values" % (
            SETTINGS_PATH, SETTINGS_ERROR)
    text = "Settings: %s loaded" % SETTINGS_PATH
    if SETTINGS_FALLBACKS:
        text += "; built-in value used for %s" % ", ".join(
            sorted(SETTINGS_FALLBACKS))
    return text
def bridge_setting(key, default):
    """bridge.config.json value at dotted `key`, or `default`."""
    node = SETTINGS
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            SETTINGS_FALLBACKS.add(key)
            return default
        node = node[part]
    if not _setting_kind_ok(node, default):
        SETTINGS_FALLBACKS.add(key)
        return default
    if isinstance(default, tuple):
        return tuple(node)
    if isinstance(default, (list, dict)):
        return json.loads(json.dumps(node))  # private copy
    return node
def _setting_tail(key, token, default):
    """'<token>\\a\\b' setting -> 'a\\b' (the part after the token)."""
    value = bridge_setting(key, token + "\\" + default)
    if not value.upper().startswith(token.upper()):
        return default
    rest = value[len(token):].strip("\\/")
    return rest or default
def _setting_leaf(key, default):
    """Last path element of a path setting."""
    leaf = re.split(r"[\\/]", bridge_setting(key, default))[-1]
    return leaf or default
TASK_SUBJECT_PREFIX = bridge_setting("mailbox.task_subject_prefix",
                                     "[JARVIS-TASK]")
RESULT_SUBJECT_PREFIX = bridge_setting("mailbox.result_subject_prefix",
                                       "[JARVIS-RESULT]")
IMAP_FOLDER = bridge_setting("mailbox.imap_folder", "INBOX")
TASK_SEARCH_CRITERIA = bridge_setting("mailbox.task_search_criteria", "UNSEEN")
USE_IDLE = bridge_setting("mailbox.use_idle", True)
DKIM_AUTHSERV_ID = bridge_setting("mailbox.dkim_authserv_id",
                                  "mx.google.com").strip().lower()
DKIM_SIGNING_DOMAIN = bridge_setting("mailbox.dkim_signing_domain",
                                     "gmail.com").strip().lower()
APP_PASSWORD_HELP_URL = bridge_setting("mailbox.app_password_help_url",
                                       "myaccount.google.com/apppasswords")
TASK_FILE_SUFFIX = bridge_setting("executors.task_file_suffix",
                                  ".jarvis-task.json")
RESULT_FILE_SUFFIX = bridge_setting("executors.result_file_suffix",
                                    ".jarvis-result.json")
SKIP_RESULT_TAG = bridge_setting("executors.skip_result_tag", ".skipped-")
SEND_FAILED_TAG = bridge_setting("executors.send_failed_tag", ".send-failed-")
SENT_TRACKER_NAME = _setting_tail("paths.sent_tracker", "%BRIDGE_DIR%",
                                  "sent.json")
UNDELIVERABLE_TRACKER_NAME = _setting_tail("paths.undeliverable_tracker",
                                           "%BRIDGE_DIR%", "undeliverable.json")
SEND_STATE_NAME = _setting_tail("paths.send_state", "%BRIDGE_DIR%",
                                "send-state.json")
RESULT_RETRY_FIRST_SECONDS = bridge_setting(
    "timeouts.result_retry_first_seconds", 30)
RESULT_RETRY_MAX_SECONDS = bridge_setting(
    "timeouts.result_retry_max_seconds", 30 * 60)
RESULT_RETRY_BACKOFF_FACTOR = bridge_setting(
    "timeouts.result_retry_backoff_factor", 2)
# SMTP DATA attempts per result file (counted durably in send-state.json).
# Connect/login/mailbox-check failures never reach DATA and are not counted.
RESULT_SEND_MAX_ATTEMPTS = bridge_setting("limits.result_send_max_attempts", 5)
TASK_TIMEOUT_MAX_MINUTES = bridge_setting(
    "timeouts.task_timeout_max_minutes", 720)  # the runner's ceiling
PROBE_TASK_TIMEOUT_MINUTES = bridge_setting(
    "timeouts.probe_task_timeout_minutes", 10)
POLL_SECONDS_DEFAULT = bridge_setting(
    "timeouts.imap_poll_code_default_seconds", 60)
POLL_SECONDS_MIN = bridge_setting("timeouts.imap_poll_min_seconds", 5)
RESULTS_POLL_SECONDS_DEFAULT = bridge_setting(
    "timeouts.results_poll_seconds", 5)
RESULTS_POLL_SECONDS_MIN = bridge_setting(
    "timeouts.results_poll_min_seconds", 1)
CONFIG_LOAD_ERROR_WAIT_SECONDS = bridge_setting(
    "timeouts.config_load_error_wait_seconds", 30)
EXECUTOR_SLOTS_STALE_SECONDS = bridge_setting(
    "timeouts.executor_slots_stale_seconds", 20)
OPENCLAW_LOG_CLOCK_SKEW_SECONDS = bridge_setting(
    "timeouts.openclaw_log_clock_skew_seconds", 5)
OPENCLAW_LOG_TAIL_BYTES = bridge_setting("limits.openclaw_log_tail_bytes",
                                         2 << 20)
IMAP_HOST = bridge_setting("network.imap_host", "imap.gmail.com")
IMAP_PORT = bridge_setting("network.imap_port", 993)
IMAP_SSL = bridge_setting("network.imap_ssl", True)
SMTP_HOST = bridge_setting("network.smtp_host", "smtp.gmail.com")
SMTP_PORT = bridge_setting("network.smtp_port", 587)
SMTP_STARTTLS = bridge_setting("network.smtp_starttls", True)
DEVICE_TAG_DEFAULT = bridge_setting("network.device_tag", "")
REQUIRED_TASK_FIELDS = bridge_setting(
    "executors.required_task_fields",
    ("task_id", "workspace", "goal", "acceptance_criteria"))
START_ON_LAUNCH_DEFAULT = bridge_setting("executors.start_on_launch", True)
MAX_ATTACHMENTS = bridge_setting("limits.max_attachments", 10)
MAX_ATTACHMENT_BYTES = bridge_setting(
    "limits.max_attachment_bytes",
    10 * 1024 * 1024)  # total across all attachments
# File types Gmail refuses as attachments, also inside archives
# (support.google.com/mail/answer/6590), plus PowerShell module/data files.
GMAIL_BLOCKED_EXTENSIONS = frozenset((
    ".ade", ".adp", ".apk", ".appx", ".appxbundle", ".bat", ".cab", ".chm",
    ".cmd", ".com", ".cpl", ".diagcab", ".diagcfg", ".diagpack", ".dll",
    ".dmg", ".ex", ".ex_", ".exe", ".hta", ".img", ".ins", ".iso", ".isp",
    ".jar", ".jnlp", ".js", ".jse", ".lib", ".lnk", ".mde", ".mjs", ".msc",
    ".msi", ".msix", ".msixbundle", ".msp", ".mst", ".nsh", ".pif", ".ps1",
    ".psd1", ".psm1", ".reg", ".scr", ".sct", ".shb", ".sys", ".vb", ".vbe",
    ".vbs", ".vhd", ".vhdx", ".vxd", ".wsc", ".wsf", ".wsh", ".xll",
))
ZIP_EXTENSIONS = (".zip",)
TAR_EXTENSIONS = (".tar", ".tgz", ".tar.gz", ".tbz", ".tbz2", ".tar.bz2",
                  ".txz", ".tar.xz")
COMPRESSED_EXTENSIONS = (".gz", ".bz2", ".xz")
UNINSPECTABLE_ARCHIVE_EXTENSIONS = (".7z", ".rar", ".arj", ".lzh", ".lha",
                                    ".ace", ".zst")
MAX_ARCHIVE_DEPTH = bridge_setting("limits.max_archive_depth", 3)
DEFAULT_EXECUTOR = bridge_setting("executors.default", "cursor")
HOME_TOKEN = "%USERPROFILE%"
OPENCLAW_WORKSPACE_TOKEN = "%OPENCLAW_WORKSPACE%"
DEFAULT_ALLOWED_WORKSPACES = bridge_setting(
    "paths.allowed_workspaces_file",
    os.path.join(HOME_TOKEN, "cursor-inbox", "allowed-workspaces.txt"))
ALLOWLIST_NAME = _setting_leaf("paths.allowed_workspaces_file",
                               "allowed-workspaces.txt")
ALLOWLIST_HEADER = ("# One allowed workspace per line. Tasks targeting "
                    "anything else are refused.\r\n")
INBOX_SUBDIRS = bridge_setting("paths.inbox_subdirs", ("done", "logs"))
BRIDGE_TASKS_DIRNAME = "jarvis-bridge-tasks"
BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
EXECUTOR_SLOTS_REL = _setting_tail("paths.executor_slots_file", "<inbox>",
                                   "logs\\executor-slots.json")
OPENCLAW_STATE_DIRNAME = _setting_tail("paths.openclaw_state_dir", HOME_TOKEN,
                                       ".openclaw")
OPENCLAW_CONFIG_NAME = _setting_leaf("paths.openclaw_config_file",
                                     "openclaw.json")
OPENCLAW_WORKSPACE_DIRNAME = _setting_leaf("paths.openclaw_default_workspace",
                                           "workspace")
OPENCLAW_HOME_DEFAULT = bridge_setting("paths.openclaw_home", HOME_TOKEN)
OPENCLAW_GATEWAY_LOG_SUBDIR = _setting_tail("paths.openclaw_gateway_log_dir",
                                            "%TEMP%", "openclaw")
BRIDGE_MUTEX_PREFIX = bridge_setting("scheduled_tasks.mutex_prefixes.bridge",
                                     "Local\\JarvisBridge-")
_EXECUTOR_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}$")
# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def home_dir():
    return os.path.expanduser("~")
def resolve_path(value):
    """Config path -> absolute path for this machine. %USERPROFILE% and ~
    expand to the current user's profile; relative paths are taken from it."""
    text = str(value or "").strip()
    if not text:
        return ""
    home = home_dir()
    if text.upper().startswith(HOME_TOKEN):
        text = home + text[len(HOME_TOKEN):]
    elif text == "~" or text.startswith(("~\\", "~/")):
        text = home + text[1:]
    text = os.path.expandvars(text)
    if not os.path.isabs(text):
        text = os.path.join(home, text)
    return os.path.normpath(text)
def portable_path(value):
    """Absolute path -> config value. Anything inside the user's profile is
    stored as %USERPROFILE%\\..., so the config moves between machines."""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.upper().startswith(HOME_TOKEN):
        return text
    path = resolve_path(text)
    home = os.path.normpath(home_dir())
    if os.path.normcase(path) == os.path.normcase(home):
        return HOME_TOKEN
    prefix = home.rstrip("\\/") + os.sep
    if os.path.normcase(path).startswith(os.path.normcase(prefix)):
        return HOME_TOKEN + os.sep + path[len(prefix):]
    return path
def _same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == \
        os.path.normcase(os.path.normpath(b))
def valid_executor(name):
    return isinstance(name, str) and bool(_EXECUTOR_RE.match(name))
def _str_map(key, default):
    """A {name: str} settings map; any non-string entry -> the default map."""
    value = bridge_setting(key, default)
    if all(isinstance(v, str) for v in value.values()):
        return value
    SETTINGS_FALLBACKS.add(key)
    return dict(default)
def default_inboxes():
    return _str_map("paths.inboxes", {
        "cursor": os.path.join(HOME_TOKEN, "cursor-inbox"),
        "openclaw": os.path.join(HOME_TOKEN, "openclaw-inbox"),
    })
def default_runners():
    """Per-executor runner command templates. The cursor runner is the
    task runner script shipped next to this file; "" = started elsewhere."""
    return {
        "cursor": bridge_setting(
            "executors.cursor.runner_command",
            'powershell.exe -NoProfile -ExecutionPolicy Bypass -NoExit '
            '-File "{bridge_dir}\\Invoke-JarvisTask.ps1" '
            '-Inbox "{inbox}" -AllowFile "{allowed_workspaces}" '
            '-AutoApprove'),
        "openclaw": bridge_setting("executors.openclaw.runner_command", ""),
    }
def default_work_dirs():
    """Executors whose task workspaces the bridge places itself. OpenClaw
    (Neo) works in its own agent workspace, never where Jarvis pointed."""
    return _str_map("paths.work_dirs", {
        "openclaw": os.path.join(OPENCLAW_WORKSPACE_TOKEN,
                                 BRIDGE_TASKS_DIRNAME)})
def default_config():
    return {
        "email": "",
        "app_password": "",
        "poll_seconds": POLL_SECONDS_DEFAULT,
        "results_poll_seconds": RESULTS_POLL_SECONDS_DEFAULT,
        "idle": USE_IDLE,
        "inboxes": default_inboxes(),
        "default_executor": DEFAULT_EXECUTOR,
        "results_dirs": _str_map("paths.results_dirs", {}),
        "done_dirs": _str_map("paths.done_dirs", {}),
        "allowed_workspaces": DEFAULT_ALLOWED_WORKSPACES,
        "runners": default_runners(),
        "allowed_senders": [],
        "device_tag": DEVICE_TAG_DEFAULT,
        "devices": default_devices(),
        "start_on_launch": START_ON_LAUNCH_DEFAULT,
        "work_dirs": default_work_dirs(),
    }
def default_devices():
    """The device registry starts empty; the GUI adds machines."""
    return {}
_DEVICE_ID_RE = re.compile(r"^D\d{2,}$")
_IPV4_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")
_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?$")
def valid_device_id(s):
    """Device IDs are a D followed by at least two digits, e.g. D12."""
    return isinstance(s, str) and bool(_DEVICE_ID_RE.match(s.strip().upper()))
def valid_host(s):
    """True for an IPv4 address, 'localhost', or a plausible hostname.
    Empty string is NOT valid here -- unset IPs are stored as ""."""
    if not isinstance(s, str):
        return False
    s = s.strip()
    if not s:
        return False
    if _IPV4_RE.match(s):
        return all(0 <= int(o) <= 255 for o in s.split("."))
    if s.lower() == "localhost":
        return True
    return bool(_HOST_RE.match(s))
def normalize_devices(raw):
    """Return an ordered {id: {"name": str, "ip": str}} registry from raw
    config data. Bad IDs are dropped; invalid stored IPs are reset to ""
    (fail closed)."""
    out = {}
    if isinstance(raw, dict):
        for dev_id, dev in raw.items():
            if not isinstance(dev_id, str):
                continue
            dev_id = dev_id.strip().upper()
            if not valid_device_id(dev_id):
                continue
            if isinstance(dev, dict):
                name = str(dev.get("name", "") or "").strip()
                ip = str(dev.get("ip", "") or "").strip()
            else:
                name, ip = "", ""
            if ip and not valid_host(ip):
                ip = ""
            out[dev_id] = {"name": name or dev_id, "ip": ip}
    return out
def _path_map(raw):
    """{executor: path} with valid names and non-empty paths, resolved."""
    out = {}
    if isinstance(raw, dict):
        for name, value in raw.items():
            name = str(name).strip().lower()
            if valid_executor(name) and isinstance(value, str) and value.strip():
                out[name] = resolve_path(value)
    return out
def _work_dir_map(raw):
    """{executor: root} as written in the config (tokens kept, resolved per
    task by work_root)."""
    out = {}
    if isinstance(raw, dict):
        for name, value in raw.items():
            name = str(name).strip().lower()
            if valid_executor(name) and isinstance(value, str) and value.strip():
                out[name] = value.strip()
    return out
def _int_setting(value, default, minimum):
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default
def load_config(path):
    """Load config, applying defaults and migrating the legacy single-inbox
    layout. Paths come back absolute for this machine. Legacy/unknown fields
    are ignored, never logged."""
    cfg = default_config()
    if not os.path.exists(path):
        raise FileNotFoundError(
            "Config not found: %s. Create it (see README) before starting "
            "the bridge." % path
        )
    with open(path, "r", encoding="utf-8-sig") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("Config %s is not a JSON object" % path)
    for key in cfg:
        if key in raw and raw[key] not in (None, ""):
            cfg[key] = raw[key]
    inboxes = _path_map(raw.get("inboxes")) if "inboxes" in raw else {}
    results_dirs = _path_map(raw.get("results_dirs"))
    done_dirs = _path_map(raw.get("done_dirs"))
    if not inboxes:
        inboxes = _path_map(default_inboxes())
        legacy_inbox = raw.get("inbox")
        if isinstance(legacy_inbox, str) and legacy_inbox.strip():
            inboxes[DEFAULT_EXECUTOR] = resolve_path(legacy_inbox)
        legacy_results = raw.get("results_dir")
        if (isinstance(legacy_results, str) and legacy_results.strip()
                and not _same_path(resolve_path(legacy_results),
                                   inboxes[DEFAULT_EXECUTOR])):
            results_dirs.setdefault(DEFAULT_EXECUTOR,
                                    resolve_path(legacy_results))
        legacy_done = raw.get("done_dir")
        if (isinstance(legacy_done, str) and legacy_done.strip()
                and not _same_path(
                    resolve_path(legacy_done),
                    default_done_dir(DEFAULT_EXECUTOR,
                                     inboxes[DEFAULT_EXECUTOR]))):
            done_dirs.setdefault(DEFAULT_EXECUTOR, resolve_path(legacy_done))
    cfg["inboxes"] = inboxes
    cfg["results_dirs"] = {k: v for k, v in results_dirs.items() if k in inboxes}
    cfg["done_dirs"] = {k: v for k, v in done_dirs.items() if k in inboxes}
    default_ex = str(cfg.get("default_executor") or "").strip().lower()
    cfg["default_executor"] = default_ex if valid_executor(default_ex) \
        else DEFAULT_EXECUTOR
    runners = default_runners()
    if isinstance(raw.get("runners"), dict):
        for name, template in raw["runners"].items():
            name = str(name).strip().lower()
            if valid_executor(name) and isinstance(template, str):
                runners[name] = template
    cfg["runners"] = runners
    allow = raw.get("allowed_workspaces")
    if isinstance(allow, str) and allow.strip():
        cfg["allowed_workspaces"] = resolve_path(allow)
    else:
        cfg["allowed_workspaces"] = os.path.join(
            inboxes.get(DEFAULT_EXECUTOR) or resolve_path(
                default_inboxes().get("cursor") or "cursor-inbox"),
            ALLOWLIST_NAME)
    # allowed_senders defaults to the bridge email itself
    if not cfg["allowed_senders"] and cfg["email"]:
        cfg["allowed_senders"] = [cfg["email"]]
    # normalize sender list for comparison
    cfg["allowed_senders"] = [s.strip().lower() for s in cfg["allowed_senders"] if s]
    cfg["devices"] = normalize_devices(cfg.get("devices"))
    cfg["device_tag"] = str(cfg.get("device_tag") or "").strip()
    cfg["poll_seconds"] = _int_setting(cfg["poll_seconds"],
                                       POLL_SECONDS_DEFAULT, POLL_SECONDS_MIN)
    cfg["results_poll_seconds"] = _int_setting(cfg["results_poll_seconds"],
                                               RESULTS_POLL_SECONDS_DEFAULT,
                                               RESULTS_POLL_SECONDS_MIN)
    cfg["idle"] = bool(cfg.get("idle"))
    cfg["start_on_launch"] = bool(cfg.get("start_on_launch"))
    cfg["work_dirs"] = _work_dir_map(raw["work_dirs"]) \
        if isinstance(raw.get("work_dirs"), dict) else default_work_dirs()
    return cfg
def save_config(path, cfg):
    """Write config. Strips any legacy secret fields; paths inside the
    user's profile are stored as %USERPROFILE%\\...; tightens file perms."""
    safe = {k: v for k, v in cfg.items() if k in default_config()}
    for key in ("inboxes", "results_dirs", "done_dirs"):
        if isinstance(safe.get(key), dict):
            safe[key] = {k: portable_path(v) for k, v in safe[key].items()}
    if safe.get("allowed_workspaces"):
        safe["allowed_workspaces"] = portable_path(safe["allowed_workspaces"])
    if isinstance(safe.get("work_dirs"), dict):
        safe["work_dirs"] = {
            k: v if v.upper().startswith(OPENCLAW_WORKSPACE_TOKEN)
            else portable_path(v)
            for k, v in _work_dir_map(safe["work_dirs"]).items()}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(safe, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
# --------------------------------------------------------------------------
# Executor routing helpers
# --------------------------------------------------------------------------
def default_done_dir(executor, inbox):
    """The sibling <executor>-done folder next to the inbox."""
    parent = os.path.dirname(os.path.normpath(inbox)) or home_dir()
    return os.path.join(parent, "%s-done" % executor)
def results_dir_for(cfg, executor):
    return cfg["results_dirs"].get(executor) or cfg["inboxes"][executor]
def done_dir_for(cfg, executor):
    return cfg["done_dirs"].get(executor) or \
        default_done_dir(executor, cfg["inboxes"][executor])
def skip_executor(cfg):
    """Executor whose results dir receives skip reports."""
    if cfg["default_executor"] in cfg["inboxes"]:
        return cfg["default_executor"]
    return next(iter(cfg["inboxes"]))
def result_sources(cfg):
    """Ordered unique [(executor, results_dir)] that Loop B scans."""
    out, seen = [], set()
    for executor in cfg["inboxes"]:
        d = results_dir_for(cfg, executor)
        key = os.path.normcase(os.path.normpath(d))
        if key not in seen:
            seen.add(key)
            out.append((executor, d))
    return out
def sent_key(cfg, executor, name):
    """sent.json key. Files in the default executor's results dir keep the
    bare file name (as before executor routing); other inboxes are
    namespaced so equal task ids in two inboxes never collide."""
    if DEFAULT_EXECUTOR in cfg["inboxes"] and _same_path(
            results_dir_for(cfg, executor),
            results_dir_for(cfg, DEFAULT_EXECUTOR)):
        return name
    return "%s/%s" % (executor, name)
def resolve_executor(task, cfg):
    """Return (executor, None) or (None, reason) for a validated task."""
    raw = task.get("executor")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        name = cfg["default_executor"]
    elif isinstance(raw, str):
        name = raw.strip().lower()
    else:
        return None, "invalid-executor (%s)" % type(raw).__name__
    if name not in cfg["inboxes"]:
        shown = name if len(name) <= 64 else name[:64] + "..."
        return None, "unknown-executor (%s)" % shown
    return name, None
def write_json_atomic(path, data):
    """Write JSON to path via path.tmp + rename: a watcher globbing the final
    name only ever sees a complete file."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
def runner_command(cfg, executor):
    """The executor's runner command line, or "" when it runs elsewhere."""
    template = (cfg.get("runners") or {}).get(executor) or ""
    if not template.strip():
        return ""
    python = sys.executable or "python"
    return template.format(
        inbox=cfg["inboxes"][executor], bridge_dir=BRIDGE_DIR,
        python=python, allowed_workspaces=cfg["allowed_workspaces"],
        executor=executor)
def device_tag(cfg):
    return cfg.get("device_tag") or socket.gethostname()
# --------------------------------------------------------------------------
# Task workspaces (work_dirs)
# --------------------------------------------------------------------------
_TASK_DIR_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
def _openclaw_user_path(value, home):
    text = str(value or "").strip()
    if text == "~" or text.startswith(("~\\", "~/")):
        text = home + text[1:]
    return os.path.abspath(os.path.expandvars(text))
def _openclaw_default_agent_workspace(config_path):
    """The workspace set in OpenClaw's config for its default agent (the
    agent's own entry first, then agents.defaults), or ""."""
    try:
        with open(config_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return ""
    agents = data.get("agents") if isinstance(data, dict) else None
    if not isinstance(agents, dict):
        return ""
    entries = agents.get("entries")
    if isinstance(entries, dict):
        entries = list(entries.values())
    elif isinstance(agents.get("list"), list):
        entries = agents["list"]
    else:
        entries = []
    entries = [e for e in entries if isinstance(e, dict)]
    entry = next((e for e in entries if e.get("default")),
                 entries[0] if entries else {})
    defaults = agents.get("defaults")
    for value in (entry.get("workspace"),
                  defaults.get("workspace") if isinstance(defaults, dict)
                  else None):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
def openclaw_workspace():
    """(path, source) of OpenClaw's agent workspace, found the way OpenClaw
    finds it: the configured agent workspace, else OPENCLAW_WORKSPACE_DIR,
    else <state dir>\\workspace. Only a folder that exists on disk counts;
    otherwise ("", reason)."""
    env = os.environ
    home = env.get("OPENCLAW_HOME", "").strip()
    default_home = home_dir() \
        if OPENCLAW_HOME_DEFAULT.upper() == HOME_TOKEN \
        else resolve_path(OPENCLAW_HOME_DEFAULT)
    home = _openclaw_user_path(home, home_dir()) if home else default_home
    state_override = env.get("OPENCLAW_STATE_DIR", "").strip()
    state_dir = _openclaw_user_path(state_override, home) if state_override \
        else os.path.join(home, OPENCLAW_STATE_DIRNAME)
    config_override = env.get("OPENCLAW_CONFIG_PATH", "").strip()
    config_path = _openclaw_user_path(config_override, home) \
        if config_override else os.path.join(state_dir, OPENCLAW_CONFIG_NAME)
    configured = _openclaw_default_agent_workspace(config_path)
    workspace_env = env.get("OPENCLAW_WORKSPACE_DIR", "").strip()
    profile = env.get("OPENCLAW_PROFILE", "").strip().lower()
    if configured:
        path = _openclaw_user_path(configured, home)
        source = "agent workspace in %s" % config_path
    elif workspace_env:
        path = _openclaw_user_path(workspace_env, home)
        source = "OPENCLAW_WORKSPACE_DIR"
    elif state_override or (profile and profile != "default"):
        path = os.path.join(state_dir, OPENCLAW_WORKSPACE_DIRNAME)
        source = "OpenClaw state folder"
    else:
        path = os.path.join(home, OPENCLAW_STATE_DIRNAME,
                            OPENCLAW_WORKSPACE_DIRNAME)
        source = "OpenClaw default"
    path = os.path.normpath(path)
    if not os.path.isdir(path):
        return "", "OpenClaw workspace %s (%s) does not exist" % (path, source)
    return path, source
def work_root(cfg, executor):
    """(root, None) when the executor's task workspaces are placed by the
    bridge, (None, reason) when its root cannot be found, (None, None) when
    the executor has no work_dirs entry."""
    value = (cfg.get("work_dirs") or {}).get(executor)
    if not value:
        return None, None
    if value.upper().startswith(OPENCLAW_WORKSPACE_TOKEN):
        base, detail = openclaw_workspace()
        if not base:
            return None, detail
        rest = value[len(OPENCLAW_WORKSPACE_TOKEN):].lstrip("\\/")
        return os.path.normpath(os.path.join(base, rest)), None
    return resolve_path(value), None
def task_dir_name(task_id):
    """task_id -> one safe folder name (no separators, never . or ..)."""
    name = _TASK_DIR_UNSAFE_RE.sub("_", str(task_id)).strip("._")[:128]
    return name or "task"
def place_task_workspace(cfg, executor, task, log=None):
    """Point task["workspace"] at a fresh <work root>\\<task-id> folder when
    the executor has a work_dirs entry. Returns the new workspace, or None
    when the task keeps the workspace it came with."""
    log = log or (lambda line: None)
    task_id = str(task.get("task_id", "")).strip()
    root, problem = work_root(cfg, executor)
    if root is None:
        if problem:
            log("task %s keeps the workspace Jarvis sent: %s"
                % (task_id, problem))
        return None
    target = os.path.join(root, task_dir_name(task_id))
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as exc:
        log("task %s keeps the workspace Jarvis sent: could not create %s "
            "(%s)" % (task_id, target, exc))
        return None
    requested = task.get("workspace")
    task["workspace"] = target
    log("task %s workspace -> %s (Jarvis sent %s)"
        % (task_id, target, requested))
    return target
def init_install(config_path, log=None):
    """First-run setup: write a default config if there is none, then create
    every configured inbox with done\\ and logs\\ plus the workspace
    allowlist. Existing files are never overwritten. Returns created paths."""
    log = log or (lambda line: None)
    created = []
    if not os.path.exists(config_path):
        parent = os.path.dirname(os.path.abspath(config_path))
        os.makedirs(parent, exist_ok=True)
        save_config(config_path, default_config())
        created.append(config_path)
        log("Init: wrote default config %s" % config_path)
    cfg = load_config(config_path)
    created += ensure_layout(cfg, log)
    return created
def ensure_layout(cfg, log=None):
    log = log or (lambda line: None)
    created = []
    for executor, inbox in cfg["inboxes"].items():
        for d in [inbox] + [os.path.join(inbox, s) for s in INBOX_SUBDIRS]:
            if not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
                created.append(d)
                log("Init: created %s" % d)
    allow = cfg["allowed_workspaces"]
    if allow and not os.path.exists(allow):
        os.makedirs(os.path.dirname(allow), exist_ok=True)
        with open(allow, "wb") as f:
            f.write(ALLOWLIST_HEADER.encode("ascii"))
        created.append(allow)
        log("Init: created %s -- add project folders (one per line)" % allow)
    return created
def newest_task_file(inbox):
    """(task_id, mtime) of the newest task package in the inbox or its
    done\\ folder, or (None, None)."""
    best = (None, None)
    for d in (inbox, os.path.join(inbox, "done")):
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if not e.name.endswith(TASK_FILE_SUFFIX):
                continue
            try:
                mtime = e.stat().st_mtime
            except OSError:
                continue
            if best[1] is None or mtime > best[1]:
                best = (e.name[:-len(TASK_FILE_SUFFIX)], mtime)
    return best
EXECUTOR_SLOTS_NAME = re.split(r"[\\/]", EXECUTOR_SLOTS_REL)[-1]
def executor_slots_path(inbox):
    """Slot state written by the cursor runner's executor pool."""
    return os.path.join(inbox, *re.split(r"[\\/]", EXECUTOR_SLOTS_REL))
def read_executor_slots(inbox, now=None,
                        stale_after=EXECUTOR_SLOTS_STALE_SECONDS):
    """The runner's executor slots, or None when it has never written them:
    {"runner_pid", "max_slots", "running", "queued", "age" (seconds since the
    runner last wrote), "live" (runner reporting and not stopped),
    "heartbeat_stale_seconds", "heartbeat_failures",
    "slots": [{"slot", "state": "running"|"idle", "task_id", "elapsed",
               "heartbeat_age" (seconds since the worker's last beat)}]}."""
    try:
        with open(executor_slots_path(inbox), "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("slots"), list):
        return None
    now = time.time() if now is None else now
    try:
        age = max(0, int(now - float(raw.get("updated_unix") or 0)))
    except (TypeError, ValueError):
        age = None
    live = age is not None and age <= stale_after and not raw.get("stopped")
    slots = []
    for s in raw["slots"]:
        if not isinstance(s, dict):
            continue
        running = live and s.get("state") == "running" and s.get("task_id")
        elapsed = heartbeat_age = None
        if running:
            try:
                elapsed = max(0, int(now - float(s.get("started_unix"))))
            except (TypeError, ValueError):
                pass
            try:
                heartbeat_age = max(0, int(now - float(s.get("heartbeat_unix"))))
            except (TypeError, ValueError):
                pass
        slots.append({"slot": s.get("slot"),
                      "state": "running" if running else "idle",
                      "task_id": str(s["task_id"]) if running else None,
                      "elapsed": elapsed, "heartbeat_age": heartbeat_age})
    try:
        stale_after_hb = int(raw.get("heartbeat_stale_seconds") or 0) or None
    except (TypeError, ValueError):
        stale_after_hb = None
    return {"runner_pid": raw.get("runner_pid"),
            "max_slots": raw.get("max_slots") or len(slots),
            "running": sum(1 for s in slots if s["state"] == "running"),
            "queued": raw.get("queued") or 0, "age": age, "live": live,
            "heartbeat_stale_seconds": stale_after_hb,
            "heartbeat_failures": raw.get("heartbeat_failures") or 0,
            "slots": slots}
# OpenClaw has no slot pool: its watcher leaves a package in the inbox until
# the run ends. The gateway's jarvis-bridge-priority plugin logs every watcher
# run start/end, the only on-disk record that a package was picked up.
OPENCLAW_RUN_MAX_SECONDS = bridge_setting(
    "timeouts.openclaw_run_max_seconds",
    35 * 60)  # the plugin's maxBridgeHoldMinutes
_OC_RUN_RE = re.compile(
    r"\[jarvis-bridge-priority\] bridge run (started|finished): ([0-9A-Za-z-]+)")
_OC_LOADED_RE = re.compile(
    r"\[jarvis-bridge-priority\] loaded v\S+ .*?tracked: (\d+) bridge")
_OC_TIME_RE = re.compile(r'"time":"([^"]+)"')
_oc_log_cache = {}
def openclaw_log_dir():
    return os.path.join(os.environ.get("TEMP") or os.environ.get("TMP")
                        or os.path.join(home_dir(), "AppData", "Local",
                                        "Temp"), OPENCLAW_GATEWAY_LOG_SUBDIR)
def _openclaw_plugin_lines(path, max_bytes):
    """The plugin's lines from the tail of one gateway log (cached by size
    and mtime: the GUI asks every 2 seconds)."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = (st.st_size, st.st_mtime)
    hit = _oc_log_cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    lines = []
    try:
        with open(path, "rb") as f:
            if st.st_size > max_bytes:
                f.seek(st.st_size - max_bytes)
                f.readline()  # partial line
            for raw in f:
                if b"[jarvis-bridge-priority]" in raw:
                    lines.append(raw.decode("utf-8", "replace"))
    except OSError:
        return []
    _oc_log_cache[path] = (key, lines)
    return lines
def oldest_pending_task(inbox):
    """task_id of the oldest package in the inbox with no result file yet
    (the one the OpenClaw watcher trigger picks), or None."""
    best = (None, None)
    try:
        entries = list(os.scandir(inbox))
    except OSError:
        return None
    names = {e.name for e in entries}
    for e in entries:
        if not e.name.endswith(TASK_FILE_SUFFIX):
            continue
        task_id = e.name[:-len(TASK_FILE_SUFFIX)]
        if task_id + RESULT_FILE_SUFFIX in names:
            continue
        try:
            mtime = e.stat().st_mtime
        except OSError:
            continue
        if best[1] is None or mtime < best[1]:
            best = (task_id, mtime)
    return best[0]
def openclaw_bridge_run(log_dir=None, now=None,
                        max_bytes=OPENCLAW_LOG_TAIL_BYTES):
    """The OpenClaw watcher run in progress, from the gateway logs:
    {"run_id", "started_unix", "elapsed"}, or None when no run is active
    (or the logs cannot be read). A run with no "finished" line stops
    counting after OPENCLAW_RUN_MAX_SECONDS or when the plugin reloads with
    no bridge runs tracked (gateway restart)."""
    log_dir = log_dir or openclaw_log_dir()
    now = time.time() if now is None else now
    try:
        names = sorted(n for n in os.listdir(log_dir)
                       if n.startswith("openclaw-") and n.endswith(".log"))
    except OSError:
        return None
    active = {}
    for name in names[-2:]:  # today + yesterday: a run can cross midnight
        for line in _openclaw_plugin_lines(os.path.join(log_dir, name),
                                           max_bytes):
            m = _OC_TIME_RE.search(line)
            try:
                when = datetime.fromisoformat(m.group(1)).timestamp()
            except (AttributeError, ValueError):
                continue
            if when > now + OPENCLAW_LOG_CLOCK_SKEW_SECONDS:
                continue
            run = _OC_RUN_RE.search(line)
            if run:
                if run.group(1) == "started":
                    active[run.group(2)] = when
                else:
                    active.pop(run.group(2), None)
                continue
            loaded = _OC_LOADED_RE.search(line)
            if loaded and loaded.group(1) == "0":
                active.clear()
    live = [(t, r) for r, t in active.items()
            if -OPENCLAW_LOG_CLOCK_SKEW_SECONDS <= now - t
            <= OPENCLAW_RUN_MAX_SECONDS]
    if not live:
        return None
    started, run_id = max(live)
    return {"run_id": run_id, "started_unix": started,
            "elapsed": max(0, int(now - started))}
def write_probe_task(cfg, executor, workspace, now=None):
    """Drop a harmless probe task straight into the executor's inbox (no
    email). Returns (task_id, path)."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    task_id = "probe-%s-%s" % (executor, stamp)
    task = {
        "task_id": task_id,
        "executor": executor,
        "probe": True,
        "mode": "ask",
        "timeout_minutes": PROBE_TASK_TIMEOUT_MINUTES,
        "workspace": workspace,
        "goal": ("Connectivity probe from the Jarvis Bridge GUI. Do not "
                 "change any files or run anything. Reply with a JARVIS "
                 "REPORT that says: PROBE OK %s" % executor),
        "acceptance_criteria": ["Report says PROBE OK %s" % executor],
    }
    place_task_workspace(cfg, executor, task)
    inbox = cfg["inboxes"][executor]
    os.makedirs(inbox, exist_ok=True)
    path = os.path.join(inbox, task_id + TASK_FILE_SUFFIX)
    write_json_atomic(path, task)
    return task_id, path
# --------------------------------------------------------------------------
# Single instance: one bridge per config file (GUI and CLI share the lock)
# --------------------------------------------------------------------------
class SingleInstance:
    def __init__(self, config_path):
        key = os.path.normcase(os.path.abspath(config_path)).encode("utf-8")
        namespace, _, base = BRIDGE_MUTEX_PREFIX.rpartition("\\")
        self._namespace = namespace + "\\" if namespace else ""
        self.name = base + hashlib.sha1(key).hexdigest()[:16]
        self._handle = None
        self._file = None
    def acquire(self):
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateMutexW.restype = wintypes.HANDLE
            k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                         wintypes.LPCWSTR]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = k32.CreateMutexW(None, False, self._namespace + self.name)
            if not handle:
                return False
            if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                k32.CloseHandle(handle)
                return False
            self._handle = (k32, handle)
            return True
        import fcntl
        import tempfile
        f = open(os.path.join(tempfile.gettempdir(), self.name + ".lock"), "w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._file = f
        return True
    def release(self):
        if self._handle:
            k32, handle = self._handle
            k32.CloseHandle(handle)
            self._handle = None
        if self._file:
            self._file.close()
            self._file = None
def redact_config(cfg):
    """Copy of config safe for logs/display (password masked)."""
    out = dict(cfg)
    if out.get("app_password"):
        out["app_password"] = "***"
    return out
# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def extract_from_addr(msg):
    """Return the bare email address from the From header, lowercased."""
    name, addr = email.utils.parseaddr(msg.get("From", ""))
    return addr.strip().lower()
def dkim_passes(msg):
    """True when the message genuinely came through Gmail for an allowed sender.
    Two genuine paths:
    1. Same-account API submission (Jarvis -> bridge mailbox): Gmail delivers
       internally with NO Authentication-Results header at all. Submission was
       already authenticated by Google (OAuth), and the From allowlist is
       checked separately in validate_task_message, so there is nothing here
       an attacker can forge.
    2. Cross-account / external delivery: Gmail's MX stamps Authentication-Results.
       Require dkim=pass with a gmail.com signing identity. Gmail's current
       format carries it as header.i=@gmail.com (older format: header.d=gmail.com);
       accept either. Only Gmail can produce this stamp for gmail.com mail.
    """
    ar_headers = msg.get_all("Authentication-Results", []) or []
    if not ar_headers:
        return True  # internally submitted; Google authenticated the sender
    for hdr in ar_headers:
        parts = hdr.split(";", 1)
        if len(parts) < 2:
            continue
        if parts[0].strip().lower() != DKIM_AUTHSERV_ID:
            continue
        for clause in parts[1].split(";"):
            c = clause.strip().lower().replace(" ", "")
            if "dkim=pass" in c and (
                    "header.d=" + DKIM_SIGNING_DOMAIN in c
                    or "header.i=@" + DKIM_SIGNING_DOMAIN in c):
                return True
    return False
def get_body_text(msg):
    """Best-effort plain-text body of the message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                payload = part.get_payload(decode=True)
                if payload is not None:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        return payload.decode(charset, errors="replace")
                    except (LookupError, UnicodeDecodeError):
                        return payload.decode("utf-8", errors="replace")
        return ""
    payload = msg.get_payload(decode=True)
    if payload is None:
        return str(msg.get_payload() or "")
    charset = msg.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")
def validate_task_message(msg, cfg):
    """Return (task_dict, None) if the message is a valid task, else
    (None, reason). Fail closed."""
    from_addr = extract_from_addr(msg)
    if from_addr not in cfg["allowed_senders"]:
        return None, "from-not-allowed (%s)" % (from_addr or "<empty>")
    if not dkim_passes(msg):
        return None, "dkim-check-failed"
    try:
        task = json.loads(get_body_text(msg).strip())
    except (json.JSONDecodeError, ValueError):
        return None, "body-not-json"
    if not isinstance(task, dict):
        return None, "body-not-object"
    missing = [f for f in REQUIRED_TASK_FIELDS if f not in task]
    if missing:
        return None, "missing-fields: %s" % ",".join(missing)
    task_id = str(task["task_id"]).strip()
    if not task_id:
        return None, "empty-task-id"
    return task, None
def task_timeout_problem(task):
    """None when task["timeout_minutes"] is a positive number (or numeric
    string, as the runner accepts); otherwise the skip reason code. A task
    without one is refused: the runner's old silent 30-minute default killed
    long jobs with no output."""
    if "timeout_minutes" not in task or task["timeout_minutes"] is None:
        return "missing-timeout"
    value = task["timeout_minutes"]
    shown = repr(value)
    if len(shown) > 40:
        shown = shown[:40] + "..."
    if isinstance(value, bool):
        return "invalid-timeout (%s)" % shown
    try:
        minutes = float(str(value).strip()) if isinstance(value, str) \
            else float(value)
    except (TypeError, ValueError):
        return "invalid-timeout (%s)" % shown
    if minutes != minutes or minutes in (float("inf"), float("-inf")) \
            or minutes <= 0:
        return "invalid-timeout (%s)" % shown
    return None
# --------------------------------------------------------------------------
# Skip reports
# --------------------------------------------------------------------------
_SUBJECT_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SKIP_REASON_TEXT = {
    "dkim-check-failed": "DKIM check failed",
    "body-not-json": "body not JSON",
    "body-not-object": "body JSON is not an object",
    "empty-task-id": "empty task_id",
}
def decoded_subject(msg):
    """Subject header with RFC 2047 encoded-words decoded, whitespace folded."""
    raw = msg.get("Subject", "") or ""
    try:
        text = str(email.header.make_header(email.header.decode_header(raw)))
    except Exception:
        text = str(raw)
    return " ".join(text.split())
def is_task_subject(subject):
    """Only subjects that start with the task prefix get skip reports, so
    replies, forwards and [JARVIS-RESULT] mail can never start a report loop."""
    return subject.startswith(TASK_SUBJECT_PREFIX)
def subject_task_id(subject):
    """'[JARVIS-TASK] b60-install-qwen-001' -> 'b60-install-qwen-001'. The id
    becomes a file name and a mail subject, so anything unsafe -> 'unknown'."""
    rest = subject[len(TASK_SUBJECT_PREFIX):].split()
    if rest and _SUBJECT_TASK_ID_RE.match(rest[0]):
        return rest[0]
    return "unknown"
def skip_reason_text(reason):
    if reason in _SKIP_REASON_TEXT:
        return _SKIP_REASON_TEXT[reason]
    if reason.startswith("missing-fields: "):
        return "missing required fields: " + reason[len("missing-fields: "):]
    if reason.startswith("from-not-allowed"):
        return "sender not allowed" + reason[len("from-not-allowed"):]
    if reason.startswith("unknown-executor"):
        return ("executor has no configured inbox"
                + reason[len("unknown-executor"):])
    if reason.startswith("invalid-executor"):
        return ("executor field is not a string"
                + reason[len("invalid-executor"):])
    if reason.startswith("duplicate-task"):
        return ("duplicate of an already-handled task (task_id %s)"
                % reason[len("duplicate-task"):].strip(" ()"))
    if reason == "missing-timeout":
        return ("timeout_minutes is required (missing from the task JSON); "
                "the bridge no longer applies a silent default timeout")
    if reason.startswith("invalid-timeout"):
        return ("timeout_minutes must be a positive number of minutes (got %s)"
                % reason[len("invalid-timeout"):].strip(" ()"))
    return reason
def build_skip_result(msg, raw, imap_seq, reason, now=None, extra=None,
                      task_id=None):
    """Return (file_name, result_dict) for a skipped task email. The file name
    is keyed on the Message-ID (or the raw bytes when there is none), so the
    same message always maps to the same result file / sent.json entry.
    `extra` adds fields (e.g. the refused executor) to the result. `task_id`
    (the validated body id) is used instead of the subject id when it is
    safe as a file name and mail subject."""
    subject = decoded_subject(msg)
    if not (isinstance(task_id, str) and _SUBJECT_TASK_ID_RE.match(task_id)):
        task_id = subject_task_id(subject)
    message_id = (msg.get("Message-ID", "") or "").strip()
    key = hashlib.sha256(
        message_id.encode("utf-8", "replace") if message_id else raw
    ).hexdigest()[:16]
    stamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    text = skip_reason_text(reason)
    if reason.startswith("duplicate-task"):
        report = ("Task email skipped by the bridge (Loop A): %s. "
                  "The task was NOT re-executed; send it with a new task_id "
                  "to run it again." % text)
    elif reason == "missing-timeout" or reason.startswith("invalid-timeout"):
        report = ("FAILED at intake (Loop A): %s. The task was NOT run and no "
                  "worker was started. Resend it with \"timeout_minutes\" set "
                  "to a positive number (max %d)."
                  % (text, TASK_TIMEOUT_MAX_MINUTES))
    else:
        report = ("Task email skipped by the bridge (Loop A): %s. "
                  "The task was NOT run; fix the email and resend it." % text)
    result = {
        "task_id": task_id,
        "status": "skipped",
        "subtype": "skipped",
        "skipped": True,
        "skip_reason": text,
        "skip_reason_code": reason,
        "message_id": message_id or None,
        "imap_seq": imap_seq,
        "subject": subject,
        "from": extract_from_addr(msg),
        "timestamp": stamp,
        "started": stamp,
        "finished": stamp,
        "exit_code": None,
        "timed_out": False,
        "workspace": "",
        "mode": "",
        "stdout": "",
        "stderr": "",
        "jarvis_report": report,
        "attachments": [],
        "attachments_skipped": [],
    }
    if extra:
        result.update(extra)
    name = "%s%s%s%s" % (task_id, SKIP_RESULT_TAG, key, RESULT_FILE_SUFFIX)
    return name, result
# --------------------------------------------------------------------------
# Result attachments
# --------------------------------------------------------------------------
def _is_inside(path, root):
    """True when realpath'd `path` lies inside realpath'd directory `root`."""
    try:
        common = os.path.commonpath([root, path])
    except ValueError:  # different drives, or mixed absolute/relative
        return False
    return os.path.normcase(common) == os.path.normcase(root)
def _attachment_filename(declared):
    name = os.path.basename(os.path.normpath(declared))
    name = "".join(ch for ch in name
                   if ch not in '"\\' and ord(ch) >= 0x20 and ord(ch) != 0x7f)
    return name.strip() or "attachment"
def _blocked_ext(name):
    """The Gmail-blocked extension of a file name, or ""."""
    ext = os.path.splitext(name.lower().rstrip(". "))[1]
    return ext if ext in GMAIL_BLOCKED_EXTENSIONS else ""
def _is_archive_name(name):
    return name.lower().endswith(ZIP_EXTENSIONS + TAR_EXTENSIONS +
                                 COMPRESSED_EXTENSIONS +
                                 UNINSPECTABLE_ARCHIVE_EXTENSIONS)
def _members_problem(members, depth):
    """members: [(name, read(), size)] of the regular files in an archive."""
    blocked = [m for m, _, _ in members if _blocked_ext(m)]
    if blocked:
        shown = ", ".join(os.path.basename(m) for m in blocked[:5])
        if len(blocked) > 5:
            shown += " and %d more" % (len(blocked) - 5)
        return "archive contains file types Gmail blocks (%s)" % shown
    for mname, read, size in members:
        if not _is_archive_name(mname):
            continue
        base = os.path.basename(mname)
        if depth >= MAX_ARCHIVE_DEPTH:
            return "archives nested too deep to inspect (%s)" % base
        if size > MAX_ATTACHMENT_BYTES:
            return "nested archive %s too large to inspect" % base
        inner = _archive_problem(mname, read(), depth + 1)
        if inner:
            return "nested %s: %s" % (base, inner)
    return None
def _archive_problem(name, data, depth):
    """Why Gmail would refuse this archive, or None."""
    lower = name.lower()
    try:
        if lower.endswith(ZIP_EXTENSIONS):
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                infos = [i for i in zf.infolist() if not i.is_dir()]
                if any(i.flag_bits & 0x1 for i in infos):
                    return ("password-protected archive (Gmail blocks "
                            "archives it cannot scan)")
                return _members_problem(
                    [(i.filename, (lambda i=i: zf.read(i)), i.file_size)
                     for i in infos], depth)
        if lower.endswith(TAR_EXTENSIONS):
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
                infos = [m for m in tf.getmembers() if m.isfile()]
                return _members_problem(
                    [(m.name, (lambda m=m: tf.extractfile(m).read()), m.size)
                     for m in infos], depth)
        if lower.endswith(COMPRESSED_EXTENSIONS):
            import bz2
            import gzip
            import lzma
            opener = {".gz": gzip.GzipFile, ".bz2": bz2.BZ2File,
                      ".xz": lzma.LZMAFile}[os.path.splitext(lower)[1]]
            inner = name[:-len(os.path.splitext(lower)[1])]
            if _blocked_ext(inner):
                return "compressed file is a type Gmail blocks (%s)" % \
                    os.path.basename(inner)
            if not _is_archive_name(inner):
                return None
            with opener(fileobj=io.BytesIO(data)) as f:
                payload = f.read(MAX_ATTACHMENT_BYTES + 1)
            if len(payload) > MAX_ATTACHMENT_BYTES:
                return "compressed archive too large to inspect"
            return _members_problem(
                [(inner, (lambda: payload), len(payload))], depth)
    except Exception as exc:
        return ("archive could not be opened to check for file types Gmail "
                "blocks (%s)" % type(exc).__name__)
    return ("Gmail scans inside %s archives and the bridge cannot"
            % os.path.splitext(lower)[1])
def blocked_attachment_reason(name, data):
    """Why Gmail would refuse a result email carrying this attachment
    (SMTP 552 "blocked for security"), or None when it is safe to attach."""
    ext = _blocked_ext(name)
    if ext:
        return ("Gmail blocks %s files; not attached so Gmail cannot refuse "
                "the whole result email" % ext)
    if _is_archive_name(name):
        problem = _archive_problem(name, data, 1)
        if problem:
            return ("%s; not attached so Gmail cannot refuse the whole "
                    "result email" % problem)
    return None
def collect_attachments(declared, workspace, deny_paths=(),
                        getsize=os.path.getsize):
    """Validate declared attachment paths against the task workspace.
    Returns (attached, skipped): attached is [(filename, bytes)], skipped is
    [{"path", "reason"}]. A bad entry is skipped, never fatal."""
    attached, skipped = [], []
    if isinstance(declared, str):
        declared = [declared]
    if not isinstance(declared, list):
        return attached, [{"path": repr(declared),
                           "reason": "attachments field is not a list"}]
    ws = None
    if isinstance(workspace, str) and workspace.strip():
        ws = os.path.realpath(workspace.strip())
        if not os.path.isdir(ws):
            ws = None
    denied = {os.path.normcase(os.path.realpath(p)) for p in deny_paths if p}
    seen = set()
    total = 0
    for item in declared:
        shown = item if isinstance(item, str) else repr(item)
        def skip(reason):
            skipped.append({"path": shown, "reason": reason})
        if not isinstance(item, str) or not item.strip():
            skip("not a path string")
            continue
        if ws is None:
            skip("task workspace missing or not a directory")
            continue
        raw = item.strip()
        if not os.path.isabs(raw):
            raw = os.path.join(ws, raw)
        real = os.path.realpath(raw)
        key = os.path.normcase(real)
        if not _is_inside(real, ws):
            skip("outside task workspace")
            continue
        if key in denied:
            skip("bridge config/state file is never attached")
            continue
        if not os.path.exists(real):
            skip("does not exist")
            continue
        if not os.path.isfile(real):
            skip("not a regular file")
            continue
        if key in seen:
            skip("duplicate")
            continue
        if len(attached) >= MAX_ATTACHMENTS:
            skip("max %d attachments reached" % MAX_ATTACHMENTS)
            continue
        try:
            size = getsize(real)
        except OSError as exc:
            skip("unreadable: %s" % exc)
            continue
        if total + size > MAX_ATTACHMENT_BYTES:
            skip("would exceed %d MB total attachment cap"
                 % (MAX_ATTACHMENT_BYTES // (1024 * 1024)))
            continue
        try:
            with open(real, "rb") as f:
                data = f.read()
        except OSError as exc:
            skip("unreadable: %s" % exc)
            continue
        filename = _attachment_filename(item)
        blocked = blocked_attachment_reason(filename, data)
        if blocked:
            skip(blocked)
            continue
        seen.add(key)
        total += size
        attached.append((filename, data))
    return attached, skipped
def result_message_id(key, digest):
    """Message-ID of a result email: the same result file with the same
    bytes always gets the same id, so the mailbox can be searched for it."""
    token = hashlib.sha256(
        ("%s\0%s" % (key, digest or "")).encode("utf-8")).hexdigest()[:32]
    return "<jarvis-result.%s@jarvis-bridge>" % token
def build_result_message(from_addr, to_addr, subject, body, deny_paths=(),
                         getsize=os.path.getsize, message_id=None):
    """Build the outgoing result email. Returns (msg, attached_names, skipped).
    No declared attachments: the exact single-part text message used before
    attachment support. Otherwise the JSON body gains "attachments" and
    "attachments_skipped"; with at least one valid file the message is
    multipart/mixed with the JSON as the first text/plain part.
    `message_id` (see result_message_id) is set as the Message-ID header."""
    msg, names, skipped = _build_result_message(
        from_addr, to_addr, subject, body, deny_paths, getsize)
    if message_id:
        msg["Message-ID"] = message_id
    return msg, names, skipped
def _build_result_message(from_addr, to_addr, subject, body, deny_paths,
                          getsize):
    report = None
    try:
        report = json.loads(body.lstrip("\ufeff"))
    except (json.JSONDecodeError, ValueError):
        pass
    declared = report.get("attachments") if isinstance(report, dict) else None
    if not declared:
        out = email.message.EmailMessage()
        out["From"] = from_addr
        out["To"] = to_addr
        out["Subject"] = subject
        out.set_content(body)
        return out, [], []
    attached, skipped = collect_attachments(
        declared, report.get("workspace"), deny_paths, getsize)
    names = [name for name, _ in attached]
    report["attachments"] = names
    report["attachments_skipped"] = skipped
    new_body = json.dumps(report, indent=2, ensure_ascii=False)
    if not attached:
        out = email.message.EmailMessage()
        out["From"] = from_addr
        out["To"] = to_addr
        out["Subject"] = subject
        out.set_content(new_body)
        return out, names, skipped
    out = MIMEMultipart("mixed")
    out["From"] = from_addr
    out["To"] = to_addr
    out["Subject"] = subject
    out.attach(MIMEText(new_body, "plain", "utf-8"))
    for name, data in attached:
        part = MIMEApplication(data)
        part.add_header("Content-Disposition", "attachment", filename=name)
        out.attach(part)
    return out, names, skipped
def is_permanent_rejection(exc):
    """True when the server refused this one message for good (5xx reply to
    DATA: blocked content, too large, policy). Login, connection and 4xx
    errors are temporary and worth retrying."""
    return isinstance(exc, smtplib.SMTPDataError) and \
        500 <= int(getattr(exc, "smtp_code", 0) or 0) < 600
def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
def _smtp_error_text(exc):
    text = getattr(exc, "smtp_error", None)
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    text = " ".join(str(text if text is not None else exc).split())
    return text[:300]
# --------------------------------------------------------------------------
# Bridge engine
# --------------------------------------------------------------------------
# re-issue IDLE this often so a stop never waits long
IDLE_CYCLE_SECONDS = bridge_setting("timeouts.idle_cycle_seconds", 60)
# newest search hits header-checked per query
MAILBOX_CHECK_MAX_MATCHES = bridge_setting("limits.mailbox_check_max_matches",
                                           100)
_IMAP_LIST_RE = re.compile(r'^\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$')
def _imap_quote(text):
    return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"')
def imap_connect():
    if IMAP_SSL:
        return imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    return imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
class Bridge:
    def __init__(self, config_path, log=None,
                 imap_factory=None, smtp_factory=None):
        self.config_path = config_path
        self._log_fn = log or (lambda line: print(line, flush=True))
        self._imap_factory = imap_factory or imap_connect
        self._smtp_factory = smtp_factory or (
            lambda: smtplib.SMTP(SMTP_HOST, SMTP_PORT))
        self.stats = {
            "tasks_received": 0,
            "results_sent": 0,
            "last_poll": None,
            "last_error": None,
            "running": False,
            "idle": "off",
            "inboxes": {},
        }
        self._clock = time.time
        self._sent_mem = set()  # sent this run, even if sent.json write failed
        self._retry = {}        # key -> {"attempts", "next"} after temp errors
    # -- logging ------------------------------------------------------
    def log(self, line):
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._log_fn("[%s] %s" % (stamp, line))
    def _note_error(self, where, exc):
        msg = "%s: %s: %s" % (where, type(exc).__name__, exc)
        self.stats["last_error"] = msg
        self.log("ERROR " + msg)
    def inbox_stats(self, executor):
        """Per-inbox watcher state shown by the GUI (times are time.time())."""
        return self.stats["inboxes"].setdefault(executor, {
            "last_scan": None, "pending": 0, "undeliverable": 0, "error": None,
            "last_task": None, "last_task_time": None,
            "last_result": None, "last_result_time": None,
        })
    # -- config -------------------------------------------------------
    def load(self):
        return load_config(self.config_path)
    # -- Loop A: tasks in ---------------------------------------------
    def _dedupe_paths(self, cfg, task_id):
        """Every place a task package with this id may already sit: each
        executor's inbox, its <inbox>\\done folder (where the runner moves
        finished packages) and its done dir (task ids are unique across
        executors)."""
        fname = "%s%s" % (task_id, TASK_FILE_SUFFIX)
        paths = []
        for executor, inbox in cfg["inboxes"].items():
            paths.append(os.path.join(inbox, fname))
            paths.append(os.path.join(inbox, "done", fname))
            paths.append(os.path.join(done_dir_for(cfg, executor), fname))
        return paths
    def _queue_skip_report(self, cfg, msg, raw, imap_seq, reason, extra=None,
                           task_id=None):
        """Drop a skip result into the default executor's results_dir for
        Loop B to mail. Called before the message is marked Seen: if this
        raises, the message stays UNSEEN and is retried, and sent.json stops
        any second send."""
        if not is_task_subject(decoded_subject(msg)):
            return
        name, result = build_skip_result(msg, raw, imap_seq, reason,
                                         extra=extra, task_id=task_id)
        executor = skip_executor(cfg)
        results_dir = results_dir_for(cfg, executor)
        path = os.path.join(results_dir, name)
        sent, _ = self._load_sent(cfg)
        if sent_key(cfg, executor, name) in sent or os.path.exists(path):
            return
        os.makedirs(results_dir, exist_ok=True)
        write_json_atomic(path, result)
        self.log("Loop A: skip report queued: %s" % name)
    def loop_a(self, cfg=None):
        cfg = cfg or self.load()
        for executor, inbox in cfg["inboxes"].items():
            os.makedirs(inbox, exist_ok=True)
            os.makedirs(done_dir_for(cfg, executor), exist_ok=True)
        imap = self._imap_factory()
        try:
            imap.login(cfg["email"], cfg["app_password"])
            imap.select(IMAP_FOLDER)
            typ, data = imap.search(
                None, TASK_SEARCH_CRITERIA, "SUBJECT",
                '"%s"' % TASK_SUBJECT_PREFIX)
            if typ != "OK":
                self.log("Loop A: IMAP search failed")
                return 0
            ids = (data[0] or b"").split()
            self.log("Loop A: %d unseen task email(s)" % len(ids))
            received = 0
            for num in ids:
                try:
                    typ, fetched = imap.fetch(num, "(RFC822)")
                    if typ != "OK" or not fetched or not fetched[0]:
                        continue
                    raw = fetched[0][1]
                    msg = email.message_from_bytes(raw)
                    task, reason = validate_task_message(msg, cfg)
                    if task is None:
                        self.log("Loop A: skipped message %s (%s)"
                                 % (num.decode(), reason))
                        self._queue_skip_report(cfg, msg, raw, num.decode(),
                                                reason)
                        imap.store(num, "+FLAGS", "\\Seen")
                        continue
                    reason = task_timeout_problem(task)
                    if reason:
                        task_id = str(task["task_id"]).strip()
                        self.log("Loop A: REJECTED task %s (%s) -- not run, "
                                 "failure result queued" % (task_id, reason))
                        self._queue_skip_report(
                            cfg, msg, raw, num.decode(), reason, extra={
                                "status": "failed",
                                "missing_fields": ["timeout_minutes"]
                                if reason == "missing-timeout" else [],
                                "invalid_fields": [] if reason ==
                                "missing-timeout" else ["timeout_minutes"],
                            }, task_id=task_id)
                        imap.store(num, "+FLAGS", "\\Seen")
                        continue
                    executor, reason = resolve_executor(task, cfg)
                    if executor is None:
                        wanted = task.get("executor")
                        self.log("Loop A: refused task %s (%s) -- not run"
                                 % (str(task["task_id"]).strip(), reason))
                        self._queue_skip_report(
                            cfg, msg, raw, num.decode(), reason, extra={
                                "executor": wanted if isinstance(wanted, str)
                                else repr(wanted),
                                "configured_executors": sorted(cfg["inboxes"]),
                            })
                        imap.store(num, "+FLAGS", "\\Seen")
                        continue
                    task_id = str(task["task_id"]).strip()
                    existing = next((p for p in self._dedupe_paths(cfg, task_id)
                                     if os.path.exists(p)), None)
                    if existing:
                        self.log("Loop A: duplicate task %s, skipping "
                                 "(already at %s)" % (task_id, existing))
                        self._queue_skip_report(
                            cfg, msg, raw, num.decode(),
                            "duplicate-task (%s)" % task_id,
                            extra={"executor": executor,
                                   "duplicate_of": portable_path(existing)},
                            task_id=task_id)
                        imap.store(num, "+FLAGS", "\\Seen")
                        continue
                    task["executor"] = executor
                    place_task_workspace(cfg, executor, task,
                                         lambda l: self.log("Loop A: " + l))
                    inbox_path = os.path.join(cfg["inboxes"][executor],
                                              task_id + TASK_FILE_SUFFIX)
                    write_json_atomic(inbox_path, task)
                    imap.store(num, "+FLAGS", "\\Seen")
                    received += 1
                    self.stats["tasks_received"] += 1
                    st = self.inbox_stats(executor)
                    st["last_task"], st["last_task_time"] = task_id, time.time()
                    self.log("Loop A: task %s -> %s inbox" % (task_id, executor))
                except Exception as exc:  # never crash the loop on one message
                    self._note_error("Loop A message %s" % num, exc)
            return received
        finally:
            try:
                imap.close()
            except Exception:
                pass
            try:
                imap.logout()
            except Exception:
                pass
    # -- Loop B: results out ------------------------------------------
    def _state_path(self, name):
        return os.path.join(os.path.dirname(self.config_path), name)
    def _load_sent(self, cfg):
        """Keys already mailed: sent.json plus everything sent by this
        process (covers a sent.json write that failed)."""
        path = self._state_path(SENT_TRACKER_NAME)
        try:
            with open(path, "r", encoding="utf-8") as f:
                sent = set(json.load(f))
        except (OSError, ValueError, TypeError):
            sent = set()
        return sent | self._sent_mem, path
    def _save_sent(self, sent, path):
        write_json_atomic(path, sorted(sent))
    def _load_undeliverable(self):
        path = self._state_path(UNDELIVERABLE_TRACKER_NAME)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        return (data if isinstance(data, dict) else {}), path
    def _is_undeliverable(self, undeliverable, key, path):
        """True while the file still has the exact bytes Gmail refused."""
        entry = undeliverable.get(key)
        if not isinstance(entry, dict):
            return False
        try:
            return entry.get("sha256") == file_sha256(path)
        except OSError:
            return True
    def _load_send_state(self):
        """send-state.json: {key: {"state": "sending"|"retrying"|"gave-up",
        "attempts", "sha256", "message_id", ...}}. Written BEFORE every SMTP
        DATA attempt, so the attempt count survives crashes and restarts."""
        path = self._state_path(SEND_STATE_NAME)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        return (data if isinstance(data, dict) else {}), path
    def _gave_up(self, send_state, key, path):
        """True while the file still has the bytes the bridge gave up on."""
        entry = send_state.get(key)
        if not isinstance(entry, dict) or entry.get("state") != "gave-up":
            return False
        try:
            return entry.get("sha256") == file_sha256(path)
        except OSError:
            return True
    def _queue_send_failure_notice(self, path, key, task_id, body, why,
                                   attempts, smtp_code=None):
        """Drop a small text-only result next to a result file the bridge
        could not deliver, so Jarvis hears about the failure instead of
        silence. Never done for a notice itself (no notice loops)."""
        name = os.path.basename(path)
        if SEND_FAILED_TAG in name:
            return None
        safe_id = task_id if _SUBJECT_TASK_ID_RE.match(task_id or "") \
            else task_dir_name(task_id)
        tag = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        notice = os.path.join(os.path.dirname(path), "%s%s%s%s" % (
            safe_id, SEND_FAILED_TAG, tag, RESULT_FILE_SUFFIX))
        if os.path.exists(notice):
            return None
        try:
            original = json.loads(body.lstrip("\ufeff"))
        except (json.JSONDecodeError, ValueError):
            original = None
        original = original if isinstance(original, dict) else {}
        excerpt = str(original.get("jarvis_report") or "")
        if len(excerpt) > 4000:
            excerpt = excerpt[:4000] + "\n...[truncated]"
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        report = ("RESULT EMAIL UNDELIVERABLE: the bridge could not email the "
                  "result of task %s (%s) after %d attempt(s) and has stopped "
                  "retrying. The result file is still on %s at %s; edit or "
                  "replace it to send it again."
                  % (task_id, why, attempts, socket.gethostname(), path))
        if excerpt:
            report += "\n\nReport excerpt from the undelivered result:\n" + excerpt
        write_json_atomic(notice, {
            "task_id": task_id,
            "status": "failed",
            "subtype": "result-undeliverable",
            "result_undeliverable": True,
            "undelivered_result_file": portable_path(path),
            "send_attempts": attempts,
            "smtp_code": smtp_code,
            "send_error": why,
            "exit_code": original.get("exit_code"),
            "timed_out": bool(original.get("timed_out")),
            "workspace": "",
            "mode": str(original.get("mode") or ""),
            "started": original.get("started") or stamp,
            "finished": original.get("finished") or stamp,
            "timestamp": stamp,
            "stdout": "",
            "stderr": "",
            "jarvis_report": report,
            "attachments": [],
            "attachments_skipped": [],
        })
        self.log("Loop B: failure notice queued for %s: %s"
                 % (task_id, os.path.basename(notice)))
        return notice
    def _retry_due(self, key, now):
        entry = self._retry.get(key)
        return entry is None or now >= entry["next"]
    def _pending(self, cfg, sent):
        """Result files across every configured inbox that are due to be
        mailed now, as [(executor, path, sent_key)]: not sent, not refused
        by Gmail, and not waiting out a retry delay. Also refreshes the
        per-inbox watcher stats."""
        out = []
        now = time.time()
        due_at = self._clock()
        undeliverable, _ = self._load_undeliverable()
        send_state, _ = self._load_send_state()
        for executor, d in result_sources(cfg):
            files = sorted(glob.glob(
                os.path.join(glob.escape(d), "*" + RESULT_FILE_SUFFIX)))
            mine = [(executor, p, sent_key(cfg, executor, os.path.basename(p)))
                    for p in files]
            mine = [m for m in mine if m[2] not in sent]
            refused = [m for m in mine
                       if self._is_undeliverable(undeliverable, m[2], m[1])
                       or self._gave_up(send_state, m[2], m[1])]
            mine = [m for m in mine if m not in refused]
            out += [m for m in mine if self._retry_due(m[2], due_at)]
            for ex in cfg["inboxes"]:
                if _same_path(results_dir_for(cfg, ex), d):
                    st = self.inbox_stats(ex)
                    st["last_scan"] = now
                    st["pending"] = len(mine)
                    st["undeliverable"] = len(refused)
                    st["error"] = None if os.path.isdir(d) else "folder missing"
        return out
    def _record_undeliverable(self, key, path, task_id, exc):
        undeliverable, upath = self._load_undeliverable()
        try:
            digest = file_sha256(path)
        except OSError:
            digest = None
        undeliverable[key] = {
            "task_id": task_id,
            "sha256": digest,
            "smtp_code": getattr(exc, "smtp_code", None),
            "error": _smtp_error_text(exc),
            "refused_at": datetime.now().astimezone().isoformat(
                timespec="seconds"),
        }
        write_json_atomic(upath, undeliverable)
    def _schedule_retry(self, key):
        entry = self._retry.get(key) or {"attempts": 0}
        entry["attempts"] += 1
        delay = min(RESULT_RETRY_MAX_SECONDS,
                    RESULT_RETRY_FIRST_SECONDS *
                    RESULT_RETRY_BACKOFF_FACTOR ** (entry["attempts"] - 1))
        entry["next"] = self._clock() + delay
        self._retry[key] = entry
        return delay
    def _mark_sent(self, key, sent, sent_path):
        """From here on the result is never mailed again by this process,
        even if sent.json cannot be written."""
        sent.add(key)
        self._sent_mem.add(key)
        self._retry.pop(key, None)
        try:
            self._save_sent(sent, sent_path)
        except Exception as exc:
            self._note_error("Loop B %s (kept in memory, saved on "
                             "the next send)" % SENT_TRACKER_NAME, exc)
    @staticmethod
    def _result_mailboxes(imap):
        """Folders searched for earlier result mail: Gmail's All Mail and
        Trash (found by their special-use flags); INBOX when there is no
        All Mail folder."""
        boxes, have_all = [], False
        try:
            typ, data = imap.list()
        except Exception:
            typ, data = None, None
        if typ == "OK":
            for line in data or []:
                if isinstance(line, bytes):
                    line = line.decode("ascii", "replace")
                m = _IMAP_LIST_RE.match(str(line))
                if not m:
                    continue
                flags = m.group("flags").lower().split()
                if "\\all" in flags:
                    boxes.insert(0, m.group("name"))
                    have_all = True
                elif "\\trash" in flags:
                    boxes.append(m.group("name"))
        if not have_all:
            boxes.insert(0, IMAP_FOLDER)
        return boxes
    def _find_in_mailbox(self, imap, cfg, task_id, message_id, primary):
        """Where an earlier email of this result already sits in the
        mailbox, or None. Matches this file's Message-ID; for the task's
        main result file (<task-id>.jarvis-result.json) also any
        "[JARVIS-RESULT] <task-id>" from the bridge address. Raises when no
        folder could be searched."""
        want = "%s %s" % (RESULT_SUBJECT_PREFIX, task_id)
        own = (cfg["email"] or "").strip().lower()
        queries = [("HEADER", "Message-ID", _imap_quote(message_id))]
        if primary:
            term = want.encode("ascii", "ignore").decode("ascii")
            queries.append(("SUBJECT", _imap_quote(term)))
        searched = 0
        for box in self._result_mailboxes(imap):
            typ, _ = imap.select(box, readonly=True)
            if typ != "OK":
                continue
            searched += 1
            for query in queries:
                typ, data = imap.search(None, *query)
                if typ != "OK":
                    raise RuntimeError("IMAP search failed in %s" % box)
                ids = (data[0] or b"").split() if data else []
                for num in reversed(ids[-MAILBOX_CHECK_MAX_MATCHES:]):
                    typ, fetched = imap.fetch(num, "(BODY.PEEK[HEADER.FIELDS "
                                              "(SUBJECT FROM DATE MESSAGE-ID)])")
                    if typ != "OK" or not fetched or \
                            not isinstance(fetched[0], tuple):
                        continue
                    hdr = email.message_from_bytes(fetched[0][1])
                    if extract_from_addr(hdr) != own:
                        continue
                    if (hdr.get("Message-ID") or "").strip() == message_id or \
                            (primary and decoded_subject(hdr) == want):
                        return "%r dated %s in %s" % (
                            decoded_subject(hdr), hdr.get("Date", "?"), box)
        if not searched:
            raise RuntimeError("no mailbox folder could be selected")
        return None
    def _report_send_failure(self, imap, cfg, executor, path, key, task_id,
                             body, message_id, name, attempts, exc, sent,
                             sent_path):
        """After a 552 or the last allowed attempt: if Gmail filed the mail
        anyway it counts as sent; otherwise queue the failure notice."""
        try:
            found = self._find_in_mailbox(
                imap, cfg, task_id, message_id,
                name == task_id + RESULT_FILE_SUFFIX) if imap else None
        except Exception:
            found = None
        if found:
            self._mark_sent(key, sent, sent_path)
            self.log("Loop B: result %s is in the mailbox despite the send "
                     "error (%s) -- marked sent" % (task_id, found))
            return
        try:
            self._queue_send_failure_notice(
                path, key, task_id, body,
                "%s: %s" % (type(exc).__name__, _smtp_error_text(exc)),
                attempts, getattr(exc, "smtp_code", None))
        except Exception as n_exc:
            self._note_error("Loop B failure notice for %s" % name, n_exc)
    @staticmethod
    def _close_imap(imap):
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass
        return None
    def loop_b(self, cfg=None):
        cfg = cfg or self.load()
        sent, sent_path = self._load_sent(cfg)
        pending = self._pending(cfg, sent)
        if not pending:
            return 0
        self.log("Loop B: %d unsent result(s)" % len(pending))
        emailed = 0
        smtp = None
        imap = None
        undeliverable_path = self._state_path(UNDELIVERABLE_TRACKER_NAME)
        try:
            for executor, path, key in pending:
                name = os.path.basename(path)
                task_id = ""
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        body = f.read()
                except Exception as exc:
                    delay = self._schedule_retry(key)
                    self._note_error("Loop B result %s (retry in %ds)"
                                     % (name, delay), exc)
                    continue
                try:
                    task_id = str(json.loads(body).get("task_id") or "")
                except (json.JSONDecodeError, ValueError, AttributeError):
                    task_id = ""
                if not task_id:
                    task_id = name[:-len(RESULT_FILE_SUFFIX)]
                message_id = result_message_id(
                    key, hashlib.sha256(body.encode("utf-8")).hexdigest())
                # Gmail can file a message and still answer DATA with an
                # error, so a send is only attempted after the mailbox shows
                # no earlier copy of this result.
                try:
                    if imap is None:
                        imap = self._imap_factory()
                        imap.login(cfg["email"], cfg["app_password"])
                    found = self._find_in_mailbox(
                        imap, cfg, task_id, message_id,
                        name == task_id + RESULT_FILE_SUFFIX)
                except Exception as exc:
                    imap = self._close_imap(imap)
                    delay = self._schedule_retry(key)
                    self._note_error("Loop B result %s not sent: mailbox "
                                     "check failed (retry in %ds)"
                                     % (name, delay), exc)
                    continue
                if found:
                    self._mark_sent(key, sent, sent_path)
                    st = self.inbox_stats(executor)
                    st["pending"] = max(0, st["pending"] - 1)
                    self.log("Loop B: result %s already in the mailbox (%s) "
                             "-- marked sent, not mailed again"
                             % (task_id, found))
                    continue
                # Connect + build: nothing can reach the mailbox yet, so a
                # failure here is retried with backoff and never counted.
                try:
                    if smtp is None:
                        smtp = self._smtp_factory()
                        smtp.ehlo()
                        if SMTP_STARTTLS:
                            smtp.starttls(
                                context=ssl.create_default_context())
                            smtp.ehlo()
                        smtp.login(cfg["email"], cfg["app_password"])
                    out, att_names, att_skipped = build_result_message(
                        cfg["email"], cfg["email"],
                        "%s %s" % (RESULT_SUBJECT_PREFIX, task_id), body,
                        deny_paths=(self.config_path, sent_path,
                                    undeliverable_path,
                                    self._state_path(SEND_STATE_NAME)),
                        message_id=message_id)
                except Exception as exc:
                    delay = self._schedule_retry(key)
                    self._note_error("Loop B result %s (retry in %ds)"
                                     % (name, delay), exc)
                    if smtp is not None:
                        try:
                            smtp.close()
                        except Exception:
                            pass
                        smtp = None
                    continue
                # Durable watermark BEFORE the DATA attempt: the attempt
                # count survives a crash/restart, so sends stay bounded.
                try:
                    digest = file_sha256(path)
                except OSError:
                    digest = None
                state, state_path = self._load_send_state()
                entry = state.get(key)
                if not isinstance(entry, dict) or entry.get("sha256") != digest:
                    entry = {"attempts": 0}
                attempt = int(entry.get("attempts") or 0) + 1
                entry.update({
                    "task_id": task_id, "sha256": digest,
                    "message_id": message_id, "state": "sending",
                    "attempts": attempt,
                    "last_attempt": datetime.now().astimezone().isoformat(
                        timespec="seconds")})
                state[key] = entry
                try:
                    write_json_atomic(state_path, state)
                except Exception as exc:
                    delay = self._schedule_retry(key)
                    self._note_error("Loop B result %s not sent: %s could not "
                                     "be written (retry in %ds)"
                                     % (name, SEND_STATE_NAME, delay), exc)
                    continue
                for s in att_skipped:
                    self.log("Loop B: result %s attachment skipped: %s (%s)"
                             % (task_id, s["path"], s["reason"]))
                try:
                    smtp.sendmail(cfg["email"], [cfg["email"]], out.as_string())
                except Exception as exc:
                    give_up = False
                    if is_permanent_rejection(exc):
                        self._note_error(
                            "Loop B result %s refused by the mail server, "
                            "not retried (edit or replace the result file to "
                            "send again)" % name, exc)
                        self._retry.pop(key, None)
                        entry["state"] = "refused"
                        give_up = True
                        try:
                            self._record_undeliverable(key, path, task_id, exc)
                        except Exception as rec_exc:
                            self._note_error("Loop B %s"
                                             % UNDELIVERABLE_TRACKER_NAME,
                                             rec_exc)
                            self._schedule_retry(key)
                    elif attempt >= RESULT_SEND_MAX_ATTEMPTS:
                        self._note_error(
                            "Loop B result %s GAVE UP after %d send attempts "
                            "(edit or replace the result file to send again)"
                            % (name, attempt), exc)
                        self._retry.pop(key, None)
                        entry["state"] = "gave-up"
                        give_up = True
                    else:
                        delay = self._schedule_retry(key)
                        entry["state"] = "retrying"
                        self._note_error("Loop B result %s (attempt %d/%d, "
                                         "retry in %ds)"
                                         % (name, attempt,
                                            RESULT_SEND_MAX_ATTEMPTS, delay),
                                         exc)
                    entry["last_error"] = _smtp_error_text(exc)
                    entry["smtp_code"] = getattr(exc, "smtp_code", None)
                    try:
                        write_json_atomic(state_path, state)
                    except Exception as st_exc:
                        self._note_error("Loop B %s" % SEND_STATE_NAME, st_exc)
                    if smtp is not None and not isinstance(
                            exc, smtplib.SMTPDataError):
                        try:
                            smtp.close()
                        except Exception:
                            pass
                        smtp = None
                    if give_up:
                        self._report_send_failure(
                            imap, cfg, executor, path, key, task_id, body,
                            message_id, name, attempt, exc, sent, sent_path)
                    continue
                # mark sent ONLY after the SMTP call succeeds
                state.pop(key, None)
                try:
                    write_json_atomic(state_path, state)
                except Exception as st_exc:
                    self._note_error("Loop B %s" % SEND_STATE_NAME, st_exc)
                self._mark_sent(key, sent, sent_path)
                emailed += 1
                self.stats["results_sent"] += 1
                st = self.inbox_stats(executor)
                st["last_result"], st["last_result_time"] = task_id, time.time()
                st["pending"] = max(0, st["pending"] - 1)
                if att_names:
                    self.log("Loop B: result %s emailed from %s inbox "
                             "(multipart/mixed, %d attachment(s): %s)"
                             % (task_id, executor, len(att_names),
                                ", ".join(att_names)))
                else:
                    self.log("Loop B: result %s emailed from %s inbox"
                             % (task_id, executor))
            return emailed
        finally:
            self._close_imap(imap)
            if smtp is not None:
                try:
                    smtp.quit()
                except Exception:
                    pass
    def _quick_results(self):
        """Between IMAP polls: mail results that appeared since the last
        pass. Results waiting out a retry delay or refused by the mail
        server are skipped by _pending."""
        try:
            cfg = self.load()
            sent, _ = self._load_sent(cfg)
            if self._pending(cfg, sent):
                self.loop_b(cfg)
        except Exception as exc:
            self._note_error("Loop B", exc)
    # -- IMAP IDLE ----------------------------------------------------
    def _idle_watch(self, stop_event, wake):
        """Keep an IMAP IDLE session open and set `wake` when new mail
        arrives. Any failure drops back to plain polling and retries after
        poll_seconds."""
        state = {"now": None}
        def set_state(new, detail, poll):
            self.stats["idle"] = new
            if new == state["now"]:
                return
            state["now"] = new
            if new == "active":
                self.log("IMAP IDLE active: new task mail is picked up at "
                         "once (%ds poll stays on as fallback)" % poll)
            elif new == "off":
                self.log("IMAP IDLE off in config: polling every %ds" % poll)
            else:
                self.log("IMAP IDLE unavailable (%s): polling every %ds"
                         % (detail, poll))
        while not stop_event.is_set():
            try:
                cfg = self.load()
            except Exception:
                stop_event.wait(CONFIG_LOAD_ERROR_WAIT_SECONDS)
                continue
            poll = cfg["poll_seconds"]
            if not cfg["idle"]:
                set_state("off", "", poll)
                stop_event.wait(poll)
                continue
            if not (cfg["email"] and cfg["app_password"]):
                set_state("fallback", "no credentials", poll)
                stop_event.wait(poll)
                continue
            imap = None
            try:
                imap = self._imap_factory()
                if not callable(getattr(imap, "idle", None)):
                    raise RuntimeError("IMAP client has no IDLE support")
                imap.login(cfg["email"], cfg["app_password"])
                imap.select(IMAP_FOLDER)
                caps = getattr(imap, "capabilities", None)
                if caps and "IDLE" not in caps:
                    raise RuntimeError("server does not offer IDLE")
                set_state("active", "", poll)
                while not stop_event.is_set():
                    with imap.idle(duration=IDLE_CYCLE_SECONDS) as idler:
                        for typ, _data in idler:
                            if stop_event.is_set():
                                break
                            if str(typ).upper() == "EXISTS":
                                wake.set()
                                break
                    if not self.load()["idle"]:
                        break
            except Exception as exc:
                set_state("fallback", "%s: %s" % (type(exc).__name__, exc),
                          poll)
                stop_event.wait(poll)
            finally:
                if imap is not None:
                    try:
                        imap.logout()
                    except Exception:
                        pass
    # -- driver --------------------------------------------------------
    def loop_once(self):
        cfg = self.load()  # hot-reload: GUI edits apply on the next poll
        self.stats["last_poll"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.loop_a(cfg)
        except Exception as exc:
            self._note_error("Loop A", exc)
        try:
            self.loop_b(cfg)
        except Exception as exc:
            self._note_error("Loop B", exc)
    def run_forever(self, stop_event):
        self.stats["running"] = True
        self.log("Bridge started (config: %s)" % self.config_path)
        self.log(settings_status())
        wake = threading.Event()
        try:
            try:
                cfg = self.load()
                ensure_layout(cfg, self.log)
                self.log("Device %s; executor inboxes: %s" % (
                    device_tag(cfg), ", ".join(
                        "%s=%s" % kv for kv in cfg["inboxes"].items())))
            except Exception as exc:
                self._note_error("config", exc)
            threading.Thread(target=self._idle_watch, args=(stop_event, wake),
                             name="imap-idle", daemon=True).start()
            while not stop_event.is_set():
                try:
                    cfg = self.load()
                except Exception as exc:
                    self._note_error("config", exc)
                    cfg = default_config()
                wake.clear()
                self.loop_once()
                poll = _int_setting(cfg.get("poll_seconds"),
                                    POLL_SECONDS_DEFAULT, POLL_SECONDS_MIN)
                quick = _int_setting(cfg.get("results_poll_seconds"),
                                     RESULTS_POLL_SECONDS_DEFAULT,
                                     RESULTS_POLL_SECONDS_MIN)
                deadline = time.monotonic() + poll
                while not stop_event.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    if wake.wait(min(quick, remaining)):
                        self.log("IMAP IDLE: new mail -- polling now")
                        break
                    if not stop_event.is_set():
                        self._quick_results()
        finally:
            self.stats["running"] = False
            self.log("Bridge stopped")
    def run_once(self):
        self.loop_once()
def test_connection(config_path):
    """Try an IMAP login with the configured credentials. Returns
    (ok: bool, message: str). Never includes the password in the message."""
    try:
        cfg = load_config(config_path)
    except Exception as exc:
        return False, "Config error: %s" % exc
    if not cfg["email"] or not cfg["app_password"]:
        return False, "Email and app password must both be set."
    try:
        imap = imap_connect()
        try:
            imap.login(cfg["email"], cfg["app_password"])
            imap.select(IMAP_FOLDER)
            return True, "Connected to %s as %s." % (IMAP_HOST, cfg["email"])
        finally:
            try:
                imap.logout()
            except Exception:
                pass
    except imaplib.IMAP4.error as exc:
        text = str(exc)
        if "535" in text:
            return False, ("Login rejected (535): wrong app password. "
                            "Create a fresh one at %s." % APP_PASSWORD_HELP_URL)
        return False, "IMAP login failed: %s" % text
    except Exception as exc:
        return False, "Connection failed: %s: %s" % (type(exc).__name__, exc)
