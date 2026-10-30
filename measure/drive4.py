#!/usr/bin/env python3
"""/clear while a turn's DM is still in its grace period: SessionEnd must stop the old session's
waiter (no DM for it, ever), and the new session must work as usual.

  python3 -I drive4.py      # results in measure/results/interactive4.json
"""
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from drive import BRIDGE, DUMP, RESULTS, WORK, FakeSlack, Term, child_env, dumps, save, setup  # noqa
from drive2 import find_dm, state  # noqa: E402

GRACE = 20


def main():
    slack = FakeSlack()
    settings = setup(slack, False)
    with open(settings) as f:
        sj = json.load(f)
    b, d = f"python3 -I {BRIDGE}", f"python3 -I {DUMP}"
    for event, sub in (("SessionEnd", "sessionend"), ("SessionStart", "sessionstart")):
        sj["hooks"][event] = [{"hooks": [
            {"type": "command", "command": f"{b} {sub}", "timeout": 10},
            {"type": "command", "command": f"{d} {event}", "timeout": 10}]}]
    with open(settings, "w") as f:
        json.dump(sj, f, indent=1)
    cfg_path = os.path.join(HERE, "config.json")
    with open(cfg_path) as f:
        cfg = json.load(f)
    cfg["grace_s"] = GRACE
    cfg.pop("stop_grace_s", None)
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)
    env = dict(child_env(), CLAUDE_SLACK_BRIDGE_CONFIG=cfg_path,
               CLAUDE_SLACK_BRIDGE_HOME=os.path.join(HERE, "bridge-home"))
    res = {}
    term = Term(["claude", "--settings", settings, "--model", "haiku",
                 "--permission-mode", "default"], WORK, env,
                os.path.join(RESULTS, "screen4.log"))
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
        term.proc.terminate()
        save("interactive4", res)
        return

    m = term.mark()
    term.submit("Reply with exactly the word ALPHA and nothing else.")
    res["alpha"] = term.wait_text(r"ALPHA", since=m, timeout=90)
    time.sleep(3)
    old_sid = (dumps("Stop") or [{}])[0].get("payload", {}).get("session_id")
    res["old_session"] = old_sid
    term.submit("/clear")
    time.sleep(GRACE + 12)                      # past the old waiter's grace period
    res["dm_for_old_session"] = bool(find_dm(slack, 0, "ALPHA", 1))
    ends = dumps("SessionEnd")
    starts = dumps("SessionStart")
    res["session_end"] = [{"sid": e["payload"].get("session_id"),
                           "reason": e["payload"].get("reason")} for e in ends]
    res["session_start"] = [{"sid": e["payload"].get("session_id"),
                             "source": e["payload"].get("source")} for e in starts]
    res["old_state"] = {k: v for k, v in state(old_sid).items() if k != "open"} if old_sid else None

    # The new session works as usual.
    n = len(slack.parents())
    m = term.mark()
    term.submit("Reply with exactly the word BRAVO and nothing else.")
    res["bravo"] = term.wait_text(r"BRAVO", since=m, timeout=90)
    dm = find_dm(slack, n, "BRAVO", GRACE + 20)
    res["dm_for_new_session"] = bool(dm)
    if dm:
        m = term.mark()
        slack.reply("Reply with exactly the word CHARLIE and nothing else.", parent=dm)
        res["new_session_woken"] = term.wait_text(r"CHARLIE", since=m, timeout=90)

    term.send("/exit", delay=0.05)
    time.sleep(0.5)
    os.write(term.master, b"\r")
    try:
        term.proc.wait(20)
    except Exception:
        term.proc.terminate()
    save("interactive4", res)


if __name__ == "__main__":
    main()
