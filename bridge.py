#!/usr/bin/env python3
"""claude-slack-bridge: a Slack DM bridge for long Claude Code sessions.

When a session waits for you, your own Slack app's bot DMs you; your reply in the thread goes back
into the session. Run by Claude Code hooks (installed into ~/.claude/settings.json by install.sh),
one subcommand per hook, the hook's JSON on stdin:

  stop        Stop, asyncRewake: DM Claude's whole last message (as Slack text, in parts when
              long), wait for a reply, exit 2 with it on stderr (that wakes the session).
  ask         PreToolUse on AskUserQuestion: DM the questions with numbered options and answer the
              tool from the reply (allow + updatedInput.answers).
  permission  PermissionRequest: the terminal's dialog shows at once; when it is still open after
              the grace period, DM the tool and its input; "yes" allows this one call, "no
              <reason>" denies it.
  prompt      UserPromptSubmit, async: you typed at the terminal: back to terminal mode, and every
              open Slack question of this session is marked answered there.
  posttool    PostToolUse, async: a tool ran; a permission still waiting (in its grace period or on
              Slack) for that same call was answered at the terminal's dialog, so close it.
  sessionend  SessionEnd: the session ended (/clear, exit, …): its waiters stop, its open DMs are
              marked ended, and nothing more goes out for it.
  sessionstart  SessionStart: a session resumed (/resume) is live again.

Two modes per session. Terminal mode (the default): nothing goes to Slack until you have not typed
for the grace period (grace_s, 10 minutes). Slack mode (once you answered on Slack): everything
goes to Slack at once, until you type at the terminal or ask Claude to go back to it
(`mode terminal`). Nothing goes out while the session runs background work (it wakes itself).

By hand, or by Claude when you ask:
  on | off                    the whole machine
  session-off | session-on    this session only ($CLAUDE_CODE_SESSION_ID)
  mode terminal | mode slack  this session's mode
  status | selftest

Rules it never breaks: it never blocks a session (every failure is a silent exit 0, logged) and it
never decides for you (only a reply from the configured Slack user ID is taken). The bot token is
never printed or logged. $CLAUDE_SLACK_BRIDGE=off silences a session from its start.

Config: ~/.config/claude-slack-bridge/config.json (or $CLAUDE_SLACK_BRIDGE_CONFIG). State and log:
the folder this file is in (or $CLAUDE_SLACK_BRIDGE_HOME). See README.md.
"""

