# 安全策略 / Security policy

Palworld Ops 会执行停服、更新、备份和恢复等高权限操作。面板默认只监听 `127.0.0.1`。请通过 SSH 隧道、VPN 或带 TLS 的反向代理访问，不要把 `8213`、Palworld REST API 或凭据文件直接暴露到公网。

Palworld Ops performs privileged operations such as stopping, updating, backing up, and restoring a server. The panel binds to `127.0.0.1` by default. Access it through an SSH tunnel, VPN, or TLS reverse proxy; never expose port `8213`, the Palworld REST API, or credential files directly to the internet.

非回环监听必须显式设置 `PALWORLD_PANEL_SECURE_COOKIE=true`，否则面板拒绝启动；这仍不替代 TLS。管理密码必须至少 16 个字符，会话只持久化不可逆 SHA-256 摘要。

A non-loopback bind requires `PALWORLD_PANEL_SECURE_COOKIE=true` or startup fails; this does not replace TLS. The admin password must contain at least 16 characters, and persistent sessions store only irreversible SHA-256 digests.

报告漏洞时请使用 GitHub 的 **Security → Report a vulnerability**，不要在公开 Issue 中附上真实地址、日志、诊断包、Cookie、存档或密码。

Please use GitHub **Security → Report a vulnerability**. Never attach real addresses, logs, diagnostics, cookies, saves, or passwords to a public issue.
