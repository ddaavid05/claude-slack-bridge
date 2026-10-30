#!/usr/bin/env python3
"""Focused measurement after the 2026-10-30 fixes: a permission allowed and one denied at the
terminal within the grace period (no DM), then Slack mode (the next DM at once after a Slack
reply, a task notification no longer counting as typing).

  python3 -I drive3.py      # results in measure/results/interactive3.json
"""
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from drive import RESULTS, WORK, FakeSlack, Term, child_env, dumps, save, setup  # noqa: E402
from drive2 import PROCEED, bridge_log, find_dm, state  # noqa: E402


def main():
    slack = FakeSlack()
    settings = setup(slack, False)
    env = dict(child_env(),
               CLAUDE_SLACK_BRIDGE_CONFIG=os.path.join(HERE, "config.json"),
               CLAUDE_SLACK_BRIDGE_HOME=os.path.join(HERE, "bridge-home"))
    res = {}
    term = Term(["claude", "--settings", settings, "--model", "haiku",
                 "--permission-mode", "default"], WORK, env,
                os.path.join(RESULTS, "screen3.log"))
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
        save("interactive3", res)
        return

    def permission_at_terminal(tag, keys):
        n = len(slack.parents())
        log_before = len(bridge_log())
        m = term.mark()
        cmd = f"curl -s --max-time 1 http://127.0.0.1:9/{tag}"
        term.submit(f"Run exactly this bash command and nothing else, even if it fails: {cmd}")
        shown = term.wait_text(PROCEED, since=m, timeout=60)
        out = {"dialog_shown": shown}
        if shown:
            time.sleep(1.0)
            for k in keys:
                os.write(term.master, k)
                time.sleep(0.3)
            time.sleep(9)                              # past the 5 s grace period
            dm = find_dm(slack, n, f"/{tag}", 1)
            out["permission_dm"] = bool(dm and "Waiting for permission" in dm["text"])
            out["log"] = bridge_log()[log_before:][-400:]
        time.sleep(8)                                  # let the turn end and its Stop settle
        return out

    # G: allowed at the terminal (Enter on "1. Yes"); H: denied ("4. No").
    res["G"] = permission_at_terminal("g", [b"\r"])
    res["H"] = permission_at_terminal("h", [b"\x1b[B", b"\x1b[B", b"\x1b[B", b"\r"])

    # A: a Slack reply puts the session in Slack mode; the woken turn's DM comes at once.
    n = len(slack.parents())
    m = term.mark()
    term.submit("Reply with exactly the word ALPHA and nothing else.")
    res["A_alpha"] = term.wait_text(r"ALPHA", since=m, timeout=90)
    first = find_dm(slack, n, "ALPHA", 60)
    sid = (dumps("Stop") or [{}])[0].get("payload", {}).get("session_id")
    if first:
        n = len(slack.parents())
        m = term.mark()
        slack.reply("Reply with exactly the word BRAVO and nothing else.", parent=first)
        res["A_bravo"] = term.wait_text(r"BRAVO", since=m, timeout=90)
        t_bravo = time.time()
        second = find_dm(slack, n, "BRAVO", 30)
        res["A_second_dm_after_s"] = round(time.time() - t_bravo, 1) if second else None
        res["A_second_dm_slack_mode"] = bool(second and "Slack mode" in second["text"])
        res["A_mode"] = state(sid).get("mode") if sid else None
        if second:
            slack.reply("Reply with exactly the word CHARLIE and nothing else.", parent=second)
            res["A_charlie"] = term.wait_text(r"CHARLIE", since=m, timeout=90)
            third = find_dm(slack, n + 1, "CHARLIE", 30)
            res["A_third_dm_at_once"] = bool(third)
    # Typing at the terminal: back to terminal mode.
    term.submit("Reply with exactly the word DELTA and nothing else.")
    time.sleep(3)
    res["mode_after_typing"] = state(sid).get("mode") if sid else None

    term.send("/exit", delay=0.05)
    time.sleep(0.5)
    os.write(term.master, b"\r")
    try:
        term.proc.wait(20)
    except Exception:
        term.proc.terminate()
    res["log_tail"] = bridge_log()[-1200:]
    save("interactive3", res)


if __name__ == "__main__":
    main()