import fcntl
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HOME_DIR = os.environ.get("CLAUDE_SLACK_BRIDGE_HOME",
                          os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.environ.get("CLAUDE_SLACK_BRIDGE_CONFIG",
                             os.path.expanduser("~/.config/claude-slack-bridge/config.json"))
STATE_DIR = os.path.join(HOME_DIR, "state")
OFF_FILE = os.path.join(STATE_DIR, "off")
LOG_PATH = os.path.join(HOME_DIR, "bridge.log")
LOG_MAX_BYTES = 1_000_000

DEFAULTS = {
    "api_base": "https://slack.com/api",
    "language": "en",           # the Slack messages' language: en | hu
    "grace_s": 600,             # terminal mode: this long without you before anything goes out
    "stop_budget_s": 3540,      # Stop: grace + watching the thread, below the hook's timeout (3600)
    "ask_wait_s": 1800,
    "permission_wait_s": 1200,  # after the DM; grace + this stays below the hook's timeout (1860)
    "poll_s": 5,
    "chunk_chars": 3900,        # one Slack message shows about 4000 characters well
    "input_chars": 12000,       # how much of a tool's input goes into a permission DM
    "http_timeout_s": 10,
}

YES_WORDS = {"yes", "y", "ok", "okay", "sure", "go", "igen", "i", "oké", "oke", "mehet", "jó", "jo"}
NO_WORDS = {"no", "n", "nope", "nem", "ne"}

EXIT_REWAKE = 2  # asyncRewake: exit 2 wakes the session with stderr
TRANSCRIPT_TAIL = 2_000_000
PLACEHOLDERS = ("PASTE", "IDE-JON")

# What you read on Slack and in the terminal. What Claude reads (the answer handed back, a denial's
# reason) is English whatever the language.
TEXTS = {
    "en": {
        "waiting_slack": "Waiting for you (Slack mode).",
        "waiting_idle": "Waiting for you — you haven't typed in the terminal for {m} min.",
        "reply_here": "Reply in this thread.",
        "reply_here_slack": ("Reply in this thread. _Going back to the computer? Say so, or just "
                             "type in the terminal._"),
        "continued": "_(continued below — {n} parts, reply on the last)_",
        "handed": "Handed over to the session.",
        "expired_wait": "Expired — waiting at the terminal.",
        "expired_ask": "Expired — asking at the terminal.",
        "superseded": "Outdated — a newer message came.",
        "moved_on": "Outdated — the session moved on.",
        "answered_terminal": "Answered at the terminal.",
        "session_gone": "The session has ended — a reply here no longer reaches it.",
        "switched_off": "Switched off for this session.",
        "empty_reply": "Empty reply — write it again.",
        "yes_no_unclear": "I don't understand — answer *yes*, or *no* followed by the reason.",
        "n_questions": "There are {n} questions — one answer per line, in their order.",
        "not_understood": ("I don't understand “{line}” — write an option's number (several "
                           "with commas), or free text."),
        "question_waiting": "A question is waiting:",
        "multi_hint": " _(several allowed: 1,3)_",
        "ask_footer": "Reply in this thread with a number or free text.",
        "ask_footer_multi": ("Reply in this thread with a number or free text — one per line, in "
                             "the questions' order."),
        "perm_waiting": "Waiting for permission: *{tool}*",
        "perm_open_for": " — open in the terminal for {m} min",
        "mcp_server": "MCP server: {s}",
        "cut": ("_CUT: only the first {n} characters are shown — if you can't see what you are "
                "approving, answer no and look at the terminal._"),
        "perm_footer": "Answer *yes* (this one call only) or *no <reason>*.",
        "allowed": "Allowed, for this one call.",
        "denied": "Denied.",
        "selftest_dm": "The Slack bridge works.",
        "selftest_footer": "Reply anything in this thread and it shows in the terminal.",
        "selftest_sent": "DM sent; waiting 2 minutes for your reply in its thread…",
        "selftest_none": "No reply within 2 minutes.",
        "selftest_thanks": "Got it, thanks.",
        "selftest_reply": "Your reply:",
        "machine_on": "The Slack bridge is ON on this machine.",
        "machine_off": "The Slack bridge is OFF on this machine.",
        "config_ok": "Config OK:",
        "config_bad": "Config incomplete:",
        "session_line": "This session ({sid}): {state}, {mode} mode, {n} open question(s).",
        "on": "ON", "off": "OFF", "slack": "Slack", "terminal": "terminal",
        "session_off": "The Slack bridge is OFF for this session.",
        "session_on": "The Slack bridge is ON for this session.",
        "mode_now": "This session is in {mode} mode now.",
        "mode_usage": "Usage: mode terminal | mode slack",
        "no_session": ("I can't see this session's ID (CLAUDE_CODE_SESSION_ID): run this from "
                       "inside the session (Claude's Bash tool, or the ! prefix)."),
    },
    "hu": {
        "waiting_slack": "Rád vár (Slack mód).",
        "waiting_idle": "Rád vár — {m} perce nem gépeltél a terminálban.",
        "reply_here": "Válaszolj ebben a threadben.",
        "reply_here_slack": ("Válaszolj ebben a threadben. _Ha visszamész a géphez, írd meg, vagy "
                             "csak gépelj a terminálban._"),
        "continued": "_(folytatás lent — {n} részben, a végén válaszolj)_",
        "handed": "Átadva a sessionnek.",
        "expired_wait": "Lejárt — a terminálban várok.",
        "expired_ask": "Lejárt — a terminálban kérdezem.",
        "superseded": "Elavult — újabb üzenet jött.",
        "moved_on": "Elavult — a session továbbment.",
        "answered_terminal": "Megválaszolva a terminálban.",
        "session_gone": "A session véget ért — ide írt válasz már nem megy át.",
        "switched_off": "Kikapcsolva ebben a sessionben.",
        "empty_reply": "Üres válasz — írd be újra.",
        "yes_no_unclear": "Nem értem — írd azt, hogy *igen*, vagy azt, hogy *nem* (utána az indok).",
        "n_questions": "{n} kérdés van — soronként egy választ írj, a kérdések sorrendjében.",
        "not_understood": ("Ezt nem értem: „{line}” — egy opció számát írd (több választásnál "
                           "vesszővel), vagy szabad szöveget."),
        "question_waiting": "Kérdés vár:",
        "multi_hint": " _(több is választható: 1,3)_",
        "ask_footer": "Válaszolj ebben a threadben egy számmal vagy szabad szöveggel.",
        "ask_footer_multi": ("Válaszolj ebben a threadben egy számmal vagy szabad szöveggel — "
                             "soronként egyet, a kérdések sorrendjében."),
        "perm_waiting": "Engedélyre vár: *{tool}*",
        "perm_open_for": " — {m} perce nyitva a terminálban",
        "mcp_server": "MCP szerver: {s}",
        "cut": ("_LEVÁGVA: csak az első {n} karakter látszik — ha nem látod, mit hagysz jóvá, "
                "írd azt, hogy nem, és nézd meg a terminálban._"),
        "perm_footer": "Válasz: *igen* (csak erre az egy hívásra) vagy *nem <indok>*.",
        "allowed": "Engedélyezve, erre az egy hívásra.",
        "denied": "Elutasítva.",
        "selftest_dm": "A Slack-híd működik.",
        "selftest_footer": "Válaszolj ebben a threadben bármit, és a terminálban megjelenik.",
        "selftest_sent": "DM elküldve, 2 percig várom a választ a threadben…",
        "selftest_none": "Nem jött válasz 2 perc alatt.",
        "selftest_thanks": "Megjött, köszönöm.",
        "selftest_reply": "A válaszod:",
        "machine_on": "A Slack-híd az egész gépen BE van kapcsolva.",
        "machine_off": "A Slack-híd az egész gépen KI van kapcsolva.",
        "config_ok": "A config rendben:",
        "config_bad": "A config hiányos:",
        "session_line": "Ez a session ({sid}): {state}, {mode} mód, {n} nyitott kérdés.",
        "on": "BE", "off": "KI", "slack": "Slack", "terminal": "terminál",
        "session_off": "A Slack-híd ebben a sessionben KI van kapcsolva.",
        "session_on": "A Slack-híd ebben a sessionben BE van kapcsolva.",
        "mode_now": "Ez a session most {mode} módban van.",
        "mode_usage": "Használat: mode terminal | mode slack",
        "no_session": ("Nem látom ennek a sessionnek az azonosítóját (CLAUDE_CODE_SESSION_ID): ezt "
                       "a parancsot a sessionből kell futtatni (Claude Bash eszközéből vagy a ! "
                       "előtaggal)."),
    },
}
LANG = "en"   # set from the config once it is read


def t(key, **kw):
    return TEXTS.get(LANG, TEXTS["en"]).get(key, TEXTS["en"][key]).format(**kw)


# --------------------------------------------------------------------------- small helpers

def log(msg):
    try:
        os.makedirs(HOME_DIR, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [{os.getpid()}] {msg}\n")
    except OSError:
        pass


def read_language():
    """The configured language, even from a config that is not complete yet."""
    global LANG
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            lang = json.load(f).get("language", "en")
    except (OSError, ValueError, AttributeError):
        lang = "en"
    LANG = lang if lang in TEXTS else "en"


def load_config():
    global LANG
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    if "stop_grace_s" in cfg and "grace_s" not in cfg:   # its name before 2026-10-30
        cfg["grace_s"] = cfg["stop_grace_s"]
    merged = dict(DEFAULTS)
    merged.update(cfg)
    LANG = merged["language"] if merged["language"] in TEXTS else "en"
    token, user = merged.get("bot_token", ""), merged.get("user_id", "")
    if not token.startswith("xoxb-") or any(p in token for p in PLACEHOLDERS):
        raise ValueError("bot_token is not set in the config")
    if not re.fullmatch(r"[UW][A-Z0-9]+", user or ""):
        raise ValueError("user_id is not set in the config")
    return merged


def attended():
    """True for an interactive session; headless runs (claude -p, the SDK) stay silent."""
    flag = os.environ.get("CLAUDE_CODE_SESSION_ATTENDED")
    if flag is not None:
        return flag == "1"
    return os.environ.get("CLAUDE_CODE_ENTRYPOINT", "cli") == "cli"


def slack_escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_unescape(text):
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def clip_head(text, n):
    if len(text) <= n:
        return text, False
    return text[:n], True


def code_block(text):
    # A ``` inside would close Slack's block early; break it with a zero-width space.
    return "```\n" + slack_escape(text).replace("```", "``​`") + "\n```"


def header(hook):
    cwd = hook.get("cwd") or os.getcwd()
    branch = ""
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True, timeout=2)
        if out.returncode == 0:
            branch = out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    parts = [socket.gethostname(), os.path.basename(cwd.rstrip("/")) or cwd]
    if branch:
        parts.append(branch)
    parts.append("session " + (hook.get("session_id") or "?")[:8])
    return "*" + slack_escape(" · ".join(parts)) + "*"


def minutes(seconds):
    return max(1, round(seconds / 60))


# --------------------------------------------------------------------------- Markdown -> Slack

_INLINE_CODE = re.compile(r"`[^`\n]+`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+")
_RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _inline(text):
    """One line of Markdown prose as Slack mrkdwn."""
    codes, links = [], []

    def keep_code(m):
        codes.append(slack_escape(m.group(0)))
        return f"\x00{len(codes) - 1}\x00"

    def keep_link(m):
        links.append(f"<{m.group(2)}|{slack_escape(m.group(1))}>")
        return f"\x02{len(links) - 1}\x02"
    text = _INLINE_CODE.sub(keep_code, text)
    text = _LINK.sub(keep_link, text)
    text = slack_escape(text)
    text = _BOLD.sub(lambda m: "\x01" + (m.group(1) or m.group(2)) + "\x01", text)
    text = _ITALIC.sub(r"_\1_", text)
    text = text.replace("\x01", "*")
    text = re.sub("\x02(\\d+)\x02", lambda m: links[int(m.group(1))], text)
    return re.sub("\x00(\\d+)\x00", lambda m: codes[int(m.group(1))], text)


def _plain(text):
    """Table cell text: Markdown marks dropped (the table goes into a code block)."""
    text = _LINK.sub(r"\1", text)
    return text.replace("**", "").replace("__", "").replace("`", "")


def _table(rows):
    cells = [[_plain(c.strip()) for c in r.strip().strip("|").split("|")] for r in rows
             if not _TABLE_SEP.match(r)]
    width = max(len(r) for r in cells)
    cells = [r + [""] * (width - len(r)) for r in cells]
    cols = [max(len(r[i]) for r in cells) for i in range(width)]
    lines = ["  ".join(c.ljust(cols[i]) for i, c in enumerate(r)).rstrip() for r in cells]
    if len(lines) > 1:
        lines.insert(1, "  ".join("-" * w for w in cols))
    return ["```"] + [slack_escape(ln) for ln in lines] + ["```"]


def to_mrkdwn(md):
    """Claude's GitHub Markdown as Slack mrkdwn: headings bold, **x** as *x*, bullets as •, links
    as <url|text>, tables as aligned text in a code block, code blocks kept."""
    out = []
    lines = (md or "").replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            out.append("```")
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith("```"):
                out.append(slack_escape(lines[i]))
                i += 1
            out.append("```")
            i += 1
            continue
        if line.lstrip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(lines[i])
                i += 1
            out.extend(_table(rows))
            continue
        h = _HEADING.match(line)
        if h:
            out.append("*" + _inline(h.group(1)).replace("*", "") + "*")
        elif _RULE.match(line):
            out.append("────────────")
        else:
            out.append(_inline(_BULLET.sub(lambda m: m.group(1) + "• ", line)))
        i += 1
    return "\n".join(out).strip("\n")


def chunks(text, limit):
    """Split Slack text into parts of at most `limit` characters on line boundaries; a code block
    cut in two is closed at the cut and opened again in the next part."""
    parts, cur, size, in_code = [], [], 0, False
    step = max(limit - 20, 20)

    def flush():
        nonlocal cur, size
        parts.append("\n".join(cur + (["```"] if in_code else [])))
        cur, size = (["```"], 4) if in_code else ([], 0)

    for line in text.split("\n"):
        for piece in [line[i:i + step] for i in range(0, len(line), step)] or [""]:
            if cur and size + len(piece) + 1 > limit - 4:
                flush()
            cur.append(piece)
            size += len(piece) + 1
        if line.strip().startswith("```"):
            in_code = not in_code
    if any(ln.strip().strip("`") for ln in cur):
        parts.append("\n".join(cur + (["```"] if in_code else [])))
    return parts or [""]


# --------------------------------------------------------------------------- Slack API

class SlackError(Exception):
    pass


class Slack:
    def __init__(self, cfg):
        self.base = cfg["api_base"].rstrip("/")
        self.token = cfg["bot_token"]
        self.timeout = cfg["http_timeout_s"]

    def call(self, method, params, http_get=False):
        for _ in range(3):
            if http_get:
                url = f"{self.base}/{method}?{urllib.parse.urlencode(params)}"
                req = urllib.request.Request(url, method="GET")
            else:
                req = urllib.request.Request(
                    f"{self.base}/{method}", data=json.dumps(params).encode(), method="POST",
                    headers={"Content-Type": "application/json; charset=utf-8"})
            req.add_header("Authorization", "Bearer " + self.token)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(min(int(e.headers.get("Retry-After", "5")), 60))
                    continue
                raise SlackError(f"{method}: HTTP {e.code}") from None
            except (urllib.error.URLError, OSError, ValueError) as e:
                raise SlackError(f"{method}: {type(e).__name__}") from None
            if not body.get("ok"):
                raise SlackError(f"{method}: {body.get('error', 'not ok')}")
            return body
        raise SlackError(f"{method}: rate limited")

    def post(self, channel, text, thread_ts=None):
        params = {"channel": channel, "text": text, "unfurl_links": False, "unfurl_media": False}
        if thread_ts:
            params["thread_ts"] = thread_ts
        body = self.call("chat.postMessage", params)
        return body["channel"], body["ts"]

    def update(self, channel, ts, text):
        self.call("chat.update", {"channel": channel, "ts": ts, "text": text})

    def replies(self, channel, ts):
        body = self.call("conversations.replies", {"channel": channel, "ts": ts, "limit": 100},
                         http_get=True)
        return [m for m in body.get("messages", []) if m.get("ts") != ts]


# --------------------------------------------------------------------------- per-session state

def _state_path(session_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id or "unknown")
    return os.path.join(STATE_DIR, safe + ".json")


class State:
    """One JSON file per session, read-modify-write under an flock: `open` (the questions still
    waiting, by key), `last_prompt`, `stop_gen`, `mode` (terminal | slack), `off`."""

    def __init__(self, session_id):
        os.makedirs(STATE_DIR, exist_ok=True)
        self.path = _state_path(session_id)

    def _locked(self, fn, write=True):
        with open(self.path + ".lock", "a") as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            try:
                try:
                    with open(self.path, encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, ValueError):
                    data = {}
                data.setdefault("open", {})
                result = fn(data)
                if not write:
                    return result
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, self.path)
                return result
            finally:
                fcntl.flock(lockf, fcntl.LOCK_UN)

    def read(self):
        return self._locked(lambda d: json.loads(json.dumps(d)), write=False)

    def update(self, fn):
        return self._locked(fn)

    def has_open(self):
        return os.path.exists(self.path) and bool(self.read()["open"])

    def set(self, **kv):
        self.update(lambda d: d.update(kv))


