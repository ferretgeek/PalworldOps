# Palworld Server Management Panel

[中文](./README.md) · English

Manage an existing Palworld server in your browser: check its status, edit world settings, back up saves, and install game updates.

**Requirements:** a Linux host with the official dedicated server already installed, systemd, Python 3.10+, and SteamCMD. Deploy the panel from source.

[Supported setup](#scope) · [Install the panel](#installation) · [Daily commands](#daily-commands) · [Backup and restore](./docs/OPERATIONS.md)

## What it does

- **Check server status:** service state, player count, server FPS, CPU, memory, disk, network, and temperature in one view.
- **Run daily operations:** save, backup, start/stop, restart, broadcast, and update with online-player checks and visible progress.
- **Back up and rehearse restores:** daily, weekly, monthly, event, manual, and pre-update backups with file manifests, hashes, capacity caps, and restore dry runs.
- **Edit world settings:** categorized `PalWorldSettings.ini` editing with protected high-risk fields; save the world and take a backup before applying changes.
- **Review operations:** performance charts, events, health checks, logs, diagnostic bundles, and CSV export.
- **Schedule maintenance:** systemd health, update, maintenance, and backup jobs with CPU, I/O, memory, and disk limits.
- **Choose a theme:** six light palettes and a `#17191d` deep-gray dark mode throughout the panel.

## Interface

<p align="center">
  <img src="./docs/images/dashboard.png" alt="Dashboard with synthetic data" width="100%" />
</p>

The preview uses entirely synthetic data — no real host, player, address, world ID, backup name, or log.

## Scope

Before installing the panel, confirm that you:

- already have the official dedicated server running on Linux (Ubuntu 22.04 / 24.04 or a comparable systemd distribution);
- are willing to adopt the `/opt/palworld` directory convention;
- will run it on a trusted LAN or VPN. **Do not expose the panel or REST API directly to the internet.**

## Installation

Requires Python 3.10+, SteamCMD, and an installed Palworld dedicated server. Download or clone this repository onto the server and run the following commands from its root. No ready-made installer is published.

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

## Technical details

**Zero third-party Python dependencies.** The panel, CLI, and automation use the standard library only. These components need no third-party packages installed through `pip install`.

**Backups carry manifests and hashes, and restore can be rehearsed.** `backup verify` checks the manifest and hashes, and `restore` defaults to a **dry run** that shows you what would happen before anything overwrites a world.

**Every systemd job has resource quotas.** Health, update, maintenance, and backup units all set CPU, I/O, memory, and disk limits. These quotas limit the resource impact of maintenance on the game process.

**Settings changes save first.** Applying `PalWorldSettings.ini` edits runs save world → backup → write, in that order, with high-risk fields additionally protected. Invalid settings can prevent a world from starting.

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
