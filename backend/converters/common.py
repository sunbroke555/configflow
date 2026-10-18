"""Small input conversions shared by client configuration generators."""
from ipaddress import ip_address
from urllib.parse import urlsplit


def ruleset_format(rule, config_data, source_url=None):
    """Prefer the source's declared format; infer legacy URLs from their path."""
    library = next((item for item in config_data.get('rule_library', [])
                    if (item.get('id') == rule.get('library_rule_id')
                        if rule.get('library_rule_id') else item.get('name') == rule.get('name'))), {})
    for source in (library, rule):
        declared = source.get('format')
        if declared in ('yaml', 'text', 'mrs'):
            return declared
    url = library.get('url') or source_url or rule.get('url') or ''
    path = urlsplit(url).path.lower()
    if path.endswith(('.yaml', '.yml')):
        return 'yaml'
    return 'mrs' if path.endswith('.mrs') else 'text'


def parse_hosts_pairs(text):
    """Read domain/address and legacy address/domain rows, for IPv4 and IPv6."""
    def is_ip(value):
        try:
            ip_address(value)
            return True
        except ValueError:
            return False

    pairs = []
    for line in (text or '').splitlines():
        fields = line.partition('#')[0].split()
        if len(fields) < 2:
            continue
        first, second = fields[:2]
        if is_ip(second):
            pairs.append((first, second))
        elif is_ip(first):
            pairs.append((second, first))
    return pairs
