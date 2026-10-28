# ConfigFlow

**在一个网页里整理代理订阅、安排上网规则，并管理家里的网络设备。**

ConfigFlow 可以帮你把订阅和节点整理成 Mihomo、Surge、Loon、MosDNS 或 OxiDNS 能用的配置。也可以把配置发到设备上，查看运行情况。你仍需准备自己的订阅或节点。

第一次用，不必先理解所有网络术语。按下面的顺序走，遇到不明白的按钮再查对应页面。

<a id="快速开始"></a>
<a id="第一次使用"></a>
## 从这里开始

1. **[选好安装方式并安装面板](doc/guide/install.md)**：Docker、NAS 怎么填；端口、数据目录和账号各是什么。
2. **[完成首次使用向导](doc/guide/first-run.md)**：先选你希望达到的效果，再填订阅、规则和设备。每个选择都说明打开与关闭的结果。
3. **[按设备组合安装](doc/guide/agent-install.md)**：只生成配置、单独安装一个程序、Mihomo + OxiDNS 同机、已有程序接管，分别该选什么。
4. **检查一台设备，再扩展到全家**：确认能上网、规则正确、关闭或重启后能恢复，再调整全家设备。

**已有安装？** 从 [完整使用手册](doc/README.md) 直接找到当前页面。准备让两台 OxiDNS 互相接替？看 [OxiDNS 双机备用指南](doc/guide/oxidns-ha.md)。

## 我想做什么

| 你的目标 | 从哪里开始 |
| --- | --- |
| 把订阅变成手机或电脑能用的配置 | [订阅来源](doc/manual/subscriptions.md) → [配置生成](doc/manual/generate.md) |
| 给不同设备安排不同上网方式 | [客户端设备](doc/manual/client-devices.md) → [上游路由](doc/manual/routers.md) |
| 自动选速度更好的节点 | [节点库](doc/manual/nodes.md) → [策略组](doc/manual/proxy-groups.md) |
| 指定哪些网站直连、哪些走代理 | [规则库](doc/manual/rule-library.md) → [策略规则](doc/manual/rules.md) |
| 查看设备有没有正常工作 | [Agent](doc/manual/agents.md)、[Mihomo 面板](doc/manual/mihomo-panel.md)、[OxiDNS 面板](doc/manual/oxidns-panel.md) |
| 一台 DNS 设备坏了，另一台自动接替 | [OxiDNS 双机备用](doc/guide/oxidns-ha.md) |
| 备份、升级、改端口，或者查错误 | [系统设置](doc/manual/system-settings.md)、[常见问题](doc/guide/troubleshooting.md) |

## 先分清这三样东西

- **ConfigFlow 面板**：你现在要安装的管理网页。它负责整理配置、下发配置和查看状态。
- **Mihomo / OxiDNS 等程序**：真正处理网络连接或“网站名称查询”的程序。可以与面板装在同一台机器，也可以装在其他设备。
- **Agent**：安装在被管理设备上的“小助手”，帮助面板更新配置、控制程序和读取状态。只下载配置给客户端使用时，不必安装。

安装了面板，不等于全家设备已经开始通过它上网；还需要按所选方案连接设备。安装指引会分开说明每一步。

<a id="详细文档"></a>
## 文档怎么读

手册按 **安装 → 首次配置 → 设备组合 → 页面操作 → 排查** 排列。页面名称、按钮名称与实际界面对应；推荐选择和软件原本的默认值会分开写。

- [文档目录与阅读路线](doc/README.md)
- [常见名词，用日常话解释](doc/guide/terms.md)
- [安装组合与选项说明](doc/guide/agent-install.md)
- [OxiDNS 高可用支持组合与逐步配置](doc/guide/oxidns-ha.md)

维护者需要把手册发布成网页时，按 [GitHub Pages 开启与自动发布步骤](doc/guide/github-pages.md) 完成首次设置；之后相关文档更新合并到 `main` 会自动发布。

截图使用隔离演示数据，不含真实订阅或生产设备信息。本文档按 **2026-10-28 的项目代码**核对；旧镜像缺少某项按钮时，先按 [备份与升级](doc/manual/system-settings.md#备份与升级) 更新，不能只靠刷新浏览器增加功能。

<a id="部署参数"></a>
<a id="备份与升级"></a>
<a id="常见问题"></a>
## 部署与维护

环境变量说明见 [部署参数](doc/guide/install.md#deployment-parameters)；升级和迁移前先读 [备份与升级](doc/manual/system-settings.md#备份与升级)。遇到访问、安装或发布问题，按 [排查顺序](doc/guide/troubleshooting.md) 处理。

## 项目链接

[Docker 镜像](https://hub.docker.com/r/thsrite/config-flow) · [使用手册](doc/README.md) · [反馈问题](https://github.com/thsrite/configflow/issues) · [Telegram 交流群](https://t.me/+fLt02_w38205OTNh)

## 许可证

主应用使用 [MIT License](LICENSE)。

独立节点测速组件复用 Mihomo 协议实现，按 [GPLv3](backend/node-probe/LICENSE) 分发，相关源码和依赖位于 `backend/node-probe/`。

## 交流

- [Telegram 交流群](https://t.me/+fLt02_w38205OTNh)

---

## ❤️ 赞助支持

如果这个项目对你有帮助，欢迎赞助支持开发者持续维护！

<a href="https://ifdian.net/order/create?user_id=1ed9d800ada811f0bdbe52540025c377&remark=&affiliate_code=" target="_blank">
  <img src="https://img.shields.io/badge/爱发电-赞助支持-ff69b4?style=for-the-badge&logo=heart" alt="爱发电赞助">
</a>

### 🏆 发电排行榜

感谢以下用户的赞助支持！

<table>
<tr>
<td align="center">
<img src="https://pic1.afdiancdn.com/default/avatar/avatar-purple.png?imageView2/1/w/50/h/50" width="50" height="50" style="border-radius: 50%;" /><br/>
<sub><b>爱发电用户_740f4</b></sub>
</td>
</tr>
</table>

---

<div align="center">

**⭐ 如果觉得有用，请给个 Star 支持一下！**

Made with ❤️ by [thsrite](https://github.com/thsrite)

</div>
