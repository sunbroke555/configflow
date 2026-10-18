# 节点库代理延迟

节点库的「测速」由 ConfigFlow 所在机器发起，使用独立的 `configflow-node-probe` 程序读取完整节点配置，通过节点访问测试地址。不要求节点先发布，也不调用正在运行的 Mihomo 控制接口。

- 默认测试地址：`https://www.gstatic.com/generate_204`，发送 HEAD 请求，要求返回 HTTP 204，不跟随重定向。
- 默认统一延迟：先完成一次请求，再计时第二次请求，尽量复用连接；首连耗时也保存并可在结果提示中查看。第二次失败会明确报错。
- 默认每个节点总超时 5 秒（包含两次请求），每批 5 个并发；页面按批次更新完成数。HTTP API 单批最多 20 个，超出明确拒绝，不静默截断。
- 测速程序按批次启动，结束后退出；不启用 TUN、DNS 监听或代理入站端口，不要求 root。服务进程内同一时间只运行一批测试。多进程部署的并发限制按进程计算。
- 支持范围跟随锁定的 Mihomo 协议实现版本（见 `backend/node-probe/go.mod`）；未知协议、配置不完整、代理链依赖会显示失败，不回退成 TCP 测试。节点域名使用测速机器的解析与出站网络，系统透明代理或网关仍可能影响路径。
- 节点库优先于同名订阅节点，沿用现有按名称记录的方式。旧 TCP 结果不再展示，新的代理结果不会混入旧历史；变更测试地址或统一延迟模式也会开始新的历史。

## 部署

新 Docker 镜像内置组件。已有安装需要重建/更新镜像，单独替换 Python 文件不会补齐二进制。自动更新镜像会校验并重建变更的组件，失败保留前一版。

源码部署需要 Go 1.26 或更新版本。在仓库根目录执行：

```sh
(cd backend/node-probe && CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o configflow-node-probe .)
```

程序会优先读取 `CONFIGFLOW_NODE_PROBE_BIN`，否则使用镜像内路径、上述源码构建路径或 PATH 中的 `configflow-node-probe`。找不到组件时页面会提示安装方式，原有结果保留。

可选环境变量（设置在 ConfigFlow 服务上，重启后生效）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CONFIGFLOW_NODE_PROBE_BIN` | 自动查找 | 独立组件的可执行文件路径 |
| `CONFIGFLOW_NODE_TEST_URL` | `https://www.gstatic.com/generate_204` | HTTP/HTTPS 地址，必须支持 HEAD 并返回 204；建议 HTTPS |
| `CONFIGFLOW_NODE_TEST_TIMEOUT_MS` | `5000` | 单节点整体超时，范围 500–10000 毫秒 |
| `CONFIGFLOW_NODE_TEST_UNIFIED_DELAY` | `1` | `0` 或 `false` 改为显示首连总耗时 |

测试地址是节点库独立设置，不改变策略组或 Provider 健康检查配置。程序通过标准输入接收节点配置，不将密码放入命令行或持久化测速结果；页面显示归类后的错误，不暴露核心的原始异常。

独立组件链接 Mihomo，按 GPLv3 分发，见 `backend/node-probe/LICENSE`；组件源码与锁定依赖在同目录。ConfigFlow 主应用继续沿用仓库根目录的许可证。
