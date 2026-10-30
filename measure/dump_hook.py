#!/usr/bin/env python3
"""Measurement helper: record what a hook receives (its stdin JSON and the session-kind
variables), never a secret. Usage in a hook: python3 -I dump_hook.py <EventName>"""
import json
import os
import sys
import time

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dumps")
os.makedirs(OUT, exist_ok=True)
event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
raw = sys.stdin.read()
try:
    payload = json.loads(raw)
except ValueError:
    payload = {"_raw": raw[:2000]}
keep = ("CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_ENTRYPOINT", "CLAUDECODE",
        "CLAUDE_CODE_CHILD_SESSION")
record = {"event": event, "time": time.time(),
          "env": {k: os.environ.get(k) for k in keep},
          "env_names": sorted(k for k in os.environ if k.startswith("CLAUDE")),
          "payload": payload}
with open(os.path.join(OUT, f"{event}_{time.time():.3f}_{os.getpid()}.json"), "w") as f:
    json.dump(record, f, indent=1, ensure_ascii=False)
