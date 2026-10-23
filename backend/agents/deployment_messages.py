"""Display explanations only: preserve protocol states and original diagnostics."""
import re
from .operation_events import clean_events


_MESSAGES = {
    'Agent not found': '找不到这台 Agent，请刷新设备列表。',
    'Invalid deployment ID': '发布任务编号无效，请重新发起发布。',
    'Agent update is in progress': 'Agent 正在更新，请等待更新结果确认后再操作。',
    'Agent already has an unfinished deployment': '这台 Agent 还有未结束的发布，请先查看并处理上次发布。',
    'Deployment preparation already running': '正在准备这次发布，请等待文件准备完成。',
    'Deployment not found': '没有找到这次发布的记录，请刷新后重新查询。',
    'Deployment belongs to a different profile': '这次发布属于其他配置空间，请确认设备绑定。',
    'Deployment belongs to a different service type': '这次发布与设备当前的服务类型不一致。',
    'Preparation interrupted before upload; current service unchanged': '发布在上传前中断，设备上的服务未改变。',
    'Deployment status temporarily unavailable': '暂时无法读取发布状态，请稍后查询。',
    'Rollback submission awaiting Agent confirmation': '回滚请求正在等待 Agent 确认，请继续查询结果。',
    'Rollback journal unavailable; restore Agent records before retrying': '无法读取回滚记录，请先恢复 Agent 上的记录再重试。',
    'Agent has no record of the uploaded deployment': 'Agent 上没有找到已上传配置的发布记录，请检查 Agent 状态。',
    'Deployment profile or service type changed; activation refused': '设备绑定的配置空间或服务类型已改变，请重新预览后发布。',
    'Deployment must be ready before activation': '配置尚未完成校验，暂时不能激活。',
    'Activation sent; awaiting Agent status': '已发送激活请求，正在等待 Agent 确认结果。',
    'Only the latest deployment of this profile can be rolled back': '只能回滚当前配置空间最近一次发布。',
    'Only a successful deployment can be rolled back': '只有发布成功的配置才能手动回滚。',
    'Rollback outcome unknown; awaiting Agent status': '暂时无法确认回滚结果，请继续查询 Agent 状态。',
    'Rollback was rejected by Agent': 'Agent 未接受回滚请求，请检查发布记录。',
    'Deployment ownership changed during preparation': '准备文件期间发布任务已改变，请重新预览后发布。',
    'Agent profile changed during preparation': '准备文件期间设备绑定的配置空间已改变，请重新发布。',
    'Agent service type changed during preparation': '准备文件期间设备的服务类型已改变，请重新发布。',
    'Agent does not support transactional deployments; upgrade the Shell-installed or Docker Agent first': '当前 Agent 不支持完整的校验和回滚发布流程，请先更新 Agent。',
    'Deployment ID already belongs to different content': '这次发布任务对应的配置已改变，请重新发起发布。',
    'Upload accepted; awaiting valid Agent status': 'Agent 已接收上传请求，正在等待发布结果。',
    'Upload outcome unknown; query deployment status': '暂时无法确认上传结果，请查询发布状态，不要重复提交。',
    'Unable to query Agent deployment capabilities': '无法确认 Agent 是否支持发布，请检查设备连接。',
    'A complete deployment bundle is required': '发布文件不完整，请重新生成配置后发布。',
    'Invalid Agent response': 'Agent 返回的数据格式异常，请检查 Agent 版本和日志。',
    'Deployment preparation failed': '准备发布文件失败，请查看 ConfigFlow 日志。',
    'Unsupported service type': '当前服务类型不支持这项操作。',
    'restart must be a boolean': '发布请求中的重启选项无效，请重新提交。',
    'Required rule has no source URL': '规则集没有下载地址，请先补充规则来源。',
    'Rule exceeds deployment file size limit': '规则文件超过发布大小限制，请检查规则源返回的内容。',
    'Invalid rule YAML payload': '规则文件的 YAML 内容格式不正确，请检查规则内容。',
    'Unrecognized MosDNS rule source content': '无法识别规则内容，请检查规则格式。',
    'Invalid IP network in MosDNS ip_set rule source': 'IP 规则不是有效的 IP 地址或网段。',
    'MosDNS requires text rules; binary MRS is only supported by Mihomo': 'MosDNS 需要文本规则，MRS 二进制规则只能用于 Mihomo。',
    'MosDNS rule source is binary': 'MosDNS 规则源返回了二进制内容，请改用文本规则。',
    'Generated configuration must be a YAML mapping': '生成的配置格式不正确，请检查配置内容。',
    'Missing deployment file content': '发布文件缺少内容，请重新生成配置。',
    'Deployment file exceeds size limit': '单个发布文件超过大小限制，请检查文件内容。',
    'Deployment exceeds size limit': '发布文件总大小超过限制，请检查配置和规则文件。',
    'another service operation is in progress': '设备正在执行其他服务操作，请稍后再试。',
    'service start failed': '服务启动失败，请查看设备上的核心日志。',
    'service stop failed': '服务停止失败，请检查设备上的服务状态。',
    'service is not running': '核心服务没有运行，请查看核心日志。',
    'Mihomo configuration check failed': 'Mihomo 配置校验未通过，请查看核心返回的校验详情。',
    'Mihomo configuration check timed out': 'Mihomo 配置校验超时，请检查设备负载和核心日志。',
    'deployment interrupted before successful confirmation': '发布在确认成功前中断，请查看恢复结果。',
    'deployment interrupted before activation': '发布在激活新配置前中断，请查看发布记录。',
    'insufficient disk space for verified backup and replacement': '设备磁盘空间不足，无法完成发布所需的文件保存与替换。',
}

_PREFIXES = {
    'manifest checksum mismatch for ': '发布文件校验失败，请重新发布。',
    'backup verification failed: ': '旧配置备份校验失败，请检查设备上的备份文件。',
    'backup checksum mismatch: ': '旧配置备份校验失败，无法使用该文件回滚。',
    'rollback cannot stop service: ': '回滚时无法停止当前服务，请检查服务状态。',
    'old service failed to start: ': '恢复旧配置后服务未能启动，请查看核心日志。',
    'old service health failed: ': '恢复旧配置后服务检查未通过，请查看核心日志。',
    'service is not running: ': '核心服务启动后退出，原始错误末尾是核心日志中的报错。',
    'timezone data for ': '设备缺少时区数据，OxiDNS 的定时任务无法启动；请在设备上安装 tzdata（Alpine：apk add tzdata）后重新发布。当前运行配置未修改。',
}


def deployment_message(message):
    if not isinstance(message, str):
        return message
    explanation = _MESSAGES.get(message)
    if not explanation:
        explanation = next((label for prefix, label in _PREFIXES.items() if message.startswith(prefix)), None)
    if not explanation and re.fullmatch(r'Agent returned HTTP \d{3}', message):
        explanation = 'Agent 请求失败，请根据下方 HTTP 状态码检查设备连接。'
    if not explanation and re.fullmatch(r'Rule .+: .+', message):
        explanation = '规则集准备失败，请检查下方规则名称和具体原因。'
    # Unknown/core/source errors are never rewritten or guessed at. Known
    # protocol failures get an explanation followed by the exact original text.
    return f'{explanation}\n原始错误：{message}' if explanation else message


def public_deployment_messages(record):
    result = dict(record)
    for key in ('message', 'error', 'rollback_error', 'rollback_message'):
        if key in result:
            result[key] = deployment_message(result[key])
    if 'events' in result:
        result['events'] = [{**event, **({'message': deployment_message(event['message'])} if event.get('message') else {})}
                            for event in clean_events(result['events'])]
    return result
