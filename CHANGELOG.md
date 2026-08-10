# 版本记录 / Changelog

## Unreleased

- 将维护锁迁至 root 持有的 `/run/lock/palworld-ops`，使用 `O_NOFOLLOW`、文件描述符所有权校验和 tmpfiles 预创建，阻断低权限服务账号的符号链接提权。
- 恢复暂存区改为 root-only 随机目录，恢复文件使用排他、禁止跟随链接的打开方式。

- 面板默认监听改为 `127.0.0.1`；非回环监听强制安全 Cookie 与 HTTPS 反向代理约束。
- 管理密码启动门禁提高为至少 16 字符。
- 补充会话、配置、备份、恢复确认、HTTP 鉴权与静态资产的标准库回归测试。
- 补齐 SVG、ICO、Apple Touch Icon、Web App Manifest 与 192/512 PNG 图标。
- 新增双语运维、升级、备份、恢复、卸载和排错说明。

- Changed the default panel bind to `127.0.0.1`; non-loopback binds now require secure cookies and an HTTPS reverse-proxy deployment.
- Enforced a minimum 16-character admin password at startup.
- Added standard-library regression coverage for sessions, settings, backups, restore confirmation, HTTP authentication, and static assets.
- Added SVG/ICO favicons, Apple Touch Icon, Web App Manifest, and 192/512 PNG icons.
- Added bilingual operations, upgrade, backup, restore, uninstall, and troubleshooting documentation.
