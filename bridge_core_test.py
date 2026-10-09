"""
bridge_core_test.py -- stdlib-fake tests for the bridge engine.
Covers the DKIM auth accept/reject matrix, dedupe (+ duplicate skip
notices), malformed-task skip notices, Loop B send-tracking,
config migration, executor routing/refusal, multi-inbox Loop B, first-run
init, IMAP IDLE wake-up/fallback and the single-instance lock. No real
Gmail needed; the user profile is redirected to a temp folder.
Run:  python bridge_core_test.py
"""
import atexit
import email
import email.message
import json
import os
import shutil
import sys
import tempfile
import threading
_PROFILE = tempfile.mkdtemp(prefix="bridge-test-profile-")
atexit.register(shutil.rmtree, _PROFILE, True)
os.environ["USERPROFILE"] = _PROFILE
os.environ["HOME"] = _PROFILE
for _var in ("OPENCLAW_HOME", "OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH",
             "OPENCLAW_WORKSPACE_DIR", "OPENCLAW_PROFILE"):
    os.environ.pop(_var, None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_core  # noqa: E402
from bridge_core import Bridge  # noqa: E402
BRIDGE_EMAIL = "bridge.test@example.com"
OTHER_EMAIL = "attacker@evil.com"
TASK = {
    "task_id": "bus-smoke-011",
    "workspace": os.path.join(_PROFILE, "workspace"),
    "goal": "smoke",
    "acceptance_criteria": ["inbox file appears"],
    "timeout_minutes": 30,
}
GOOD_AR = ("mx.google.com; dkim=pass header.i=@gmail.com header.s=20230601 "
           "header.d=gmail.com; spf=pass smtp.mailfrom=gmail.com")
PASS = 0
FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s  %s" % (name, detail))
def make_msg(from_addr=BRIDGE_EMAIL, ar_headers=(GOOD_AR,), body=None,
             subject="[JARVIS-TASK] bus-smoke-011"):
    m = email.message.EmailMessage()
    m["From"] = from_addr
    m["To"] = BRIDGE_EMAIL
    m["Subject"] = subject
    for ar in ar_headers:
        m["Authentication-Results"] = ar
    m.set_content(body if body is not None else json.dumps(TASK))
    return m.as_bytes()
class FakeIMAP:
    def __init__(self, messages):
        self.messages = list(messages)  # list of raw bytes
        self.seen = set()
        self.logged_in = False
    def login(self, user, password):
        self.logged_in = True
        self.login_args = (user, password)
    def select(self, box, readonly=False):
        return "OK", [b"1"]
    def search(self, *args):
        ids = [str(i + 1).encode() for i, m in enumerate(self.messages)
               if (i + 1) not in self.seen]
        return "OK", [b" ".join(ids)]
    def fetch(self, num, _spec):
        idx = int(num) - 1
        return "OK", [(b"%s (RFC822 {%d})" % (num, len(self.messages[idx])),
                       self.messages[idx])]
    def store(self, num, _op, _flag):
        self.seen.add(int(num))
        return "OK", []
    def close(self):
        return "OK", []
    def logout(self):
        return "OK", []
class FakeSMTP:
    instances = []
    def __init__(self, fail_send=False):
        self.fail_send = fail_send
        self.sent = []
        FakeSMTP.instances.append(self)
    def ehlo(self):
        return (250, b"ok")
    def starttls(self, context=None):
        return (220, b"ok")
    def login(self, user, password):
        self.login_args = (user, password)
    def sendmail(self, from_addr, to_addrs, msg):
        if self.fail_send:
            raise OSError("simulated failure")
        self.sent.append((from_addr, to_addrs, msg))
    def quit(self):
        return (221, b"bye")
def make_bridge(tmp, messages, smtp_fail=False):
    cfg_path = os.path.join(tmp, "gmail-bridge-config.json")
    inbox = os.path.join(tmp, "cursor-inbox")
    results = os.path.join(tmp, "cursor-results")
    os.makedirs(results, exist_ok=True)
    cfg = {
        "email": BRIDGE_EMAIL,
        "app_password": "x" * 16,
        "poll_seconds": 60,
        "inbox": inbox,
        "results_dir": results,
        "done_dir": os.path.join(tmp, "cursor-done"),
        "allowed_senders": [BRIDGE_EMAIL],
    }
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)
    logs = []
    imap = FakeIMAP(messages)
    bridge = Bridge(cfg_path, log=logs.append,
                    imap_factory=lambda: imap,
                    smtp_factory=lambda: FakeSMTP(fail_send=smtp_fail))
    return bridge, cfg, logs
def inbox_has(tmp, task_id):
    return os.path.exists(os.path.join(
        tmp, "cursor-inbox", task_id + ".jarvis-task.json"))
# --- Loop A: DKIM matrix ----------------------------------------------
def t_accept_valid():
    with tempfile.TemporaryDirectory() as tmp:
        b, cfg, logs = make_bridge(tmp, [make_msg()])
        b.loop_a()
        check("ACCEPT valid DKIM task -> inbox file", inbox_has(tmp, "bus-smoke-011"))
        check("no secret/bridge_secret required or logged",
              not any("bridge_secret" in l for l in logs))
def t_reject_bad_from():
    with tempfile.TemporaryDirectory() as tmp:
        b, cfg, logs = make_bridge(tmp, [make_msg(from_addr=OTHER_EMAIL)])
        b.loop_a()
        check("REJECT From not in allowed_senders",
              not inbox_has(tmp, "bus-smoke-011") and
              any("from-not-allowed" in l for l in logs))
def t_accept_no_ar():
    with tempfile.TemporaryDirectory() as tmp:
        b, cfg, logs = make_bridge(tmp, [make_msg(ar_headers=())])
        b.loop_a()
        check("ACCEPT internally-submitted mail with no Authentication-Results",
              inbox_has(tmp, "bus-smoke-011"))
def t_accept_current_gmail_ar():
    ar = ("mx.google.com; dkim=pass header.i=@gmail.com header.s=20251104 "
          "header.b=QLqJswrV; arc=pass (i=1); spf=pass smtp.mailfrom=gmail.com")
    with tempfile.TemporaryDirectory() as tmp:
        b, cfg, logs = make_bridge(tmp, [make_msg(ar_headers=(ar,))])
        b.loop_a()
        check("ACCEPT current Gmail AR (header.i=@gmail.com, no header.d=)",
              inbox_has(tmp, "bus-smoke-011"))
def t_reject_evil_i():
    ar = ("mx.google.com; dkim=pass header.i=@evil.com header.s=20251104")
    with tempfile.TemporaryDirectory() as tmp:
        b, cfg, logs = make_bridge(tmp, [make_msg(ar_headers=(ar,))])
        b.loop_a()
        check("REJECT dkim=pass with header.i=@evil.com",
              not inbox_has(tmp, "bus-smoke-011"))
def t_reject_dkim_fail():
    ar = ("mx.google.com; dkim=fail header.d=gmail.com; spf=pass")
    with tempfile.TemporaryDirectory() as tmp:
        b, cfg, logs = make_bridge(tmp, [make_msg(ar_headers=(ar,))])
        b.loop_a()
        check("REJECT dkim=fail", not inbox_has(tmp, "bus-smoke-011"))
def t_reject_wrong_d():
    ar = ("mx.google.com; dkim=pass header.d=evil.com; spf=pass")
    with tempfile.TemporaryDirectory() as tmp:
        b, cfg, logs = make_bridge(tmp, [make_msg(ar_headers=(ar,))])
        b.loop_a()
        check("REJECT dkim=pass with header.d=evil.com",
              not inbox_has(tmp, "bus-smoke-011"))
def t_reject_fake_ar():
    # attacker attaches their own Authentication-Results; no mx.google.com verdict
    ar = ("evil.com; dkim=pass header.d=gmail.com; spf=pass")
    with tempfile.TemporaryDirectory() as tmp:
        b, cfg, logs = make_bridge(tmp, [make_msg(ar_headers=(ar,))])
        b.loop_a()
        check("REJECT forged non-Google Authentication-Results",
              not inbox_has(tmp, "bus-smoke-011"))
def t_reject_bad_json():
    with tempfile.TemporaryDirectory() as tmp:
        b, cfg, logs = make_bridge(tmp, [make_msg(body="{not json")])
        b.loop_a()  # must not raise
        check("REJECT malformed JSON without crashing",
              not inbox_has(tmp, "bus-smoke-011"))
def t_dedupe():
    with tempfile.TemporaryDirectory() as tmp:
        b, cfg, logs = make_bridge(tmp, [make_msg()])
        b.loop_a()
        b2logs = []
        imap2 = FakeIMAP([make_msg()])
        b2 = Bridge(b.config_path, log=b2logs.append,
                    imap_factory=lambda: imap2,
                    smtp_factory=lambda: FakeSMTP())
        b2.loop_a()
        check("dedupe: duplicate task_id skipped",
              any("duplicate task" in l for l in b2logs))
# --- Loop B ------------------------------------------------------------
def t_loop_b_sends_and_tracks():
    FakeSMTP.instances.clear()
    with tempfile.TemporaryDirectory() as tmp:
        b, cfg, logs = make_bridge(tmp, [])
        result = {"task_id": "bus-smoke-011", "status": "done"}
        rp = os.path.join(cfg["results_dir"], "bus-smoke-011.jarvis-result.json")
        with open(rp, "w") as f:
            json.dump(result, f)
        b.loop_b()
        sent = FakeSMTP.instances[-1].sent
        ok_subject = sent and "[JARVIS-RESULT] bus-smoke-011" in sent[0][2]
        tracker = os.path.join(tmp, "sent.json")
        tracked = os.path.exists(tracker) and \
            "bus-smoke-011.jarvis-result.json" in open(tracker).read()
        check("Loop B emails result with correct subject", bool(ok_subject))
        check("Loop B marks sent only after success", tracked)
        n_before = len(sent)
        b.loop_b()
        check("Loop B does not resend tracked results",
              len(FakeSMTP.instances[-1].sent) == n_before)
def t_loop_b_sends_timeout_result():
    # same shape Invoke-JarvisTask.ps1 writes when it kills a task on timeout
    FakeSMTP.instances.clear()
    with tempfile.TemporaryDirectory() as tmp:
        b, cfg, logs = make_bridge(tmp, [])
        result = {
            "task_id": "timeout-sim-002", "exit_code": -1, "timed_out": True,
            "workspace": tmp, "mode": "agent", "stdout": "",
            "stderr": "--- last 50 lines of stdout (x.log) ---\nlast-line",
            "jarvis_report": "Task timeout-sim-002 timed out. The runner stopped "
                             "it after about 30 minutes because it hit the "
                             "30-minute time limit.",
            "attachments": [], "attachments_skipped": [],
        }
        rp = os.path.join(cfg["results_dir"], "timeout-sim-002.jarvis-result.json")
        with open(rp, "w", encoding="utf-8") as f:
            json.dump(result, f)
        b.loop_b()
        sent = FakeSMTP.instances[-1].sent if FakeSMTP.instances else []
        msg = sent[0][2] if sent else ""
        check("Loop B emails timeout result as [JARVIS-RESULT]",
              "Subject: [JARVIS-RESULT] timeout-sim-002" in msg)
        check("timeout email carries the plain-language report and log tail",
              "timed out" in msg and "last-line" in msg)
