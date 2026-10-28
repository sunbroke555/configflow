# 技术资料与文档维护

普通用户从 [安装](guide/install.md) 或 [页面目录](README.md#页面介绍) 开始即可。本页保留实现说明、旧版迁移和文档维护入口，方便需要进一步核对的人。

## 进一步阅读

| 需要查什么 | 对应资料 |
| --- | --- |
| Docker 部署、完整目录备份 | [部署技术说明](https://github.com/thsrite/configflow/blob/main/doc/module/deployment.md) |
| 旧版单文件 / 多目录配置升级和回退 | [多配置管理与迁移](https://github.com/thsrite/configflow/blob/main/doc/design/multi-config-management.md#升级备份与恢复) |
| OxiDNS 全部参数和配置包 | [OxiDNS 技术说明](https://github.com/thsrite/configflow/blob/main/docs/OXIDNS.md) |
| Agent 安装、监控和回滚 | [Agent 技术说明](https://github.com/thsrite/configflow/blob/main/docs/AGENT_OXIDNS.md) |
| Linux 网关实现与迁移边界 | [Linux 网关](https://github.com/thsrite/configflow/blob/main/docs/LINUX_GATEWAY.md) |
| RouterOS 容器的完整安装命令 | [RouterOS 安装](https://github.com/thsrite/configflow/blob/main/docs/CONFIGFLOW_AGENT_ROUTEROS_INSTALL.md) |
| 节点测速原理 | [独立节点测速](https://github.com/thsrite/configflow/blob/main/docs/NODE_LATENCY.md) |
| 接入 AI / MCP 客户端 | [MCP 服务](https://github.com/thsrite/configflow/blob/main/doc/module/mcp.md) |
| 项目测试 | [CI 与测试](https://github.com/thsrite/configflow/blob/main/scripts/ci/README.md) |

旧的 `doc/module/` 和 `docs/` 专题继续保留。它们面向深入配置与维护；新手操作以本手册为入口，遇到旧截图或名称差异时，以当前界面和版本为准。

## 部署参数

修改环境变量后需要重新创建容器。普通用户只需安装页中的基本项，其他参数没有需要时保留。

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | 空 | 两项同时非空才启用网页登录 |
| `JWT_SECRET_KEY` | 进程启动时随机生成 | 固定设置可避免重启使原登录会话失效；如改值需重新登录 |
| `DATA_DIR` | 镜像内 `/data` | 数据保存位置，需持久挂载 |
| `TZ` | 镜像内 `Asia/Shanghai` | 服务时区 |
| `CONFIGFLOW_HTTP_THREADS` | `8` | 同时处理请求的后端工作线程，允许 `1–64`；不是 CPU 核心数开关 |
| `CONFIGFLOW_WEB_PORT` | 未指定则读保存值，再退到 `80` | 覆盖 Docker Web 端口；设置后网页端口只读，见 [端口说明](manual/system-settings.md) |
| `LOG_LEVEL` | `INFO` | 日志级别；`DEBUG` 会启用开发调试模式，不建议普通部署长期开启 |

## 文档排版与范围

本手册参考 [MatchRouter 文档](https://www.matchrouter.com/docs/overview) 的阅读组织方式：左侧按任务分组，中间按步骤说明，右侧列本页目录，底部连接上一篇和下一篇。内容根据 ConfigFlow 自身的代码和界面编写，不照搬其他软件的安装步骤或功能。

文件组织：

- 根目录 `README.md`：项目介绍和最短阅读路线。
- `doc/README.md`：完整手册目录，与应用侧栏逐项对应。
- `doc/guide/`：面板安装、首次向导、设备组合、HA、常见问题和名词解释。
- `doc/manual/`：17 个侧栏页面，加一篇共用核心设置说明。
- `doc/.vitepress/`：文档站配置；Markdown 也能直接在 GitHub 阅读。
- `doc/img/guide/`：隔离演示实例的真实截图和采集记录。

本轮依据 `6625c51a`（2026-10-28）及其对应界面核对。版本继续更新时，需要同步检查选项、默认值和操作效果；不要只把文档日期往后改。

## 怎样更新一篇操作说明

1. 先写用户要达到的结果，并说明入口与当前配置空间。
2. 按真实步骤写操作；每个开关区分“代码初始值”“推荐场景”“打开后”“关闭后”。
3. 明确何时只是保存草稿，何时开始下载、写文件、发布或改变真实设备。
4. 说明成功标志、失败后的下一步，以及取消勾选是否会撤销已经做过的事。
5. 用新手能理解的描述解释界面名词；保留按钮原名，方便对照寻找。
6. 涉及多设备的功能按实际组合列出条件，不把“代码允许”写成“所有环境都实测通过”。
7. 更新目录与交叉链接，构建后检查页面与图片。

## 截图如何产生

本轮安装截图使用测试服务器上的真实前端、后端和隔离临时数据库，没有模拟 API、正式订阅、真实 Agent 或路由器。图片展示表单和选择过程，**不表示已经完成核心安装、配置发布或高可用配对**。没有真实设备的运行结果不伪造成功截图。

[截图清单与复现方法](https://github.com/thsrite/configflow/blob/main/doc/img/guide/README.md)记录了每张图的状态。[采集脚本](https://github.com/thsrite/configflow/blob/main/scripts/docs/capture-guide.mjs)在完成或出错后关闭本次前后端服务并删除临时数据。

## 在测试服务器预览和检查

按项目要求，以下构建、检查和开发服务都从 worktree 通过 `rt` 放到测试服务器运行，不在本机运行。

```bash
rt -- npm --prefix doc ci --ignore-scripts --no-audit --no-fund
rt -- npm --prefix doc run build
rt -- python scripts/docs/check-site.py
rt -- npm --prefix doc run preview -- --port 15673 --strictPort
```

预览启动后，本机浏览器访问 `http://192.168.0.32:15673`。检查桌面目录、手机菜单、搜索、段落链接和截图后，停止这次预览进程。默认从站点根路径访问；需要挂在子目录时，构建时用 `rt -e DOCS_BASE=/configflow/ -- npm --prefix doc run build`，再按该前缀部署。

构建生成 `doc/.vitepress/dist/`，不提交生成文件，也不会自动发布网站。本仓库未在本轮擅自新增公开托管；将来确定部署位置后再配置发布流程。
