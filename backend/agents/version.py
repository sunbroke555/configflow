"""Agent 版本管理"""

# Agent 最新版本号
LATEST_AGENT_VERSION = "1.8.18-go"

# 版本更新日志（可选）
VERSION_CHANGELOG = {
    "1.8.18-go": {
        "date": "2026-10-18",
        "features": [
            "修复 Mihomo 面板订阅节点单独测速提示 Resource not found；普通节点和订阅节点按来源使用对应测速接口"
        ]
    },
    "1.8.17-go": {
        "date": "2026-10-18",
        "features": [
            "Mihomo、MosDNS、OxiDNS 发布失败保留具体校验、服务和回滚原因，发布记录可展开查看和复制；敏感信息自动隐藏"
        ]
    },
    "1.8.16-go": {
        "date": "2026-10-18",
        "features": [
            "Mihomo 面板支持重载配置（只从内核当前配置文件重新读取）和更新内核；两者与配置推送、服务重启共用服务操作锁"
        ]
    },
    "1.8.15-go": {
        "date": "2026-10-17",
        "features": [
            "新增 Mihomo 面板接口：经 Agent 查看和切换策略组、测速，查看与断开连接，查看规则并更新 provider，可替代独立的 Mihomo Web 面板"
        ]
    },
    "1.8.14-go": {
        "date": "2026-10-17",
        "features": [
            "DNS 监控增加客户端、请求域名、标准广告拦截域名排行，以及具体上游请求次数和平均耗时",
            "支持重置当前设备的面板统计起点；不清查询历史或 DNS 缓存，不重启核心"
        ]
    },
    "1.8.13-go": {
        "date": "2026-10-17",
        "features": [
            "看门狗探测期间不再占用服务操作锁，只在真正重启服务时加锁；修复与 10 分钟一次的磁盘清理同时触发、导致清理一直被推迟的问题"
        ]
    },
    "1.8.12-go": {
        "date": "2026-10-17",
        "features": [
            "修复 OxiDNS 的 AAAA 查询被 IPv4 优先策略抑制时来源显示为未识别；列表与详情显示“IPv4 优先（抑制 AAAA）”"
        ]
    },
    "1.8.11-go": {
        "date": "2026-10-17",
        "features": [
            "定期磁盘清理增加 Agent 更新残留：更新结束后删除历次 worker/backup 程序、任务记录和日志，只保留当前更新的记录与上一版备份；更新进行中或回滚失败时不清理"
        ]
    },
    "1.8.10-go": {
        "date": "2026-10-17",
        "features": [
            "OxiDNS 查询列表通过核心现有实时推送缓存解析来源，减少逐条详情请求；历史记录与旧 Agent 保留兼容补查，无需修改核心"
        ]
    },
    "1.8.9-go": {
        "date": "2026-10-17",
        "features": [
            "回流生效时关闭 all 与 LAN 网卡的 send_redirects（避免旁路网关向客户端发 ICMP 重定向），记录原值并在撤销回流时恢复；跳过重放时也会重新确认"
        ]
    },
    "1.8.8-go": {
        "date": "2026-10-17",
        "features": [
            "Agent 重启时若受管回流资源完整且与当前规格（网卡、网段、主路由 MAC）一致，则跳过重放，避免先撤后加造成的短暂回流空档；缺失、漂移或 MAC 变化时仍按原逻辑重放"
        ]
    },
    "1.8.7-go": {
        "date": "2026-10-17",
        "features": [
            "回流路由表、优先级和标记被占用时自动选择空闲资源；预览展示实际分配，发布成功后持久化复用",
            "发布前再次核对资源分配；无归属记录的旧回流状态保留不动，不再自动迁移删除旧规则"
        ]
    },
    "1.8.6-go": {
        "date": "2026-10-17",
        "features": [
            "回流状态迁移：1.8.2–1.8.4 写入的旧状态在 Agent 启动时逐项核对现有资源，与旧版创建内容完全一致才原地迁移为带归属记录的资源（路由并存切换、策略规则紧接替换、nft 原子替换），否则保持不动"
        ]
    },
    "1.8.5-go": {
        "date": "2026-10-17",
        "features": [
            "回流发布检查路由、策略规则和 nft 资源占用；只管理具有归属记录的对象，不再自动接管旧手工规则",
            "撤销按精确路由、规则和 nft 句柄执行，不清空路由表或删除 nft 表；兼容部分 iproute2 版本的 VRF 读取差异"
        ]
    },
    "1.8.4-go": {
        "date": "2026-10-17",
        "features": [
            "看门狗扩展到 mihomo：控制接口（或 DNS 监听）连续 30 秒无响应时自动重启，规则与 OxiDNS 相同"
        ]
    },
    "1.8.3-go": {
        "date": "2026-10-17",
        "features": [
            "DNS 看门狗：OxiDNS / MosDNS 进程在运行但连续 30 秒不应答时自动重启（每小时最多 3 次），发布、手动停止期间不干预；可用 dns_watchdog_disabled 关闭"
        ]
    },
    "1.8.2-go": {
        "date": "2026-10-17",
        "features": [
            "回流规则只标记经主路由转发进来的连接（匹配主路由 MAC），直连或收到 ICMP 重定向的客户端回包直接返回，不再被主路由丢弃"
        ]
    },
    "1.8.1-go": {
        "date": "2026-10-17",
        "features": [
            "OxiDNS 推送优先热重载：运行中的核心可原地应用新配置时不再停止服务，DNS 中断从数秒降到约 0.6 秒",
            "runtime / api / log 变化、重载被拒或运行版本不一致时自动退回重启，健康检查与回滚不变"
        ]
    },
    "1.8.0-go": {
        "date": "2026-10-17",
        "features": [
            "回流规则管理：按 ConfigFlow 下发的期望状态维护独立 nft 表与策略路由，可接管同标记的手工规则并支持恢复",
            "Agent 启动时自动重新应用已发布的回流规则"
        ]
    },
    "1.7.1-go": {
        "date": "2026-10-17",
        "features": [
            "OxiDNS 客户端 IP 选择：独立搜索时间范围内最近最多 10000 条记录中的客户端",
            "按客户端 IP 精确筛选并保留跨页游标，避免相似 IP 混入查询结果"
        ]
    },
    "1.7.0-go": {
        "date": "2026-10-16",
        "features": ["OxiDNS 原生 DNS 监控：实时统计、缓存命中率、查询记录筛选与执行详情", "受限只读 API 连接，核心认证信息仅保留在 Agent 本机"]
    },
    "1.6.1-go": {
        "date": "2026-10-16",
        "features": [
            "Mihomo、MosDNS、OxiDNS 独立启动，升级自动清理旧恢复服务依赖",
            "Agent 启动仅检查并提示未完成发布，保留推送备份、健康检查和失败回滚"
        ]
    },
    "1.6.0-go": {
        "date": "2026-10-10",
        "features": ["区域检测：经 Mihomo 本机检测入口切换节点访问服务与域名，用于判断解锁情况与地区限制"]
    },
    "1.5.0-go": {
        "date": "2026-10-10",
        "features": ["域名探测：经本机 Mihomo 分别测试直连与代理的可达性，用于判断域名该走哪条路"]
    },
    "1.4.0-go": {
        "date": "2026-10-10",
        "features": ["域名发现：采集 Mihomo 未覆盖与直连失败的域名并定期上报，默认规则集更新后自动刷新"]
    },
    "1.3.1-go": {
        "date": "2026-10-10",
        "features": ["修复 Mihomo HTTP 代理健康检查误判，避免正常推送被错误回滚"]
    },
    "1.3.0-go": {
        "date": "2026-10-10",
        "features": ["Shell 网页一键更新与旧配置自动迁移", "独立更新任务、SHA-256 校验、健康确认和失败回滚", "持久更新进度及 Docker 镜像更新提示"]
    },
    "1.2.0-go": {
        "date": "2026-10-10",
        "features": [
            "Mihomo/MosDNS 完整发布包、SHA-256 校验和暂存后激活",
            "替换前备份、启动健康检查、失败回滚和中断恢复",
            "支持 Shell 安装的 systemd/OpenRC 和 Docker Supervisor 服务生命周期",
            "持久化发布进度、幂等请求及旧协议升级提示"
        ]
    },
    "1.0.8-go": {
        "date": "2026-02-09",
        "features": [
            "合并4个镜像为统一镜像，通过环境变量控制服务模式",
            "支持清空指定日志文件"
        ]
    },
    "1.0.7-go": {
        "date": "2026-02-07",
        "features": [
            "优化配置推送逻辑"
        ]
    },
    "1.0.6-go": {
        "date": "2026-01-21",
        "features": [
            "logrotate日志轮转"
        ]
    },
    "1.0.5-go": {
        "date": "2026-01-19",
        "features": [
            "支持自定义日志路径",
            "开源"
        ]
    },
    "1.0.4-go": {
        "date": "2025-11-13",
        "features": [
            "推送更新逻辑",
        ]
    },
    "1.0.3-go": {
        "date": "2025-11-13",
        "features": [
            "推送mihomo配置时同时推送规则和订阅配置",
            "备份mihomo和mosdns只备份关键配置项",
        ]
    },
    "1.0.2-go": {
        "date": "2025-11-12",
        "features": [
            "新增系统监控功能：CPU、内存、磁盘、网络",
            "支持监控数据历史记录（24小时）",
            "提供监控数据统计和图表展示",
            "监控功能可配置开关（默认开启）",
            "完善的异常处理机制，监控失败不影响心跳"
        ]
    },
    "1.0.1-go": {
        "date": "2025-11-12",
        "features": [
            "支持agent在线更新"
        ]
    },
    "1.0.0-go": {
        "date": "2025-10-12",
        "features": [
            "初始版本",
            "支持 Mihomo 和 MosDNS 配置管理",
            "支持远程配置推送",
            "支持服务重启",
            "支持日志查看"
        ]
    }
}