def slack_mode(state):
    return state.read().get("mode") == "slack"


def session_silenced(state):
    s = state.read()
    return (os.environ.get("CLAUDE_SLACK_BRIDGE", "").lower() == "off"
            or bool(s.get("off")) or bool(s.get("ended")))


def take_all(**kv):
    """A state update: set kv, take every open question off the list, return them."""
    def take(d):
        d.update(kv)
        taken = list(d["open"].values())
        d["open"] = {}
        return taken
    return take


# --------------------------------------------------------------------------- the waiter

class Cancelled(Exception):
    pass


def claude_alive():
    """The session's own process (CLAUDE_PID, given to every hook; measured 2026-10-29)."""
    try:
        pid = int(os.environ.get("CLAUDE_PID", "0"))
    except ValueError:
        return True
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def wait_for_reply(slack, cfg, state, key, channel, ts, wait_s, parse):
    """Poll the thread until a reply from the user parses, the wait ends, or the question is
    taken off the open list (answered at the terminal, superseded, switched off). Returns parse's
    value or None on timeout; raises Cancelled. A reply puts the session in Slack mode."""
    deadline = time.monotonic() + wait_s
    seen = set()
    while time.monotonic() < deadline:
        if key not in state.read()["open"]:
            raise Cancelled()
        if not claude_alive():
            # Never answer "handed over" into a session that is gone.
            def take(d):
                q = d["open"].pop(key, None)
                return [q] if q else []
            close_all(slack, state.update(take), t("session_gone"))
            raise Cancelled()
        try:
            msgs = slack.replies(channel, ts)
        except SlackError as e:
            log(f"replies failed: {e}")
            msgs = []
        for m in msgs:
            if m["ts"] in seen:
                continue
            seen.add(m["ts"])
            if m.get("user") != cfg["user_id"] or m.get("bot_id"):
                continue
            ok, value, complaint = parse(slack_unescape(m.get("text", "")))
            if ok:
                state.set(mode="slack")
                return value
            try:
                slack.post(channel, complaint, thread_ts=ts)
            except SlackError as e:
                log(f"complaint failed: {e}")
        time.sleep(cfg["poll_s"])
    return None


