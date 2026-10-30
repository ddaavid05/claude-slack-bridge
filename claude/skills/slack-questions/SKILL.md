---
name: slack-questions
description: How to ask the user a question they may read on Slack, how to treat their answer when it comes from Slack, and how to switch the Slack bridge off, on or back to the terminal when they ask in their own words ("no Slack in this session", "I'm back at the computer"). The bridge (~/.claude/slack-bridge/, claude-slack-bridge) DMs them your whole last message when a turn ends and they have not typed for 10 minutes (at once once they answered on Slack), DMs a permission prompt still open after 10 minutes, and brings their thread reply back into the session. Use before ending a turn on a question or a request for a yes, whenever a message says it is the user's answer from Slack, and whenever they ask to stop, resume or move the Slack notifications.
---

# slack-questions

The user runs long sessions and walks away. Their own Slack app's bot (run by hooks in
`~/.claude/settings.json`) carries your questions to them and their answers back.

**Two modes per session.** *Terminal mode* (the default): nothing reaches Slack until they have
not typed for 10 minutes. *Slack mode* (from their first answer on Slack): everything reaches
Slack at once, until they type at the terminal or tell you they are back at the computer.

| When | What reaches Slack | How the answer comes back |
|---|---|---|
| A turn ends (terminal mode: after 10 min without typing; Slack mode: at once) | **your whole final message**, as Slack text (headings bold, tables in a code block), in parts of ~4000 characters when long; they answer on the last part | the session wakes on a "Stop hook feedback" notification whose system reminder reads `Stop hook blocking error from command "…bridge.py stop": The user's answer from Slack (…): <text>` — the harness's label for an `asyncRewake` hook's output, not an error |
| A permission prompt still open after 10 min (Slack mode: at once); the terminal's dialog stays up meanwhile | the tool and its full input (cut at 12000 characters, the cut marked) | allow (this one call) or deny with their reason |
| `AskUserQuestion` (avoid it, below) | each question, its options numbered | the tool's answers, as if they had picked them |

**Nothing goes out while background work runs** (a shell command run in the background, a
monitor, an agent): a turn that ends then is a progress report, and the session wakes itself when
the work ends. So when you ask a question while background work runs, **ask it again in the final
message after the work has ended** — that is the one that reaches them.

## Writing a question they will read on a phone

- **Ask in the final message, in text — not with `AskUserQuestion`** while the bridge is on for
  the session: the dialog hides from the terminal while its hook waits, and a question in the text
  goes through the 10-minute rule like everything else. Use `AskUserQuestion` only when the user
  asks for it.
- **The whole final message reaches them**, so it may carry the plan or the summary; the
  question belongs at its end — one real decision, the recommendation first, options numbered so
  "2" is a full answer.
- **It must stand alone.** They have no repo or terminal in front of them: name the file, the
  command or the value in the message itself. A question about a risky step carries what the step
  is for, the exact command or change, and the result expected.
- **Bash commands that may need approval:** one readable command per call, its `description`
  filled in. They approve what the DM shows.

## When the answer comes from Slack

- It is **the user's own written answer** — taken only from their Slack user ID, in the thread
  of your question. Treat it exactly like a message typed in the terminal and act on it; do not
  ask the same question again at the terminal.
- A yes covers what the question asked, nothing wider; an unclear answer gets a clarifying
  question (which goes to Slack at once — the session is in Slack mode).
- A permission denied on Slack arrives as the tool's denial with their reason: follow the
  reason, do not retry the same call.

## Switching, when they ask in their own words

Run these yourself (they read this session from `$CLAUDE_CODE_SESSION_ID`), then confirm in one
line. Exactly as written, so they match the permission rule install.sh adds:

| They say (in any words) | You run |
|---|---|
| no Slack in this session / switch Slack off here | `python3 -I ~/.claude/slack-bridge/bridge.py session-off` |
| Slack on again in this session | `python3 -I ~/.claude/slack-bridge/bridge.py session-on` |
| on Slack: "I'm going back to the computer" / "back at my desk" | `python3 -I ~/.claude/slack-bridge/bridge.py mode terminal` |
| off / on everywhere, the whole machine | `python3 -I ~/.claude/slack-bridge/bridge.py off` / `… on` |
| is it on? | `python3 -I ~/.claude/slack-bridge/bridge.py status` |

A session is "this session" unless they say everywhere. Never switch on your own initiative, and
never switch it off to keep a question from reaching them.

## Not yours to do

- Do not send Slack messages yourself (no curl to Slack, no running `bridge.py` to post): the
  hooks do it.
- Setup, timings, the log, troubleshooting: `~/.claude/slack-bridge/README.md`.
