# 设备组第一步验收记录

日期：2026-10-24。分支：`codex/device-groups`。基线：`c602993`，已合入逐条规则 DNS 的 #352；本地验收结束时重新 fetch，确认基线仍为最新 `upstream/main`。本记录描述提交前的本地验收结果。

## 实施与兼容性

- 先保存原生成器完整输出，再实现模型和生成器；静态 golden 文件不在测试中重新生成。82 项逐字节断言覆盖 OxiDNS 模板/native、Mihomo、MosDNS、Surge、Loon，以及字段缺省、null、默认组和跟随全局组。
- 采用最长前缀归属和拦截优先。校验、引用清理、native 受管补丁和迁移互斥均经过仓储最终写入闸口。
- 显式升级同时清空两处旧设备策略；来源独立保存。模板存档不作为生效策略回退来源；native 旧链与所有权记录不一致时阻断升级。
- 新旧来源编译及生成入口按 CIDR 文本、分段边界和顺序比较，不只比较地址集合。
- 双核心发布先完成所有 Mihomo，再开始 OxiDNS。明确的本地任务预留拒绝可以恢复原 ID；网络未知、丢失回执和崩溃歧义仍先核对原任务，不能重复提交。
- Go Agent 源码无改动。主仓库目录没有修改、暂存或提交；所有开发文件位于任务 worktree。

## 自动化回归

| 检查 | 结果 |
| --- | --- |
| 后端全量 | 8628 passed，5 skipped，9 subtests passed；1108.12 秒 |
| 最后发布恢复修复的相关集成测试 | 423 passed；其中发布专项 49 passed |
| 前端全量 | 146 个文件，2213 passed |
| Vue/TypeScript | `npx vue-tsc --noEmit` 通过 |
| Go | `go test ./...` 通过 |
| Diff | `git diff --check` 通过 |

后端使用主目录的 Python 虚拟环境执行 `python -m pytest -q -p no:cacheprovider backend`，并设置 `PYTHONDONTWRITEBYTECODE=1`、`OXIDNS_TEST_BINARY`、`MIHOMO_BINARY`、`MIHOMO_MIN_BINARY`。最后的发布恢复修复另用 8 个相关测试文件重跑，避免把更早启动的全量进程视为覆盖了后续修改。

前端使用仓库 Vitest 依赖和与仓库相同的配置运行，临时配置仅将 root/依赖路径明确化并把缓存放入 `/private/tmp`，避免软链接依赖导致主目录产生测试文件；使用 `--cache=false`。Node 26 的 webstorage 参数与仓库配置一致。

已核对的跳过原因：3 项未提供 MosDNS 真实内核的 watchdog 测试、1 项仅 Windows 的文件锁测试、1 项 Mihomo 支持有效拓扑而不适用的非法拓扑参数项。OxiDNS 与 Mihomo 的本次真实内核测试不在这些跳过项中。

## 真实内核与路由器

### OxiDNS 1.6.1

直接对新生成器及真实下发 watchdog 处理后的结果执行 `check -c <cfg> -d <dir>`、`start` 和 `dig`，不在输出中手工注入设备组。

四个运行用例覆盖：不同来源走不同路径、两种组模式的拦截开关、四类 DNS 拦截、MATCH 截止、默认组兜底、完整直连、native、hosts/AdGuard 全局前缀，以及两种查询先后顺序的缓存隔离。默认组显式例外和多层前缀重新归组另有模型及生成器自动化测试。

另外真实复现了 native 直连/代理共享同一个缓存实例导致串组的反例。现在仓储和生成器拒绝这种共享实例/缓存文件形态；独立缓存通过真实仓储导入、生成、check/start/dig，默认组和代理组分别取得各自上游应答，两端上游各收到一次查询。

### Mihomo 1.19.1 与 1.19.32

两个版本、两种拦截开关组合共 4 份配置通过 `-t`，运行 28 条请求验证来源匹配、指定策略组、DIRECT、拦截开关与 MATCH 截止。另有逻辑链策略组的两种出口形态在两个版本上共 4 次 `-t` 通过。

实验策略组选 DIRECT 作为出站替身；此结果验证选组和分段，不代表验证了外部代理服务器。

### RouterOS CHR 7.22.3

在用户授权的 `192.168.0.6` 上创建隔离 VM 9710，使用当前迁移模型、来源编译器和路由器生成/应用路径回归。10 条 TCP、3 条 DNS 探针全部通过。

迁移前后的 4 条来源列表，内容及顺序完全一致；新模型与已部署的真实来源包比较产生 0 条操作。DNS guard hairpin 和跨网段 fake-IP 的来源改写限制如实记录，没有用同网段结果推断跨网段安全。

首次即时读回曾出现 NAT `invalid=true`；未修改规则的后续读回恢复健康，稳定探针全部通过。原因未确定，原始读回与重试证据保留。

验收结束后，VM 9710、专用磁盘、3 个桥、5 个 namespaces 和任务目录均已删除；既有 VM 101 未改动，原有 VM 和网络接口身份/归属与测试前一致。

## 界面与已知限制

真实 API 完成升级、编辑、预览、保存和 IP 归属查询。375px、深浅色与 clay/kraft/sky 主题均检查无页面横向溢出；编辑 Sheet 全屏且底栏可见。迁移来源对照在 375px 深浅主题分别展开核查，浏览器无错误。临时验收服务已关闭。

当前限制见 [设备组使用说明](device-groups.md)：native 不支持开启「仍执行 DNS 拦截」；不安全的自定义共享缓存形态会被拒绝；客户端 IP 丢失只能告警与诊断；MosDNS/Surge/Loon 不生成组；迁移后 RouterOS 自动规则同步需手动预览并发布一次策略以刷新基线，运行中的来源列表不因保存迁移而变化。

## 本地证据索引

- 全量后端：`/private/tmp/configflow-device-groups-backend-complete.log`
- 最后发布集成测试：`/private/tmp/configflow-device-group-publish-final-tests.log`
- 前端：`/private/tmp/configflow-device-groups-frontend-complete.log`
- OxiDNS 仓库验收：`backend/test_oxidns_device_groups_core.py`
- 独立缓存：`/private/tmp/configflow-device-groups-audit/independent-cache/result.json`
- Mihomo：`/private/tmp/configflow-device-groups-p0-mihomo/generated/result.json`
- 逻辑链策略组：`/private/tmp/configflow-device-groups-audit/mihomo-chains/result.json`
- RouterOS：`/private/tmp/configflow-device-groups-p0-routeros/implementation/`
- 浏览器验收：`/private/tmp/configflow-device-groups-qa/real-result.json`、`real-themes-result.json`、`migration-sources-visual-result.json`

临时目录可能被系统清理；可持续复跑的断言保留在仓库测试中。
