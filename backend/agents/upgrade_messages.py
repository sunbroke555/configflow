"""Chinese display messages for Agent updates, including older Agent replies.

Only admin-facing responses use these messages. Keep the persisted error and
Agent protocol unchanged so recovery and diagnostics retain the original data.
Mihomo core errors use a separate path and must not pass through this formatter.
"""
import re


_MESSAGES = {
    'Agent download connection failed': '无法连接 Agent 更新下载地址，请检查设备到 ConfigFlow 的网络连接。',
    'Agent download write failed': 'Agent 更新文件下载或写入失败，请检查网络连接、磁盘空间及目录权限。',
    'Agent download size or SHA-256 mismatch': 'Agent 更新文件校验失败，文件大小或 SHA-256 与预期不符，请重新下载。',
    'candidate version or executable validation failed': '新版 Agent 无法运行或版本不符，请检查更新文件是否适用于这台设备。',
    'Agent did not confirm expected version, identity and migration readiness': '未能确认 Agent 的版本、设备身份及配置迁移结果，请查看 Agent 运行状态和日志。',
    'Agent backup hash mismatch': '旧版 Agent 的备份文件校验失败，无法用它恢复，请检查备份文件。',
    'interrupted Agent update recovered from durable journal': '检测到上次 Agent 更新中断，已进入恢复处理，请查看最终更新结果。',
    'Agent executable must be a regular file': 'Agent 程序路径不是普通文件，无法在线更新。',
    'unsupported Agent architecture': '当前设备的处理器架构不支持在线更新。',
    'another Agent update or service operation is active': '已有 Agent 更新或服务操作正在执行，请等待完成后再试。',
    'another service operation is in progress': '已有服务操作正在执行，请等待完成后再试。',
    'Agent update requires completion or recovery': '上次 Agent 更新尚未结束，请先查询结果或完成恢复。',
    'previous Agent update requires completion or recovery': '上次 Agent 更新尚未结束，请先查询结果或完成恢复。',
    'valid update ID, target version, SHA-256 and bounded size are required': '更新请求缺少有效的任务编号、版本或文件校验信息，请刷新页面后重试。',
    'update ID already belongs to different content': '这个更新任务对应的文件已发生变化，请重新发起更新。',
    'unreadable Agent update journal': '无法读取 Agent 更新记录，请检查记录文件及目录权限。',
    'Docker requires image update': 'Docker Agent 需要更新镜像并重建容器，请使用容器更新方式。',
    'Docker Agent requires an image update; container binary replacement is unsupported': 'Docker Agent 需要更新镜像并重建容器，请使用容器更新方式。',
    'Agent registration is required': 'Agent 尚未完成注册，请先确认它已连接到 ConfigFlow。',
    'OpenRC supervise-daemon is required for recoverable updates': '设备缺少 OpenRC 的 supervise-daemon，请安装后再更新 Agent。',
    'empty Agent configuration': 'Agent 配置文件为空，请检查配置文件。',
    'unsupported core service': '当前核心服务类型不支持 Agent 在线更新。',
    'cannot infer custom lifecycle; configure service_manager and service_unit before upgrading': '无法确定服务的启动方式，请先配置 service_manager 和 service_unit。',
    'online migration supports systemd and OpenRC': '在线更新仅支持由 systemd 或 OpenRC 管理的服务。',
    'invalid service unit': '服务名称无效，请检查 Agent 和核心服务的名称。',
    'Agent and core config paths must be absolute': 'Agent 和核心配置文件必须使用绝对路径。',
    'invalid Agent config path': 'Agent 配置文件路径无效，请检查路径。',
    'invalid Agent API port': 'Agent 接口端口无效，请填写 1 到 65535 之间的端口。',
    'invalid binary path': 'Agent 程序路径无效，请检查安装路径。',
    'unreadable migration journal': '无法读取配置迁移记录，请检查记录文件。',
    'malformed OpenRC recovery section': 'OpenRC 服务中的旧版恢复配置格式异常，请检查服务文件。',
    'migration failed': 'Agent 配置迁移失败，请查看 Agent 日志。',
    'Migration failed': 'Agent 配置迁移失败，请查看 Agent 日志。',
    'update not recorded': '没有找到这次 Agent 更新的记录，请刷新后重新查询。',
    'invalid update request': 'Agent 更新请求格式无效，请刷新页面后重试。',
    'Agent rejected upgrade': 'Agent 未接受更新请求，请查看 Agent 日志。',
    'Agent not found': '找不到这个 Agent，请刷新设备列表。',
    'No upgrade recorded': '这个 Agent 暂无更新记录。',
    'Upgrade ownership changed': '当前更新任务已改变，请刷新后查看最新结果。',
    'Invalid legacy upgrade report': '旧版 Agent 上报的更新结果无效，请检查任务是否一致。',
    'Invalid update report': 'Agent 上报的更新结果格式无效。',
    'Agent artifact changed while being read; please retry': '读取 Agent 更新文件时文件发生变化，请稍后重试。',
}

