# Switching an existing install to this fork

For a device already running upstream HomeDeck (installed with
`guides/linux/install.sh`). Nothing is deleted — the switch is a `git remote`
change, and you can go back with two commands.

The official installer puts things here:

| What | Where |
|:--|:--|
| Repo | `/app/homedeck` |
| Virtualenv | `/app/homedeck-venv` |
| Your config | `/app/homedeck/assets/configuration.yml` |
| Your secrets | `/app/homedeck/.env` |
| Autostart | a `@reboot` crontab entry running `server.py` |

Adjust the paths below if yours differ. SSH into the Orange Pi to begin:

```bash
ssh root@<orange-pi-ip>
```

## 1. Stop it and back up

```bash
# Stop whatever is running
pkill -f server.py; pkill -f deck.py

# Back up the two files that are yours, not the project's
cp /app/homedeck/assets/configuration.yml ~/configuration.yml.bak
cp /app/homedeck/.env ~/.env.bak
```

Both files are gitignored, so the switch won't touch them — the backup is
belt-and-braces.

## 2. Point the repo at this fork

```bash
cd /app/homedeck

# Keep a reference to where you were, in case you want to go back
git remote add upstream https://github.com/redphx/homedeck.git 2>/dev/null || true
git remote set-url origin https://github.com/fkras/homedeck.git

git fetch origin
git checkout wall-mount-schedule
git pull
```

Confirm you're on the fork:

```bash
git log --oneline -3
```

You should see `Document and test the YAML time-quoting hazard` at the top.

## 3. Reinstall the package

The package is installed in editable mode, so code changes are already live.
Re-running `pip install -e .` just makes sure the metadata matches this branch —
cheap, and it catches a dependency drift if one ever appears:

```bash
source /app/homedeck-venv/bin/activate
cd /app/homedeck
pip install -e .
```

## 4. Clear the icon cache once

Generated icons are now named by a stable hash instead of Python's per-process
`hash()`. Old cached files can never be matched again, so delete them once:

```bash
rm -rf /app/homedeck/.cache/icons/_generated
```

The cache rebuilds on first run and then persists across restarts — the first
start after this will be slower than the ones after it.

## 5. Turn on always-on + night dimming

Edit `/app/homedeck/assets/configuration.yml` and replace the `sleep:` block:

```yaml
brightness: 80

sleep:
  sleep_timeout: 0      # never blank the screen
  dim_timeout: 30       # settle to the night level after 30s idle
  dim_brightness: 10

  schedule:
    - from: '22:00'
      to: '06:00'
      brightness: 10
```

> [!IMPORTANT]
> Quote the times. Unquoted `22:00` is read by YAML as the number `1320`. The
> config will be rejected rather than silently dimming at the wrong hour, but
> it's an easy trap.

The schedule uses the Pi's local clock, so set the timezone and check it:

```bash
timedatectl set-timezone Europe/Zurich    # use your own zone
date                                       # sanity-check the clock
```

Also add it to `/app/homedeck/.env` so it survives regardless of system settings:

```bash
echo 'TIMEZONE="Europe/Zurich"' >> /app/homedeck/.env
```

## 6. Try it in the foreground first

Before touching autostart, watch it run:

```bash
source /app/homedeck-venv/bin/activate
cd /app/homedeck
python deck.py
```

Expect `Device connected`, then `Authenticated successfully`, then the deck
redraws. Leave it running and confirm the screen **does not blank** after a few
minutes. `Ctrl+C` when satisfied.

To check the dimming without waiting until 22:00, temporarily set the window to
a couple of minutes from now — e.g. if it's 14:05, use `from: '14:07'` /
`to: '14:09'`. The config hot-reloads on save, so you don't need to restart.
The screen should dim after `dim_timeout` inside the window, brighten when you
press a button, and return to full brightness once the window ends.

## 7. Make it start on boot

The upstream installer uses a crontab entry, which gives you no logs and no
restart-on-crash. Switch to systemd.

**Remove the old entry:**

```bash
crontab -l | grep -v homedeck | crontab -
crontab -l          # confirm the homedeck line is gone
```

**Then pick one** — these are alternatives, not complements:

**A. Deck only** (configure over SSH):

```bash
cp /app/homedeck/homedeck.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now homedeck
systemctl status homedeck
```

**B. Deck + Home Assistant editing** (what upstream's crontab did):

```bash
cp /app/homedeck/homedeck-server.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now homedeck-server
systemctl status homedeck-server
```

`server.py` starts and manages `deck.py` itself, so **enabling both means
systemd will instantly restart any deck you stop from the UI.** Option B if you
want the Home Assistant add-on; option A otherwise.

Follow the logs:

```bash
journalctl -u homedeck -f          # or -u homedeck-server
```

## 8. Reboot test

```bash
reboot
```

After it comes back, the deck should redraw on its own. If not:

```bash
systemctl status homedeck
journalctl -u homedeck -b --no-pager | tail -40
```

## Going back to upstream

```bash
cd /app/homedeck
git checkout main
git remote set-url origin https://github.com/redphx/homedeck.git
git pull
source /app/homedeck-venv/bin/activate && pip install -e .
rm -rf /app/homedeck/.cache/icons/_generated
systemctl restart homedeck
```

Your `configuration.yml` and `.env` are untouched throughout — though upstream
will reject a `sleep.schedule` block, so remove it if you go back.

## Troubleshooting

**`Could not find any device`** — the deck isn't visible over USB. Check
`lsusb` for `2207:0019`, and try the other USB-C port (one is power-only on the
Orange Pi Zero 2W).

**Config rejected on startup** — the app exits if `configuration.yml` fails
schema validation at boot. Run `python deck.py` in the foreground to see which
line it objects to.

**Screen still blanks** — confirm `sleep_timeout: 0` is set and that the file
you edited is the one being read (`/app/homedeck/assets/configuration.yml`).

**Dimming at the wrong time** — check `date` on the Pi. The schedule follows the
system clock, and a Pi without network time can be badly off.
