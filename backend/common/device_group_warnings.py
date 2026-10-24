"""Warnings travel beside downloaded configuration, never inside its bytes."""
import json
from urllib.parse import quote


def generation_warnings(config, service):
    if config.get('device_groups') is None:
        return []
    if service == 'mihomo':
        from backend.common.device_groups import compile_groups
        compiled = compile_groups(config)
        if any(group['id'] != 'default' and group['mode'] != 'follow' and compiled['effective'].get(group['id'])
               for group in compiled['groups']):
            return ['设备组需要 Mihomo 1.19.1 或更新版本；此下载未检查接收方的内核版本。']
        return []
    names = {'mosdns': 'MosDNS', 'surge': 'Surge', 'loon': 'Loon'}
    if service in names:
        return [f'{names[service]} 暂不支持设备组，生成内容仍使用全局规则。']
    return []


def generation_warning_headers(config, service):
    warnings = generation_warnings(config, service)
    return {'X-ConfigFlow-Warnings': quote(json.dumps(warnings, ensure_ascii=False), safe='')} if warnings else {}
