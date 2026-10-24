"""Resolve declared rule sources without guessing a type from a consumer part."""
from backend.converters.common import ruleset_format
from backend.utils.ruleset_content import RemoteRuleValidationError


def _same_url(left, right, config):
    base = config.get('system_config', {}).get('server_domain', '').rstrip('/')
    def absolute(value):
        return base + value if value.startswith('/') and base else value
    return absolute(left or '') == absolute(right or '')


def resolve_remote_rule_source(url, config, source_id=None):
    """Find the snapshot declaration; arbitrary native resources stay untyped.

    Old proxy URLs have no identity. They are safe only when all matching
    declarations agree; an explicit identity must match its declared URL.
    """
    library = config.get('rule_library', [])
    by_id = {str(item.get('id')): item for item in library if item.get('id') is not None}
    items = [item for item in config.get('rule_configs', []) if item.get('itemType') == 'ruleset']
    if source_id:
        source = by_id.get(str(source_id))
        if source is None:
            item = next((item for item in items if str(item.get('id')) == str(source_id)), None)
            source = by_id.get(str((item or {}).get('library_rule_id'))) or item
        if source is None:
            raise RemoteRuleValidationError('规则来源已不存在，请重新生成并发布配置。')
        if source.get('source_type', 'url') != 'url':
            raise RemoteRuleValidationError('规则来源已改为手写内容，请重新生成并发布配置。')
        if not _same_url(source.get('url'), url, config):
            raise RemoteRuleValidationError('规则订阅地址已变更，请重新生成并发布配置。')
        return source
    candidates = [item for item in library if _same_url(item.get('url'), url, config)]
    candidates.extend(by_id.get(str(item.get('library_rule_id'))) or item
                      for item in items if _same_url(item.get('url'), url, config))
    candidates = [item for item in candidates if item.get('source_type', 'url') == 'url']
    if not candidates:
        return None
    declarations = {(item.get('behavior', 'domain'), ruleset_format(item, config)) for item in candidates}
    if len(declarations) != 1:
        raise RemoteRuleValidationError('同一订阅地址对应多个不同规则类型或格式，无法确定当前来源；请重新生成并发布配置。')
    return candidates[0]