def post_parts(slack, cfg, text, footer):
    """Post `text` (Slack mrkdwn) as one DM or several, the footer on the last; returns the
    channel, the last part's ts (the thread to answer in) and that part's text."""
    parts = chunks(text, cfg["chunk_chars"] - len(footer) - 60)
    n = len(parts)
    channel, ts, body = cfg["user_id"], None, ""
    for i, part in enumerate(parts, 1):
        body = (f"_({i}/{n})_\n" if i > 1 else "") + part
        if i == n:
            body += "\n" + footer
        elif i == 1:
            body += "\n" + t("continued", n=n)
        channel, ts = slack.post(channel, body)
    return channel, ts, body


def open_question(slack, cfg, state, kind, text, footer, **extra):
    channel, ts, body = post_parts(slack, cfg, text, footer)
    key = f"{kind}:{ts}"
    now = time.time()

    def add(d):
        d["open"][key] = dict(extra, kind=kind, channel=channel, ts=ts, text=body,
                              since=now, pid=os.getpid())
    state.update(add)
    return key, channel, ts, body


def close_question(state, key):
    state.update(lambda d: d["open"].pop(key, None))


def close_all(slack, taken, note):
    for q in taken:
        if not q.get("channel") or slack is None:
            continue                     # a permission still in its grace period: no DM yet
        try:
            slack.update(q["channel"], q["ts"], strike(q["text"], note))
        except SlackError as e:
            log(f"close update failed: {e}")