def t_loop_b_failure_not_marked():
    FakeSMTP.instances.clear()
    with tempfile.TemporaryDirectory() as tmp:
        b, cfg, logs = make_bridge(tmp, [], smtp_fail=True)
        rp = os.path.join(cfg["results_dir"], "bus-smoke-012.jarvis-result.json")
        with open(rp, "w") as f:
            json.dump({"task_id": "bus-smoke-012"}, f)
        b.loop_b()
        tracker = os.path.join(tmp, "sent.json")
        tracked = os.path.exists(tracker) and "bus-smoke-012" in open(tracker).read()
        check("Loop B failure: not marked sent, error logged",
              not tracked and any("ERROR" in l for l in logs))
class GmailLikeSMTP(FakeSMTP):
    """Gmail on a script attachment: files the message in the mailbox, then
    answers DATA with 552. The first `transient_left` sends (shared by all
    connections) raise OSError instead."""
    attempts = []
    transient_left = 0
    def __init__(self, refuse=False):
        super().__init__()
        self.refuse = refuse
    def sendmail(self, from_addr, to_addrs, msg):
        GmailLikeSMTP.attempts.append(msg)
        if GmailLikeSMTP.transient_left > 0:
            GmailLikeSMTP.transient_left -= 1
            raise OSError("simulated network down")
        self.sent.append((from_addr, to_addrs, msg))
        if self.refuse:
            import smtplib
            raise smtplib.SMTPDataError(
                552, b"5.7.0 This message was blocked because its content "
                     b"presents a potential security issue.")
def _gmail_bridge(tmp, refuse=False, transient=0):
    """Bridge on the make_bridge config, mailing through GmailLikeSMTP, with
    a controllable clock. A second call on the same tmp is a restart."""
    if not os.path.exists(os.path.join(tmp, "gmail-bridge-config.json")):
        GmailLikeSMTP.attempts = []
        FakeSMTP.instances.clear()
    b, cfg, logs = make_bridge(tmp, [])
    GmailLikeSMTP.transient_left = transient
    b._smtp_factory = lambda: GmailLikeSMTP(refuse=refuse)
    clock = [1000.0]
    b._clock = lambda: clock[0]
    return b, cfg, logs, clock
def _write_result(cfg, task_id, extra=None):
    rp = os.path.join(cfg["results_dir"], task_id + ".jarvis-result.json")
    data = {"task_id": task_id, "status": "done"}
    data.update(extra or {})
    with open(rp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return rp
def _cycle(b, clock, n, step):
    """n Loop B passes the way run_forever drives them: full poll, then a
    quick results check, the clock moving `step` seconds each time."""
    for _ in range(n):
        b.loop_once()
        clock[0] += step
        b._quick_results()
        clock[0] += step
def t_loop_b_refused_never_resent():
    # regression: openclaw-watcher-setup-001 was mailed every 30 s for hours
    # because Gmail's 552 reply was retried on each poll
    with tempfile.TemporaryDirectory() as tmp:
        b, cfg, logs, clock = _gmail_bridge(tmp, refuse=True)
        rp = _write_result(cfg, "refused-js-001")
        _cycle(b, clock, 6, 3600)
        und = os.path.join(tmp, "undeliverable.json")
        rec = json.load(open(und)) if os.path.exists(und) else {}
        entry = rec.get("refused-js-001.jarvis-result.json", {})
        def originals():
            return sum(1 for m in GmailLikeSMTP.attempts
                       if "result-undeliverable" not in m)
        def notices():
            return sum(1 for m in GmailLikeSMTP.attempts
                       if "result-undeliverable" in m)
        check("refused result (552) is attempted exactly once across 6 cycles",
              originals() == 1, originals())
        check("refused result: one failure notice attempted (and a refused "
              "notice gets no notice of its own)", notices() == 1, notices())
        check("refused result recorded in undeliverable.json with 552 + sha",
              entry.get("smtp_code") == 552 and
              entry.get("sha256") == bridge_core.file_sha256(rp), entry)
        check("refused result: loud error, not counted as pending",
              any("refused by the mail server" in l for l in logs) and
              b.stats["inboxes"]["cursor"]["pending"] == 0 and
              b.stats["inboxes"]["cursor"]["undeliverable"] == 2,
              b.stats["inboxes"]["cursor"])
        b2, _, _, clock2 = _gmail_bridge(tmp, refuse=True)
        _cycle(b2, clock2, 3, 3600)
        check("refused result not re-sent after a bridge restart",
              originals() == 1 and notices() == 1, len(GmailLikeSMTP.attempts))
        _write_result(cfg, "refused-js-001", {"attachments": []})
        _cycle(b2, clock2, 3, 3600)
        check("changed result file is tried again, once",
              originals() == 2 and notices() == 1, len(GmailLikeSMTP.attempts))
def t_loop_b_sent_never_resent_tracker_unwritable():
    with tempfile.TemporaryDirectory() as tmp:
        b, cfg, logs, clock = _gmail_bridge(tmp)
        os.makedirs(os.path.join(tmp, "sent.json"))  # every write fails
        _write_result(cfg, "tracker-down-001")
        _cycle(b, clock, 5, 60)
        check("sent result never re-sent even when sent.json cannot be written",
              len(GmailLikeSMTP.attempts) == 1 and
              b.stats["results_sent"] == 1, len(GmailLikeSMTP.attempts))
        check("sent.json write failure logged, not treated as a send failure",
              any("sent.json" in l and "ERROR" in l for l in logs) and
              not any("retry in" in l for l in logs))
def t_loop_b_temp_failure_backoff_then_once():
    with tempfile.TemporaryDirectory() as tmp:
        b, cfg, logs, clock = _gmail_bridge(tmp, transient=2)
        _write_result(cfg, "net-down-001")
        b.loop_b()
        for _ in range(5):  # quick checks inside the 30 s retry delay
            clock[0] += 5
            b._quick_results()
        check("temporary failure waits out a delay instead of every cycle",
              len(GmailLikeSMTP.attempts) == 1, len(GmailLikeSMTP.attempts))
        clock[0] += 10
        b.loop_once()
        clock[0] += 30
        b.loop_once()
        check("retry delay grows (30 s, then 60 s)",
              len(GmailLikeSMTP.attempts) == 2, len(GmailLikeSMTP.attempts))
        clock[0] += 60
        _cycle(b, clock, 5, 60)
        delivered = sum(len(i.sent) for i in FakeSMTP.instances
                        if isinstance(i, GmailLikeSMTP))
        check("after the outage the result is delivered exactly once",
              len(GmailLikeSMTP.attempts) == 3 and delivered == 1,
              (len(GmailLikeSMTP.attempts), delivered))
def t_loop_b_stale_result_after_restart_once():
    # a result left unsent across sleep / bridge restart goes out once
    with tempfile.TemporaryDirectory() as tmp:
        b, cfg, logs, clock = _gmail_bridge(tmp, transient=1)
        _write_result(cfg, "stale-001")
        b.loop_b()
        b2, _, _, clock2 = _gmail_bridge(tmp)
        _cycle(b2, clock2, 4, 30)
        delivered = sum(len(i.sent) for i in FakeSMTP.instances)
        tracked = "stale-001.jarvis-result.json" in json.load(
            open(os.path.join(tmp, "sent.json")))
        check("stale unsent result: sent exactly once after a restart",
              delivered == 1 and tracked and len(GmailLikeSMTP.attempts) == 2,
              (delivered, len(GmailLikeSMTP.attempts)))
# --- Loop B: mailbox check before sending ---------------------------------
class GmailMailbox:
    """One Gmail account seen from both sides: MailboxSMTP files every
    message in it (Gmail does this even when it then answers DATA with an
    error) and MailboxIMAP searches it."""
    def __init__(self):
        self.messages = []
        self.attempts = []
        self.imaps = []
        self.imap_down = False
        self.drop_left = 0
    def post(self, subject, from_addr=BRIDGE_EMAIL, message_id=None):
        m = email.message.EmailMessage()
        m["From"] = from_addr
        m["To"] = BRIDGE_EMAIL
        m["Subject"] = subject
        m["Date"] = "Fri, 02 Oct 2026 13:40:35 -0700"
        if message_id:
            m["Message-ID"] = message_id
        m.set_content("{}")
        self.messages.append(m.as_bytes())
    def subjects(self, subject):
        return sum(1 for raw in self.messages
                   if email.message_from_bytes(raw)["Subject"] == subject)
class MailboxIMAP:
    def __init__(self, box):
        self.box = box
        self.selected = []
        box.imaps.append(self)
    def login(self, user, password):
        if self.box.imap_down:
            raise OSError("simulated IMAP outage")
    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"',
                      b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"',
                      b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"']
    def select(self, box, readonly=False):
        self.selected.append((box, readonly))
        return "OK", [str(len(self.box.messages)).encode()]
    def search(self, charset, *query):
        field, value = query[0].upper(), query[-1]
        value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\").lower()
        header = "Subject" if field == "SUBJECT" else query[1]
        hits = [str(i + 1).encode() for i, raw in enumerate(self.box.messages)
                if value in str(email.message_from_bytes(raw)[header]
                                or "").lower()]
        return "OK", [b" ".join(hits)]
    def fetch(self, num, spec):
        raw = self.box.messages[int(num) - 1]
        return "OK", [(b"%s (BODY[HEADER] {%d})" % (num, len(raw)), raw), b")"]
    def logout(self):
        return "BYE", []
class MailboxSMTP(FakeSMTP):
    """Files the message, then fails the way Gmail does: `refuse` -> 552,
    box.drop_left > 0 -> connection lost after DATA (looks temporary),
    or 552 when an attachment / zip member is a script type."""
    def __init__(self, box, refuse=False, scan_scripts=True):
        super().__init__()
        self.box, self.refuse, self.scan = box, refuse, scan_scripts
    def sendmail(self, from_addr, to_addrs, msg):
        import io
        import smtplib
        import zipfile
        self.box.attempts.append(msg)
        self.box.messages.append(msg.encode("utf-8"))
        self.sent.append((from_addr, to_addrs, msg))
        if self.box.drop_left > 0:
            self.box.drop_left -= 1
            raise smtplib.SMTPServerDisconnected("connection lost")
        blocked = False
        for part in email.message_from_string(msg).walk():
            name = (part.get_filename() or "").lower()
            if name.endswith((".ps1", ".js", ".exe", ".bat")):
                blocked = True
            if name.endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(
                        part.get_payload(decode=True))) as zf:
                    blocked |= any(n.lower().endswith((".ps1", ".js"))
                                   for n in zf.namelist())
        if self.refuse or (self.scan and blocked):
            raise smtplib.SMTPDataError(
                552, b"5.7.0 This message was blocked because its content "
                     b"presents a potential 5.7.0 security issue.")
