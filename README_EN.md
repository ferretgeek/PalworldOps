<p align="center">
  <img src="./docs/images/social-preview.png" alt="Palworld server panel — run your own server without the dread" width="100%" />
</p>

# Palworld server panel

[中文](./README.md) · English

[![CI](https://img.shields.io/github/actions/workflow/status/ferretgeek/palworld-server-panel/ci.yml?branch=main&style=flat-square&label=CI)](https://github.com/ferretgeek/palworld-server-panel/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.10%2B-14354C?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Linux](https://img.shields.io/badge/Linux-systemd-334155?style=flat-square&logo=linux&logoColor=white)](https://systemd.io/)
[![License](https://img.shields.io/badge/License-MIT-0f766e?style=flat-square)](./LICENSE)

> Status, world settings, backups, and updates for a Palworld server you host — with every step visible, confirmable, and reversible.

## Why this exists

Standing up a Palworld dedicated server on Linux isn't hard. What's uncomfortable is everything after that:

- It's lagging. Is the CPU saturated, is memory tight, or did server FPS drop? Without a panel you're guessing.
- You want to change a world setting. What happens if it's wrong? Is there a save from before?
- There's an update. Will it break the world? If it does, how do you get back?
- A backup file is sitting right there — but will it actually restore? Untested means nonexistent.

This panel turns those four things into operations you can see, confirm, and undo. It is not a one-click installer — it assumes the official dedicated server is already installed and helps you live with it.

## Interface

<p align="center">
  <img src="./docs/images/dashboard.png" alt="Dashboard with synthetic data" width="100%" />
</p>

The preview uses entirely synthetic data — no real host, player, address, world ID, backup name, or log.

## What it does

- **See the current state** — service status, player count, server FPS, CPU, memory, disk, network, and temperature on one screen.
- **Guardrails on dangerous actions** — save, backup, start/stop, restart, broadcast, and update all check for online players first and show explicit progress.
- **Backups that actually restore** — daily, weekly, monthly, event, manual, and pre-update backups, each with a member manifest, hashes, capacity caps, and a **restore dry run**.
- **Categorized world settings** — edit `PalWorldSettings.ini` by category with high-risk fields protected; applying changes saves the world and takes a backup first.
- **History you can query** — performance charts, operation events, health checks, logs, diagnostic bundles, and CSV export.
- **Bounded automation** — every systemd health, update, maintenance, and backup job carries CPU, I/O, memory, and disk limits, so maintenance never freezes the people still playing.
- **Six light palettes plus a `#17191d` deep-gray dark mode**, across the entire admin flow.

## Scope

**This is not a Palworld server installer.** It assumes you:

- already have the official dedicated server running on Linux (Ubuntu 22.04 / 24.04 or a comparable systemd distribution);
- are willing to adopt the `/opt/palworld` directory convention;
- will run it on a trusted LAN or VPN. **Do not expose the panel or REST API directly to the internet.**

## Installation

Requires Python 3.10+, SteamCMD, and an installed Palworld dedicated server.

```bash
sudo install -d -m 0755 /opt/palworld/bin /opt/palworld/panel /opt/palworld/secrets /opt/palworld/state
sudo install -m 0755 palworldctl.py /opt/palworld/bin/palworldctl
sudo install -m 0755 palworld-panel.py /opt/palworld/bin/palworld-panel
sudo cp -a panel/. /opt/palworld/panel/
sudo install -m 0600 .env.example /etc/palworld-ops.env
sudo sh -c 'umask 077; printf "%s" "REPLACE_WITH_AT_LEAST_16_RANDOM_CHARACTERS" > /opt/palworld/secrets/admin-password'
```

**Review the paths and resource limits in every `.service` and `.timer` first**, then install the units you want:

```bash
sudo install -m 0644 palworld.service palworld-panel.service /etc/systemd/system/
sudo install -m 0644 palworld-health.* palworld-update.* /etc/systemd/system/
sudo install -m 0644 palworld-backup-* palworld-maintenance.* /etc/systemd/system/
sudo install -m 0644 palworld-ops-tmpfiles.conf /etc/tmpfiles.d/palworld-ops.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/palworld-ops.conf
sudo systemctl daemon-reload
sudo systemctl enable --now palworld.service palworld-panel.service
sudo systemctl enable --now palworld-health.timer palworld-update.timer
```

The panel binds to `127.0.0.1:8213` by default. Open `http://127.0.0.1:8213` locally; for remote administration prefer an SSH tunnel:

```bash
ssh -L 8213:127.0.0.1:8213 your-server
```

Or configure an HTTPS reverse proxy as documented in the [operations guide](./docs/OPERATIONS.md). The username is `admin` and the password comes from `/opt/palworld/secrets/admin-password`, which must contain **at least 16 characters**.

## Daily commands

```bash
sudo /opt/palworld/bin/palworldctl status
sudo /opt/palworld/bin/palworldctl health
sudo /opt/palworld/bin/palworldctl api players
sudo /opt/palworld/bin/palworldctl backup create --kind manual
sudo /opt/palworld/bin/palworldctl backup verify latest
sudo /opt/palworld/bin/palworldctl update          # check only
sudo /opt/palworld/bin/palworldctl update --apply  # save, back up, then update
```

> **Restore is a dry run by default.** Only `--apply` overwrites the active world, discarding all progress made after the chosen backup.

## Worth noting technically

**Zero third-party Python dependencies.** The panel, CLI, and automation use the standard library only. The reason is practical: game servers tend to be machines nobody touches for a year, and an operations layer that never needs `pip install` still starts two years from now.

**Backups carry manifests and hashes, and restore can be rehearsed.** "Backup succeeded" means nothing on its own, so `backup verify` checks the manifest and hashes, and `restore` defaults to a **dry run** that shows you what would happen before anything overwrites a world.

**Every systemd job has resource quotas.** Health, update, maintenance, and backup units all set CPU, I/O, memory, and disk limits. A 4 a.m. backup shouldn't drop the people still playing.

**Settings changes save first.** Applying `PalWorldSettings.ini` edits runs save world → backup → write, in that order, with high-risk fields additionally protected. That isn't excess caution: one wrong field can leave a world that won't boot.

**The maintenance lock lives in a root-owned directory.** It's at `/run/lock/palworld-ops`, not somewhere the `palworld` user can write — otherwise the game process itself could tamper with operational state. Run `systemd-tmpfiles --create` once after upgrading.

## Verification

```bash
python3 -m py_compile palworldctl.py palworld-panel.py
python3 -m unittest discover -s tests -v
node --check panel/panel.js
bash -n backup-after-stop.sh
```

Windows can validate syntax and the front end, but systemd, permissions, SteamCMD, real saves, backups, restores, and players actually joining **must** be verified on the target Linux host.

## Security

- Never commit `/opt/palworld/secrets`, saves, databases, backups, sessions, diagnostic bundles, or logs.
- Never stop, update, restore, or apply restart-requiring settings while players are online.
- Restrict production deployments to trusted networks; use a VPN or an authenticated HTTPS entry point for remote access.
- For security issues, read [SECURITY.md](./SECURITY.md).

## More documentation

[Installation, architecture, upgrade, backup, restore, health checks, uninstall, troubleshooting](./docs/OPERATIONS.md) · [Changelog](./CHANGELOG.md) · [Contributing](./CONTRIBUTING.md)

## Stack

`Python stdlib` · `HTML` · `CSS` · `JavaScript` · `SQLite` · `systemd` · `SteamCMD`

## License and disclaimer

MIT License — see [LICENSE](./LICENSE).

Unofficial community project with no affiliation with, authorization from, or endorsement by Pocketpair. Palworld and related names and assets belong to their respective owners.