def strike(text, note):
    """The question as it stood, with a note under its first line saying what became of it."""
    first, _, rest = text.partition("\n")
    return f"{first}\n_{note}_\n{rest}"


def call_fingerprint(tool_name, tool_input):
    blob = json.dumps([tool_name, tool_input], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


# --------------------------------------------------------------------------- reply parsers

def parse_free(text):
    text = text.strip()
    if not text:
        return False, None, t("empty_reply")
    return True, text, ""


def parse_yes_no(text):
    text = text.strip()
    word = re.split(r"[\s,.!:;]+", text.lower(), maxsplit=1)[0] if text else ""
    rest = text[len(word):].strip(" ,.!:;-–") if word else ""
    if word in YES_WORDS:
        return True, ("allow", rest), ""
    if word in NO_WORDS:
        return True, ("deny", rest), ""
    return False, None, t("yes_no_unclear")


def make_question_parser(questions):
    def parse_one(q, line):
        opts = [o.get("label", "") for o in q.get("options", [])]
        line = line.strip()
        if re.fullmatch(r"\d+(\s*,\s*\d+)*", line):
            nums = [int(x) for x in re.split(r"\s*,\s*", line)]
            if any(n < 1 or n > len(opts) for n in nums):
                return None
            if len(nums) > 1 and not q.get("multiSelect"):
                return None
            return ", ".join(opts[n - 1] for n in nums)
        return line or None  # free text: the dialog's "Other"

    def parse(text):
        lines = [ln for ln in (s.strip() for s in text.strip().splitlines()) if ln]
        if len(questions) > 1:
            lines = [re.sub(r"^\d+\s*[.):]\s*", "", ln) for ln in lines]
            if len(lines) != len(questions):
                return False, None, t("n_questions", n=len(questions))
        else:
            lines = [" ".join(lines)]
        answers = {}
        for q, line in zip(questions, lines):
            value = parse_one(q, line)
            if value is None:
                return False, None, t("not_understood", line=slack_escape(line))
            answers[q.get("question", "")] = value
        return True, answers, ""
    return parse


# --------------------------------------------------------------------------- the hooks

def typed_within(state, seconds):
    return time.time() - state.read().get("last_prompt", 0.0) < seconds


def busy(hook):
    """Background work still running in the session (a shell command, a monitor, an agent): the
    session wakes itself when it ends, so a turn ending now is a progress report, not a wait.
    Measured 2026-10-30: [{"id", "type": "shell", "status": "running", ...}]."""
    tasks = hook.get("background_tasks") or []
    if isinstance(tasks, dict):
        tasks = list(tasks.values())
    if not isinstance(tasks, list):
        return bool(tasks)
    live = 0
    for task in tasks:
        status = str(task.get("status", "running")).lower() if isinstance(task, dict) else "running"
        if status not in ("completed", "done", "finished", "failed", "killed", "stopped",
                          "cancelled", "canceled", "error"):
            live += 1
    return live > 0


def cmd_stop(hook, cfg, slack, state):
    started = time.time()

    def next_gen(d):
        d["stop_gen"] = d.get("stop_gen", 0) + 1
        return d["stop_gen"]
    gen = state.update(next_gen)

    # The turn ended: whatever this session still has open on Slack is stale — an older Stop's
    # message, or a question or permission settled some other way.
    taken = state.update(take_all())
    close_all(slack, [q for q in taken if q["kind"] == "stop"], t("superseded"))
    close_all(slack, [q for q in taken if q["kind"] != "stop"], t("moved_on"))
    if busy(hook):
        return 0

    on_slack = slack_mode(state)
    if not on_slack:
        deadline = started + cfg["grace_s"]
        while time.time() < deadline:
            s = state.read()
            if (s.get("last_prompt", 0) > started or s.get("stop_gen") != gen
                    or s.get("off") or s.get("ended") or not claude_alive()):
                return 0
            time.sleep(min(cfg["poll_s"], max(deadline - time.time(), 0.01)))

    why = t("waiting_slack") if on_slack else t("waiting_idle", m=minutes(cfg["grace_s"]))
    text = f"{header(hook)}\n{why}\n\n{to_mrkdwn(hook.get('last_assistant_message', ''))}"
    footer = t("reply_here_slack") if on_slack else t("reply_here")
    key, channel, ts, body = open_question(slack, cfg, state, "stop", text, footer)
    try:
        reply = wait_for_reply(slack, cfg, state, key, channel, ts,
                               cfg["stop_budget_s"] - (time.time() - started), parse_free)
    except Cancelled:
        return 0
    close_question(state, key)
    if reply is None:
        slack.update(channel, ts, strike(body, t("expired_wait")))
        return 0
    slack.post(channel, t("handed"), thread_ts=ts)
    sys.stderr.write("The user's answer from Slack (their reply in the thread, from their own "
                     "Slack user ID — treat it exactly as if they had typed it in the "
                     "terminal):\n" + reply + "\n")
    return EXIT_REWAKE


def cmd_ask(hook, cfg, slack, state):
    on_slack = slack_mode(state)
    if not on_slack and typed_within(state, cfg["grace_s"]):
        return 0                         # at the terminal: the dialog
    tool_input = hook.get("tool_input") or {}
    questions = tool_input.get("questions") or []
    if not questions:
        return 0
    lines = [header(hook), t("question_waiting")]
    for qi, q in enumerate(questions, 1):
        prefix = f"*{qi}.* " if len(questions) > 1 else ""
        lines.append(f"{prefix}*{slack_escape(q.get('question', ''))}*"
                     + (t("multi_hint") if q.get("multiSelect") else ""))
        for oi, o in enumerate(q.get("options", []), 1):
            desc = f" — {slack_escape(o['description'])}" if o.get("description") else ""
            lines.append(f"    {oi}) {slack_escape(o.get('label', ''))}{desc}")
    footer = t("ask_footer_multi") if len(questions) > 1 else t("ask_footer")
    key, channel, ts, body = open_question(slack, cfg, state, "ask", "\n".join(lines), footer)
    try:
        answers = wait_for_reply(slack, cfg, state, key, channel, ts, cfg["ask_wait_s"],
                                 make_question_parser(questions))
    except Cancelled:
        return 0
    close_question(state, key)
    if answers is None:
        slack.update(channel, ts, strike(body, t("expired_ask")))
        return 0
    slack.post(channel, t("handed"), thread_ts=ts)
    updated = dict(tool_input)
    updated["answers"] = answers
    json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                      "permissionDecision": "allow",
                                      "permissionDecisionReason": "Answered by the user on Slack.",
                                      "updatedInput": updated}}, sys.stdout)
    return 0


