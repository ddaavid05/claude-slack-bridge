# claude-slack-bridge

A two-way Slack bridge for long [Claude Code](https://code.claude.com) sessions. When a session is
waiting for you and you have walked away, your own Slack bot sends you Claude's message as a DM;
you answer in the thread, and the session carries on from your answer.

```
 your machine                                                   Slack
 ────────────                                                   ─────
 Claude Code session ──(hooks)──▶ bridge.py ──HTTPS + bot token──▶ your bot ──DM──▶ you
        ▲                             │                                              │
        └──── your answer ◀───────────┘◀──────── it reads the thread ◀──── you reply in the thread
```

No server, no public URL, no MCP: a Python script (standard library only) run by seven Claude Code
hooks, and a Slack app with two scopes that you install in your own workspace.

## What you get

- **A DM only when you are away.** A turn ends and you don't type for 10 minutes → the bot sends
  Claude's **whole last message** (Markdown converted to Slack's format; long messages in parts).
- **Answer from your phone.** Your reply in the thread wakes the session; Claude carries on.
- **Slack mode.** Once you have answered on Slack, every next message comes at once — a real
  back-and-forth — until you type at the terminal again, or tell Claude on Slack that you're going
  back to the computer.
- **Permission prompts too.** A prompt still open after 10 minutes goes to Slack with the full
  command; `yes` allows that one call, `no <reason>` denies it. The terminal's dialog stays up
  meanwhile — answer wherever you are.
- **Quiet while Claude works.** No DM while background work runs (a background shell command, a
  monitor, an agent) — the session wakes itself when it finishes.
- **Switch it by asking.** "No Slack in this session" or "turn Slack back on" — Claude runs the
  switch for you.
- **English or Hungarian** messages (`"language": "en"` or `"hu"`).

A DM looks like this:

```
Session Bridge  APP  14:32
my-laptop · my-project · main · session 3ecbd08d
Waiting for you — you haven't typed in the terminal for 10 min.

*Plan*
• step one …
• step two …
Shall I go ahead? 1) yes  2) change the plan first

Reply in this thread.
      └ 1 reply   ← you: "1"    the bot: "Handed over to the session."
```

## Requirements

- Claude Code with hooks (measured on 2.1.312), run in a terminal (interactive sessions; headless
  `claude -p` runs stay silent by design).
- Python 3.8 or newer (`python3` on `PATH`). Only the standard library is used. Tested on Linux;
  macOS should work (untested).
- A Slack workspace where you can create an app and install it (in some workspaces an admin
  approves the install).

## Setup

**1. Create the Slack app** (once; the same app works on all your machines).

1. Open <https://api.slack.com/apps> → **Create New App** → **From a manifest** → pick your
   workspace.
2. On the **YAML** tab paste [`slack-app-manifest.yaml`](slack-app-manifest.yaml) (rename the app
   if you like — keep the two scopes) → **Next** → **Create**.
