"""Keep explicit inbound policy references aligned with managed group edits."""
from copy import deepcopy

import yaml

from backend.utils.strategy_references import StrategyReferenceError


class ListenerReferenceError(StrategyReferenceError):
    pass


BUILTIN_POLICIES = {'DIRECT', 'REJECT', 'REJECT-DROP', 'PASS', 'PASS-RULE', 'COMPATIBLE', 'GLOBAL'}


def _listeners(document):
    listeners = document.get('listeners')
    if listeners is None:
        return []
    if not isinstance(listeners, list):
        raise ListenerReferenceError('Mihomo listeners 必须是列表，无法确认入口的固定策略引用')
    for index, listener in enumerate(listeners):
        if not isinstance(listener, dict):
            raise ListenerReferenceError(f'Mihomo listeners[{index}] 必须是对象')
        target = listener.get('proxy')
        if target is not None and not isinstance(target, str):
            raise ListenerReferenceError(f'Mihomo listeners[{index}].proxy 必须是策略名称或留空')
    return listeners


def relink_group_listeners(profile, old_name, new_name=None):
    """Return changed paths; an omitted new name restores the listener's rules.

    Only exact scalar references are changed. Other configuration values and
    listener.rule stay intact; no replacement proxy is selected implicitly.
    """
    settings = profile.get('mihomo') or {}
    source = settings.get('custom_config') or ''
    if not source.strip() or old_name == new_name:
        return []
    try:
        document = yaml.safe_load(source)
    except yaml.YAMLError as error:
        raise ListenerReferenceError('Mihomo 基础配置无法解析，未修改策略组或入口引用') from error
    if not isinstance(document, dict):
        raise ListenerReferenceError('Mihomo 基础配置必须是对象，未修改策略组或入口引用')
    listeners = _listeners(document)
    changed = []
    replacements = list(listeners)
    for index, listener in enumerate(listeners):
        target = listener.get('proxy')
        if isinstance(target, str) and '${' in target:
            raise ListenerReferenceError(
                f'Mihomo listeners[{index}].proxy 含未解析变量，无法安全联动此策略组；原配置已保留')
        if target != old_name:
            continue
        # Copy this listener so YAML aliases elsewhere cannot change with it.
        replacement = deepcopy(listener)
        if new_name is None:
            replacement.pop('proxy', None)
        else:
            replacement['proxy'] = new_name
        replacements[index] = replacement
        changed.append(f'listeners[{index}].proxy')
    if changed:
        document['listeners'] = replacements
        profile.setdefault('mihomo', {})['custom_config'] = yaml.safe_dump(
            document, allow_unicode=True, sort_keys=False)
    return changed


def validate_listener_policies(document):
    """Validate every fixed listener target, even without a dialer topology."""
    available = BUILTIN_POLICIES | {
        item['name'] for key in ('proxies', 'proxy-groups')
        for item in document.get(key, []) if isinstance(item, dict) and isinstance(item.get('name'), str)
    }
    for index, listener in enumerate(_listeners(document)):
        target = listener.get('proxy')
        if target and target not in available:
            raise ListenerReferenceError(
                f'Mihomo listeners[{index}].proxy 引用了不存在或未启用的策略「{target}」')
