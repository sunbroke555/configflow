"""Validate policy keys against the policies actually emitted by a converter."""


class StrategyReferenceError(ValueError):
    """An enabled rule targets a policy absent from the generated configuration."""


def validate_rule_policies(config_data, available_names, format):
    # https://wiki.metacubex.one/config/proxies/built-in/
    # https://manual.nssurge.com/policies/built-in.html
    builtins = {
        'mihomo': {'DIRECT', 'REJECT', 'REJECT-DROP', 'PASS', 'PASS-RULE', 'COMPATIBLE'},
        'surge': {'DIRECT', 'REJECT', 'REJECT-DROP', 'REJECT-TINYGIF', 'REJECT-NO-DROP',
                  'CELLULAR', 'CELLULAR-ONLY', 'HYBRID', 'NO-HYBRID'},
    }
    available = set(available_names) | builtins[format]
    default = 'PROXY' if format == 'mihomo' else 'Proxy'
    for rule in config_data.get('rule_configs', []):
        if not rule.get('enabled', True) or not rule.get('library_enabled', True) or rule.get('itemType') not in ('rule', 'ruleset'):
            continue
        policy = rule.get('policy', default if rule.get('itemType') == 'ruleset' else '')
        if policy not in available:
            raise StrategyReferenceError(
                f"规则 {rule.get('id', rule.get('name', ''))} 引用了不存在或未启用的策略 '{policy}'，请修改策略引用"
            )
