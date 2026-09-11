# Switching an existing install to this fork

For a device already running upstream HomeDeck (installed with
`guides/linux/install.sh`). Nothing is deleted — the switch is a `git remote`
change, and you can go back with two commands.

## First: find your install

Paths differ depending on how HomeDeck was installed, so detect them rather than
assuming:

```bash
ls -d /opt/homedeck /app/homedeck 2>/dev/null            # the repo
ls -d /opt/homedeck/.venv /app/homedeck-venv 2>/dev/null # the virtualenv
crontab -l 2>/dev/null | grep -i homedeck                # crontab autostart?
systemctl list-units --all 'homedeck*' --no-pager        # systemd autostart?
```

Two layouts are common:

| | Manual install | `guides/linux/install.sh` |
|:--|:--|:--|
| Repo | `/opt/homedeck` | `/app/homedeck` |
| Virtualenv | `/opt/homedeck/.venv` | `/app/homedeck-venv` |
| Autostart | usually systemd | `@reboot` crontab |

Set these once and the rest of the guide will paste as-is:

```bash
export HD=/opt/homedeck                # repo, from the check above
export HD_PY=$HD/.venv/bin/python      # or /app/homedeck-venv/bin/python
```

Verify before continuing:

```bash
ls $HD/deck.py && $HD_PY --version
```

Your config lives at `$HD/assets/configuration.yml` and your secrets at
`$HD/.env`. Neither is tracked by git, so the switch won't touch them.

SSH into the Orange Pi to begin:

```bash
ssh root@<orange-pi-ip>
```

## 1. Stop it and back up

```bash
# Stop whatever is running
pkill -f server.py; pkill -f deck.py

# Back up the two files that are yours, not the project's
cp $HD/assets/configuration.yml ~/configuration.yml.bak
cp $HD/.env ~/.env.bak
```

Both files are gitignored, so the switch won't touch them — the backup is
belt-and-braces.

## 2. Point the repo at this fork

```bash
cd $HD

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
cd $HD
$HD_PY -m pip install -e .
```

## 4. Clear the icon cache once

Generated icons are now named by a stable hash instead of Python's per-process
`hash()`. Old cached files can never be matched again, so delete them once:

```bash
rm -rf $HD/.cache/icons/_generated
```

The cache rebuilds on first run and then persists across restarts — the first
start after this will be slower than the ones after it.

## 5. Turn on always-on + night dimming

Edit `$HD/assets/configuration.yml` and replace the `sleep:` block:

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

Also add it to `$HD/.env` so it survives regardless of system settings:

```bash
echo 'TIMEZONE="Europe/Zurich"' >> $HD/.env
```

## 6. Try it in the foreground first

Before touching autostart, watch it run:

```bash
cd $HD
$HD_PY deck.py
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

The shipped unit files assume the `install.sh` layout (`/app/homedeck` +
`/app/homedeck-venv`). If yours differs — e.g. `/opt/homedeck` with a `.venv` —
rewrite the paths as you install them:

```bash
sed -e "s|/app/homedeck-venv/bin/python|$HD_PY|" \
    -e "s|/app/homedeck|$HD|g" \
    $HD/homedeck.service > /etc/systemd/system/homedeck.service

# same for the API server unit, if you use it
sed -e "s|/app/homedeck-venv/bin/python|$HD_PY|" \
    -e "s|/app/homedeck|$HD|g" \
    $HD/homedeck-server.service > /etc/systemd/system/homedeck-server.service
```

Check it landed correctly before enabling:

```bash
grep -E 'WorkingDirectory|ExecStart' /etc/systemd/system/homedeck.service
```

**Then pick one** — these are alternatives, not complements:

**A. Deck only** (configure over SSH):

```bash
# (skip the cp if you used the sed above)
systemctl daemon-reload
systemctl enable --now homedeck
systemctl status homedeck
```

**B. Deck + Home Assistant editing** (what upstream's crontab did):

```bash
# (skip the cp if you used the sed above)
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
cd $HD
git remote set-url origin https://github.com/redphx/homedeck.git
git fetch origin && git reset --hard origin/main
$HD_PY -m pip install -e .
rm -rf $HD/.cache/icons/_generated
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
you edited is the one being read (`$HD/assets/configuration.yml`).

**Dimming at the wrong time** — check `date` on the Pi. The schedule follows the
system clock, and a Pi without network time can be badly off.