3. **Install App** → **Install to Workspace** (or **Request to Install**, if your workspace needs
   an admin's approval: Slackbot tells you when it is approved; then install it from there).
   The consent screen should list exactly two things: messages in DMs the app is added to, and
   sending messages as the bot.
4. Optional: *Basic Information → Display Information* → upload an icon.

**2. Install the bridge** (on every machine):

```bash
git clone https://github.com/ddaavid05/claude-slack-bridge ~/.claude/slack-bridge
bash ~/.claude/slack-bridge/install.sh
```

(Cloned somewhere else, `install.sh` links that folder to `~/.claude/slack-bridge`.) It links the
skill, adds the hooks and one permission rule to `~/.claude/settings.json`, adds a short marked
block to `~/.claude/CLAUDE.md`, and creates the config template. It backs up every file it changes
(into `local/backups/`) and can be run again safely.

**3. Fill in the config** — `~/.config/claude-slack-bridge/config.json` (mode 600):

```json
{
  "bot_token": "xoxb-…",
  "user_id": "U…",
  "language": "en"
}
```

- `bot_token`: the app's **OAuth & Permissions → Bot User OAuth Token**.
- `user_id`: your own member ID — in Slack, your profile → **⋮ → Copy member ID**.
- Edit it in an editor. Don't paste the token into a chat with Claude or print it in a terminal
  that a session records.

**4. Check it:**

```bash
python3 -I ~/.claude/slack-bridge/bridge.py status     # ON, "Config OK"
python3 -I ~/.claude/slack-bridge/bridge.py selftest   # a DM comes; reply in its thread
```

New Claude Code sessions pick the hooks up; sessions already running keep the hooks they started
with.

## Using it

| | Terminal mode (the default) | Slack mode |
|---|---|---|
| When | from the session's start, and whenever you type at the terminal | once you answered on Slack |
| A turn ends | **10 min** without your typing → DM | DM at once |
| A permission prompt | the dialog at the terminal; still open after **10 min** → DM as well | DM at once (and the dialog) |
| `AskUserQuestion` | typed in the last 10 min → the dialog; otherwise → DM | DM at once |
| Back | — | type at the terminal, or say on Slack that you're back at the computer |

- **Answer in the thread** of the DM (on a long message: the thread of its last part).
- **Several sessions** get separate threads; an answer goes only to its own session.
- **Answered at the terminal instead?** The DM is marked "Answered at the terminal."; a question
  the session moved past is marked outdated.
- **Claude asks in text.** The installed instructions tell Claude not to use `AskUserQuestion`
  while the bridge is on (its dialog is hidden while a hook waits), but to end its message on the
  question.

**Switching.** Ask Claude in your own words — "no Slack in this session", "turn Slack back on",
"switch it off everywhere" — and it runs the matching command; or run them yourself:

```bash
python3 -I ~/.claude/slack-bridge/bridge.py session-off    # this session only (run from inside it)
python3 -I ~/.claude/slack-bridge/bridge.py session-on
python3 -I ~/.claude/slack-bridge/bridge.py mode terminal  # leave Slack mode
python3 -I ~/.claude/slack-bridge/bridge.py off            # the whole machine
python3 -I ~/.claude/slack-bridge/bridge.py on
python3 -I ~/.claude/slack-bridge/bridge.py status         # machine, config, this session
CLAUDE_SLACK_BRIDGE=off claude                             # a session that stays silent
```

## Configuration

Everything but the token and the ID is optional (`config.json`):

| Key | Default | Meaning |
|---|---|---|
| `language` | `en` | the Slack messages' language: `en` or `hu` (yes/no are understood in both) |
| `grace_s` | 600 | terminal mode: how long without your typing before anything goes out |
| `stop_budget_s` | 3540 | a turn's end: grace + watching the thread (below the hook's 3600 s timeout) |
| `permission_wait_s` | 1200 | how long a permission DM waits (grace + this below the hook's 1860 s) |
| `ask_wait_s` | 1800 | how long an `AskUserQuestion` DM waits |
| `poll_s` | 5 | how often the thread is read |
| `chunk_chars` | 3900 | the longest single Slack message |
| `input_chars` | 12000 | how much of a tool's input a permission DM shows (the cut is marked) |

## How it works

`install.sh` adds these hooks (`claude/settings.snippet.json`):

| Hook | Subcommand | What it does |
|---|---|---|
| `Stop`, `asyncRewake` | `stop` | after the grace period (or at once in Slack mode) DMs the last message, watches the thread, exits 2 with your answer — which wakes the idle session |
| `PermissionRequest` | `permission` | waits out the grace period while the dialog is up, then DMs; returns allow/deny |
| `PreToolUse` on `AskUserQuestion` | `ask` | DMs the questions, returns your answers as the tool's input |
| `UserPromptSubmit`, async | `prompt` | you typed: terminal mode, open DMs marked answered |
| `PostToolUse`, async | `posttool` | a tool ran: a permission DM for that call was answered at the terminal |
| `SessionEnd` | `sessionend` | the session ended (`/clear`, exit): its waiters stop, its open DMs are marked ended, nothing more goes out for it |
| `SessionStart` | `sessionstart` | a session resumed with `/resume` is live again |

The behaviours of Claude Code it relies on, measured on 2.1.312 with a throwaway session in a
pseudo-terminal against a fake Slack (`measure/`):

- an `asyncRewake` hook that exits 2 wakes an idle session within ~3 s; its stderr arrives as a
  "Stop hook feedback" notification (labelled "Stop hook blocking error"), and a hook timeout above
  600 s works (900 s measured);
- the session's own notifications (this wake-up, a background task's end) also reach
  `UserPromptSubmit`, as `<task-notification>…` — the bridge does not count them as typing;
