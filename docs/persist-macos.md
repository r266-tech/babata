# Persist babata on macOS (launchd)

Run babata in the background, restart on crash, start on boot — all via macOS native `launchd`. No Docker, no cron, no `nohup`.

## 1. Install the plist template

```bash
# From the babata repo root
mkdir -p ~/Library/LaunchAgents
cp docs/com.babata.plist.template ~/Library/LaunchAgents/com.babata.plist
```

Edit the copy and replace **two paths** (your actual values):

```xml
<key>ProgramArguments</key>
<array>
    <string>__REPO_DIR__/.venv/bin/python</string>
    <string>__REPO_DIR__/bot.py</string>
</array>

<key>WorkingDirectory</key>
<string>__REPO_DIR__</string>
```

Replace `__REPO_DIR__` with the absolute path to where you cloned babata, e.g. `/Users/yourname/code/babata`.

If `claude` isn't on the default PATH, also edit `CLAUDE_CLI_PATH` in the `EnvironmentVariables` dict at the bottom.

## 2. Start it

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.babata.plist
```

It's now running, will auto-restart if it crashes, and starts on next login.

## 3. Verify

```bash
# Check it's loaded
launchctl list | grep com.babata

# Tail logs
tail -f ~/Library/Logs/babata.log
tail -f ~/Library/Logs/babata.err.log
```

Send a message to your Telegram bot — should respond.

## 4. Restart after editing code

```bash
launchctl kickstart -k gui/$(id -u)/com.babata
```

Or use the provided helper:
```bash
bash scripts/self-ops.sh restart
```

## 5. Stop / uninstall

```bash
# Stop (until next login)
launchctl bootout gui/$(id -u)/com.babata

# Uninstall fully
launchctl bootout gui/$(id -u)/com.babata
rm ~/Library/LaunchAgents/com.babata.plist
```

## Multi-instance

Same plist template, different `Label` and different `BABATA_INSTANCE` env. Example for a second bot named "alice":

```xml
<key>Label</key>
<string>com.babata.alice</string>

<key>EnvironmentVariables</key>
<dict>
    <!-- existing keys ... -->
    <key>BABATA_INSTANCE</key>
    <string>alice</string>
</dict>
```

State files / sockets / logs all derive from the instance name — no collision with the main bot.

Bootstrap as usual:
```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.babata.alice.plist
```

## Linux: systemd

Same idea, different syntax. Roughly:

```ini
# ~/.config/systemd/user/babata.service
[Unit]
Description=babata bot

[Service]
WorkingDirectory=/home/yourname/code/babata
ExecStart=/home/yourname/code/babata/.venv/bin/python /home/yourname/code/babata/bot.py
Restart=always
Environment=CLAUDE_CLI_PATH=/home/yourname/.local/bin/claude

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now babata
journalctl --user -u babata -f    # logs
```
