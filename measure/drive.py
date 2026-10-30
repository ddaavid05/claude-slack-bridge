#!/usr/bin/env python3
"""Measurement driver: runs a throwaway Claude Code session against the fake Slack and records
the Claude Code behaviours the bridge relies on (see measure/README.md).

  python3 -I drive.py headless      # claude -p: must the bridge stay silent?
  python3 -I drive.py interactive   # a real TUI session in a pseudo-terminal
  python3 -I drive.py interactive --long   # + a Stop waiter answered after 11 minutes

Everything it learns goes to measure/results/<phase>.json and the screen log beside it. It never
touches ~/.claude/settings.json: the hooks come in through --settings, the session runs in a
scratch directory, and the bridge uses its own config and state (env variables).
"""
import fcntl
import json
import os
import pty
import re
import shutil
import struct
import subprocess
import sys
import termios
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from test_bridge import TOKEN, USER, FakeSlack  # noqa: E402

BRIDGE = os.path.join(os.path.dirname(HERE), "bridge.py")
DUMP = os.path.join(HERE, "dump_hook.py")
WORK = os.path.join(HERE, "work")          # the throwaway session's cwd
RESULTS = os.path.join(HERE, "results")
CURSOR_FWD = re.compile(r"\x1b\[(\d*)C")
CURSOR_COL = re.compile(r"\x1b\[\d*G")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[()][0-9A-B]|\x1b[=>]")


def setup(slack, long_run):
    for d in (WORK, RESULTS, os.path.join(HERE, "dumps"), os.path.join(HERE, "bridge-home")):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
    cfg = {"bot_token": TOKEN, "user_id": USER, "language": "en",
           "api_base": f"http://127.0.0.1:{slack.port}",
           "stop_grace_s": 5, "present_window_s": 0, "poll_s": 1,
           "stop_budget_s": 850 if long_run else 300,
           "ask_wait_s": 240, "permission_wait_s": 240}
    config = os.path.join(HERE, "config.json")
    with open(config, "w") as f:
        json.dump(cfg, f)
    envs = (f"CLAUDE_SLACK_BRIDGE_CONFIG={config} "
            f"CLAUDE_SLACK_BRIDGE_HOME={os.path.join(HERE, 'bridge-home')}")
    b = f"{envs} python3 -I {BRIDGE}"
    d = f"python3 -I {DUMP}"
    settings = {"hooks": {
        "Stop": [{"hooks": [
            {"type": "command", "command": f"{b} stop", "asyncRewake": True,
             "timeout": 900 if long_run else 360},
            {"type": "command", "command": f"{d} Stop", "timeout": 10}]}],
        "PreToolUse": [{"matcher": "AskUserQuestion", "hooks": [
            {"type": "command", "command": f"{b} ask", "timeout": 300},
            {"type": "command", "command": f"{d} PreToolUse", "timeout": 10}]}],
        "PermissionRequest": [{"hooks": [
            {"type": "command", "command": f"{b} permission", "timeout": 300},
            {"type": "command", "command": f"{d} PermissionRequest", "timeout": 10}]}],
        "PostToolUse": [{"hooks": [
            {"type": "command", "command": f"{b} posttool", "async": True}]}],
        "UserPromptSubmit": [{"hooks": [
            {"type": "command", "command": f"{b} prompt", "async": True},
            {"type": "command", "command": f"{d} UserPromptSubmit", "timeout": 10}]}],
    }}
    path = os.path.join(HERE, "settings.json")
    with open(path, "w") as f:
        json.dump(settings, f, indent=1)
    return path


def child_env():
    """A fresh terminal's environment: none of this session's CLAUDE* variables."""
    return {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}


def dumps(event):
    out = []
    for name in sorted(os.listdir(os.path.join(HERE, "dumps"))):
        if name.startswith(event + "_"):
            with open(os.path.join(HERE, "dumps", name)) as f:
                out.append(json.load(f))
    return out