def _mailbox_bridge(tmp, box, refuse=False, drop_after_filing=0):
    b, cfg, logs = make_bridge(tmp, [])
    box.drop_left = drop_after_filing
    b._imap_factory = lambda: MailboxIMAP(box)
    b._smtp_factory = lambda: MailboxSMTP(box, refuse=refuse)
    clock = [1000.0]
    b._clock = lambda: clock[0]
    return b, cfg, logs, clock
def _sent_keys(tmp):
    p = os.path.join(tmp, "sent.json")
    return json.load(open(p)) if os.path.exists(p) else []
def t_loop_b_skips_result_already_in_mailbox():
    with tempfile.TemporaryDirectory() as tmp:
        box = GmailMailbox()
        box.post("[JARVIS-RESULT] bridge-backup-001")  # no bridge Message-ID
        b, cfg, logs, clock = _mailbox_bridge(tmp, box)
        _write_result(cfg, "bridge-backup-001")
        _cycle(b, clock, 4, 60)
        check("mailbox check: result already in the mailbox is never "
              "attempted", len(box.attempts) == 0, len(box.attempts))
        check("mailbox check: it is marked sent and logged",
              "bridge-backup-001.jarvis-result.json" in _sent_keys(tmp) and
              any("already in the mailbox" in l and "marked sent" in l
                  for l in logs), logs[-3:])
        selected = [s for i in box.imaps for s in i.selected]
        check("mailbox check: All Mail searched read-only",
              ('"[Gmail]/All Mail"', True) in selected and
              ('"[Gmail]/All Mail"', False) not in selected, selected)
def t_loop_b_mailbox_near_misses_still_sent():
    with tempfile.TemporaryDirectory() as tmp:
        box = GmailMailbox()
        box.post("[JARVIS-RESULT] task-007-live")
        box.post("Re: [JARVIS-RESULT] task-007")
        box.post("[JARVIS-RESULT] task-007", from_addr=OTHER_EMAIL)
        b, cfg, logs, clock = _mailbox_bridge(tmp, box)
        _write_result(cfg, "task-007")
        _cycle(b, clock, 4, 60)
        check("mailbox check: other task ids, replies and other senders do "
              "not count -- result mailed exactly once",
              len(box.attempts) == 1 and
              "task-007.jarvis-result.json" in _sent_keys(tmp),
              len(box.attempts))
        selected = [s for i in box.imaps for s in i.selected]
        check("mailbox check: with no match, All Mail and Trash both "
              "searched read-only",
              ('"[Gmail]/All Mail"', True) in selected and
              ('"[Gmail]/Trash"', True) in selected, selected)
def t_loop_b_552_after_filing_never_resent():
    # replay of bridge-backup-001: Gmail filed the mail, then answered 552
    with tempfile.TemporaryDirectory() as tmp:
        box = GmailMailbox()
        b, cfg, logs, clock = _mailbox_bridge(tmp, box, refuse=True)
        _write_result(cfg, "filed-552-001")
        _cycle(b, clock, 3, 3600)
        und = json.load(open(os.path.join(tmp, "undeliverable.json")))
        check("552 refusal still recorded in undeliverable.json, one attempt",
              len(box.attempts) == 1 and und.get(
                  "filed-552-001.jarvis-result.json", {}).get("smtp_code")
              == 552, (len(box.attempts), und))
        _write_result(cfg, "filed-552-001", {"attachments": []})  # rewritten
        b2, _, logs2, clock2 = _mailbox_bridge(tmp, box, refuse=True)
        _cycle(b2, clock2, 3, 3600)
        check("rewritten result whose copy Gmail filed is NOT sent again "
              "(even after a restart)", len(box.attempts) == 1 and
              box.subjects("[JARVIS-RESULT] filed-552-001") == 1 and
              "filed-552-001.jarvis-result.json" in _sent_keys(tmp) and
              any("in the mailbox despite the send error" in l for l in logs),
              len(box.attempts))
        check("552 after Gmail filed the mail: no failure notice queued",
              not any(bridge_core.SEND_FAILED_TAG in n
                      for n in os.listdir(cfg["results_dir"])))
def t_loop_b_temp_error_after_filing_no_duplicate():
    # old 30 s flood: the send "failed" but Gmail had filed it, so each
    # retry made another copy
    with tempfile.TemporaryDirectory() as tmp:
        box = GmailMailbox()
        b, cfg, logs, clock = _mailbox_bridge(tmp, box, drop_after_filing=5)
        _write_result(cfg, "flood-001")
        b.loop_b()
        clock[0] += 31
        _cycle(b, clock, 6, 60)
        check("temporary error after Gmail filed the mail: retry finds it, "
              "exactly one copy", len(box.attempts) == 1 and
              box.subjects("[JARVIS-RESULT] flood-001") == 1 and
              "flood-001.jarvis-result.json" in _sent_keys(tmp),
              (len(box.attempts), box.subjects("[JARVIS-RESULT] flood-001")))
        check("temporary error still logged with its retry delay",
              any("retry in 30s" in l for l in logs))
def t_loop_b_skip_report_not_hidden_by_main_result():
    with tempfile.TemporaryDirectory() as tmp:
        box = GmailMailbox()
        box.post("[JARVIS-RESULT] dup-task-001")  # the original result
        b, cfg, logs, clock = _mailbox_bridge(tmp, box)
        name = "dup-task-001.skipped-0123456789abcdef.jarvis-result.json"
        with open(os.path.join(cfg["results_dir"], name), "w") as f:
            json.dump({"task_id": "dup-task-001", "status": "skipped"}, f)
        _cycle(b, clock, 3, 60)
        check("duplicate-task skip report is still mailed once (matched by "
              "its own Message-ID, not the shared subject)",
              len(box.attempts) == 1 and name in _sent_keys(tmp),
              len(box.attempts))
        os.remove(os.path.join(tmp, "sent.json"))
        b2, _, logs2, clock2 = _mailbox_bridge(tmp, box)
        _cycle(b2, clock2, 3, 60)
        check("skip report with sent.json lost: found by Message-ID, not "
              "re-sent", len(box.attempts) == 1 and
              any("already in the mailbox" in l for l in logs2),
              len(box.attempts))
def t_loop_b_mailbox_unreachable_waits():
    with tempfile.TemporaryDirectory() as tmp:
        box = GmailMailbox()
        box.imap_down = True
        b, cfg, logs, clock = _mailbox_bridge(tmp, box)
        _write_result(cfg, "imap-down-001")
        b.loop_b()
        for _ in range(5):
            clock[0] += 5
            b._quick_results()
        check("mailbox unreachable: nothing sent blind, retry scheduled",
              len(box.attempts) == 0 and
              any("mailbox check failed (retry in 30s)" in l for l in logs),
              logs[-2:])
        box.imap_down = False
        clock[0] += 30
        _cycle(b, clock, 3, 60)
        check("mailbox back: result mailed exactly once",
              len(box.attempts) == 1, len(box.attempts))
# --- risky attachments ----------------------------------------------------
def _zip_bytes(members, encrypted=False):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members:
            zf.writestr(name, data)
    data = bytearray(buf.getvalue())
    if encrypted:  # zipfile cannot write encrypted entries: set bit 0 by hand
        for sig, off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            at = data.find(sig)
            while at >= 0:
                data[at + off] |= 0x1
                at = data.find(sig, at + 4)
    return bytes(data)
def t_risky_attachment_filter():
    import io
    import tarfile
    with tempfile.TemporaryDirectory() as ws:
        files = {
            "backup.zip": _zip_bytes([("jarvis-bridge/Start.ps1", b"x"),
                                      ("jarvis-bridge/notes.md", b"y")]),
            "docs.zip": _zip_bytes([("a.txt", b"a"), ("b.png", b"b")]),
            "nested.zip": _zip_bytes([("inner.zip", _zip_bytes(
                [("tool.js", b"z")]))]),
            "locked.zip": _zip_bytes([("a.txt", b"a")], encrypted=True),
            "run.ps1": b"Write-Host hi",
            "setup.EXE": b"MZ",
            "shot.png": b"\x89PNG",
            "data.7z": b"7z\xbc\xaf",
            "broken.zip": b"not a zip",
        }
        tbuf = io.BytesIO()
        with tarfile.open(fileobj=tbuf, mode="w:gz") as tf:
            ti = tarfile.TarInfo("go.bat")
            ti.size = 2
            tf.addfile(ti, io.BytesIO(b"@x"))
        files["logs.tar.gz"] = tbuf.getvalue()
        for n, d in files.items():
            with open(os.path.join(ws, n), "wb") as f:
                f.write(d)
        attached, skipped = bridge_core.collect_attachments(
            list(files), ws)
        names = [n for n, _ in attached]
        why = {s["path"]: s["reason"] for s in skipped}
        check("risky attachments: safe files still attached",
              sorted(names) == ["docs.zip", "shot.png"], names)
        check("risky attachments: zip holding a .ps1 skipped with reason",
              "Start.ps1" in why.get("backup.zip", "") and
              "Gmail blocks" in why.get("backup.zip", ""), why)
        check("risky attachments: .ps1 / .EXE skipped by type",
              "Gmail blocks .ps1" in why.get("run.ps1", "") and
              "Gmail blocks .exe" in why.get("setup.EXE", ""), why)
        check("risky attachments: nested, encrypted, tar.gz, 7z, broken "
              "archives skipped",
              "nested inner.zip" in why.get("nested.zip", "") and
              "password-protected" in why.get("locked.zip", "") and
              "go.bat" in why.get("logs.tar.gz", "") and
              ".7z" in why.get("data.7z", "") and
              "could not be opened" in why.get("broken.zip", ""), why)
