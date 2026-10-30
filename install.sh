#!/usr/bin/env bash
# claude-slack-bridge installer, for the current user:
#   - makes the repo reachable as ~/.claude/slack-bridge (a link when cloned elsewhere),
#   - links the slack-questions skill into ~/.claude/skills/,
#   - adds the five hooks and one permission rule to ~/.claude/settings.json,
#   - adds a short, marked block to ~/.claude/CLAUDE.md,
#   - creates the config template ~/.config/claude-slack-bridge/config.json (mode 600).
# Safe to run again: nothing is added twice, and a changed hook is replaced. Every file it changes
# is backed up first into local/backups/<time>/ inside the repo.
#
#   bash install.sh              install or update
#   bash install.sh --uninstall  take all of it out again (the config with your token stays)
set -euo pipefail

MODE=install
case "${1:-}" in
  "") ;;
  --uninstall) MODE=uninstall ;;
  *) echo "usage: bash install.sh [--uninstall]" >&2; exit 2 ;;
esac

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
CLAUDE_DIR="$HOME/.claude"
TARGET="$CLAUDE_DIR/slack-bridge"
CONFIG_DIR="$HOME/.config/claude-slack-bridge"

command -v python3 >/dev/null || { echo "python3 is needed (3.8 or newer)" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
  || { echo "python3 is too old: 3.8 or newer is needed" >&2; exit 1; }

# A settings file that is not valid JSON is the user's to fix: change nothing at all.
if [ -f "$CLAUDE_DIR/settings.json" ]; then
  python3 -c 'import json, sys; json.load(open(sys.argv[1]))' "$CLAUDE_DIR/settings.json" 2>/dev/null \
    || { echo "$CLAUDE_DIR/settings.json is not valid JSON: fix it first, nothing was changed" >&2; exit 1; }
fi

# The hooks call "$HOME/.claude/slack-bridge/bridge.py": the repo must be reachable there.
if [ "$MODE" = install ]; then
  mkdir -p "$CLAUDE_DIR"
  if [ -e "$TARGET" ] || [ -L "$TARGET" ]; then
    if [ "$(cd "$TARGET" 2>/dev/null && pwd -P)" != "$HERE" ]; then
      echo "$TARGET already exists and is not this repo ($HERE)." >&2
      echo "Move it away, or clone the repo there, then run this again." >&2
      exit 1
    fi
  else
    ln -s "$HERE" "$TARGET"
    echo "linked   $TARGET -> $HERE"
  fi
fi

python3 - "$MODE" "$HERE" "$CLAUDE_DIR" "$CONFIG_DIR" <<'PY'
import json, os, shutil, sys, time

mode, here, claude_dir, config_dir = sys.argv[1:5]
backup_dir = os.path.join(here, "local", "backups",
                          time.strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}")
BEGIN = "<!-- claude-slack-bridge: begin (managed by install.sh) -->"
END = "<!-- claude-slack-bridge: end -->"
snippet = json.load(open(os.path.join(here, "claude", "settings.snippet.json")))
rule = snippet["permissions"]["allow"][0]


def backup(path):
    if not (os.path.exists(path) or os.path.islink(path)):
        return
    os.makedirs(backup_dir, exist_ok=True)
    dst = os.path.join(backup_dir, os.path.basename(path))
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.copytree(path, dst, symlinks=True)
    else:
        shutil.copy2(path, dst, follow_symlinks=False)
    print(f"backup   {path} -> {dst}")


def write_text(path, text):
    tmp = path + ".tmp-slack-bridge"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def ours(hook):
    return "slack-bridge/bridge.py" in hook.get("command", "")


# --- settings.json: our hooks and rule out, then (install) in again -----------------------------
settings_path = os.path.join(claude_dir, "settings.json")
if os.path.exists(settings_path):
    with open(settings_path, encoding="utf-8") as f:
        try:
            settings = json.load(f)
        except ValueError as e:
            sys.exit(f"{settings_path} is not valid JSON ({e}); fix it first, nothing was changed")
else:
    settings = {}
before = json.dumps(settings, sort_keys=True)

hooks = settings.get("hooks", {})
for event in list(hooks):
    kept = []
    for group in hooks[event]:
        rest = [h for h in group.get("hooks", []) if not ours(h)]
        if rest:
            kept.append(dict(group, hooks=rest))
    if kept:
        hooks[event] = kept
    else:
        del hooks[event]
if "hooks" in settings and not hooks:
    del settings["hooks"]
perms = settings.get("permissions", {})
if rule in perms.get("allow", []):
    perms["allow"] = [r for r in perms["allow"] if r != rule]
    if not perms["allow"]:
        del perms["allow"]
    if not perms:
        del settings["permissions"]

if mode == "install":
    for event, groups in snippet["hooks"].items():
        settings.setdefault("hooks", {}).setdefault(event, []).extend(groups)
    settings.setdefault("permissions", {}).setdefault("allow", []).append(rule)

if json.dumps(settings, sort_keys=True) != before:
    os.makedirs(claude_dir, exist_ok=True)
    backup(settings_path)
    write_text(settings_path, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    print(f"updated  {settings_path}")
else:
    print(f"ok       {settings_path}")

# --- CLAUDE.md: the marked block out, then (install) in again ------------------------------------
md_path = os.path.join(claude_dir, "CLAUDE.md")
md = open(md_path, encoding="utf-8").read() if os.path.exists(md_path) else ""
new = md
if BEGIN in new and END in new:
    head, _, rest = new.partition(BEGIN)
    _, _, tail = rest.partition(END)
    new = head.rstrip("\n") + ("\n" if head.strip() else "") + tail.lstrip("\n")
if mode == "install":
    block = open(os.path.join(here, "claude", "CLAUDE.snippet.md"), encoding="utf-8").read()
    new = (new.rstrip("\n") + "\n\n" if new.strip() else "") + f"{BEGIN}\n{block.rstrip()}\n{END}\n"
if new != md:
    backup(md_path)
    write_text(md_path, new)
    print(f"updated  {md_path}")
else:
    print(f"ok       {md_path}")

# --- the skill: a link to the repo's copy --------------------------------------------------------
src = os.path.join(here, "claude", "skills", "slack-questions")
link = os.path.join(claude_dir, "skills", "slack-questions")
points_here = os.path.islink(link) and os.path.realpath(link) == os.path.realpath(src)
if mode == "install" and not points_here:
    if os.path.exists(link) or os.path.islink(link):
        backup(link)
        if os.path.isdir(link) and not os.path.islink(link):
            shutil.rmtree(link)
        else:
            os.remove(link)
    os.makedirs(os.path.dirname(link), exist_ok=True)
    os.symlink(src, link)
    print(f"linked   {link} -> {src}")
elif mode == "uninstall" and points_here:
    os.remove(link)
    print(f"removed  {link}")
else:
    print(f"ok       {link}")

# --- the config (your token): created once, never overwritten, kept on uninstall ---------------
cfg = os.path.join(config_dir, "config.json")
if mode == "install":
    os.makedirs(config_dir, exist_ok=True)
    os.chmod(config_dir, 0o700)
    if not os.path.exists(cfg):
        fd = os.open(cfg, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"bot_token": "xoxb-PASTE-YOUR-BOT-TOKEN-HERE",
                       "user_id": "U-PASTE-YOUR-MEMBER-ID-HERE",
                       "language": "en"}, f, indent=2)
            f.write("\n")
        print(f"created  {cfg} (fill in your bot token and member ID)")
    else:
        os.chmod(cfg, 0o600)
        print(f"ok       {cfg}")
else:
    print(f"kept     {cfg} (holds your token: delete it yourself if you want it gone)")
PY

if [ "$MODE" = uninstall ]; then
  if [ -L "$TARGET" ] && [ "$(cd "$TARGET" && pwd -P)" = "$HERE" ]; then
    rm "$TARGET"
    echo "removed  $TARGET"
  fi
  echo "Uninstalled. Running sessions keep their hooks until they restart."
  exit 0
fi

echo
python3 -I "$TARGET/bridge.py" status
echo
echo "New sessions pick the hooks up; running ones keep the hooks they started with."
echo "Next: fill in the config (README, step 3), then: python3 -I ~/.claude/slack-bridge/bridge.py selftest"