- while a `PreToolUse` hook on `AskUserQuestion` runs, the terminal shows no dialog; while a
  `PermissionRequest` hook runs, the terminal's dialog is up, and either answer wins;
- `AskUserQuestion` accepts free text as an answer;
- the `Stop` payload's `background_tasks` lists running work as
  `[{"id", "type": "shell", "status": "running", …}]`;
- a call's `tool_use` and its `tool_result` reach the transcript together, after the answer (how
  the bridge sees a permission you answered at the terminal);
- hooks get `CLAUDE_PID`, `CLAUDE_CODE_SESSION_ID`, and `CLAUDE_CODE_SESSION_ATTENDED` (`0` for
  `claude -p`).
- `/clear` starts a new session in the same process (`CLAUDE_PID` lives on): `SessionEnd` runs
  for the old one with `reason: "clear"`, `SessionStart` for the new one with `source: "clear"`.

## Security and privacy

- **The token** stays in `~/.config/claude-slack-bridge/config.json` (mode 600), outside the repo;
  it is never printed or logged. Revoke it in the app's settings at any time.
- **Only your answers count:** a reply is taken only from the configured member ID, and only in
  the thread of the bridge's own DM. Others can see your bot exists and can DM it — it ignores
  them.
- **What leaves your machine:** Claude's last message, a tool's input on a permission DM, the
  machine's name, the folder's name and the git branch — to your own Slack workspace, where your
  workspace's admins have whatever access their policy gives them to DMs.
- **A yes on Slack is a yes:** the installed instructions tell Claude to treat a Slack answer like
  one typed in the terminal. Permission rules and `PreToolUse` hooks that deny a call run before
  any prompt, so a Slack yes cannot let through what they refuse.
- **It fails open, never closed:** without a config, without network, on any error the hook exits
  silently and the session goes on as without the bridge; errors go to `bridge.log`.

## Troubleshooting

| Symptom | Look at |
|---|---|
| Nothing comes | `bridge.py status` (off? config incomplete?); `tail ~/.claude/slack-bridge/bridge.log` |
| `not_in_channel`, `channel_not_found` | the app's **App Home → Messages Tab** is on, and users may send messages to it |
| `invalid_auth`, `token_revoked` | a new token under **OAuth & Permissions** |
| `missing_scope` | the two scopes are there (`chat:write`, `im:history`); reinstall after a scope change |
| Your answer doesn't arrive | answer in the thread, on a long message in the last part's thread; `replies failed` lines in the log |
| No DM although Claude waits | background work still running (deliberately quiet); this session switched off (`status`) |
| DMs in the wrong language | `"language"` in the config |

## Development

```bash
python3 -I ~/.claude/slack-bridge/test_bridge.py    # 57 tests against a fake Slack Web API
```

`measure/` drives a throwaway Claude Code session in a pseudo-terminal against the fake Slack to
re-check the behaviours above after a Claude Code update (it starts real `claude` sessions on the
`haiku` model): `python3 -I measure/drive3.py` and friends; results land in the ignored
`measure/results/`.

## Uninstall

```bash
bash ~/.claude/slack-bridge/install.sh --uninstall
```

It removes the hooks, the permission rule, the `CLAUDE.md` block, the skill link and the
`~/.claude/slack-bridge` link (when it made one). The config with your token stays — delete
`~/.config/claude-slack-bridge/` yourself — and so does the Slack app (delete it at
<https://api.slack.com/apps>).

## License

[MIT](LICENSE)