def t_loop_b_risky_attachment_does_not_block_result():
    with tempfile.TemporaryDirectory() as tmp:
        box = GmailMailbox()
        b, cfg, logs, clock = _mailbox_bridge(tmp, box)
        ws = os.path.join(tmp, "ws")
        os.makedirs(ws)
        with open(os.path.join(ws, "bridge-backup.zip"), "wb") as f:
            f.write(_zip_bytes([("Start-JarvisBridge.ps1", b"x")]))
        with open(os.path.join(ws, "shot.png"), "wb") as f:
            f.write(b"\x89PNG")
        _write_result(cfg, "risky-att-001", {
            "workspace": ws, "attachments": ["bridge-backup.zip", "shot.png"]})
        _cycle(b, clock, 3, 60)
        msg = email.message_from_string(box.attempts[0]) \
            if box.attempts else None
        files = [p.get_filename() for p in msg.walk()
                 if p.get_filename()] if msg else []
        body = json.loads(msg.get_payload()[0].get_payload(decode=True)) \
            if msg else {}
        check("risky attachment: result email delivered (no 552), once",
              len(box.attempts) == 1 and not os.path.exists(
                  os.path.join(tmp, "undeliverable.json")) and
              "risky-att-001.jarvis-result.json" in _sent_keys(tmp),
              len(box.attempts))
        check("risky attachment: zip left off, screenshot attached",
              files == ["shot.png"], files)
        check("risky attachment: reason in attachments_skipped and the log",
              body.get("attachments_skipped", [{}])[0].get("path") ==
              "bridge-backup.zip" and any(
                  "attachment skipped: bridge-backup.zip" in l and
                  "Gmail blocks" in l for l in logs), body)
# --- config -------------------------------------------------------------
def t_legacy_config():
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = os.path.join(tmp, "gmail-bridge-config.json")
        with open(cfg_path, "w") as f:
            json.dump({"email": BRIDGE_EMAIL, "app_password": "x" * 16,
                       "bridge_secret": "old-secret-value",
                       "poll_seconds": 60}, f)
        logs = []
        b = Bridge(cfg_path, log=logs.append,
                   imap_factory=lambda: FakeIMAP([]),
                   smtp_factory=lambda: FakeSMTP())
        cfg = b.load()  # must not raise
        b.loop_once()   # must not raise
        leaked = any("old-secret-value" in l for l in logs)
        check("legacy bridge_secret config loads, secret never logged",
              cfg["email"] == BRIDGE_EMAIL and not leaked)
        bridge_core.save_config(cfg_path, cfg)
        check("save_config strips legacy secret",
              "bridge_secret" not in open(cfg_path).read())
def t_missing_config():
    with tempfile.TemporaryDirectory() as tmp:
        b = Bridge(os.path.join(tmp, "nope.json"), log=lambda l: None)
        try:
            b.load()
            check("missing config fails closed", False, "no exception")
        except FileNotFoundError as exc:
            check("missing config fails closed with clear message",
                  "gmail-bridge-config.json" in str(exc) or "Config not found" in str(exc))
# --- executor routing ----------------------------------------------------
def body_json(m):
    part = m.get_payload()[0] if m.is_multipart() else m
    return json.loads(part.get_payload(decode=True).decode("utf-8"))
def task_msg(task_id, executor=None, omit=False):
    task = dict(TASK, task_id=task_id)
    if not omit:
        task["executor"] = executor
    return make_msg(body=json.dumps(task),
                    subject="[JARVIS-TASK] %s" % task_id)
def make_routed_bridge(tmp, messages, **over):
    cfg_path = os.path.join(tmp, "gmail-bridge-config.json")
    cfg = {
        "email": BRIDGE_EMAIL, "app_password": "x" * 16,
        "inboxes": {"cursor": os.path.join(tmp, "cursor-inbox"),
                    "openclaw": os.path.join(tmp, "openclaw-inbox")},
        "allowed_senders": [BRIDGE_EMAIL],
    }
    cfg.update(over)
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)
    logs = []
    imap = FakeIMAP(messages)
    bridge = Bridge(cfg_path, log=logs.append, imap_factory=lambda: imap,
                    smtp_factory=lambda: FakeSMTP())
    return bridge, logs, imap
def in_inbox(tmp, executor, task_id):
    return os.path.exists(os.path.join(
        tmp, "%s-inbox" % executor, task_id + ".jarvis-task.json"))
def t_routing():
    with tempfile.TemporaryDirectory() as tmp:
        b, logs, _ = make_routed_bridge(tmp, [
            task_msg("r-oc", "openclaw"), task_msg("r-cu", "cursor"),
            task_msg("r-missing", omit=True), task_msg("r-null", None),
            task_msg("r-case", " OpenClaw ")])
        b.loop_a()
        check("ROUTE executor=openclaw -> openclaw-inbox",
              in_inbox(tmp, "openclaw", "r-oc")
              and not in_inbox(tmp, "cursor", "r-oc"))
        check("ROUTE executor=cursor -> cursor-inbox",
              in_inbox(tmp, "cursor", "r-cu")
              and not in_inbox(tmp, "openclaw", "r-cu"))
        check("ROUTE missing/null executor -> cursor-inbox (default)",
              in_inbox(tmp, "cursor", "r-missing")
              and in_inbox(tmp, "cursor", "r-null"))
        check("ROUTE executor name is trimmed/case-folded",
              in_inbox(tmp, "openclaw", "r-case"))
        with open(os.path.join(tmp, "openclaw-inbox",
                               "r-oc.jarvis-task.json")) as f:
            check("routed package records its executor",
                  json.load(f).get("executor") == "openclaw")
        leftovers = [n for d in ("cursor-inbox", "openclaw-inbox")
                     for n in os.listdir(os.path.join(tmp, d))
                     if n.endswith(".tmp")]
        check("no .tmp files left after routing", not leftovers, leftovers)
def t_routing_atomic():
    """At the rename the final name must not exist yet and the .tmp must
    already hold the complete JSON."""
    seen = []
    real_replace = os.replace
    def spy(src, dst):
        if str(dst).endswith(".jarvis-task.json"):
            with open(src, encoding="utf-8") as f:
                seen.append((src.endswith(".tmp"), os.path.exists(dst),
                             json.load(f)["task_id"]))
        return real_replace(src, dst)
    with tempfile.TemporaryDirectory() as tmp:
        b, logs, _ = make_routed_bridge(tmp, [task_msg("a-oc", "openclaw")])
        bridge_core.os.replace = spy
        try:
            b.loop_a()
        finally:
            bridge_core.os.replace = real_replace
        check("ATOMIC task write goes .tmp -> rename with complete JSON",
              seen == [(True, False, "a-oc")], seen)
def t_refuse_unknown_executor():
    FakeSMTP.instances.clear()
    with tempfile.TemporaryDirectory() as tmp:
        b, logs, imap = make_routed_bridge(tmp, [task_msg("bad-ex", "nope"),
                                                 task_msg("bad-type", 5)])
        b.loop_a()
        routed = [n for d in ("cursor-inbox", "openclaw-inbox")
                  for n in os.listdir(os.path.join(tmp, d))
                  if n.endswith(".jarvis-task.json")]
        check("REFUSE unknown executor: nothing routed to any inbox",
              not routed, routed)
        check("REFUSE: both messages marked Seen (handled, not retried)",
              imap.seen == {1, 2})
        skips = sorted(n for n in os.listdir(os.path.join(tmp, "cursor-inbox"))
                       if ".skipped-" in n)
        check("REFUSE: skip report queued for each", len(skips) == 2, skips)
        b.loop_b()
        sent = FakeSMTP.instances[-1].sent if FakeSMTP.instances else []
        bodies = [email.message_from_string(s[2]) for s in sent]
        subjects = sorted(m["Subject"] for m in bodies)
        check("REFUSE: skip notices emailed as [JARVIS-RESULT]",
              subjects == ["[JARVIS-RESULT] bad-ex", "[JARVIS-RESULT] bad-type"],
              subjects)
        nope = [body_json(m) for m in bodies
                if m["Subject"].endswith("bad-ex")][0]
        check("REFUSE: notice names the executor and the reason",
              nope["status"] == "skipped" and nope["executor"] == "nope"
              and "nope" in nope["skip_reason"]
              and nope["configured_executors"] == ["cursor", "openclaw"],
              nope)
def t_dedupe_across_executors():
    with tempfile.TemporaryDirectory() as tmp:
        b, logs, _ = make_routed_bridge(tmp, [task_msg("dup-1", "cursor")])
        b.loop_a()
        imap2 = FakeIMAP([task_msg("dup-1", "openclaw")])
        b._imap_factory = lambda: imap2
        b.loop_a()
        check("dedupe: same task_id for another executor is skipped",
              not in_inbox(tmp, "openclaw", "dup-1")
              and any("duplicate task dup-1" in l for l in logs))
def _runner_finish(inbox, task_id):
    """What Invoke-JarvisTask.ps1 does after a run: package -> <inbox>\\done."""
    name = task_id + ".jarvis-task.json"
    done = os.path.join(inbox, "done")
    os.makedirs(done, exist_ok=True)
    shutil.move(os.path.join(inbox, name), os.path.join(done, name))
    return os.path.join(done, name)
def t_dedupe_inbox_done():
    FakeSMTP.instances.clear()
    for executor in ("cursor", "openclaw"):
        with tempfile.TemporaryDirectory() as tmp:
            tid = "fin-%s" % executor
            b, logs, _ = make_routed_bridge(tmp, [task_msg(tid, executor)])
            b.loop_a()
            inbox = b.load()["inboxes"][executor]
            finished = _runner_finish(inbox, tid)
            with open(finished, encoding="utf-8") as f:
                before = f.read()
            imap2 = FakeIMAP([task_msg(tid, executor), task_msg(tid, None)])
            b._imap_factory = lambda: imap2
            received = b.loop_a()
            routed = [n for d in ("cursor-inbox", "openclaw-inbox")
                      for n in os.listdir(os.path.join(tmp, d))
                      if n.endswith(".jarvis-task.json")]
            check("dedupe <inbox>\\done (%s): re-sent finished id not re-queued"
                  % executor, received == 0 and not routed, (received, routed))
            dup_lines = [l for l in logs if "duplicate task %s" % tid in l]
            check("dedupe <inbox>\\done (%s): reported as duplicate with the "
                  "done\\ path" % executor,
                  len(dup_lines) == 2
                  and all(finished in l for l in dup_lines), dup_lines)
            check("dedupe <inbox>\\done (%s): messages marked Seen, finished "
                  "package untouched" % executor,
                  imap2.seen == {1, 2}
                  and open(finished, encoding="utf-8").read() == before)
            FakeSMTP.instances.clear()
            b.loop_b()
            sent = [email.message_from_string(s[2])
                    for i in FakeSMTP.instances for s in i.sent]
            notices = [body_json(m) for m in sent]
            check("dedupe <inbox>\\done (%s): each re-send gets a skip notice "
                  "email, not just a log line" % executor,
                  len(sent) == 2 and all(
                      m["Subject"] == "[JARVIS-RESULT] %s" % tid for m in sent),
                  [m["Subject"] for m in sent])
            check("dedupe <inbox>\\done (%s): notice says duplicate, names the "
                  "task_id, says not re-executed" % executor,
                  notices and all(
                      n["status"] == "skipped" and n["skipped"] is True
                      and n["task_id"] == tid
                      and n["skip_reason"] == "duplicate of an already-handled"
                      " task (task_id %s)" % tid
                      and n["skip_reason_code"] == "duplicate-task (%s)" % tid
                      and "NOT re-executed" in n["jarvis_report"]
                      and tid in n["jarvis_report"]
                      and n["duplicate_of"] == bridge_core.portable_path(
                          finished)
                      for n in notices), notices)
            n_sent = len(sent)
            imap3 = FakeIMAP([task_msg(tid, executor)])  # same raw message
            b._imap_factory = lambda: imap3
            check("dedupe <inbox>\\done (%s): same message seen again is "
                  "still not run" % executor, b.loop_a() == 0
                  and not os.path.exists(os.path.join(
                      inbox, tid + ".jarvis-task.json")))
            b.loop_b()
            check("dedupe <inbox>\\done (%s): no second notice for the same "
                  "message, none resent" % executor,
                  sum(len(i.sent) for i in FakeSMTP.instances) == n_sent)
