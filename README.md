# ConfigFlow

**用一个网页管理代理订阅、上网规则和网络设备。**

ConfigFlow 将订阅和节点整理成 Mihomo、Surge、Loon、MosDNS、OxiDNS 可用的配置，也能把配置发送到设备并查看运行状态。代理订阅或节点需要自行准备。

[使用手册](https://thsrite.github.io/configflow/) · [Docker 镜像](https://hub.docker.com/r/thsrite/config-flow) · [反馈问题](https://github.com/thsrite/configflow/issues)

## 可以做什么

- 整理订阅和节点，检查连通性，安排自动更新。
- 设置网站和设备的上网规则，生成配置给手机、电脑或路由器使用。
- 管理 Linux、Docker、RouterOS 上的服务，发送配置、查看日志和运行情况。
- 为两台 OxiDNS 配置共同的 DNS 地址，在其中一台故障时由另一台接替。

<a id="快速开始"></a>
<a id="第一次使用"></a>
## 开始使用

1. [安装管理面板](https://thsrite.github.io/configflow/guide/install)：选择 Docker 或 NAS 部署，设置账号和数据保存目录。
2. [完成首次使用向导](https://thsrite.github.io/configflow/guide/first-run)：填入订阅，选择适合自己的使用方式。
3. [安装和接入设备](https://thsrite.github.io/configflow/guide/agent-install)：按实际设备选择安装组合；只下载配置给客户端使用时可跳过。
4. 先测试一台设备，确认正常后再让其他设备使用。

<a id="详细文档"></a>
## 使用文档

安装步骤、每个页面的按钮和选项、推荐选择及修改方法，都在[完整使用手册](https://thsrite.github.io/configflow/)中。

| 需要什么 | 打开这里 |
| --- | --- |
| 在 RouterOS 中安装 Mihomo 和 OxiDNS | [RouterOS 容器安装](https://thsrite.github.io/configflow/guide/routeros-install) |
| 配置两台 OxiDNS 互为备用 | [OxiDNS 高可用](https://thsrite.github.io/configflow/guide/oxidns-ha) |
| 改端口、备份或升级 | [系统设置](https://thsrite.github.io/configflow/manual/system-settings) |
| 页面打不开、安装或上网失败 | [常见问题](https://thsrite.github.io/configflow/guide/troubleshooting) |

也可以在仓库中阅读[手册目录](doc/README.md)。

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
