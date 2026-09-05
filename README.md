# 帕鲁服务器管理面板

中文 · [English](./README_EN.md)

在浏览器里管理已有的幻兽帕鲁服务器：查看运行状态、修改世界设置、备份存档和更新游戏。

**使用条件：** 已安装官方专用服务器的 Linux 主机，需要 systemd、Python 3.10+ 和 SteamCMD；本项目需自行部署。

[适用环境](#适用边界) · [安装面板](#安装) · [日常命令](#日常命令) · [备份与恢复](./docs/OPERATIONS.md)

## 它能做什么

- **查看运行状态**：服务状态、在线人数、服务器 FPS、CPU、内存、磁盘、网络与温度集中展示。
- **执行日常操作**：保存、备份、启停、重启、广播和更新前检查在线玩家，并显示操作进度。
- **备份和恢复演练**：支持日、周、月、事件、手动、更新前六类备份，带文件清单、哈希和容量上限，可先演练恢复过程。
- **编辑世界设置**：分类编辑 `PalWorldSettings.ini`，保护高风险字段；应用前先保存世界并自动备份。
- **查询运维记录**：性能曲线、操作事件、健康检查、日志、诊断包和 CSV 导出。
- **定时维护**：systemd 健康检查、更新、维护和备份任务均设有 CPU、I/O、内存和磁盘上限。
- **切换主题**：六套浅色配色与 `#17191d` 深灰暗色，覆盖完整管理流程。

## 界面

<p align="center">
  <img src="./docs/images/dashboard.png" alt="面板界面（合成数据）" width="100%" />
</p>

预览使用完全合成的数据，不含真实主机名、玩家、地址、世界 ID、备份名或日志。

## 适用边界

安装面板前，请确认：

- 已经在 Linux（Ubuntu 22.04 / 24.04 或同类 systemd 发行版）上装好了官方专用服务器；
- 愿意采用 `/opt/palworld` 的目录约定；
- 在内网或 VPN 环境下使用。**不要把面板或 REST API 直接暴露到公网。**

## 安装

需要 Python 3.10+、SteamCMD 和已安装的 Palworld Dedicated Server。将本仓库源码下载或克隆到服务器，在源码根目录运行以下命令；目前没有现成安装包。

```bash
sudo install -d -m 0755 /opt/palworld/bin /opt/palworld/panel /opt/palworld/secrets /opt/palworld/state
sudo install -m 0755 palworldctl.py /opt/palworld/bin/palworldctl
sudo install -m 0755 palworld-panel.py /opt/palworld/bin/palworld-panel
sudo cp -a panel/. /opt/palworld/panel/
sudo install -m 0600 .env.example /etc/palworld-ops.env
sudo sh -c 'umask 077; printf "%s" "REPLACE_WITH_AT_LEAST_16_RANDOM_CHARACTERS" > /opt/palworld/secrets/admin-password'
```

**先逐项核对 `.service` 与 `.timer` 里的路径和资源上限**，再安装需要的单元：

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

面板默认只监听 `127.0.0.1:8213`。本地直接打开 `http://127.0.0.1:8213`；远程运维优先用 SSH 隧道：

```bash
ssh -L 8213:127.0.0.1:8213 你的服务器
```

或按[运维手册](./docs/OPERATIONS.md)配 HTTPS 反向代理。账号是 `admin`，密码取自 `/opt/palworld/secrets/admin-password`，**至少 16 个字符**。

## 日常命令

```bash
sudo /opt/palworld/bin/palworldctl status
sudo /opt/palworld/bin/palworldctl health
sudo /opt/palworld/bin/palworldctl api players
sudo /opt/palworld/bin/palworldctl backup create --kind manual
sudo /opt/palworld/bin/palworldctl backup verify latest
sudo /opt/palworld/bin/palworldctl update          # 只检查
sudo /opt/palworld/bin/palworldctl update --apply  # 先保存、再备份、然后更新
```

> **恢复默认只演练。** 加 `--apply` 才会真的覆盖活动世界，并丢失备份时间点之后的全部进度。

## 技术说明

**零第三方 Python 依赖。** 面板、CLI 和自动化全部只用标准库。运行这些组件不需要 `pip install` 第三方包。

**备份带成员清单和哈希，而且能演练恢复。** `backup verify` 会核对清单和哈希，`restore` 默认是**演练模式**，让你在真的覆盖世界之前先看到会发生什么。

**systemd 自动化全部带资源配额。** 健康检查、更新、维护和备份的 timer/service 都设了 CPU、I/O、内存和磁盘上限。这些配额用于限制维护任务对游戏进程的资源影响。

**改设置之前先存档。** 应用 `PalWorldSettings.ini` 的改动会触发"先保存世界 → 再备份 → 然后写入"的顺序，高风险字段另有保护。错误配置可能导致世界无法启动。

**维护锁放在 root 持有的目录。** 锁在 `/run/lock/palworld-ops`，不在 `palworld` 用户可写的位置——否则游戏进程本身就能篡改运维状态。升级后记得先跑一次 `systemd-tmpfiles --create`。

## 验证

```bash
python3 -m py_compile palworldctl.py palworld-panel.py
python3 -m unittest discover -s tests -v
node --check panel/panel.js
bash -n backup-after-stop.sh
```

Windows 上可以完成语法和前端检查，但 systemd、权限、SteamCMD、真实的保存 / 备份 / 恢复以及玩家进服**必须**在目标 Linux 主机上验证。

## 安全

- 不要提交 `/opt/palworld/secrets`、存档、数据库、备份、会话、诊断包或日志。
- 不要在有玩家在线时停服、更新、恢复或应用需要重启的设置。
- 生产部署限制到可信网段；需要远程访问时用 VPN 或经过鉴权的 HTTPS 入口。
- 安全问题请阅读 [SECURITY.md](./SECURITY.md)。

## 更多文档

[安装、架构、升级、备份、恢复、健康检查、卸载与排错](./docs/OPERATIONS.md) · [版本记录](./CHANGELOG.md) · [参与贡献](./CONTRIBUTING.md)

## 技术栈

`Python 标准库` · `HTML` · `CSS` · `JavaScript` · `SQLite` · `systemd` · `SteamCMD`

## 许可与声明

MIT License，见 [LICENSE](./LICENSE)。

非官方社区项目，与 Pocketpair 没有隶属、授权或背书关系。Palworld / 幻兽帕鲁及相关名称与资产归其权利人所有。
