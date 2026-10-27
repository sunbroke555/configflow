# Docker HA 粘贴预检验收（2026-10-27）

功能版本 `1a500bc6b14e553b1c0a66f843b28feaaefc49c0`，部署于测试预览 `http://192.168.0.32:15810`。所有测试、构建与浏览器均在测试服务器执行。

- 相关后端测试 155 项、前端组件与 HA 回归 56 项通过；类型检查、生产构建通过。
- 真实 Linux / Docker 29.9.2 / Compose 5.6.1：默认 Docker daemon 停止时，两种模式均未通过；使用独立本地 rootful daemon（无镜像、无容器）时，真实回环 TCP/UDP 53 占用使 Host 未通过，macvlan 基础检查通过。
- 31 项真实 HTTP 检查通过。rootless、Desktop、缺命令、缺项、截断等边界由明确标注的 fixture 覆盖。额外使用临时 TCP 监听器验证远端 `DOCKER_HOST` 时采集器不发起连接。
- 4 组生产预览浏览器场景通过，覆盖 375/1440 宽度、深浅主题、Host/macvlan、网卡/IP 展示、重复输出、旧版与截断、修改失效、跨步骤保留、超长输入和真实响应延迟后的过期结果保护。无页面错误、无横向溢出。

采集后仅修改解析器以展示网卡/IP，采集脚本 SHA-256 保持 `311f8562e2f83f6aaee5f4223a4975bf204a6be1c0f6acc8a7dc6b374e87bbf1`。最终解析模块 SHA-256 为 `622fea5a71d61c7ea64dfbfb1af1739586cc13f353552782c81d3eb867734b68`；已用最终模块与真实 API 重新核对保存的样本。

临时 daemon 和私有采集目录已清理。默认 Docker/socket 仍为 inactive，原 TCP/UDP 53 监听未改变，两个预览仍返回 HTTP 200，PVE 未修改。

- [检查结果与浏览器记录](oxidns-docker-ha-preflight-2026-10-27.json)
- [清理记录](oxidns-docker-ha-preflight-cleanup-2026-10-27.json)
- [桌面 macvlan 结果](results/oxidns-docker-ha-preflight-2026-10-27/desktop-macvlan-pass.png)
- [手机不完整输出提示](results/oxidns-docker-ha-preflight-2026-10-27/mobile-truncated-unknown.png)

公开证据不含原始采集文本或宿主指纹；截图由 Playwright 原生遮罩隐藏输入区。本轮验证采集、诊断和界面流程，安装与 VIP 切换验证沿用依赖 PR #424 / #422 的验收。

复现脚本为 `scripts/tests/oxidns_docker_preflight_lab.py` 与 `scripts/tests/oxidns_docker_preflight_browser.mjs`，仅在测试服务器运行。先将最终采集模块复制至脚本指定的独立实验目录，依次执行 `collect`、`fixtures`、`remote-probe`、`test-api`；浏览器脚本通过 `CF_PLAYWRIGHT_MODULE` 指定服务器已有 Playwright。结束执行 `cleanup` 并移除私有样本目录。
