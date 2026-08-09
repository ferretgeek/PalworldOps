# 版本记录 / Changelog

## Unreleased

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
