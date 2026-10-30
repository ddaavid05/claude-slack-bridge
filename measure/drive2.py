#!/usr/bin/env python3
"""Measurement driver for the 2026-10-30 changes (modes, the unified grace period, the permission
answered at the terminal, a busy session), on a throwaway Claude Code session against the fake
Slack. Reuses drive.py's terminal, setup and helpers.

  python3 -I drive2.py      # results in measure/results/interactive2.json
"""
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import drive  # noqa: E402
from drive import RESULTS, WORK, FakeSlack, Term, child_env, dumps, save, setup  # noqa: E402

PROCEED = r"Do you want to proceed|Do you want to make this edit|Do you want to"


def find_dm(slack, start, needle, timeout):
    end = time.time() + timeout
    while time.time() < end:
        for m in slack.parents()[start:]:
            if needle in m["text"]:
                return m
        time.sleep(0.3)
    return None


def state(sid):
    p = os.path.join(HERE, "bridge-home", "state", sid + ".json")
    try:
        with open(p) as f:
            return json.load(f)
    except OSError:
        return {}


def bridge_log():
    p = os.path.join(HERE, "bridge-home", "bridge.log")
    return open(p).read() if os.path.exists(p) else ""


def approve_dialog_if_shown(term, since, timeout=15):
    if term.wait_text(PROCEED, since=since, timeout=timeout):
        time.sleep(1.0)
        os.write(term.master, b"\r")
        return True
    return False


def main():
    slack = FakeSlack()
    settings = setup(slack, False)
    env = dict(child_env(),
               CLAUDE_SLACK_BRIDGE_CONFIG=os.path.join(HERE, "config.json"),
               CLAUDE_SLACK_BRIDGE_HOME=os.path.join(HERE, "bridge-home"))
    res = {}
    term = Term(["claude", "--settings", settings, "--model", "haiku",
                 "--permission-mode", "default"], WORK, env,
                os.path.join(RESULTS, "screen2.log"))
    if term.wait_text(r"trust this folder|Quick safety check", timeout=40):
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
        save("interactive2", res)
        return

    # A. Terminal mode, then a Slack reply: Slack mode, the next DM at once; "back at the
    #    computer" on Slack: Claude switches the session back to terminal mode.
    m = term.mark()
    term.submit("Reply with exactly the word ALPHA and nothing else.")
    res["A_alpha"] = term.wait_text(r"ALPHA", since=m, timeout=90)
    t_alpha = time.time()
    first = find_dm(slack, 0, "ALPHA", 60)
    res["A_first_dm_after_s"] = round(time.time() - t_alpha, 1) if first else None
    sid = (dumps("Stop") or [{}])[0].get("payload", {}).get("session_id")
    res["session_id"] = sid
    if first:
        n = len(slack.parents())
        m = term.mark()
        slack.reply("Reply with exactly the word BRAVO and nothing else.", parent=first)
        res["A_bravo"] = term.wait_text(r"BRAVO", since=m, timeout=90)
        t_bravo = time.time()
        second = find_dm(slack, n, "BRAVO", 30)
        res["A_second_dm_after_s"] = round(time.time() - t_bravo, 1) if second else None
        res["A_second_dm_slack_mode"] = bool(second and "Slack mode" in second["text"])
        res["A_mode_after_reply"] = state(sid).get("mode") if sid else None
        if second:
            n = len(slack.parents())
            slack.reply("OK, I'm going back to the computer.", parent=second)
            # Claude should run `bridge.py mode terminal`; in default permission mode that asks,
            # and in Slack mode the permission DM comes at once: approve it on Slack.
            perm = find_dm(slack, n, "Waiting for permission", 90)
            res["A_back_permission_dm"] = perm["text"][:600] if perm else None
            if perm:
                slack.reply("igen", parent=perm)
            end = time.time() + 60
            while time.time() < end and state(sid).get("mode") != "terminal":
                time.sleep(1)
            res["A_mode_after_back"] = state(sid).get("mode")
    time.sleep(8)

    # G. A permission answered (allowed) at the terminal within the grace period: no DM.
    g_file = os.path.join(WORK, "g_file")
    n = len(slack.parents())
    m = term.mark()
    term.submit(f"Run exactly this bash command and nothing else: touch {g_file}")
    shown = term.wait_text(PROCEED, since=m, timeout=60)
    res["G_dialog_shown"] = shown
    if shown:
        time.sleep(1.0)
        os.write(term.master, b"\r")
        end = time.time() + 20
        while time.time() < end and not os.path.exists(g_file):
            time.sleep(0.5)
        res["G_ran"] = os.path.exists(g_file)
        time.sleep(9)
        res["G_permission_dm"] = bool(find_dm(slack, n, "g_file", 1))

    # H. A permission denied at the terminal within the grace period: no DM, seen in the
    #    transcript.
    h_file = os.path.join(WORK, "h_file")
    n = len(slack.parents())
    log_before = len(bridge_log())
    m = term.mark()
    term.submit(f"Run exactly this bash command and nothing else: touch {h_file}")
    shown = term.wait_text(PROCEED, since=m, timeout=60)
    res["H_dialog_shown"] = shown
    if shown:
        time.sleep(1.0)
        for _ in range(3):
            os.write(term.master, b"\x1b[B")
            time.sleep(0.3)
        os.write(term.master, b"\r")
        time.sleep(10)
        res["H_ran"] = os.path.exists(h_file)
        res["H_permission_dm"] = bool(find_dm(slack, n, "h_file", 1)
                                      and "Waiting for permission" in find_dm(slack, n, "h_file", 1)["text"])
        res["H_seen_in_transcript"] = "seen in the transcript" in bridge_log()[log_before:]
    time.sleep(8)

    # K. A busy session: background work running, the turn's end sends nothing; once the work
    #    ends and Claude finishes, the DM comes.
    n = len(slack.parents())
    stops_before = len(dumps("Stop"))
    m = term.mark()
    term.submit("Use the Bash tool with run_in_background set to true to run exactly: sleep 25 . "
                "Do not wait for it. Then reply with exactly the word BUSY and end your turn. "
                "When it finishes later, reply with exactly the word FINISHED.")
    approve_dialog_if_shown(term, m, timeout=20)
    res["K_busy"] = term.wait_text(r"BUSY", since=m, timeout=90)
    t_busy = time.time()
    time.sleep(10)
    res["K_dm_while_busy"] = bool(find_dm(slack, n, "BUSY", 1))
    new_stops = dumps("Stop")[stops_before:]
    res["K_background_tasks_seen"] = [s["payload"].get("background_tasks") for s in new_stops][:3]
    res["K_finished"] = term.wait_text(r"FINISHED", since=m, timeout=90)
    after = find_dm(slack, n, "FINISHED", 30)
    res["K_dm_after_done_s"] = round(time.time() - t_busy, 1) if after else None

    term.send("/exit", delay=0.05)
    time.sleep(0.5)
    os.write(term.master, b"\r")
    try:
        term.proc.wait(20)
    except Exception:
        term.proc.terminate()
    res["log_tail"] = bridge_log()[-1500:]
    save("interactive2", res)


if __name__ == "__main__":
    main()
