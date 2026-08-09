# Palworld Ops 运维手册 / Operations Guide

## 架构 / Architecture

`palworld-panel.py` 只提供认证后的 Web 控制面；所有高权限动作委托给 `palworldctl.py`。管理器以参数数组调用 `systemctl`、SteamCMD 和系统工具，使用单一维护锁串行化更新、备份、恢复与设置写入。状态保存在 `/opt/palworld/state`，受管备份保存在 `/opt/palworld/backups/managed`，游戏存档格式不由面板改写。

`palworld-panel.py` provides the authenticated web control plane and delegates privileged actions to `palworldctl.py`. The manager invokes system tools with argument arrays and serializes updates, backups, restores, and setting writes with one maintenance lock. State lives under `/opt/palworld/state`, managed backups under `/opt/palworld/backups/managed`, and the panel never rewrites the game save format.

## 本地与服务器入口 / Local and server access

- 同机访问：保持 `PALWORLD_PANEL_HOST=127.0.0.1`，打开 `http://127.0.0.1:8213`。
- 远程临时访问：`ssh -L 8213:127.0.0.1:8213 SERVER`，然后打开本机同一地址。
- 长期服务器入口：让 Nginx/Caddy 代理 `127.0.0.1:8213`，外层启用 TLS、访问控制和限流，并设置 `PALWORLD_PANEL_SECURE_COOKIE=true`。
- 不推荐直接监听非回环地址；若显式这样做，缺少安全 Cookie 配置时进程会拒绝启动。

- Same-host access: keep `PALWORLD_PANEL_HOST=127.0.0.1` and open `http://127.0.0.1:8213`.
- Temporary remote access: run `ssh -L 8213:127.0.0.1:8213 SERVER`, then use the same local URL.
- Persistent server access: proxy `127.0.0.1:8213` through Nginx or Caddy with TLS, access control, and rate limiting; set `PALWORLD_PANEL_SECURE_COOKIE=true`.
- Direct non-loopback binding is discouraged and fails closed without secure-cookie configuration.

## 安装与配置 / Install and configure

按 README 安装文件后，检查 `/etc/palworld-ops.env`、所有 unit 中的路径和 `/opt/palworld/secrets/admin-password`。密码文件权限应为 `0600`，内容至少 16 字符。运行：

After installing the files as described in the README, review `/etc/palworld-ops.env`, every unit path, and `/opt/palworld/secrets/admin-password`. The password file should be mode `0600` and contain at least 16 characters. Run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now palworld.service palworld-panel.service
sudo /opt/palworld/bin/palworldctl health --json
curl --fail --silent http://127.0.0.1:8213/api/session
```

## 升级与回滚 / Upgrade and rollback

1. 创建并校验手动备份：`sudo palworldctl backup create --kind manual`、`sudo palworldctl backup verify latest`。
2. 备份 `/etc/palworld-ops.env`、unit 文件、面板和两个 Python 入口。
3. 停止面板，替换程序文件，运行本仓库验证命令，再执行 `daemon-reload` 并启动。
4. 健康检查失败时恢复同一批旧文件；不要只回滚 Python 而保留不匹配的 unit 或静态资源。

1. Create and verify a manual backup.
2. Back up the environment file, units, static panel, and both Python entry points.
3. Stop the panel, replace files, run repository checks, then reload systemd and start it.
4. On failure restore the complete matching file set, not only one Python file.

## 备份与恢复 / Backup and restore

```bash
sudo palworldctl backup create --kind manual
sudo palworldctl backup verify latest
sudo palworldctl restore latest          # dry run
sudo palworldctl restore latest --apply  # destructive, creates a pre-restore backup
```

恢复会验证路径、成员类型、每个文件哈希、总指纹和必要内容；失败时尝试恢复原目录并复核服务。恢复前仍应另行复制最新受管备份到另一磁盘。

Restore validates paths, member types, per-file hashes, the combined fingerprint, and required content. A failed apply attempts to restore the previous directories and re-check the service. Keep an additional copy of the latest managed backup on another disk before applying.

## 健康检查与重启恢复 / Health and restart recovery

```bash
sudo systemctl status palworld.service palworld-panel.service
sudo palworldctl status --json
sudo palworldctl health --json
sudo palworldctl backup verify latest
systemctl list-timers 'palworld-*' --all
```

面板和健康任务重启后从 `/opt/palworld/state` 恢复必要状态；手动停止标记会阻止健康任务擅自拉起服务器。不要手工删除状态文件来“修复”告警。

The panel and health task recover required state from `/opt/palworld/state`. A manual-stop marker prevents automatic restarts. Do not delete state files merely to hide an alert.

## 卸载 / Uninstall

先停用并删除 `palworld-panel` 及 `palworld-*` 自动任务，再执行 `daemon-reload`。只在确认备份后删除 `/opt/palworld/bin/palworldctl`、面板静态文件和状态数据库。游戏目录、存档、秘密与受管备份默认保留，除非操作者另行明确删除。

Disable and remove the panel and `palworld-*` automation units, then reload systemd. Remove the manager, static panel, and state database only after backups are confirmed. The game directory, saves, secrets, and managed backups remain by default unless the operator explicitly removes them.

## 故障排查 / Troubleshooting

- 启动提示密码过短或不可读：检查密码文件内容、所有者和 `0600` 权限。
- 非回环监听被拒绝：恢复 `127.0.0.1`，或确认 HTTPS 代理后设置安全 Cookie。
- 页面可开但状态为空：运行 `palworldctl health --json`，确认 REST API 只在本机可达且密码一致。
- 操作一直繁忙：检查维护锁对应的 systemd/SteamCMD 进程，不要直接删除仍在使用的锁。
- 备份验证失败：保留原文件，不执行恢复；从另一份已验证备份重新开始。

- Password startup failure: inspect the password contents, owner, and `0600` mode.
- Non-loopback bind rejected: return to `127.0.0.1`, or enable secure cookies only after configuring HTTPS.
- Empty status: run `palworldctl health --json` and verify the local REST API and password.
- Operation remains busy: inspect the corresponding systemd or SteamCMD process; never delete an active lock.
- Backup verification failure: preserve the archive and do not restore it; use another verified copy.