def describe_input(tool_name, tool_input, limit):
    if tool_name == "Bash":
        body = tool_input.get("command", "")
        extra = tool_input.get("description", "")
    elif tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        extra = tool_input.get("file_path", "") or tool_input.get("notebook_path", "")
        rest = {k: v for k, v in tool_input.items() if k not in ("file_path", "notebook_path")}
        body = json.dumps(rest, ensure_ascii=False, indent=1)
    else:
        extra = ""
        body = json.dumps(tool_input, ensure_ascii=False, indent=1)
    body, cut = clip_head(body, limit)
    return extra, body, cut


def _find_tool_use(node, name, tool_input):
    """The id of the last tool_use block for this call in one transcript record."""
    if isinstance(node, dict):
        if node.get("type") == "tool_use" and node.get("name") == name:
            got = node.get("input") or {}
            if got == tool_input or (name == "Bash" and got.get("command")
                                     and got.get("command") == tool_input.get("command")):
                return node.get("id")
        children = list(node.values())
    elif isinstance(node, list):
        children = node
    else:
        return None
    for child in reversed(children):
        found = _find_tool_use(child, name, tool_input)
        if found:
            return found
    return None


def _result_ids(text):
    return set(re.findall(r'"tool_use_id"\s*:\s*"([^"]+)"', text))