def t_dedupe_sibling_done_unchanged():
    with tempfile.TemporaryDirectory() as tmp:
        b, logs, _ = make_routed_bridge(tmp, [])
        cfg = b.load()
        sib = bridge_core.done_dir_for(cfg, "cursor")
        check("sibling done dir is still <name>-done next to the inbox",
              sib == os.path.join(tmp, "cursor-done"), sib)
        os.makedirs(sib, exist_ok=True)
        with open(os.path.join(sib, "sib-1.jarvis-task.json"), "w") as f:
            json.dump(dict(TASK, task_id="sib-1"), f)
        imap = FakeIMAP([task_msg("sib-1", "cursor")])
        b._imap_factory = lambda: imap
        b.loop_a()
        check("dedupe sibling <name>-done: still skipped as duplicate",
              not in_inbox(tmp, "cursor", "sib-1") and imap.seen == {1}
              and any("duplicate task sib-1" in l for l in logs))
    with tempfile.TemporaryDirectory() as tmp:
        custom = os.path.join(tmp, "elsewhere-done")
        b, logs, _ = make_routed_bridge(tmp, [],
                                        done_dirs={"openclaw": custom})
        os.makedirs(custom)
        with open(os.path.join(custom, "sib-2.jarvis-task.json"), "w") as f:
            json.dump(dict(TASK, task_id="sib-2"), f)
        imap = FakeIMAP([task_msg("sib-2", "openclaw")])
        b._imap_factory = lambda: imap
        b.loop_a()
        check("dedupe done_dirs override: still skipped as duplicate",
              not in_inbox(tmp, "openclaw", "sib-2")
              and any("duplicate task sib-2" in l for l in logs))
def t_dedupe_skip_notice_sibling_done():
    FakeSMTP.instances.clear()
    with tempfile.TemporaryDirectory() as tmp:
        b, logs, _ = make_routed_bridge(tmp, [])
        sib = bridge_core.done_dir_for(b.load(), "openclaw")
        os.makedirs(sib, exist_ok=True)
        with open(os.path.join(sib, "sib-3.jarvis-task.json"), "w") as f:
            json.dump(dict(TASK, task_id="sib-3"), f)
        resend = []
        for n in (1, 2):
            m = email.message_from_bytes(task_msg("sib-3", "openclaw"))
            m["Message-ID"] = "<sib-3.resend-%d@test>" % n
            resend.append(m.as_bytes())
        imap = FakeIMAP(resend)
        b._imap_factory = lambda: imap
        received = b.loop_a()
        b.loop_b()
        sent = [email.message_from_string(s[2])
                for i in FakeSMTP.instances for s in i.sent]
        notices = [body_json(m) for m in sent]
        check("dedupe sibling <name>-done: not re-executed, a skip notice "
              "per re-send (distinct Message-IDs)",
              received == 0 and not in_inbox(tmp, "openclaw", "sib-3")
              and len(notices) == 2 and imap.seen == {1, 2}
              and sorted(n["message_id"] for n in notices) ==
              ["<sib-3.resend-1@test>", "<sib-3.resend-2@test>"]
              and all(n["task_id"] == "sib-3" and n["executor"] == "openclaw"
                      and "duplicate of an already-handled task" in
                      n["skip_reason"] for n in notices), notices)
def t_dedupe_notice_uses_body_task_id():
    with tempfile.TemporaryDirectory() as tmp:
        b, logs, _ = make_routed_bridge(tmp, [task_msg("body-id", "cursor")])
        b.loop_a()
        odd = make_msg(body=json.dumps(dict(TASK, task_id="body-id")),
                       subject="[JARVIS-TASK] subject-id")
        imap = FakeIMAP([odd])
        b._imap_factory = lambda: imap
        b.loop_a()
        skips = [n for n in os.listdir(os.path.join(tmp, "cursor-inbox"))
                 if ".skipped-" in n]
        check("dedupe notice is keyed on the body task_id, not the subject",
              len(skips) == 1 and skips[0].startswith("body-id.skipped-"),
              skips)
MALFORMED_KEYS = sorted([
    "task_id", "status", "subtype", "skipped", "skip_reason",
    "skip_reason_code", "message_id", "imap_seq", "subject", "from",
    "timestamp", "started", "finished", "exit_code", "timed_out",
    "workspace", "mode", "stdout", "stderr", "jarvis_report", "attachments",
    "attachments_skipped"])
