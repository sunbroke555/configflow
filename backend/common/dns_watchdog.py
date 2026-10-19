"""Local DNS liveness replies in delivered configurations, never profile drafts.

The Agent checks this exact first rule before probing a DNS listener. Keep the
rule in sync with routes/dns_watchdog.go. All other names retain the original
execution path; the probe bypasses caches, recorders and upstreams.
"""
from copy import deepcopy
from hashlib import sha256


WATCHDOG_NAME = 'configflow-watchdog.invalid'
WATCHDOG_RULE = {'matches': [f'qname full:{WATCHDOG_NAME}'], 'exec': 'reject 3'}


def ensure_local_dns_watchdog(document):
    """Add an idempotent local reply at each actual DNS server entry.

    OxiDNS native includes have already been expanded by its converter. Do not
    guess at unresolved external MosDNS includes. Such configurations retain
    their contents and the Agent does not enable its DNS watchdog for them.
    Non-sequence executors are wrapped without changing the original plugin.
    Wrappers precede their server for MosDNS's ordered plugin initialization.
    """
    result = deepcopy(document)
    if result.get('include'):
        return result
    plugins = result.get('plugins', [])
    if not isinstance(plugins, list):
        raise ValueError('DNS watchdog requires a plugins list')
    tagged = {}
    for plugin in plugins:
        if not isinstance(plugin, dict):
            raise ValueError('DNS watchdog requires plugin objects')
        tag = plugin.get('tag')
        if not tag:
            continue  # MosDNS server plugins need not have a tag.
        if not isinstance(tag, str) or tag in tagged:
            raise ValueError('DNS watchdog cannot use ambiguous plugin tags')
        tagged[tag] = plugin

    output, wrappers, protected = [], {}, set()

    def protect(owner, key):
        entry = owner.get(key)
        if isinstance(entry, str) and '${' in entry:
            # Native OxiDNS resolves these in the core's environment, which
            # need not match the publisher or Agent. Preserve the reference;
            # the Agent will pause probing an entry it cannot verify locally.
            return
        if not isinstance(entry, str) or entry not in tagged:
            raise ValueError('DNS watchdog cannot resolve the DNS server entry')
        target = tagged[entry]
        if target.get('type') == 'sequence':
            if entry not in protected:
                args = target.get('args')
                if not isinstance(args, list):
                    raise ValueError('DNS watchdog requires sequence rules to be a list')
                # Move an existing exact rule to the front, without removing
                # broader/custom rules or changing their relative order.
                target['args'] = [deepcopy(WATCHDOG_RULE),
                                  *(rule for rule in args if rule != WATCHDOG_RULE)]
                protected.add(entry)
            return
        if entry not in wrappers:
            base = 'cf_watchdog_' + sha256(entry.encode()).hexdigest()[:16]
            name, suffix = base, 1
            while name in tagged:
                name = f'{base}_{suffix}'
                suffix += 1
            wrapper = {'tag': name, 'type': 'sequence', 'args': [
                deepcopy(WATCHDOG_RULE), {'exec': '$' + entry}]}
            tagged[name] = wrapper
            wrappers[entry] = name
            output.append(wrapper)
        owner[key] = wrappers[entry]

    for plugin in plugins:
        kind = plugin.get('type')
        if kind in ('udp_server', 'tcp_server', 'quic_server', 'http_server'):
            args = plugin.get('args')
            if not isinstance(args, dict):
                raise ValueError('DNS watchdog requires DNS server arguments')
            if kind == 'http_server':
                entries = args.get('entries', [])
                if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
                    raise ValueError('DNS watchdog requires HTTP DNS entries')
                for entry in entries:
                    protect(entry, 'exec')
            else:
                protect(args, 'entry')
        output.append(plugin)
    if 'plugins' in result:
        result['plugins'] = output
    return result