class Transcript:
    """Watches the session's transcript for this call's result: a permission answered at the
    terminal's dialog, allowed or denied, shows there as a tool_result with the call's id.

    Measured 2026-10-30: the call's tool_use and its tool_result reach the transcript together,
    after the answer — so the call is looked for in what is written after the hook started, and,
    in case it was written before, among the tail's calls that have no result yet (an earlier
    identical call, already answered, is never taken for this one)."""

    def __init__(self, path, tool_name, tool_input):
        self.path, self.name, self.input = path, tool_name, tool_input
        self.id, self.offset, self.buf, self.found_by = None, 0, "", None
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                f.seek(max(0, size - TRANSCRIPT_TAIL))
                tail = f.read().decode("utf-8", errors="replace")
        except (OSError, TypeError):
            self.path = None
            return
        self.offset = size
        answered_before = _result_ids(tail)
        for line in reversed(tail.splitlines()):
            found = self._match(line)
            if found and found not in answered_before:
                self.id, self.found_by = found, "before"
                return

    def _match(self, line):
        if '"tool_use"' not in line:
            return None
        try:
            return _find_tool_use(json.loads(line), self.name, self.input)
        except ValueError:
            return None

    def answered(self):
        if not self.path:
            return False
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                new = f.read()
        except OSError:
            return False
        self.offset += len(new)
        self.buf += new.decode("utf-8", errors="replace")
        lines = self.buf.split("\n")
        self.buf = lines.pop()                    # a line not written to its end yet
        for line in lines:
            if not self.id:
                found = self._match(line)
                if found:
                    self.id, self.found_by = found, "after"
            if self.id and self.id in _result_ids(line):
                return True
        return False


