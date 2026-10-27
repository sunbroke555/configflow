# NAS 粘贴预检修复验收（2026-10-27）

功能版本 `dcbdf2c4632fa72b548facc8757cd6980b46d394`，测试预览为 `http://192.168.0.32:15810`。

- 测试服务器完成后端 191 项、前端 57 项、类型检查与生产构建，均通过。
- 真实 Linux 采集：正常 ss 与 PATH 屏蔽 ss 后的 /proc 回退，各识别到相同的两个 TCP/UDP 53 绑定地址。使用 GNU awk；未执行实际群晖/BusyBox 测试。
- 用户群晖输出采用明确标注的脱敏重建：保留字段值及网卡结构，替换宿主指纹并重排横向空白，另覆盖 Markdown 行末反斜杠。两份均可显示 12 项结果；Host 的旧端口信息为 unknown，macvlan 不因宿主端口未知受阻，Compose 2.20 显示维护工具提醒。
- 8 项真实认证 API、2 项生产预览浏览器场景通过。1440 浅色及 375 深色均无横向溢出、无页面错误，能切换 Host/macvlan 后重新判断。
- 未连接用户 NAS、未启动 Docker daemon、未改 PVE。默认 Docker/socket 仍为 inactive，原 53 监听不变，15800/15810 均返回 HTTP 200；私有采集目录已清理。

采集脚本 SHA-256：`df15bc95103e1089e0f3e32683a8fc33747b6815394d4949d1c99354b6af9933`。最终模块 SHA-256：`bbeaeea6673c26e10aa082e5e72d9712d312fafe30b6eb10c01cd39c438d644c`。

[紧凑结果](oxidns-docker-preflight-nas-2026-10-27.json) · [清理记录](oxidns-docker-preflight-nas-cleanup-2026-10-27.json) · [桌面截图](results/oxidns-docker-preflight-nas-2026-10-27/desktop-nas-itemized.png) · [手机截图](results/oxidns-docker-preflight-nas-2026-10-27/mobile-nas-markdown.png)。截图用 Playwright 原生遮罩隐藏原始输入，公开报告无机器指纹。

复现入口：`scripts/tests/oxidns_docker_preflight_nas_lab.py` 与 `scripts/tests/oxidns_docker_preflight_nas_browser.mjs`，仅在测试服务器运行。此轮不改变 HA 对通配 UDP 53 与 VIP DNS 探测的要求。