_CAUSE_PREFIXES = {
    'interrupted migration restore failed: ': '上次配置迁移中断，恢复配置失败',
    'migration failed; configuration rollback failed: ': '配置迁移失败，恢复原配置也失败',
    'automatic migration failed: ': 'Agent 配置自动迁移失败',
    'migration failed; legacy executable backup unavailable: ': '配置迁移失败，旧版 Agent 备份不可用',
    'migration failed; no previous executable: ': '配置迁移失败，没有可恢复的旧版 Agent',
    'migration failed; executable restore failed: ': '配置迁移失败，恢复旧版 Agent 程序也失败',
}

_PATH_PREFIXES = {
    'refusing nonregular upgrade target: ': '更新目标不是普通文件，请检查安装目录',
    'legacy recovery dependency in linked unit requires cleanup at its source: ': '服务链接指向的原文件仍有旧版恢复配置，请先检查该文件',
    'unreadable deployment state: ': '无法读取配置发布记录，请先检查发布状态',
}

_SYSTEM_ERRORS = {
    'no space left on device': '磁盘空间不足，请清理空间后重试。',
    'permission denied': '文件或服务操作权限不足，请检查 Agent 的运行权限。',
    'read-only file system': '安装目录所在的文件系统只读，无法写入更新文件。',
    'no such file or directory': '更新所需的文件或目录不存在，请检查 Agent 安装目录。',
    'executable file not found': '找不到更新所需的服务管理程序，请检查设备的运行环境。',
    'connection refused': '连接被拒绝，请检查目标服务是否已启动及端口是否正确。',
    'no such host': '无法解析服务器地址，请检查设备的 DNS 配置。',
    'network is unreachable': '设备无法访问目标网络，请检查网络连接。',
    'i/o timeout': '网络请求超时，请检查连接或稍后重试。',
    'context deadline exceeded': '操作超时，请检查服务状态或稍后重试。',
    'certificate signed by unknown authority': '无法验证服务器证书，请检查证书配置。',
}


def _upgrade_explanation(message):
    """Explain failures without interpreting success or rollback state."""
    if not isinstance(message, str) or not message.strip():
        return 'Agent 更新遇到异常，请查看 Agent 日志。'
    text = message.strip()
    if text in _MESSAGES:
        return _MESSAGES[text]
    # A failed update can also fail to restore the old version. Show both causes.
    original, separator, rollback = text.partition('; rollback: ')
    if separator:
        return f'{_upgrade_explanation(original)} 恢复旧版本失败：{_upgrade_explanation(rollback)}'
    for prefix, label in _CAUSE_PREFIXES.items():
        if text.startswith(prefix):
            return f'{label}：{_upgrade_explanation(text[len(prefix):])}'
    for prefix, label in _PATH_PREFIXES.items():
        if text.startswith(prefix):
            return label + '。'
    http_error = re.fullmatch(r'Agent download HTTP (\d{3})', text)
    if http_error:
        return f'Agent 更新下载失败，服务器返回 HTTP {http_error[1]}，请检查 ConfigFlow 的更新文件是否可用。'
    recovery = re.fullmatch(r'deployment \S+ requires (rollback )?recovery', text)
    if recovery:
        return '之前的配置发布尚未完成恢复，请先到发布记录中处理，再更新 Agent。'
    # System error strings vary by platform; the public wrapper retains them.
    lower = text.lower()
    for cause, label in _SYSTEM_ERRORS.items():
        if cause in lower:
            return label
    if re.match(r'^(systemctl|rc-service|rc-update|supervise-daemon) operation failed:', text):
        return '服务管理命令执行失败，请查看 Agent 日志和设备上的服务状态。'
    if re.search(r'[\u4e00-\u9fff]', text):
        return text
    return 'Agent 更新遇到异常，请查看 Agent 日志。'


def upgrade_error_message(message):
    """Add a Chinese explanation while retaining the complete original error."""
    # Already-localized messages and unknown core replies containing Chinese
    # must not acquire repeated explanations when a response is formatted twice.
    if isinstance(message, str) and re.search(r'[\u4e00-\u9fff]', message):
        return message
    explanation = _upgrade_explanation(message)
    if isinstance(message, str) and message.strip():
        return f'{explanation}\n原始错误：{message}'
    return explanation


def public_upgrade_state(state):
    """Localize a response copy; never rewrite the durable recovery journal."""
    result = dict(state)
    if result.get('error'):
        result['error'] = upgrade_error_message(result['error'])
    return result
