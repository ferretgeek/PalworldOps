<p align="center">
  <img src="./docs/images/social-preview.png" alt="帕鲁服务器面板 — 自己开的服，管起来不心慌" width="100%" />
</p>

# 帕鲁服务器面板

中文 · [English](./README_EN.md)

[![CI](https://img.shields.io/github/actions/workflow/status/ferretgeek/palworld-server-panel/ci.yml?branch=main&style=flat-square&label=CI)](https://github.com/ferretgeek/palworld-server-panel/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.10%2B-14354C?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Linux](https://img.shields.io/badge/Linux-systemd-334155?style=flat-square&logo=linux&logoColor=white)](https://systemd.io/)
[![License](https://img.shields.io/badge/License-MIT-0f766e?style=flat-square)](./LICENSE)

> 自己开的幻兽帕鲁服务器，在网页里看状态、改设置、备份和更新——每一步都知道自己在干什么。

## 为什么会需要它

自己在 Linux 上开一个帕鲁服，跑起来其实不难。让人不安的是之后：

- 卡了。是 CPU 满了、内存不够，还是服务器 FPS 掉了？没有面板就只能靠猜。
- 想改一个世界设置。改错了会怎样？改之前有存档吗？
- 该更新了。更新会不会把存档搞坏？坏了怎么回来？
- 备份文件在那儿躺着，但它到底能不能恢复回来？没试过就等于没有。

这个面板把这几件事变成"看得见、能确认、能回退"的操作。它不是一键开服工具——它假设你已经装好了官方专用服务器，帮你安心地管下去。

## 界面

<p align="center">
  <img src="./docs/images/dashboard.png" alt="面板界面（合成数据）" width="100%" />
</p>

预览使用完全合成的数据，不含真实主机名、玩家、地址、世界 ID、备份名或日志。

## 它能做什么

- **一眼看懂现状** — 服务状态、在线人数、服务器 FPS、CPU、内存、磁盘、网络与温度集中在一屏。
- **危险操作有护栏** — 保存、备份、启停、重启、广播和更新都会先检查在线玩家，并显示明确进度。
- **备份是能恢复的备份** — 日 / 周 / 月 / 事件 / 手动 / 更新前六类备份，带成员清单、哈希、容量上限，以及**恢复演练**。
- **世界设置分类可编辑** — 分类编辑 `PalWorldSettings.ini`，高风险字段保持保护；应用前先保存世界、再自动备份。
- **有历史可查** — 性能曲线、运维事件、健康检查、日志、诊断包和 CSV 导出。
- **自动化有边界** — systemd 的健康检查、更新、维护和备份任务全部带 CPU、I/O、内存和磁盘上限，不会为了跑维护把游戏卡死。
- **六套浅色配色 + `#17191d` 深灰暗色**，覆盖完整管理流程。

## 适用边界

**这不是帕鲁服务端的一键安装器。** 它假设你：

- 已经在 Linux（Ubuntu 22.04 / 24.04 或同类 systemd 发行版）上装好了官方专用服务器；
- 愿意采用 `/opt/palworld` 的目录约定；
- 在内网或 VPN 环境下使用。**不要把面板或 REST API 直接暴露到公网。**

## 安装

需要 Python 3.10+、SteamCMD 和已安装的 Palworld Dedicated Server。

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

## 技术上值得一提的地方

**零第三方 Python 依赖。** 面板、CLI 和自动化全部只用标准库。理由很实际：游戏服务器往往是长期不动的机器，一个不需要 `pip install` 的运维层，两年后还能直接跑起来。

**备份带成员清单和哈希，而且能演练恢复。** "备份成功"这句话本身没有意义——所以 `backup verify` 会核对清单和哈希，`restore` 默认是**演练模式**，让你在真的覆盖世界之前先看到会发生什么。

**systemd 自动化全部带资源配额。** 健康检查、更新、维护和备份的 timer/service 都设了 CPU、I/O、内存和磁盘上限。凌晨的备份任务不该让还在玩的人掉线。

**改设置之前先存档。** 应用 `PalWorldSettings.ini` 的改动会触发"先保存世界 → 再备份 → 然后写入"的顺序，高风险字段另有保护。这不是多余的谨慎，是因为改错一个字段可能让世界起不来。

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