def t_malformed_skip_notice_unchanged():
    from datetime import datetime, timezone
    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
    raw = make_msg(body="{not json", subject="[JARVIS-TASK] mal-001")
    name, res = bridge_core.build_skip_result(
        email.message_from_bytes(raw), raw, "7", "body-not-json", now=now)
    key = bridge_core.hashlib.sha256(raw).hexdigest()[:16]
    check("malformed notice: exact file name and result as before", (
        name == "mal-001.skipped-%s.jarvis-result.json" % key and res == {
            "task_id": "mal-001", "status": "skipped", "subtype": "skipped",
            "skipped": True, "skip_reason": "body not JSON",
            "skip_reason_code": "body-not-json", "message_id": None,
            "imap_seq": "7", "subject": "[JARVIS-TASK] mal-001",
            "from": BRIDGE_EMAIL, "timestamp": "2026-09-28T12:00:00+00:00",
            "started": "2026-09-28T12:00:00+00:00",
            "finished": "2026-09-28T12:00:00+00:00", "exit_code": None,
            "timed_out": False, "workspace": "", "mode": "", "stdout": "",
            "stderr": "",
            "jarvis_report": "Task email skipped by the bridge (Loop A): body "
                             "not JSON. The task was NOT run; fix the email "
                             "and resend it.",
            "attachments": [], "attachments_skipped": []}), (name, res))
    FakeSMTP.instances.clear()
    bad_task = dict(TASK, task_id="mal-003")
    del bad_task["goal"]
    cases = [
        ("mal-001", make_msg(body="{not json",
                             subject="[JARVIS-TASK] mal-001"),
         "body not JSON"),
        ("mal-002", make_msg(body="[1, 2]", subject="[JARVIS-TASK] mal-002"),
         "body JSON is not an object"),
        ("mal-003", make_msg(body=json.dumps(bad_task),
                             subject="[JARVIS-TASK] mal-003"),
         "missing required fields: goal"),
        ("mal-004", make_msg(ar_headers=(
            "mx.google.com; dkim=fail header.d=gmail.com",),
            subject="[JARVIS-TASK] mal-004"), "DKIM check failed"),
        ("mal-005", make_msg(from_addr=OTHER_EMAIL,
                             subject="[JARVIS-TASK] mal-005"),
         "sender not allowed (%s)" % OTHER_EMAIL),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        b, logs, imap = make_routed_bridge(tmp, [c[1] for c in cases])
        received = b.loop_a()
        b.loop_b()
        sent = {m["Subject"]: body_json(m) for m in (
            email.message_from_string(s[2])
            for i in FakeSMTP.instances for s in i.sent)}
        for tid, _raw, text in cases:
            n = sent.get("[JARVIS-RESULT] %s" % tid)
            check("malformed notice unchanged: %s (%s)" % (tid, text),
                  n is not None and sorted(n) == MALFORMED_KEYS
                  and n["skip_reason"] == text and n["task_id"] == tid
                  and n["jarvis_report"] ==
                  "Task email skipped by the bridge (Loop A): %s. The task "
                  "was NOT run; fix the email and resend it." % text, n)
        check("malformed: nothing routed, all marked Seen, no duplicate lines",
              received == 0 and imap.seen == {1, 2, 3, 4, 5}
              and not any("duplicate task" in l for l in logs))
def t_dedupe_new_id_still_routes():
    with tempfile.TemporaryDirectory() as tmp:
        b, logs, _ = make_routed_bridge(tmp, [task_msg("old-1", "cursor")])
        b.loop_a()
        _runner_finish(b.load()["inboxes"]["cursor"], "old-1")
        imap = FakeIMAP([task_msg("new-1", "cursor")])
        b._imap_factory = lambda: imap
        b.loop_a()
        check("dedupe: a new task id is still routed when done\\ has others",
              in_inbox(tmp, "cursor", "new-1")
              and not any("duplicate task new-1" in l for l in logs))
def t_loop_b_both_inboxes():
    FakeSMTP.instances.clear()
    with tempfile.TemporaryDirectory() as tmp:
        b, logs, _ = make_routed_bridge(tmp, [])
        cfg = b.load()
        for ex in ("cursor", "openclaw"):
            os.makedirs(cfg["inboxes"][ex], exist_ok=True)
            with open(os.path.join(cfg["inboxes"][ex],
                                   "same-id.jarvis-result.json"), "w") as f:
                json.dump({"task_id": "same-id", "from": ex}, f)
        b.loop_b()
        sent = FakeSMTP.instances[-1].sent
        froms = sorted(body_json(email.message_from_string(s[2]))["from"]
                       for s in sent)
        check("Loop B sends results from BOTH inboxes", froms ==
              ["cursor", "openclaw"], froms)
        with open(os.path.join(tmp, "sent.json")) as f:
            keys = sorted(json.load(f))
        check("sent.json: cursor key stays bare, openclaw key namespaced",
              keys == ["openclaw/same-id.jarvis-result.json",
                       "same-id.jarvis-result.json"], keys)
        n = len(sent)
        b.loop_b()
        check("Loop B does not resend from either inbox",
              len(FakeSMTP.instances[-1].sent) == n)
# --- config / portability ----------------------------------------------
def t_legacy_migration():
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = os.path.join(tmp, "c.json")
        inbox = os.path.join(_PROFILE, "cursor-inbox")
        with open(cfg_path, "w") as f:
            json.dump({"email": BRIDGE_EMAIL, "app_password": "x" * 16,
                       "inbox": inbox, "results_dir": inbox,
                       "done_dir": os.path.join(_PROFILE, "cursor-done")}, f)
        cfg = bridge_core.load_config(cfg_path)
        check("legacy inbox -> inboxes.cursor, openclaw default added",
              cfg["inboxes"] == {
                  "cursor": inbox,
                  "openclaw": os.path.join(_PROFILE, "openclaw-inbox")}
              and cfg["results_dirs"] == {} and cfg["done_dirs"] == {},
              cfg["inboxes"])
        check("results_dir defaults to inbox; done_dir to sibling <name>-done",
              bridge_core.results_dir_for(cfg, "openclaw") ==
              cfg["inboxes"]["openclaw"] and
              bridge_core.done_dir_for(cfg, "openclaw") ==
              os.path.join(_PROFILE, "openclaw-done"))
        bridge_core.save_config(cfg_path, cfg)
        text = open(cfg_path).read()
        saved = json.loads(text)
        check("saved config is portable (%USERPROFILE%, no absolute profile)",
              saved["inboxes"]["cursor"] == "%USERPROFILE%\\cursor-inbox"
              and _PROFILE.replace("\\", "\\\\") not in text
              and "inbox" not in saved, saved.get("inboxes"))
        legacy_custom = os.path.join(tmp, "q", "my-inbox")
        with open(cfg_path, "w") as f:
            json.dump({"inbox": legacy_custom,
                       "results_dir": os.path.join(tmp, "res"),
                       "done_dir": os.path.join(tmp, "dn")}, f)
        cfg = bridge_core.load_config(cfg_path)
        check("legacy custom results/done dirs kept as cursor overrides",
              cfg["results_dirs"] == {"cursor": os.path.join(tmp, "res")}
              and cfg["done_dirs"] == {"cursor": os.path.join(tmp, "dn")}
              and cfg["allowed_workspaces"] ==
              os.path.join(legacy_custom, "allowed-workspaces.txt"))
def t_paths():
    rp, pp = bridge_core.resolve_path, bridge_core.portable_path
    check("resolve %USERPROFILE% / ~ / relative",
          rp("%USERPROFILE%\\x") == os.path.join(_PROFILE, "x")
          and rp("~\\y") == os.path.join(_PROFILE, "y")
          and rp("z-inbox") == os.path.join(_PROFILE, "z-inbox"))
    check("portable_path round-trips profile paths, keeps others",
          pp(os.path.join(_PROFILE, "a", "b")) == "%USERPROFILE%\\a\\b"
          and pp("D:\\elsewhere") == "D:\\elsewhere"
          and rp(pp(os.path.join(_PROFILE, "a"))) == os.path.join(_PROFILE, "a"))
def t_init_install():
    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ["USERPROFILE"]
        os.environ["USERPROFILE"] = tmp
        try:
            cfg_path = os.path.join(tmp, "bridge", "gmail-bridge-config.json")
            created = bridge_core.init_install(cfg_path)
            ok = all(os.path.isdir(os.path.join(tmp, box, sub))
                     for box in ("cursor-inbox", "openclaw-inbox")
                     for sub in ("", "done", "logs"))
            allow = os.path.join(tmp, "cursor-inbox", "allowed-workspaces.txt")
            check("init creates both inboxes with done\\ and logs\\", ok)
            check("init creates allowed-workspaces.txt and default config",
                  os.path.isfile(allow) and os.path.isfile(cfg_path)
                  and cfg_path in created)
            again = bridge_core.init_install(cfg_path)
            check("init is idempotent (second run creates nothing)",
                  again == [], again)
        finally:
            os.environ["USERPROFILE"] = old
# --- task workspaces (work_dirs) -------------------------------------------
class _Profile:
    """Point the user profile (and so OpenClaw's home) at `home` for a test;
    extra OPENCLAW_* variables can be set too."""
    def __init__(self, home, **env):
        self.home, self.env, self.saved = home, env, {}
    def __enter__(self):
        for key in ("USERPROFILE", "HOME") + tuple(self.env):
            self.saved[key] = os.environ.get(key)
        os.environ["USERPROFILE"] = os.environ["HOME"] = self.home
        os.environ.update(self.env)
        return self
    def __exit__(self, *a):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
def fake_openclaw(home, agents=None, make_workspace=True):
    """An OpenClaw install under `home`: .openclaw\\openclaw.json with the
    given agents block and (optionally) its workspace folder."""
    state = os.path.join(home, ".openclaw")
    os.makedirs(state, exist_ok=True)
    if agents is None:
        agents = {"defaults": {"workspace": "~/.openclaw/workspace"}}
    with open(os.path.join(state, "openclaw.json"), "w") as f:
        json.dump({"agents": agents}, f)
    ws = os.path.join(state, "workspace")
    if make_workspace:
        os.makedirs(ws, exist_ok=True)
    return ws
def t_openclaw_workspace_lookup():
    with tempfile.TemporaryDirectory() as tmp:
        with _Profile(tmp):
            path, why = bridge_core.openclaw_workspace()
            check("OpenClaw workspace: none on disk -> not used",
                  path == "" and "does not exist" in why, (path, why))
            fake_openclaw(tmp, make_workspace=False)
            path, why = bridge_core.openclaw_workspace()
            check("OpenClaw workspace: configured but missing -> not used",
                  path == "" and "does not exist" in why, (path, why))
            ws = fake_openclaw(tmp)
            path, why = bridge_core.openclaw_workspace()
            check("OpenClaw workspace: agents.defaults.workspace (~) found",
                  path == ws and "openclaw.json" in why, (path, why))
            fake_openclaw(tmp, agents={})
            check("OpenClaw workspace: no setting -> <home>\\.openclaw\\"
                  "workspace", bridge_core.openclaw_workspace()[0] == ws)
            own = os.path.join(tmp, "neo-ws")
            os.makedirs(own)
            fake_openclaw(tmp, agents={
                "defaults": {"workspace": "~/.openclaw/workspace"},
                "entries": {"main": {"workspace": own}}})
            check("OpenClaw workspace: default agent's own entry wins",
                  bridge_core.openclaw_workspace()[0] == own)
        state = os.path.join(tmp, "alt-state")
        os.makedirs(os.path.join(state, "workspace"))
        with _Profile(tmp, OPENCLAW_STATE_DIR=state):
            check("OpenClaw workspace: OPENCLAW_STATE_DIR honoured",
                  bridge_core.openclaw_workspace()[0] ==
                  os.path.join(state, "workspace"))
def _package(tmp, executor, task_id):
    with open(os.path.join(tmp, "%s-inbox" % executor,
                           task_id + ".jarvis-task.json"),
              encoding="utf-8") as f:
        return json.load(f)
def t_work_dirs_reroute():
    with tempfile.TemporaryDirectory() as tmp:
        with _Profile(tmp):
            ws = fake_openclaw(tmp)
            b, logs, _ = make_routed_bridge(tmp, [
                task_msg("wd-oc", "openclaw"), task_msg("wd-cu", "cursor"),
                task_msg("odd id!", "openclaw")])
            b.loop_a()
            want = os.path.join(ws, "jarvis-bridge-tasks", "wd-oc")
            oc = _package(tmp, "openclaw", "wd-oc")
            check("work_dirs: openclaw task workspace -> OpenClaw workspace\\"
                  "jarvis-bridge-tasks\\<task-id>",
                  oc["workspace"] == want and os.path.isdir(want), oc)
            check("work_dirs: rest of the openclaw package unchanged",
                  dict(oc, workspace=None) == dict(
                      TASK, task_id="wd-oc", executor="openclaw",
                      workspace=None), oc)
            check("work_dirs: cursor task keeps Jarvis's workspace",
                  _package(tmp, "cursor", "wd-cu")["workspace"] ==
                  TASK["workspace"])
            check("work_dirs: reroute is logged with both paths",
                  any("wd-oc workspace -> %s" % want in l
                      and TASK["workspace"] in l for l in logs), logs)
            odd = [n for n in os.listdir(os.path.join(ws, "jarvis-bridge-tasks"))
                   if n != "wd-oc"]
            check("work_dirs: unsafe task_id -> one safe folder inside the "
                  "root", odd == ["odd_id"], odd)
def t_work_dirs_fallback_and_off():
    with tempfile.TemporaryDirectory() as tmp:
        with _Profile(tmp):
            b, logs, _ = make_routed_bridge(tmp, [task_msg("nf-oc", "openclaw")])
            b.loop_a()
            check("work_dirs: no OpenClaw workspace -> task still routed with "
                  "Jarvis's workspace, reason logged",
                  _package(tmp, "openclaw", "nf-oc")["workspace"] ==
                  TASK["workspace"]
                  and any("keeps the workspace Jarvis sent" in l
                          for l in logs), logs)
    with tempfile.TemporaryDirectory() as tmp:
        with _Profile(tmp):
            ws = fake_openclaw(tmp)
            b, logs, _ = make_routed_bridge(tmp, [task_msg("off-oc", "openclaw")],
                                            work_dirs={})
            b.loop_a()
            check("work_dirs: {} turns rerouting off",
                  _package(tmp, "openclaw", "off-oc")["workspace"] ==
                  TASK["workspace"]
                  and not os.path.exists(os.path.join(ws, "jarvis-bridge-tasks")))
    with tempfile.TemporaryDirectory() as tmp:
        with _Profile(tmp):
            fake_openclaw(tmp)
            custom = os.path.join(tmp, "elsewhere")
            b, logs, _ = make_routed_bridge(
                tmp, [task_msg("cu-dir", "cursor")],
                work_dirs={"cursor": custom})
            b.loop_a()
            check("work_dirs: plain path root works for any executor",
                  _package(tmp, "cursor", "cu-dir")["workspace"] ==
                  os.path.join(custom, "cu-dir"))
def t_work_dirs_config():
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = os.path.join(tmp, "c.json")
        with open(cfg_path, "w") as f:
            json.dump({"email": BRIDGE_EMAIL}, f)
        cfg = bridge_core.load_config(cfg_path)
        check("work_dirs default: openclaw -> %OPENCLAW_WORKSPACE%\\"
              "jarvis-bridge-tasks",
              cfg["work_dirs"] == {"openclaw": "%OPENCLAW_WORKSPACE%\\"
                                   "jarvis-bridge-tasks"}, cfg["work_dirs"])
        bridge_core.save_config(cfg_path, cfg)
        with open(cfg_path) as f:
            saved = json.load(f)
        check("work_dirs saved with the token kept (portable)",
              saved.get("work_dirs") == cfg["work_dirs"], saved)
        cfg["work_dirs"] = {"cursor": os.path.join(_PROFILE, "wk")}
        bridge_core.save_config(cfg_path, cfg)
        with open(cfg_path) as f:
            saved = json.load(f)
        check("work_dirs plain profile path saved as %USERPROFILE%\\...",
              saved["work_dirs"] == {"cursor": "%USERPROFILE%\\wk"},
              saved["work_dirs"])
        with open(cfg_path, "w") as f:
            json.dump({"email": BRIDGE_EMAIL, "work_dirs": {}}, f)
        check("work_dirs {} stays off after load",
              bridge_core.load_config(cfg_path)["work_dirs"] == {})
def t_work_dirs_probe_and_attachments():
    with tempfile.TemporaryDirectory() as tmp:
        with _Profile(tmp):
            ws = fake_openclaw(tmp)
            b, logs, _ = make_routed_bridge(tmp, [])
            cfg = b.load()
            task_id, path = bridge_core.write_probe_task(
                cfg, "openclaw", bridge_core.BRIDGE_DIR)
            with open(path, encoding="utf-8") as f:
                probe = json.load(f)
            check("work_dirs: GUI probe to openclaw also uses the OpenClaw "
                  "workspace", probe["workspace"] ==
                  os.path.join(ws, "jarvis-bridge-tasks", task_id), probe)
            _, cpath = bridge_core.write_probe_task(
                cfg, "cursor", bridge_core.BRIDGE_DIR)
            with open(cpath, encoding="utf-8") as f:
                check("work_dirs: GUI probe to cursor unchanged",
                      json.load(f)["workspace"] == bridge_core.BRIDGE_DIR)
            work = probe["workspace"]
            with open(os.path.join(work, "proof.txt"), "w") as f:
                f.write("proof")
            body = json.dumps({"task_id": task_id, "workspace": work,
                               "attachments": ["proof.txt"]})
            _, names, skipped = bridge_core.build_result_message(
                BRIDGE_EMAIL, BRIDGE_EMAIL, "[JARVIS-RESULT] %s" % task_id,
                body)
            check("work_dirs: attachments in the task folder are sent as "
                  "before", names == ["proof.txt"] and skipped == [],
                  (names, skipped))
# --- IMAP IDLE ------------------------------------------------------------
class FakeIdleIMAP(FakeIMAP):
    capabilities = ("IMAP4REV1", "IDLE")
    def __init__(self, responses):
        super().__init__([])
        self.responses = responses
    def idle(self, duration=None):
        outer = self
        class _Idler:
            def __enter__(self):
                return iter(outer.responses)
            def __exit__(self, *a):
                return False
        return _Idler()
def _run_idle(imap, cfg_over=None):
    with tempfile.TemporaryDirectory() as tmp:
        b, logs, _ = make_routed_bridge(tmp, [], **(cfg_over or {}))
        b._imap_factory = lambda: imap
        stop, wake = threading.Event(), threading.Event()
        t = threading.Thread(target=b._idle_watch, args=(stop, wake),
                             daemon=True)
        t.start()
        woke = wake.wait(3)
        stop.set()
        return woke, b, logs
def t_idle_wakes():
    woke, b, logs = _run_idle(FakeIdleIMAP([("EXISTS", [b"7"])]))
    check("IDLE: EXISTS push wakes the poll loop at once",
          woke and any("IMAP IDLE active" in l for l in logs))
def t_idle_fallback():
    woke, b, logs = _run_idle(FakeIMAP([]))
    check("IDLE unsupported -> falls back to polling, logged",
          not woke and b.stats["idle"] == "fallback"
          and any("IMAP IDLE unavailable" in l for l in logs))
def t_executor_slots():
    with tempfile.TemporaryDirectory() as inbox:
        check("slots: no file -> None",
              bridge_core.read_executor_slots(inbox) is None)
        os.makedirs(os.path.join(inbox, "logs"))
        state = {"runner_pid": 42, "max_slots": 3, "running": 2, "queued": 1,
                 "stopped": False, "updated_unix": 1000, "slots": [
                     {"slot": 1, "state": "running", "task_id": "a",
                      "started_unix": 970},
                     {"slot": 2, "state": "idle"},
                     {"slot": 3, "state": "running", "task_id": "b",
                      "started_unix": 990}]}
        path = bridge_core.executor_slots_path(inbox)
        with open(path, "w", encoding="utf-8-sig") as f:
            json.dump(state, f)
        p = bridge_core.read_executor_slots(inbox, now=1005)
        check("slots: live pool read (BOM ok)", p and p["live"] and
              p["running"] == 2 and p["max_slots"] == 3 and p["queued"] == 1)
        check("slots: per-slot state", [(s["slot"], s["state"], s["task_id"])
              for s in p["slots"]] == [(1, "running", "a"), (2, "idle", None),
                                       (3, "running", "b")])
        check("slots: elapsed", p["slots"][0]["elapsed"] == 35)
        p = bridge_core.read_executor_slots(inbox, now=1100)
        check("slots: stale runner shows no running slots",
              not p["live"] and p["running"] == 0 and p["age"] == 100)
        state["stopped"] = True
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f)
        p = bridge_core.read_executor_slots(inbox, now=1001)
        check("slots: stopped runner is not live", not p["live"])
        with open(path, "w") as f:
            f.write("{broken")
        check("slots: corrupt file -> None",
              bridge_core.read_executor_slots(inbox) is None)

