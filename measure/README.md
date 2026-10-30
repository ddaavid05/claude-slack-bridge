# measure/

Re-checks the Claude Code behaviours the bridge relies on (listed in the main README), with a
throwaway Claude Code session in a pseudo-terminal against the fake Slack of `test_bridge.py`. Run
them after a Claude Code update. They start real `claude` sessions (the `haiku` model, a few cents
each) in `measure/work/`, with the hooks passed by `--settings` and the bridge pointed at its own
config and state, so your real setup is not touched. Results go to `measure/results/` (ignored by
git: they hold the sessions' screens and transcripts).

| Script | What it checks |
|---|---|
| `drive.py headless` | `claude -p` gets `CLAUDE_CODE_SESSION_ATTENDED=0`: the bridge stays silent |
| `drive.py interactive [--long]` | the wake-up from a Slack reply, `AskUserQuestion` with free text, the dialogs while a hook waits, a permission answered at the terminal; `--long`: a hook timeout above 600 s |
| `drive2.py` | a busy session (background work) sends nothing; the `background_tasks` format |
| `drive3.py` | a permission allowed and one denied at the terminal within the grace period (no DM); Slack mode (the next DM at once); typing returns to terminal mode |
| `drive4.py` | `/clear` while a turn's DM is in its grace period: no DM for the old session, the new one works |

`dump_hook.py` records what a hook receives (its JSON and the session-kind variables, never a
secret) into `measure/dumps/`. The terminal driver handles the folder-trust dialog; a newer Claude
Code may draw its screens differently, so read `results/*.json` and the screen logs when a check
fails.
