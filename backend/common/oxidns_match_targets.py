"""Read executable custom-match targets from the current native draft."""
from collections import Counter, deque

from backend.common.oxidns_native import TAG, native_document
from backend.common.oxidns_reference_validation import collect_reference_issues
from backend.common.oxidns_validation import schemas


def executable_targets(document, origins, graph, excluded=()):
    """Executor choices relative to the plugin being edited, never a guessed role."""
    plugins = document.get('plugins', [])
    tags = [plugin['tag'] for plugin in plugins if isinstance(plugin, dict) and isinstance(plugin.get('tag'), str)]
    counts = Counter(tags)
    catalog = schemas()[1]
    reverse = {tag: set() for tag in graph}
    for owner, targets in graph.items():
        for target in targets:
            reverse.setdefault(target, set()).add(owner)

    # Removing leaves leaves exactly the plugins that depend on a cycle.
    pending = {tag: len(set(targets)) for tag, targets in graph.items()}
    leaves = deque(tag for tag, count in pending.items() if not count)
    while leaves:
        target = leaves.popleft()
        for owner in reverse[target]:
            pending[owner] -= 1
            if pending[owner] == 0:
                leaves.append(owner)
    unsafe = {tag for tag, count in pending.items() if count}
    unsafe.update(tag for tag in tags if counts[tag] != 1 or not 0 < len(tag) <= 64 or
                  not TAG.fullmatch(tag) or tag.startswith(('qs.exec.', 'qs.match.', 'qs.cron.')))
    unsafe.update(excluded)
    # Calling anything that reaches the edited plugin would create recursion.
    # Also omit dependencies on ambiguous tag names.
    queue = deque(unsafe)
    while queue:
        target = queue.popleft()
        for owner in reverse.get(target, ()):
            if owner not in unsafe:
                unsafe.add(owner)
                queue.append(owner)
    targets = []
    for plugin, origin in zip(plugins, origins):
        if not isinstance(plugin, dict):
            continue
        tag, kind = plugin.get('tag'), plugin.get('type')
        if not isinstance(tag, str) or tag in unsafe or not isinstance(kind, str) or catalog.get(kind, {}).get('x-kind') != 'executor':
            continue
        targets.append({'tag': tag, 'type': kind, 'exec': ('goto ' if kind == 'sequence' else '$') + tag,
                        'source': origin['source']})
    return targets


def read_match_targets(document, settings):
    sources = {}
    if document.get('include'):
        document = native_document(settings, validate_arguments=False, sources=sources)
    plugins = document.get('plugins', [])
    origins = sources.get('plugins', [{'source': 'config.yaml'} for _ in plugins])
    tags = [plugin['tag'] for plugin in plugins if isinstance(plugin, dict) and isinstance(plugin.get('tag'), str)]
    graph = {}
    collect_reference_issues(document, schemas()[1], dependency_graph=graph)
    excluded = {'sequence_main'}
    parked = settings.get('_common_disabled')
    aliases = parked.get('renamed_plugins') if isinstance(parked, dict) else None
    main_alias = aliases.get('sequence_main') if isinstance(aliases, dict) else None
    if isinstance(main_alias, str):
        excluded.add(main_alias)
    return {'custom_match_targets': executable_targets(document, origins, graph, excluded),
            'custom_match_target_tags': list(dict.fromkeys(tags))}