def _oc_line(when, text):
    return json.dumps({"0": '{"subsystem":"plugins"}', "1": text,
                       "time": when, "hostname": "EXAMPLE-PC"},
                      separators=(",", ":")) + "\n"
def t_openclaw_bridge_run():
    from datetime import datetime as _dt
    t0 = _dt.fromisoformat("2026-10-08T08:57:16.684-07:00").timestamp()
    started = _oc_line("2026-10-08T08:57:16.684-07:00",
                       "[jarvis-bridge-priority] bridge run started: "
                       "758f59a2-03b3 (provider ollama); chat on this "
                       "provider now waits")
    finished = _oc_line("2026-10-08T09:01:41.196-07:00",
                        "[jarvis-bridge-priority] bridge run finished: "
                        "758f59a2-03b3 after 4.4 min (success=true)")
    reload0 = _oc_line("2026-10-08T08:58:00.000-07:00",
                       "[jarvis-bridge-priority] loaded v2.0.0 (hold chat="
                       "true, tracked: 0 bridge / 0 chat run(s))")
    reload1 = reload0.replace("tracked: 0 bridge", "tracked: 1 bridge")
    noise = '{"1":"gateway/ws res sessions.list","time":"2026-10-08T08:57:30-07:00"}\n'
    with tempfile.TemporaryDirectory() as d:
        log = os.path.join(d, "openclaw-2026-10-08.log")
        def write(*lines):
            with open(log, "w", encoding="utf-8") as f:
                f.writelines(lines)
            bridge_core._oc_log_cache.clear()
        check("oc run: no log dir -> None",
              bridge_core.openclaw_bridge_run(os.path.join(d, "x")) is None)
        write(noise, started, noise)
        r = bridge_core.openclaw_bridge_run(d, now=t0 + 125)
        check("oc run: started, not finished -> running",
              r and r["run_id"] == "758f59a2-03b3" and r["elapsed"] == 125)
        write(noise, started, noise, finished)
        check("oc run: finished -> None",
              bridge_core.openclaw_bridge_run(d, now=t0 + 300) is None)
        check("oc run: a finish logged after 'now' is not applied",
              bridge_core.openclaw_bridge_run(d, now=t0 + 125) is not None)
        write(started)
        check("oc run: older than the plugin hold limit -> None",
              bridge_core.openclaw_bridge_run(
                  d, now=t0 + bridge_core.OPENCLAW_RUN_MAX_SECONDS + 5) is None)
        write(started, reload0)
        check("oc run: plugin reload tracking 0 bridge runs clears it",
              bridge_core.openclaw_bridge_run(d, now=t0 + 90) is None)
        write(started, reload1)
        check("oc run: plugin reload still tracking the run keeps it",
              bridge_core.openclaw_bridge_run(d, now=t0 + 90) is not None)
        os.remove(log)
        with open(os.path.join(d, "openclaw-2026-10-07.log"), "w") as f:
            f.write(started)
        with open(os.path.join(d, "openclaw-2026-10-08.log"), "w") as f:
            f.write(noise)
        bridge_core._oc_log_cache.clear()
        check("oc run: run started in yesterday's log still counts",
              bridge_core.openclaw_bridge_run(d, now=t0 + 60) is not None)
    with tempfile.TemporaryDirectory() as inbox:
        check("oc pending: empty inbox -> None",
              bridge_core.oldest_pending_task(inbox) is None)
        for i, tid in enumerate(("old-done", "mid", "new")):
            p = os.path.join(inbox, tid + bridge_core.TASK_FILE_SUFFIX)
            with open(p, "w") as f:
                f.write("{}")
            os.utime(p, (1000 + i, 1000 + i))
        with open(os.path.join(inbox, "old-done"
                               + bridge_core.RESULT_FILE_SUFFIX), "w") as f:
            f.write("{}")
        check("oc pending: oldest package without a result",
              bridge_core.oldest_pending_task(inbox) == "mid")

# --- Defect 2: timeout_minutes required at intake --------------------------
def t_reject_missing_timeout():
    FakeSMTP.instances.clear()
    with tempfile.TemporaryDirectory() as tmp:
        task = {k: v for k, v in TASK.items() if k != "timeout_minutes"}
        task["task_id"] = "no-timeout-001"
        b, cfg, logs = make_bridge(tmp, [make_msg(
            body=json.dumps(task), subject="[JARVIS-TASK] no-timeout-001")])
        b.loop_a()
        check("timeout: task without timeout_minutes never reaches the inbox",
              not inbox_has(tmp, "no-timeout-001") and
              b.stats["tasks_received"] == 0)
        files = [n for n in os.listdir(cfg["results_dir"])
                 if n.startswith("no-timeout-001.skipped-")]
        res = json.load(open(os.path.join(cfg["results_dir"], files[0]),
                             encoding="utf-8")) if files else {}
        check("timeout: failure result queued for the body task_id",
              len(files) == 1 and res.get("task_id") == "no-timeout-001" and
              res.get("status") == "failed" and
              res.get("missing_fields") == ["timeout_minutes"], res)
        check("timeout: report names the field and says it was NOT run",
              "timeout_minutes is required" in res.get("jarvis_report", "") and
              "NOT run" in res.get("jarvis_report", ""), res.get("jarvis_report"))
        check("timeout: rejection logged loudly",
              any("REJECTED task no-timeout-001 (missing-timeout)" in l
                  for l in logs), logs[-3:])
        b.loop_b()
        sent = FakeSMTP.instances[-1].sent if FakeSMTP.instances else []
        check("timeout: failure emailed at once as [JARVIS-RESULT] <task_id>",
              len(sent) == 1 and
              "Subject: [JARVIS-RESULT] no-timeout-001" in sent[0][2], len(sent))
