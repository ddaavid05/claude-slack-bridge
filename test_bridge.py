#!/usr/bin/env python3
"""Tests for bridge.py against a fake Slack Web API on 127.0.0.1 (no network, no real token).

Run: python3 ~/.claude/slack-bridge/test_bridge.py
Each test runs bridge.py as the hook would (a subprocess, the hook's JSON on stdin) with its own
home, config and state directory, and timings shortened to fractions of a second.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BRIDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.py")
TOKEN = "xox" + "b-not-a-real-token"   # split: no secret scanner takes it for one
USER = "UTESTUSER1"
OTHER = "UOTHERUSER2"


class FakeSlack:
    """chat.postMessage, chat.update and conversations.replies, enough for the bridge."""

    def __init__(self):
        self.lock = threading.Lock()
        self.messages = []   # dicts: channel, ts, text, user, bot_id, thread_ts
        self.calls = []
        self.fail = None     # an error string to return from every call
        self._ts = 1000
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u = urlparse(self.path)
                params = {k: v[0] for k, v in parse_qs(u.query).items()}
                self._send(fake.handle(u.path.rsplit("/", 1)[-1], params,
                                       self.headers.get("Authorization")))

            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                params = json.loads(self.rfile.read(n) or b"{}")
                self._send(fake.handle(self.path.rsplit("/", 1)[-1], params,
                                       self.headers.get("Authorization")))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def next_ts(self):
        self._ts += 1
        return f"{self._ts}.000100"

    def handle(self, method, p, auth):
        with self.lock:
            self.calls.append((method, p))
            if auth != "Bearer " + TOKEN:
                return {"ok": False, "error": "invalid_auth"}
            if self.fail:
                return {"ok": False, "error": self.fail}
            if method == "chat.postMessage":
                channel = "D" + p["channel"][1:] if p["channel"].startswith("U") else p["channel"]
                m = {"channel": channel, "ts": self.next_ts(), "text": p["text"],
                     "bot_id": "BBRIDGE", "thread_ts": p.get("thread_ts")}
                self.messages.append(m)
                return {"ok": True, "channel": channel, "ts": m["ts"]}
            if method == "chat.update":
                for m in self.messages:
                    if m["channel"] == p["channel"] and m["ts"] == p["ts"]:
                        m["text"] = p["text"]
                        return {"ok": True}
                return {"ok": False, "error": "message_not_found"}
            if method == "conversations.replies":
                thread = [m for m in self.messages if m["channel"] == p["channel"]
                          and (m["ts"] == p["ts"] or m.get("thread_ts") == p["ts"])]
                return {"ok": True, "messages": thread}
            return {"ok": False, "error": "unknown_method"}

    # what the tests use
    def parents(self):
        with self.lock:
            return [m for m in self.messages if not m.get("thread_ts")]

    def thread(self, parent):
        with self.lock:
            return [m for m in self.messages if m.get("thread_ts") == parent["ts"]]

    def reply(self, text, user=USER, parent=None):
        with self.lock:
            parent = parent or [m for m in self.messages if not m.get("thread_ts")][-1]
            self.messages.append({"channel": parent["channel"], "ts": self.next_ts(),
                                  "text": text, "user": user, "thread_ts": parent["ts"]})

    def wait_parents(self, n, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if len(self.parents()) >= n:
                return self.parents()
            time.sleep(0.02)
        raise AssertionError(f"expected {n} DM(s), got {len(self.parents())}")


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.slack = FakeSlack()
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        self.config = os.path.join(self.tmp.name, "config.json")
        self.write_config()
        self.env = dict(os.environ, CLAUDE_SLACK_BRIDGE_HOME=self.home,
                        CLAUDE_SLACK_BRIDGE_CONFIG=self.config,
                        CLAUDE_CODE_SESSION_ATTENDED="1", CLAUDE_CODE_ENTRYPOINT="cli")

    def tearDown(self):
        self.slack.server.shutdown()
        self.slack.server.server_close()
        self.tmp.cleanup()

    def write_config(self, **over):
        cfg = {"bot_token": TOKEN, "user_id": USER,
               "api_base": f"http://127.0.0.1:{self.slack.port}",
               "grace_s": 0.3, "stop_budget_s": 3,
               "ask_wait_s": 3, "permission_wait_s": 3, "poll_s": 0.05,
               "chunk_chars": 3900, "input_chars": 40, "http_timeout_s": 2}
        cfg.update(over)
        with open(self.config, "w") as f:
            json.dump(cfg, f)

    def hook(self, cmd, payload, env=None):
        p = subprocess.Popen([sys.executable, "-I", BRIDGE, cmd], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=env or self.env)
        p.stdin.write(json.dumps(payload))
        p.stdin.close()
        return p

    def finish(self, p, timeout=10):
        p.wait(timeout)
        out, err = p.stdout.read(), p.stderr.read()
        p.stdout.close()
        p.stderr.close()
        for text in (out, err, self.log()):
            self.assertNotIn(TOKEN, text)
        return p.returncode, out, err

    def log(self):
        try:
            with open(os.path.join(self.home, "bridge.log")) as f:
                return f.read()
        except OSError:
            return ""

    def stop_payload(self, msg="Done. Which way?\n1) hooks\n2) channels", sid="sess-a"):
        return {"session_id": sid, "cwd": self.tmp.name, "hook_event_name": "Stop",
                "last_assistant_message": msg, "stop_hook_active": False}

    def ask_payload(self, questions, sid="sess-a"):
        return {"session_id": sid, "cwd": self.tmp.name, "hook_event_name": "PreToolUse",
                "tool_name": "AskUserQuestion", "tool_input": {"questions": questions}}

    def perm_payload(self, command, sid="sess-a"):
        return {"session_id": sid, "cwd": self.tmp.name, "hook_event_name": "PermissionRequest",
                "tool_name": "Bash", "tool_input": {"command": command, "description": "list"}}

    def prompt(self, sid="sess-a"):
        rc, _, _ = self.finish(self.hook("prompt", {"session_id": sid, "prompt": "hi"}))
        self.assertEqual(rc, 0)

    # ------------------------------------------------------------------ Stop

    def test_stop_reply_wakes_the_session(self):
        p = self.hook("stop", self.stop_payload())
        parent = self.slack.wait_parents(1)[0]
        self.assertIn("session sess-a", parent["text"])
        self.assertIn("2) channels", parent["text"])
        self.slack.reply("2, but measure it first")
        rc, out, err = self.finish(p)
        self.assertEqual(rc, 2)
        self.assertIn("2, but measure it first", err)
        self.assertEqual(out, "")
        self.assertIn("Handed over", self.slack.thread(parent)[-1]["text"])

    def test_stop_silent_when_typed_during_grace(self):
        self.write_config(grace_s=1.0)
        p = self.hook("stop", self.stop_payload())
        time.sleep(0.3)
        self.prompt()
        rc, out, err = self.finish(p)
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(self.slack.parents(), [])

    def test_stop_cancelled_when_answered_at_terminal(self):
        p = self.hook("stop", self.stop_payload())
        parent = self.slack.wait_parents(1)[0]
        self.prompt()
        rc, _, err = self.finish(p)
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("Answered at the terminal", self.slack.parents()[0]["text"])

    def test_stop_ignores_other_users_and_bots(self):
        p = self.hook("stop", self.stop_payload())
        self.slack.wait_parents(1)
        self.slack.reply("yes", user=OTHER)
        time.sleep(0.3)
        self.assertIsNone(p.poll())
        self.slack.reply("the real answer")
        rc, _, err = self.finish(p)
        self.assertEqual(rc, 2)
        self.assertIn("the real answer", err)
        self.assertNotIn("yes", err.split("\n", 1)[1])

    def test_stop_times_out(self):
        self.write_config(stop_budget_s=0.8)
        p = self.hook("stop", self.stop_payload())
        self.slack.wait_parents(1)
        rc, _, err = self.finish(p)
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("Expired", self.slack.parents()[0]["text"])

    def test_newer_stop_supersedes_older(self):
        p1 = self.hook("stop", self.stop_payload("first"))
        self.slack.wait_parents(1)
        p2 = self.hook("stop", self.stop_payload("second"))
        rc1, _, _ = self.finish(p1)
        self.assertEqual(rc1, 0)
        self.assertIn("Outdated", self.slack.parents()[0]["text"])
        self.slack.wait_parents(2)
        self.slack.reply("the answer to the second")
        rc2, _, err2 = self.finish(p2)
        self.assertEqual(rc2, 2)
        self.assertIn("the answer to the second", err2)

    def test_two_sessions_get_their_own_answers(self):
        pa = self.hook("stop", self.stop_payload("A?", sid="sess-a"))
        pb = self.hook("stop", self.stop_payload("B?", sid="sess-b"))
        parents = self.slack.wait_parents(2)
        by_sid = {("sess-a" if "sess-a" in m["text"] else "sess-b"): m for m in parents}
        self.slack.reply("for B", parent=by_sid["sess-b"])
        self.slack.reply("for A", parent=by_sid["sess-a"])
        _, _, err_a = self.finish(pa)
        _, _, err_b = self.finish(pb)
        self.assertIn("for A", err_a)
        self.assertNotIn("for B", err_a)
        self.assertIn("for B", err_b)

    def test_whole_message_goes_out_as_slack_text(self):
        msg = ("## Plan\n**Goal:** the whole message.\n- first\n\n| A | B |\n|---|---|\n| x | y |\n\n"
               + "long text " * 30 + "\nTHE QUESTION?")
        p = self.hook("stop", self.stop_payload(msg))
        parent = self.slack.wait_parents(1)[0]
        text = parent["text"]
        self.assertIn("*Plan*", text)
        self.assertIn("*Goal:* the whole message.", text)
        self.assertIn("• first", text)
        self.assertIn("```\nA  B", text)
        self.assertIn("long text " * 30, text)
        self.assertIn("THE QUESTION?", text)
        self.assertNotIn("**", text)
        self.slack.reply("ok")
        self.assertEqual(self.finish(p)[0], 2)

    def test_long_message_in_parts_answer_on_the_last(self):
        self.write_config(chunk_chars=400)
        msg = "\n".join(f"line {i}: " + "x" * 60 for i in range(30)) + "\nTHE END?"
        p = self.hook("stop", self.stop_payload(msg))
        end = time.time() + 5
        while time.time() < end and not any("THE END?" in m["text"] for m in self.slack.parents()):
            time.sleep(0.05)
        parents = self.slack.parents()
        self.assertGreater(len(parents), 2)
        self.assertTrue(all(len(m["text"]) <= 400 for m in parents))
        self.assertIn(f"({len(parents)}/{len(parents)})", parents[-1]["text"])
        self.assertIn("Reply in this thread", parents[-1]["text"])
        self.assertNotIn("Reply in this thread", parents[0]["text"])
        joined = "\n".join(m["text"] for m in parents)
        for i in range(30):
            self.assertIn(f"line {i}: ", joined)
        self.slack.reply("the answer on the last", parent=parents[-1])
        rc, _, err = self.finish(p)
        self.assertEqual(rc, 2)
        self.assertIn("the answer on the last", err)

    def test_slack_entities_unescaped_for_claude(self):
        p = self.hook("stop", self.stop_payload())
        self.slack.wait_parents(1)
        self.slack.reply("a &lt; b &amp;&amp; c &gt; d")
        _, _, err = self.finish(p)
        self.assertIn("a < b && c > d", err)

    # ------------------------------------------------------------------ the gates

    def test_off_switch_silences_hooks(self):
        r = subprocess.run([sys.executable, "-I", BRIDGE, "off"], env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        rc, out, err = self.finish(self.hook("stop", self.stop_payload()))
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(self.slack.calls, [])

    def test_headless_session_is_silent(self):
        env = dict(self.env, CLAUDE_CODE_SESSION_ATTENDED="0")
        rc, out, err = self.finish(self.hook("stop", self.stop_payload(), env=env))
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(self.slack.calls, [])

    def test_slack_failure_is_silent_and_logged(self):
        self.slack.fail = "channel_not_found"
        rc, out, err = self.finish(self.hook("stop", self.stop_payload()))
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertIn("channel_not_found", self.log())

    def test_slack_down_is_silent(self):
        self.write_config(api_base="http://127.0.0.1:9")
        rc, out, err = self.finish(self.hook("ask", self.ask_payload(
            [{"question": "Q?", "header": "Q", "options": [{"label": "a"}, {"label": "b"}],
              "multiSelect": False}])))
        self.assertEqual((rc, out, err), (0, "", ""))

    def test_missing_config_is_silent(self):
        os.remove(self.config)
        rc, out, err = self.finish(self.hook("permission", self.perm_payload("ls")))
        self.assertEqual((rc, out, err), (0, "", ""))

    def test_placeholder_config_is_silent(self):
        self.write_config(bot_token="xoxb-IDE-JON-A-BOT-TOKENJE")
        rc, out, err = self.finish(self.hook("stop", self.stop_payload()))
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(self.slack.calls, [])

    # ------------------------------------------------------------------ AskUserQuestion

    Q1 = {"question": "Which way shall we go?", "header": "Way",
          "options": [{"label": "Hooks", "description": "no MCP"},
                      {"label": "Channels", "description": "preview"}],
          "multiSelect": False}
    Q2 = {"question": "What shall we measure?", "header": "Measure",
          "options": [{"label": "timeout"}, {"label": "asyncRewake"}, {"label": "headless"}],
          "multiSelect": True}

    def answers_of(self, out):
        data = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(data["hookEventName"], "PreToolUse")
        self.assertEqual(data["permissionDecision"], "allow")
        self.assertEqual(data["updatedInput"]["questions"][0]["question"],
                         data["updatedInput"]["questions"][0]["question"])
        return data["updatedInput"]["answers"]

    def test_ask_number_picks_the_option(self):
        p = self.hook("ask", self.ask_payload([self.Q1]))
        parent = self.slack.wait_parents(1)[0]
        self.assertIn("2) Channels — preview", parent["text"])
        self.slack.reply("2")
        rc, out, _ = self.finish(p)
        self.assertEqual(rc, 0)
        self.assertEqual(self.answers_of(out), {"Which way shall we go?": "Channels"})

    def test_ask_free_text(self):
        p = self.hook("ask", self.ask_payload([self.Q1]))
        self.slack.wait_parents(1)
        self.slack.reply("neither, let us talk first")
        _, out, _ = self.finish(p)
        self.assertEqual(self.answers_of(out),
                         {"Which way shall we go?": "neither, let us talk first"})

    def test_ask_two_questions_multiselect(self):
        p = self.hook("ask", self.ask_payload([self.Q1, self.Q2]))
        self.slack.wait_parents(1)
        self.slack.reply("1")                # one line for two questions: refused
        time.sleep(0.3)
        self.slack.reply("1. 1\n2. 1, 3")
        _, out, _ = self.finish(p)
        self.assertEqual(self.answers_of(out), {"Which way shall we go?": "Hooks",
                                                "What shall we measure?": "timeout, headless"})
        complaints = [m["text"] for m in self.slack.thread(self.slack.parents()[0])]
        self.assertTrue(any("one answer per line" in t for t in complaints))

    def test_ask_out_of_range_number_is_refused(self):
        p = self.hook("ask", self.ask_payload([self.Q1]))
        self.slack.wait_parents(1)
        self.slack.reply("7")
        time.sleep(0.3)
        self.assertIsNone(p.poll())
        self.slack.reply("1")
        _, out, _ = self.finish(p)
        self.assertEqual(self.answers_of(out), {"Which way shall we go?": "Hooks"})

    def test_ask_multiple_numbers_on_single_select_refused(self):
        p = self.hook("ask", self.ask_payload([self.Q1]))
        self.slack.wait_parents(1)
        self.slack.reply("1,2")
        time.sleep(0.3)
        self.assertIsNone(p.poll())
        self.slack.reply("2")
        _, out, _ = self.finish(p)
        self.assertEqual(self.answers_of(out), {"Which way shall we go?": "Channels"})

    def test_ask_skipped_when_user_present(self):
        self.write_config(grace_s=30)
        self.prompt()
        rc, out, err = self.finish(self.hook("ask", self.ask_payload([self.Q1])))
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(self.slack.parents(), [])

    def test_ask_timeout_gives_the_dialog_back(self):
        self.write_config(ask_wait_s=0.5)
        rc, out, err = self.finish(self.hook("ask", self.ask_payload([self.Q1])))
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertIn("Expired", self.slack.parents()[0]["text"])

    # ------------------------------------------------------------------ PermissionRequest

    def decision_of(self, out):
        data = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(data["hookEventName"], "PermissionRequest")
        return data["decision"]

    def test_permission_yes_allows_once(self):
        p = self.hook("permission", self.perm_payload("ls -la /ws"))
        parent = self.slack.wait_parents(1)[0]
        self.assertIn("Waiting for permission: *Bash*", parent["text"])
        self.assertIn("ls -la /ws", parent["text"])
        self.slack.reply("Yes")
        _, out, _ = self.finish(p)
        self.assertEqual(self.decision_of(out), {"behavior": "allow"})
        self.assertNotIn("updatedPermissions", out)

    def test_permission_no_with_reason(self):
        p = self.hook("permission", self.perm_payload("rm -rf build"))
        self.slack.wait_parents(1)
        self.slack.reply("no, look at what is in it first")
        _, out, _ = self.finish(p)
        d = self.decision_of(out)
        self.assertEqual(d["behavior"], "deny")
        self.assertIn("look at what is in it first", d["message"])

    def test_permission_unclear_reply_is_asked_again(self):
        p = self.hook("permission", self.perm_payload("ls"))
        self.slack.wait_parents(1)
        self.slack.reply("maybe")
        time.sleep(0.3)
        self.assertIsNone(p.poll())
        self.slack.reply("sure")
        _, out, _ = self.finish(p)
        self.assertEqual(self.decision_of(out), {"behavior": "allow"})

    def test_permission_long_input_marked_cut(self):
        p = self.hook("permission", self.perm_payload("echo " + "y" * 200))
        parent = self.slack.wait_parents(1)[0]
        self.assertIn("CUT:", parent["text"])
        self.slack.reply("no")
        self.finish(p)

    def test_permission_code_fence_in_command_cannot_break_out(self):
        p = self.hook("permission", self.perm_payload("echo ```x``` <b>"))
        parent = self.slack.wait_parents(1)[0]
        body = parent["text"].split("```\n", 1)[1].rsplit("\n```", 1)[0]
        self.assertNotIn("```", body)
        self.assertIn("&lt;b&gt;", body)
        self.slack.reply("no")
        self.finish(p)

    def test_permission_timeout_gives_the_dialog_back(self):
        self.write_config(permission_wait_s=0.5)
        rc, out, err = self.finish(self.hook("permission", self.perm_payload("ls")))
        self.assertEqual((rc, out, err), (0, "", ""))

    def test_permission_waits_the_grace_period_first(self):
        self.write_config(grace_s=1.0)
        t0 = time.time()
        p = self.hook("permission", self.perm_payload("ls"))
        parent = self.slack.wait_parents(1)
        self.assertGreater(time.time() - t0, 0.9)
        self.assertIn("open in the terminal for", parent[0]["text"])
        self.slack.reply("yes")
        _, out, _ = self.finish(p)
        self.assertEqual(self.decision_of(out), {"behavior": "allow"})

    def test_permission_answered_at_the_terminal_in_the_grace_period(self):
        self.write_config(grace_s=1.5)
        p = self.hook("permission", self.perm_payload("touch /tmp/z"))
        time.sleep(0.4)
        same = dict(self.perm_payload("touch /tmp/z"), hook_event_name="PostToolUse")
        self.finish(self.hook("posttool", same))
        rc, out, err = self.finish(p)
        self.assertEqual((rc, out, err), (0, "", ""))
        time.sleep(1.3)
        self.assertEqual(self.slack.parents(), [])

    def test_permission_denied_at_the_terminal_seen_in_the_transcript(self):
        self.write_config(grace_s=2.0)
        transcript = os.path.join(self.tmp.name, "t.jsonl")
        cmd = "rm -rf /tmp/zz"
        with open(transcript, "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "toolu_ABC", "name": "Bash",
                 "input": {"command": cmd, "description": "list"}}]}}) + "\n")
        payload = dict(self.perm_payload(cmd), transcript_path=transcript)
        p = self.hook("permission", payload)
        time.sleep(0.4)
        with open(transcript, "a") as f:
            f.write(json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "toolu_ABC", "is_error": True,
                 "content": "The user doesn't want to proceed"}]}}) + "\n")
        rc, out, err = self.finish(p)
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(self.slack.parents(), [])

    def tool_use(self, tid, cmd):
        return json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tid, "name": "Bash",
             "input": {"command": cmd, "description": "list"}}]}}) + "\n"

    def tool_result(self, tid):
        return json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "content": "done"}]}}) + "\n"

    def test_permission_call_written_after_the_hook_started(self):
        # As measured: the tool_use and its tool_result reach the transcript together, later.
        self.write_config(grace_s=2.0)
        transcript = os.path.join(self.tmp.name, "t2.jsonl")
        cmd = "curl http://127.0.0.1:9/x"
        open(transcript, "w").close()
        p = self.hook("permission", dict(self.perm_payload(cmd), transcript_path=transcript))
        time.sleep(0.4)
        with open(transcript, "a") as f:
            f.write(self.tool_use("toolu_NEW", cmd) + self.tool_result("toolu_NEW"))
        rc, out, err = self.finish(p)
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(self.slack.parents(), [])
        self.assertIn("found after the hook started", self.log())

    def test_an_earlier_identical_call_is_not_taken_for_this_one(self):
        self.write_config(grace_s=0.8)
        transcript = os.path.join(self.tmp.name, "t3.jsonl")
        cmd = "touch /tmp/same"
        with open(transcript, "w") as f:
            f.write(self.tool_use("toolu_OLD", cmd) + self.tool_result("toolu_OLD"))
        p = self.hook("permission", dict(self.perm_payload(cmd), transcript_path=transcript))
        self.slack.wait_parents(1)
        with open(transcript, "a") as f:        # the old call replayed: still not this one
            f.write(self.tool_result("toolu_OLD"))
        self.slack.reply("yes")
        self.assertEqual(self.decision_of(self.finish(p)[1]), {"behavior": "allow"})

    def test_a_task_notification_is_not_typing(self):
        self.cli("mode", "slack")
        before = self.state_of().get("last_prompt")
        payload = {"session_id": "sess-a", "prompt": "<task-notification>\n<summary>Stop hook "
                   "feedback</summary>\n</task-notification>\nThe user's answer from Slack: 2"}
        self.assertEqual(self.finish(self.hook("prompt", payload))[0], 0)
        self.assertEqual(self.state_of()["mode"], "slack")
        self.assertEqual(self.state_of().get("last_prompt"), before)

    def test_permission_skips_the_grace_period_in_slack_mode(self):
        self.write_config(grace_s=30)
        self.cli("mode", "slack")
        p = self.hook("permission", self.perm_payload("ls"))
        parent = self.slack.wait_parents(1, timeout=3)[0]
        self.assertNotIn("open in the terminal", parent["text"])
        self.slack.reply("no")
        self.assertEqual(self.decision_of(self.finish(p)[1])["behavior"], "deny")

    # ------------------------------------------------------------------ language

    def test_messages_in_hungarian_when_configured(self):
        self.write_config(language="hu")
        p = self.hook("stop", self.stop_payload())
        parent = self.slack.wait_parents(1)[0]
        self.assertIn("Rád vár", parent["text"])
        self.assertIn("Válaszolj ebben a threadben", parent["text"])
        self.slack.reply("2")
        _, _, err = self.finish(p)
        self.assertIn("The user's answer from Slack", err)      # what Claude reads: English
        self.assertIn("Átadva a sessionnek", self.slack.thread(parent)[-1]["text"])

    def test_yes_and_no_in_either_language(self):
        for word, behavior in (("igen", "allow"), ("nem, mert", "deny"), ("OK", "allow")):
            p = self.hook("permission", self.perm_payload(f"echo {word}"))
            parent = self.slack.wait_parents(len(self.slack.parents()) + 1)[-1]
            self.slack.reply(word, parent=parent)
            self.assertEqual(self.decision_of(self.finish(p)[1])["behavior"], behavior)

    # ------------------------------------------------------------------ modes and switches

    def cli(self, *args, env=None):
        env = env or dict(self.env, CLAUDE_CODE_SESSION_ID="sess-a")
        return subprocess.run([sys.executable, "-I", BRIDGE, *args], env=env,
                              capture_output=True, text=True, timeout=10)

    def state_of(self, sid="sess-a"):
        with open(os.path.join(self.home, "state", sid + ".json")) as f:
            return json.load(f)

    def test_a_slack_reply_puts_the_session_in_slack_mode(self):
        p = self.hook("stop", self.stop_payload())
        self.slack.wait_parents(1)
        self.slack.reply("2")
        self.assertEqual(self.finish(p)[0], 2)
        self.assertEqual(self.state_of()["mode"], "slack")
        self.write_config(grace_s=30)                 # Slack mode: no grace period
        p2 = self.hook("stop", self.stop_payload("Next question?"))
        second = self.slack.wait_parents(2, timeout=3)[1]
        self.assertIn("Slack mode", second["text"])
        self.assertIn("Going back to the computer", second["text"])
        self.slack.reply("ok", parent=second)
        self.assertEqual(self.finish(p2)[0], 2)

    def test_typing_at_the_terminal_returns_to_terminal_mode(self):
        self.cli("mode", "slack")
        self.prompt()
        self.assertEqual(self.state_of()["mode"], "terminal")
        self.write_config(grace_s=30)
        p = self.hook("stop", self.stop_payload())
        time.sleep(1.0)
        self.assertEqual(self.slack.parents(), [])
        self.prompt()
        self.assertEqual(self.finish(p)[0], 0)

    def test_mode_terminal_from_the_cli(self):
        r = self.cli("mode", "slack")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.state_of()["mode"], "slack")
        r = self.cli("mode", "terminal")
        self.assertIn("terminal", r.stdout)
        self.assertEqual(self.state_of()["mode"], "terminal")
        self.assertEqual(self.cli("mode", "nonsense").returncode, 1)

    def test_session_off_silences_this_session_only_and_closes_its_questions(self):
        p = self.hook("stop", self.stop_payload())
        self.slack.wait_parents(1)
        r = self.cli("session-off")
        self.assertIn("OFF for this session", r.stdout)
        self.assertEqual(self.finish(p)[:2], (0, ""))
        self.assertIn("Switched off for this session", self.slack.parents()[0]["text"])
        rc, out, err = self.finish(self.hook("stop", self.stop_payload()))
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(len(self.slack.parents()), 1)
        other = self.hook("stop", self.stop_payload("another session", sid="sess-b"))
        self.slack.wait_parents(2)                      # another session still speaks
        self.slack.reply("ok")
        self.assertEqual(self.finish(other)[0], 2)
        self.cli("session-on")
        p3 = self.hook("stop", self.stop_payload())
        self.slack.wait_parents(3)
        self.slack.reply("ok")
        self.assertEqual(self.finish(p3)[0], 2)

    def test_the_env_switch_silences_a_session(self):
        env = dict(self.env, CLAUDE_SLACK_BRIDGE="off")
        rc, out, err = self.finish(self.hook("stop", self.stop_payload(), env=env))
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(self.slack.calls, [])

    def test_session_commands_need_the_session_id(self):
        env = {k: v for k, v in self.env.items() if k != "CLAUDE_CODE_SESSION_ID"}
        r = self.cli("session-off", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("CLAUDE_CODE_SESSION_ID", r.stdout)

    def test_status_names_this_session(self):
        self.cli("mode", "slack")
        r = self.cli("status")
        self.assertIn("This session (sess-a", r.stdout)
        self.assertIn("Slack mode", r.stdout)

    def test_a_busy_session_sends_nothing(self):
        payload = dict(self.stop_payload(), background_tasks=[{"id": "b1", "status": "running"}])
        rc, out, err = self.finish(self.hook("stop", payload))
        self.assertEqual((rc, out, err), (0, "", ""))
        time.sleep(0.5)
        self.assertEqual(self.slack.parents(), [])
        done = dict(self.stop_payload(), background_tasks=[{"id": "b1", "status": "completed"}])
        p = self.hook("stop", done)
        self.slack.wait_parents(1)
        self.slack.reply("ok")
        self.assertEqual(self.finish(p)[0], 2)

    def test_ask_goes_to_slack_at_once_in_slack_mode(self):
        self.write_config(grace_s=30)
        self.prompt()
        self.cli("mode", "slack")
        p = self.hook("ask", self.ask_payload([self.Q1]))
        self.slack.wait_parents(1, timeout=3)
        self.slack.reply("1")
        _, out, _ = self.finish(p)
        self.assertEqual(self.answers_of(out), {"Which way shall we go?": "Hooks"})

    def test_posttool_closes_a_permission_answered_at_the_terminal(self):
        p = self.hook("permission", self.perm_payload("touch /tmp/x"))
        self.slack.wait_parents(1)
        other = dict(self.perm_payload("touch /tmp/other"), hook_event_name="PostToolUse")
        self.finish(self.hook("posttool", other))       # a different call: left open
        time.sleep(0.2)
        self.assertIsNone(p.poll())
        same = dict(self.perm_payload("touch /tmp/x"), hook_event_name="PostToolUse")
        rc, out, err = self.finish(self.hook("posttool", same))
        self.assertEqual((rc, out, err), (0, "", ""))
        rc, out, err = self.finish(p)
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertIn("Answered at the terminal", self.slack.parents()[0]["text"])

    def test_posttool_without_open_questions_touches_nothing(self):
        payload = dict(self.perm_payload("ls"), hook_event_name="PostToolUse",
                       session_id="never-seen")
        rc, out, err = self.finish(self.hook("posttool", payload))
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(self.slack.calls, [])
        state_dir = os.path.join(self.home, "state")
        self.assertFalse(os.path.exists(os.path.join(state_dir, "never-seen.json")))

    def test_stop_closes_questions_left_open(self):
        p = self.hook("permission", self.perm_payload("touch /tmp/y"))
        self.slack.wait_parents(1)
        s = self.hook("stop", self.stop_payload())
        rc, out, _ = self.finish(p)
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("the session moved on", self.slack.parents()[0]["text"])
        self.slack.wait_parents(2)
        self.slack.reply("ok")
        self.assertEqual(self.finish(s)[0], 2)

    def dead_pid(self):
        p = subprocess.Popen(["true"])
        p.wait()
        return str(p.pid)

    def test_waiter_stops_when_the_session_is_gone(self):
        self.write_config(grace_s=0.1)
        env = dict(self.env)
        keeper = subprocess.Popen(["sleep", "30"])   # stands in for the claude process
        env["CLAUDE_PID"] = str(keeper.pid)
        p = self.hook("stop", self.stop_payload(), env=env)
        self.slack.wait_parents(1)
        keeper.kill()
        keeper.wait()
        rc, out, err = self.finish(p)
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertIn("The session has ended", self.slack.parents()[0]["text"])

    def test_no_dm_from_a_session_already_gone(self):
        env = dict(self.env, CLAUDE_PID=self.dead_pid())
        rc, out, err = self.finish(self.hook("stop", self.stop_payload(), env=env))
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(self.slack.parents(), [])

    # ------------------------------------------------------------------ prompt

    def test_prompt_without_config_still_stops_waiters(self):
        p = self.hook("stop", self.stop_payload())
        self.slack.wait_parents(1)
        os.remove(self.config)
        self.prompt()
        rc, _, err = self.finish(p)
        self.assertEqual((rc, err), (0, ""))


class ConvertTest(unittest.TestCase):
    """The Markdown -> Slack conversion and the splitting, in-process."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.dirname(BRIDGE))
        import bridge
        cls.b = bridge

    def test_marks(self):
        out = self.b.to_mrkdwn("# Title\n**bold** and *italic* and `code **not**`\n- point\n"
                               "[link](https://example.com) a < b")
        self.assertEqual(out.split("\n"), [
            "*Title*", "*bold* and _italic_ and `code **not**`", "• point",
            "<https://example.com|link> a &lt; b"])

    def test_code_and_table(self):
        out = self.b.to_mrkdwn("```py\nx = a<b\n```\n| a | bb |\n|--|--|\n| ccc | d |")
        self.assertEqual(out.split("\n"), [
            "```", "x = a&lt;b", "```", "```", "a    bb", "---  --", "ccc  d", "```"])

    def test_chunks_keep_code_blocks_closed(self):
        text = "start\n```\n" + "\n".join("y" * 50 for _ in range(40)) + "\n```\nend"
        parts = self.b.chunks(text, 500)
        self.assertGreater(len(parts), 3)
        for part in parts:
            self.assertLessEqual(len(part), 500)
            self.assertEqual(sum(ln.strip() == "```" for ln in part.split("\n")) % 2, 0)
        self.assertTrue(parts[0].startswith("start"))
        self.assertTrue(parts[-1].endswith("end"))

    def test_a_short_text_is_one_part(self):
        self.assertEqual(self.b.chunks("one line", 3900), ["one line"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
