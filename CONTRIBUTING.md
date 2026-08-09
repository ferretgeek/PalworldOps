# 参与贡献 / Contributing

Palworld Ops 直接控制游戏服务和存档，改动应保持小、可回滚并有测试。提交前请勿加入真实地址、玩家、存档、密码、日志、诊断包或服务器路径。

Palworld Ops controls a live game service and save data. Keep changes small, recoverable, and covered by tests. Never add real addresses, players, saves, passwords, logs, diagnostics, or deployment paths.

## 检查 / Checks

```bash
python3 -m py_compile palworldctl.py palworld-panel.py
python3 -m unittest discover -s tests -v
node --check panel/panel.js
bash -n backup-after-stop.sh
```

涉及 systemd、SteamCMD、真实备份或恢复的改动，还必须在没有私人数据的临时 Linux 环境验证。UI 改动需检查桌面、390px 窄屏、七套主题、键盘和减少动态效果。

Changes involving systemd, SteamCMD, real backup, or restore behavior also require a disposable Linux environment without private data. UI changes must be checked at desktop and 390px widths across all seven themes, with keyboard and reduced-motion support.