def t_timeout_values():
    bad = [0, -5, "abc", True, "nan", float("inf"), "", [], {}]
    good = [45, "90", 0.5, 720, 1e3]
    check("timeout: invalid values rejected",
          all(bridge_core.task_timeout_problem({"timeout_minutes": v})
              for v in bad),
          [(v, bridge_core.task_timeout_problem({"timeout_minutes": v}))
           for v in bad])
    check("timeout: positive numbers / numeric strings accepted",
          not any(bridge_core.task_timeout_problem({"timeout_minutes": v})
                  for v in good))
    check("timeout: missing and null both 'missing-timeout'",
          bridge_core.task_timeout_problem({}) == "missing-timeout" and
          bridge_core.task_timeout_problem({"timeout_minutes": None})
          == "missing-timeout")
    with tempfile.TemporaryDirectory() as tmp:
        task = dict(TASK, task_id="zero-timeout-001", timeout_minutes=0)
        b, cfg, logs = make_bridge(tmp, [make_msg(body=json.dumps(task))])
        b.loop_a()
        files = [n for n in os.listdir(cfg["results_dir"])
                 if n.startswith("zero-timeout-001.skipped-")]
        res = json.load(open(os.path.join(cfg["results_dir"], files[0]),
                             encoding="utf-8")) if files else {}
        check("timeout: zero rejected with invalid_fields + clear text",
              not inbox_has(tmp, "zero-timeout-001") and
              res.get("invalid_fields") == ["timeout_minutes"] and
              "positive number" in res.get("skip_reason", ""), res)
    with tempfile.TemporaryDirectory() as tmp:
        task = dict(TASK, task_id="explicit-timeout-001", timeout_minutes=360)
        b, cfg, logs = make_bridge(tmp, [make_msg(body=json.dumps(task))])
        b.loop_a()
        p = os.path.join(tmp, "cursor-inbox",
                         "explicit-timeout-001.jarvis-task.json")
        routed = json.load(open(p, encoding="utf-8")) if os.path.exists(p) \
            else {}
        check("timeout: explicit timeout_minutes routed unchanged",
              routed.get("timeout_minutes") == 360, routed)
def t_probe_task_has_timeout():
    with tempfile.TemporaryDirectory() as tmp:
        b, logs, inboxes = make_routed_bridge(tmp, [])
        cfg = b.load()
        _, path = bridge_core.write_probe_task(cfg, "cursor", tmp)
        probe = json.load(open(path, encoding="utf-8"))
        check("timeout: GUI probe task carries a timeout_minutes",
              bridge_core.task_timeout_problem(probe) is None, probe)
# --- Defect 1: bounded, durable send attempts --------------------------------
def _state(tmp):
    p = os.path.join(tmp, bridge_core.SEND_STATE_NAME)
    return json.load(open(p)) if os.path.exists(p) else {}
def t_loop_b_bounded_retries_then_notice():
    with tempfile.TemporaryDirectory() as tmp:
        b, cfg, logs, clock = _gmail_bridge(tmp, transient=5)
        _write_result(cfg, "flaky-001", {"jarvis_report": "did the thing"})
        _cycle(b, clock, 12, 3600)
        originals = [m for m in GmailLikeSMTP.attempts
                     if "result-undeliverable" not in m]
        notices = [m for m in GmailLikeSMTP.attempts
                   if "result-undeliverable" in m]
        st = _state(tmp).get("flaky-001.jarvis-result.json", {})
        check("bounded: a failing result gets exactly %d send attempts"
              % bridge_core.RESULT_SEND_MAX_ATTEMPTS,
              len(originals) == bridge_core.RESULT_SEND_MAX_ATTEMPTS,
              len(originals))
        check("bounded: gave-up state recorded durably with the error",
              st.get("state") == "gave-up" and st.get("attempts") == 5 and
              "network down" in (st.get("last_error") or ""), st)
        check("bounded: give-up logged loudly",
              any("GAVE UP after 5 send attempts" in l for l in logs))
        check("bounded: one failure notice emailed (subject unchanged, "
              "report excerpt kept)", len(notices) == 1 and
              "Subject: [JARVIS-RESULT] flaky-001" in notices[0] and
              "did the thing" in notices[0] and
              "RESULT EMAIL UNDELIVERABLE" in notices[0], len(notices))
        b2, _, _, clock2 = _gmail_bridge(tmp)
        _cycle(b2, clock2, 4, 3600)
        check("bounded: gave-up result not retried after a restart",
              len(GmailLikeSMTP.attempts) == 6, len(GmailLikeSMTP.attempts))
def t_loop_b_attempts_survive_restart():
    with tempfile.TemporaryDirectory() as tmp:
        b, cfg, logs, clock = _gmail_bridge(tmp, transient=10)
        _write_result(cfg, "restart-flaky-001")
        for _ in range(3):
            b.loop_b()
            clock[0] += 3600
        first = len(GmailLikeSMTP.attempts)
        b2, _, logs2, clock2 = _gmail_bridge(tmp, transient=10)
        _cycle(b2, clock2, 10, 3600)
        originals = [m for m in GmailLikeSMTP.attempts
                     if "result-undeliverable" not in m]
        check("durable count: attempts before + after a restart total 5",
              first == 3 and len(originals) == 5, (first, len(originals)))
def t_loop_b_connect_failures_not_counted():
    with tempfile.TemporaryDirectory() as tmp:
        b, cfg, logs, clock = _gmail_bridge(tmp)
        down = [8]
        class LoginFails(GmailLikeSMTP):
            def login(self, user, password):
                if down[0] > 0:
                    down[0] -= 1
                    raise OSError("simulated login outage")
        b._smtp_factory = lambda: LoginFails()
        _write_result(cfg, "login-down-001")
        _cycle(b, clock, 12, 3600)
        check("connect/login failures never count toward the cap -- result "
              "delivered once after the outage",
              len(GmailLikeSMTP.attempts) == 1 and
              "login-down-001.jarvis-result.json" in _sent_keys(tmp) and
              not _state(tmp), (len(GmailLikeSMTP.attempts), _state(tmp)))
def t_loop_b_intent_recorded_before_data():
    with tempfile.TemporaryDirectory() as tmp:
        b, cfg, logs, clock = _gmail_bridge(tmp)
        seen = []
        class Peek(GmailLikeSMTP):
            def sendmail(self, *a):
                seen.append(_state(tmp).get("intent-001.jarvis-result.json"))
                return super().sendmail(*a)
        b._smtp_factory = lambda: Peek()
        _write_result(cfg, "intent-001")
        b.loop_b()
        check("watermark: send-state says 'sending' (attempt 1) before DATA",
              seen and seen[0] and seen[0].get("state") == "sending" and
              seen[0].get("attempts") == 1, seen)
        check("watermark: cleared and sent.json set after success",
              not _state(tmp) and
              "intent-001.jarvis-result.json" in _sent_keys(tmp))
def t_loop_b_twice_in_a_row_one_email():
    with tempfile.TemporaryDirectory() as tmp:
        box = GmailMailbox()
        b, cfg, logs, clock = _mailbox_bridge(tmp, box)
        _write_result(cfg, "twice-001")
        b.loop_b()
        b.loop_b()
        os.remove(os.path.join(tmp, "sent.json"))  # watermark lost
        b2, _, logs2, _ = _mailbox_bridge(tmp, box)
        b2.loop_b()
        b2.loop_b()
        check("send twice (and again after losing sent.json): exactly one "
              "email in the mailbox",
              len(box.attempts) == 1 and
              box.subjects("[JARVIS-RESULT] twice-001") == 1,
              len(box.attempts))
def t_executor_slots_heartbeat():
    with tempfile.TemporaryDirectory() as inbox:
        os.makedirs(os.path.join(inbox, "logs"))
        state = {"runner_pid": 7, "max_slots": 1, "updated_unix": 1000,
                 "heartbeat_stale_seconds": 300, "heartbeat_failures": 2,
                 "slots": [{"slot": 1, "state": "running", "task_id": "a",
                            "started_unix": 900, "heartbeat_unix": 940}]}
        with open(bridge_core.executor_slots_path(inbox), "w") as f:
            json.dump(state, f)
        p = bridge_core.read_executor_slots(inbox, now=1001)
        check("slots: heartbeat age + limit + failure count read",
              p["slots"][0]["heartbeat_age"] == 61 and
              p["heartbeat_stale_seconds"] == 300 and
              p["heartbeat_failures"] == 2, p)
def t_single_instance():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "cfg.json")
        a, b = bridge_core.SingleInstance(p), bridge_core.SingleInstance(p)
        other = bridge_core.SingleInstance(os.path.join(tmp, "other.json"))
        first, second, third = a.acquire(), b.acquire(), other.acquire()
        a.release()
        again = b.acquire()
        b.release()
        other.release()
        check("single instance: second bridge on same config refused",
              first and not second and third and again,
              (first, second, third, again))
if __name__ == "__main__":
    t_accept_valid()
    t_reject_bad_from()
    t_accept_no_ar()
    t_accept_current_gmail_ar()
    t_reject_dkim_fail()
    t_reject_wrong_d()
    t_reject_evil_i()
    t_reject_fake_ar()
    t_reject_bad_json()
    t_dedupe()
    t_loop_b_sends_and_tracks()
    t_loop_b_sends_timeout_result()
    t_loop_b_failure_not_marked()
    t_loop_b_refused_never_resent()
    t_loop_b_sent_never_resent_tracker_unwritable()
    t_loop_b_temp_failure_backoff_then_once()
    t_loop_b_stale_result_after_restart_once()
    t_loop_b_skips_result_already_in_mailbox()
    t_loop_b_mailbox_near_misses_still_sent()
    t_loop_b_552_after_filing_never_resent()
    t_loop_b_temp_error_after_filing_no_duplicate()
    t_loop_b_skip_report_not_hidden_by_main_result()
    t_loop_b_mailbox_unreachable_waits()
    t_risky_attachment_filter()
    t_loop_b_risky_attachment_does_not_block_result()
    t_legacy_config()
    t_missing_config()
    t_routing()
    t_routing_atomic()
    t_refuse_unknown_executor()
    t_dedupe_across_executors()
    t_dedupe_inbox_done()
    t_dedupe_sibling_done_unchanged()
    t_dedupe_skip_notice_sibling_done()
    t_dedupe_notice_uses_body_task_id()
    t_malformed_skip_notice_unchanged()
    t_dedupe_new_id_still_routes()
    t_loop_b_both_inboxes()
    t_legacy_migration()
    t_paths()
    t_init_install()
    t_openclaw_workspace_lookup()
    t_work_dirs_reroute()
    t_work_dirs_fallback_and_off()
    t_work_dirs_config()
    t_work_dirs_probe_and_attachments()
    t_idle_wakes()
    t_idle_fallback()
    t_single_instance()
    t_executor_slots()
    t_reject_missing_timeout()
    t_timeout_values()
    t_probe_task_has_timeout()
    t_loop_b_bounded_retries_then_notice()
    t_loop_b_attempts_survive_restart()
    t_loop_b_connect_failures_not_counted()
    t_loop_b_intent_recorded_before_data()
    t_loop_b_twice_in_a_row_one_email()
    t_executor_slots_heartbeat()
    t_openclaw_bridge_run()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