def cmd_permission(hook, cfg, slack, state):
    tool_name = hook.get("tool_name", "?")
    tool_input = hook.get("tool_input") or {}
    call = call_fingerprint(tool_name, tool_input)
    on_slack = slack_mode(state)
    if not on_slack:
        # The dialog is on the terminal meanwhile (measured): wait out the grace period, and step
        # aside when the user answers it there, types, or the session moves on.
        pending = f"permission-pending:{os.getpid()}:{time.time()}"

        def add(d):
            d["open"][pending] = {"kind": "permission-pending", "call": call, "since": time.time()}
        state.update(add)
        seen = Transcript(hook.get("transcript_path"), tool_name, tool_input)
        deadline = time.time() + cfg["grace_s"]
        while time.time() < deadline:
            if seen.answered():
                log(f"permission: answered at the terminal (seen in the transcript, the call "
                    f"found {seen.found_by} the hook started)")
                close_question(state, pending)
                return 0
            if pending not in state.read()["open"] or not claude_alive():
                close_question(state, pending)
                return 0
            time.sleep(min(cfg["poll_s"], max(deadline - time.time(), 0.01)))
        still = state.update(lambda d: d["open"].pop(pending, None))
        if not still or seen.answered() or session_silenced(state):
            return 0
    extra, body, cut = describe_input(tool_name, tool_input, cfg["input_chars"])
    lines = [header(hook), t("perm_waiting", tool=slack_escape(tool_name))
             + ("" if on_slack else t("perm_open_for", m=minutes(cfg["grace_s"])))]
    if hook.get("mcp_server"):
        lines.append(t("mcp_server", s=slack_escape(str(hook["mcp_server"]))))
    if extra:
        lines.append(slack_escape(extra))
    lines.append(code_block(body))
    if cut:
        lines.append(t("cut", n=cfg["input_chars"]))
    key, channel, ts, msg = open_question(slack, cfg, state, "permission", "\n".join(lines),
                                          t("perm_footer"), call=call)
    try:
        decision = wait_for_reply(slack, cfg, state, key, channel, ts, cfg["permission_wait_s"],
                                  parse_yes_no)
    except Cancelled:
        return 0
    close_question(state, key)
    if decision is None:
        slack.update(channel, ts, strike(msg, t("expired_ask")))
        return 0
    behavior, reason = decision
    slack.post(channel, t("allowed") if behavior == "allow" else t("denied"), thread_ts=ts)
    out = {"behavior": behavior}
    if behavior == "deny":
        out["message"] = "The user denied this on Slack" + (f": {reason}" if reason else ".")
    json.dump({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": out}},
              sys.stdout)
    return 0


def prune_states(max_age_s=14 * 86400):
    cutoff = time.time() - max_age_s
    try:
        for name in os.listdir(STATE_DIR):
            path = os.path.join(STATE_DIR, name)
            if name != "off" and os.path.getmtime(path) < cutoff:
                os.remove(path)
    except OSError:
        pass


def cmd_prompt(hook, cfg, slack, state):
    # Measured 2026-10-30: the session's own notifications (this bridge's wake-up, a background
    # task's end) arrive through UserPromptSubmit too, as "<task-notification>…". They are not
    # the user typing: no terminal mode, nothing closed.
    if (hook.get("prompt") or "").lstrip().startswith("<task-notification>"):
        return 0
    prune_states()
    taken = state.update(take_all(last_prompt=time.time(), mode="terminal"))
    close_all(slack, taken, t("answered_terminal"))
    return 0


def cmd_posttool(hook, cfg, slack, state):
    call = call_fingerprint(hook.get("tool_name", "?"), hook.get("tool_input") or {})

    def take(d):
        keys = [k for k, v in d["open"].items()
                if v["kind"] in ("permission", "permission-pending") and v.get("call") == call]
        return [d["open"].pop(k) for k in keys]
    close_all(slack, state.update(take), t("answered_terminal"))
    return 0


def cmd_sessionend(hook, cfg, slack, state):
    # /clear starts a new session in the same Claude Code process (so CLAUDE_PID still lives):
    # without this, the old session's waiter DMs its last message 10 minutes later, and an answer
    # to it would wake the new session.
    taken = state.update(take_all(ended=True, ended_reason=hook.get("reason", "")))
    close_all(slack, taken, t("session_gone"))
    return 0


def cmd_sessionstart(hook, cfg, slack, state):
    if state.read().get("ended"):          # the same session resumed: live again
        state.set(ended=False)
    return 0


HOOKS = {"stop": cmd_stop, "ask": cmd_ask, "permission": cmd_permission}
BOOKKEEPING = {"prompt": cmd_prompt, "posttool": cmd_posttool,     # they run even when silenced
               "sessionend": cmd_sessionend, "sessionstart": cmd_sessionstart}


# --------------------------------------------------------------------------- by hand

def cmd_switch(name):
    read_language()
    os.makedirs(STATE_DIR, exist_ok=True)
    if name == "off":
        open(OFF_FILE, "a").close()
        print(t("machine_off"))
    elif name == "on":
        if os.path.exists(OFF_FILE):
            os.remove(OFF_FILE)
        print(t("machine_on"))
    else:
        print(t("machine_off") if os.path.exists(OFF_FILE) else t("machine_on"))
        try:
            load_config()
            print(t("config_ok"), CONFIG_PATH)
        except (OSError, ValueError) as e:
            print(t("config_bad"), CONFIG_PATH, "—", e)
        sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
        if sid:
            state = State(sid)
            s = state.read()
            print(t("session_line", sid=sid[:8],
                    state=t("off") if session_silenced(state) else t("on"),
                    mode=t("slack") if s.get("mode") == "slack" else t("terminal"),
                    n=len(s["open"])))
    return 0


def cmd_session(arg, value=None):
    read_language()
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not sid:
        print(t("no_session"))
        return 1
    state = State(sid)
    try:
        slack = Slack(load_config())
    except (OSError, ValueError):
        slack = None
    if arg == "session-off":
        close_all(slack, state.update(take_all(off=True)), t("switched_off"))
        print(t("session_off"))
    elif arg == "session-on":
        state.set(off=False)
        print(t("session_on"))
    elif arg == "mode" and value in ("terminal", "slack"):
        state.set(mode=value)
        print(t("mode_now", mode=t(value)))
    else:
        print(t("mode_usage"))
        return 1
    return 0


def cmd_selftest():
    """Post one DM and wait 2 minutes for a reply — the live check after setup."""
    cfg = load_config()
    slack = Slack(cfg)
    state = State("selftest")
    text = f"*{slack_escape(socket.gethostname())} · selftest*\n{t('selftest_dm')}"
    key, channel, ts, _ = open_question(slack, cfg, state, "selftest", text, t("selftest_footer"))
    print(t("selftest_sent"))
    reply = wait_for_reply(slack, cfg, state, key, channel, ts, 120, parse_free)
    close_question(state, key)
    if reply is None:
        print(t("selftest_none"))
        return 1
    slack.post(channel, t("selftest_thanks"), thread_ts=ts)
    print(t("selftest_reply"), reply)
    return 0


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 0
    cmd = argv[1]
    if cmd in ("on", "off", "status"):
        return cmd_switch(cmd)
    if cmd in ("session-off", "session-on"):
        return cmd_session(cmd)
    if cmd == "mode":
        return cmd_session("mode", argv[2] if len(argv) > 2 else None)
    if cmd == "selftest":
        return cmd_selftest()
    if cmd not in HOOKS and cmd not in BOOKKEEPING:
        print(__doc__)
        return 0
    # A hook: never block the session, never decide on a failure.
    try:
        raw = sys.stdin.read()
        hook = json.loads(raw) if raw.strip() else {}
        state = State(hook.get("session_id", "unknown"))
        if cmd in BOOKKEEPING:
            if cmd == "posttool" and not state.has_open():
                return 0  # every tool call lands here: the common case touches nothing
            if cmd in ("sessionend", "sessionstart") and not os.path.exists(state.path):
                return 0  # a session the bridge never touched
            try:
                cfg = load_config()
                slack = Slack(cfg)
            except (OSError, ValueError):
                read_language()
                cfg, slack = None, None
            return BOOKKEEPING[cmd](hook, cfg, slack, state)
        if os.path.exists(OFF_FILE) or not attended() or session_silenced(state):
            return 0
        cfg = load_config()
        return HOOKS[cmd](hook, cfg, Slack(cfg), state)
    except Exception as e:  # noqa: BLE001 — a hook must fail silent and open
        log(f"{cmd}: {type(e).__name__}: {e}")
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