def get_latest_version() -> str:
    """获取最新的 Agent 版本号"""
    return LATEST_AGENT_VERSION

def compare_versions(version1: str, version2: str) -> int:
    """
    比较两个版本号

    Args:
        version1: 版本号1
        version2: 版本号2

    Returns:
        int: -1 如果 version1 < version2
              0 如果 version1 == version2
              1 如果 version1 > version2
    """
    # 移除后缀（如 "-go"）
    v1_parts = version1.split('-')[0].split('.')
    v2_parts = version2.split('-')[0].split('.')

    # 补齐长度
    max_len = max(len(v1_parts), len(v2_parts))
    v1_parts.extend(['0'] * (max_len - len(v1_parts)))
    v2_parts.extend(['0'] * (max_len - len(v2_parts)))

    # 比较每个部分
    for p1, p2 in zip(v1_parts, v2_parts):
        try:
            n1, n2 = int(p1), int(p2)
            if n1 < n2:
                return -1
            elif n1 > n2:
                return 1
        except ValueError:
            # 如果无法转换为整数，按字符串比较
            if p1 < p2:
                return -1
            elif p1 > p2:
                return 1

    return 0

def has_update(current_version: str) -> bool:
    """
    检查是否有新版本可用

    Args:
        current_version: 当前版本号

    Returns:
        bool: True 如果有新版本，False 否则
    """
    return compare_versions(current_version, LATEST_AGENT_VERSION) < 0
