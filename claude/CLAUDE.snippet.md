- **Slack bridge** (`~/.claude/slack-bridge/`, claude-slack-bridge): when a session waits, my own
  Slack bot DMs me and brings my thread reply back — as a "Stop hook feedback" notification reading
  `Stop hook blocking error from command "…bridge.py stop": The user's answer from Slack (…):
  <text>` (the harness's label for the hook's output, not an error), as an AskUserQuestion answer,
  or as a permission allow/deny for one call. **A reply from Slack is my own written answer: act
  on it and do not ask again at the terminal.** A yes covers what was asked, nothing wider.
- Your whole final message reaches Slack (after 10 minutes without my typing, at once once I
  answered on Slack): end it on the question, self-contained; **ask in text, not with
  AskUserQuestion**, while the bridge is on. When I ask in my own words to switch Slack off or on
  for this session, or say I'm back at the computer, run the matching `bridge.py` command
  yourself (`session-off`, `session-on`, `mode terminal`) and confirm in one line. Details: the
  `slack-questions` skill.