def save(phase, data):
    with open(os.path.join(RESULTS, phase + ".json"), "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    print(json.dumps(data, indent=1, ensure_ascii=False))


# ------------------------------------------------------------------------------ headless

def headless():
    slack = FakeSlack()
    settings = setup(slack, False)
    res = {}
    for label, env in (("fresh_env", child_env()), ("inherited_env", dict(os.environ))):
        before = len(slack.parents())
        t0 = time.time()
        r = subprocess.run(["claude", "-p", "Reply with exactly the word OK.", "--settings",
                            settings, "--model", "haiku"], cwd=WORK, env=env,
                           capture_output=True, text=True, timeout=180)
        took = time.time() - t0
        time.sleep(12)  # past the 5 s grace: would a left-behind waiter DM?
        stops = dumps("Stop")
        res[label] = {"exit": r.returncode, "stdout": r.stdout.strip()[:200],
                      "seconds": round(took, 1),
                      "dms_after": len(slack.parents()) - before,
                      "stop_env": stops[-1]["env"] if stops else None,
                      "stop_payload_keys": sorted(stops[-1]["payload"]) if stops else None}
    save("headless", res)


# ------------------------------------------------------------------------------ interactive

class Term:
    def __init__(self, argv, cwd, env, log_path):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 150, 0, 0))
        env = dict(env, TERM="xterm-256color", COLUMNS="150", LINES="45")
        self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=slave, stdout=slave,
                                     stderr=slave, start_new_session=True, close_fds=True)
        os.close(slave)
        self.raw = b""
        self.lock = threading.Lock()
        self.log = open(log_path, "wb")
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        while True:
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                return
            if not chunk:
                return
            with self.lock:
                self.raw += chunk
            self.log.write(chunk)
            self.log.flush()

    def text(self, since=0):
        with self.lock:
            raw = self.raw[since:]
        s = raw.decode("utf-8", "replace")
        # The TUI draws spaces as cursor-forward moves; give them back before stripping.
        s = CURSOR_FWD.sub(lambda m: " " * int(m.group(1) or 1), s)
        s = CURSOR_COL.sub(" ", s)
        return ANSI.sub("", s)

    def mark(self):
        with self.lock:
            return len(self.raw)

    def send(self, s, delay=0.03):
        for ch in s:
            os.write(self.master, ch.encode())
            time.sleep(delay)

    def submit(self, prompt):
        self.send(prompt)
        time.sleep(0.5)
        os.write(self.master, b"\r")

    def wait_text(self, pattern, since=0, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            if re.search(pattern, self.text(since)):
                return True
            time.sleep(0.3)
        return False


def wait_parent(slack, n, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if len(slack.parents()) >= n:
            return slack.parents()[n - 1]
        time.sleep(0.3)
    return None


def transcript_text(session_id):
    """The session's transcript as one string (assistant text, tool results, attachments)."""
    root = os.path.expanduser("~/.claude/projects")
    for d in os.listdir(root):
        p = os.path.join(root, d, session_id + ".jsonl")
        if os.path.exists(p):
            with open(p) as f:
                return f.read()
    return ""


def interactive(long_run):
    slack = FakeSlack()
    settings = setup(slack, long_run)
    res = {}
    term = Term(["claude", "--settings", settings, "--model", "haiku",
                 "--permission-mode", "default"], WORK, child_env(),
                os.path.join(RESULTS, "screen.log"))

    # The folder-trust dialog defaults to "No, exit": step down to "Yes" before Enter.
    if term.wait_text(r"trust this folder|Quick safety check", timeout=40):
        res["trust_dialog"] = True
        for _ in range(4):
            time.sleep(1.5)
            sel = re.findall(r"❯\s*([^\r\n]{0,40})", term.text()[-2500:])
            if sel and sel[-1].strip().lower().startswith("yes"):
                os.write(term.master, b"\r")
                break
            os.write(term.master, b"\x1b[B")
    res["ready"] = term.wait_text(r"for shortcuts|Try \"", timeout=60)
    if not res["ready"]:
        res["screen_not_ready"] = term.text()[-1500:]
        term.proc.terminate()
        save("interactive", res)
        return

    # A. Stop + asyncRewake: does a Slack reply wake an idle session?
    m = term.mark()
    term.submit("Reply with exactly the word ALPHA and nothing else.")
    res["A_alpha_seen"] = term.wait_text(r"ALPHA", since=m, timeout=90)
    p = wait_parent(slack, 1, 60)
    res["A_dm_posted"] = bool(p)
    if p:
        t0 = time.time()
        m = term.mark()
        slack.reply("Reply with exactly the word BRAVO and nothing else.", parent=p)
        res["A_woken"] = term.wait_text(r"BRAVO", since=m, timeout=120)
        res["A_wake_seconds"] = round(time.time() - t0, 1)
        res["A_thread"] = [x["text"] for x in slack.thread(p)]

    stops = dumps("Stop")
    sid = stops[0]["payload"].get("session_id") if stops else None
    res["session_id"] = sid
    res["stop_env"] = stops[0]["env"] if stops else None

    # The woken turn ended too: its Stop waiter DMs; the next prompt must close it.
    second = wait_parent(slack, 2, 60)
    res["A_second_dm"] = bool(second)

    # C. AskUserQuestion answered with free text from Slack; is the dialog on screen meanwhile?
    n = len(slack.parents())
    m = term.mark()
    term.submit("Use the AskUserQuestion tool to ask me one question: which colour I prefer, "
                "with the two options Crimson and Azure. After I answer, repeat my answer "
                "verbatim inside square brackets.")
    if second:
        time.sleep(3)
        res["prompt_closed_waiter"] = "Answered at the terminal" in slack.parents()[1]["text"]
    q = None
    end = time.time() + 120
    while time.time() < end and not q:
        for msg in slack.parents()[n:]:
            if "A question is waiting" in msg["text"]:
                q = msg
        time.sleep(0.5)
    res["C_dm_posted"] = bool(q)
    if q:
        time.sleep(4)
        res["C_screen_while_waiting"] = term.text(m)[-1500:]
        m2 = term.mark()
        slack.reply("Neither, green", parent=q)
        res["C_answer_echoed"] = term.wait_text(r"\[[^\]]*[Gg]reen[^\]]*\]", since=m2, timeout=120)
        res["C_screen_after"] = term.text(m2)[-1200:]

    # E. PermissionRequest approved from Slack; is the dialog on screen meanwhile?
    target = os.path.join(WORK, "perm_test_file")
    n = len(slack.parents())
    m = term.mark()
    term.submit(f"Run exactly this bash command and nothing else: touch {target}")
    perm = None
    end = time.time() + 120
    while time.time() < end and not perm:
        for msg in slack.parents()[n:]:
            if "Waiting for permission" in msg["text"]:
                perm = msg
        time.sleep(0.5)
    res["E_dm_posted"] = bool(perm)
    if perm:
        time.sleep(4)
        res["E_screen_while_waiting"] = term.text(m)[-1500:]
        slack.reply("igen", parent=perm)
        end = time.time() + 60
        while time.time() < end and not os.path.exists(target):
            time.sleep(0.5)
        res["E_command_ran"] = os.path.exists(target)
        res["E_dm_text"] = perm["text"]
    perms = dumps("PermissionRequest")
    res["permission_payload_keys"] = sorted(perms[0]["payload"]) if perms else None

    # F. A permission answered at the terminal while its DM waits: is the waiter stopped?
    target2 = os.path.join(WORK, "perm_test_file_2")
    n = len(slack.parents())
    term.submit(f"Run exactly this bash command and nothing else: touch {target2}")
    perm2 = None
    end = time.time() + 120
    while time.time() < end and not perm2:
        for msg in slack.parents()[n:]:
            if "Waiting for permission" in msg["text"]:
                perm2 = msg
        time.sleep(0.5)
    res["F_dm_posted"] = bool(perm2)
    if perm2 and sid:
        state_file = os.path.join(HERE, "bridge-home", "state", sid + ".json")
        with open(state_file) as f:
            pids = [q["pid"] for q in json.load(f)["open"].values() if q["kind"] == "permission"]
        time.sleep(2)
        os.write(term.master, b"\r")   # the dialog's default, "1. Yes"
        end = time.time() + 30
        while time.time() < end and not os.path.exists(target2):
            time.sleep(0.5)
        res["F_command_ran"] = os.path.exists(target2)
        time.sleep(8)

        def alive(pid):
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False
        res["F_waiter_alive_8s_after"] = [alive(p) for p in pids]
        res["F_dm_text_after"] = [m["text"] for m in slack.parents() if m["ts"] == perm2["ts"]]

    # B. A Stop waiter answered after 11 minutes (past the 600 s default timeout).
    if long_run:
        n = len(slack.parents())
        last = wait_parent(slack, n + 1, 90) or (slack.parents()[-1] if slack.parents() else None)
        res["B_waiter_dm"] = bool(last)
        if last:
            time.sleep(660)
            m = term.mark()
            slack.reply("Reply with exactly the word CHARLIE and nothing else.", parent=last)
            res["B_woken_after_11_min"] = term.wait_text(r"CHARLIE", since=m, timeout=120)
            res["B_thread"] = [x["text"] for x in slack.thread(last)]

    if sid:
        tr = transcript_text(sid)
        i = tr.find("BRAVO")
        res["transcript_around_bravo"] = tr[max(0, i - 700):i + 100] if i >= 0 else None
        j = tr.find("Neither")
        res["transcript_around_answer"] = tr[max(0, j - 400):j + 300] if j >= 0 else None

    term.send("/exit", delay=0.05)
    time.sleep(0.5)
    os.write(term.master, b"\r")
    try:
        term.proc.wait(20)
    except subprocess.TimeoutExpired:
        term.proc.terminate()
    res["log_tail"] = open(os.path.join(HERE, "bridge-home", "bridge.log")).read()[-1500:] \
        if os.path.exists(os.path.join(HERE, "bridge-home", "bridge.log")) else ""
    save("interactive", res)


if __name__ == "__main__":
    if sys.argv[1:2] == ["headless"]:
        headless()
    elif sys.argv[1:2] == ["interactive"]:
        interactive("--long" in sys.argv)
    else:
        print(__doc__)
