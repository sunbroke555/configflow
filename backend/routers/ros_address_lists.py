"""Scoped RouterOS address-list reads.

Serializing members to REST JSON costs RouterOS about a millisecond each, and a
release used to read the whole table a dozen times. Callers now read only the
lists their rules reference. Lists whose exact content ConfigFlow already
knows (its own published members) are proven with one router-side digest
instead of being downloaded; any difference falls back to a full read.
"""
import copy
from contextlib import contextmanager
import hashlib
from urllib.parse import urlencode

from backend.routers.base import RouterError

ADDRESSES = '/ip/firewall/address-list'
REFERENCE_KEYS = ('src-address-list', 'dst-address-list')
# Runtime fields never take part in ownership comparisons or digests.
RUNTIME = {'creation-time', 'timeout'}
DIGEST_CHUNK = 500
DIGEST_MARKER = 'cf-digest:'


def referenced(rows):
    """List names a set of firewall rows selects on (negations included)."""
    names = set()
    for row in rows or []:
        for key in REFERENCE_KEYS:
            value = row.get(key)
            if isinstance(value, str) and value.lstrip('!'):
                names.add(value.lstrip('!'))
    return names


def filtered(driver, name):
    rows = driver._list(ADDRESSES + '?' + urlencode({'list': name}))
    return [row for row in rows if row.get('list') == name]


def _encode(row):
    """``[:len $r] . ":" . [:tostr $r]`` of a member without runtime keys.

    RouterOS prints associative arrays as ``key=value;...`` in key order. The
    key count makes an added or removed field change the encoding even when a
    value happens to contain separators.
    """
    fields = sorted(key for key in row if key not in RUNTIME)
    text = ';'.join(f'{key}={row[key]}' for key in fields)
    return f'{len(fields)}:{text}\n'.encode()


def digest(rows):
    """(count, md5) over members ordered by ID, matching the router script."""
    ordered = sorted(rows, key=lambda row: str(row['.id']))
    chunks = []
    for start in range(0, len(ordered), DIGEST_CHUNK):
        chunks.append(hashlib.md5(b''.join(_encode(row) for row in ordered[start:start + DIGEST_CHUNK])).hexdigest())
    if not ordered:
        chunks.append(hashlib.md5(b'').hexdigest())
    return len(ordered), hashlib.md5(''.join(chunks).encode()).hexdigest()


def _script_string(value):
    text = str(value)
    for raw, escaped in (('\\', '\\\\'), ('"', '\\"'), ('$', '\\$'), ('\n', '\\n'), ('\r', '\\r'), ('\t', '\\t')):
        text = text.replace(raw, escaped)
    return '"' + text + '"'


def digest_script(names):
    """One console script printing count and digest for each named list.

    A single scan collects members of every list; members are keyed by ID in
    associative arrays, which RouterOS iterates in key order, so the digest
    does not depend on internal print order.
    """
    lists = ' || '.join(f'list={_script_string(name)}' for name in names)
    drop = ' '.join(f':set ($r->{_script_string(key)});' for key in sorted(RUNTIME))
    lines = [':local groups ({})',
             f':foreach r in=[/ip/firewall/address-list/print as-value where ({lists})] do={{'
             f'{drop} :local l ($r->"list"); '
             ':if ([:typeof ($groups->$l)] = "nothing") do={:set ($groups->$l) ({})}; '
             ':set ($groups->$l->[:tostr ($r->".id")]) ([:len $r] . ":" . [:tostr $r] . "\\n")}']
    for index, name in enumerate(names):
        lines.append(
            '{\n:local chunk ""; :local hashes ""; :local n 0\n'
            f':foreach id,line in=($groups->{_script_string(name)}) do={{:set chunk ($chunk . $line); :set n ($n+1); '
            f':if ($n % {DIGEST_CHUNK} = 0) do={{:set hashes ($hashes . [:convert $chunk transform=md5 to=hex]); :set chunk ""}}}}\n'
            f':if (($n % {DIGEST_CHUNK} != 0) || ($n = 0)) do={{:set hashes ($hashes . [:convert $chunk transform=md5 to=hex])}}\n'
            f':put ("{DIGEST_MARKER}{index}:" . $n . ":" . [:convert $hashes transform=md5 to=hex])\n}}')
    return '\n'.join(lines)


def router_digests(driver, names):
    result = driver._request('POST', '/execute', {'script': digest_script(names), 'as-string': ''})
    output = str(result.get('ret', '')) if isinstance(result, dict) else ''
    digests = {}
    for line in output.replace('\r', '').split('\n'):
        if not line.startswith(DIGEST_MARKER):
            continue
        index, count, value = line[len(DIGEST_MARKER):].split(':', 2)
        if index.isdigit() and int(index) < len(names) and count.isdigit():
            digests[names[int(index)]] = (int(count), value.strip())
    if len(digests) != len(names):
        raise RouterError('无法计算地址列表摘要：' + (output.strip()[:200] or 'RouterOS 无输出'))
    return digests


def known_lists(driver):
    """Members ConfigFlow expects, from the release manifest and this session's writes."""
    known = {}
    manifest = getattr(driver, 'ros_policy_manifest', None) or {}
    for item in manifest.get('resources', []):
        if item.get('path') == ADDRESSES and item.get('config', {}).get('list'):
            known.setdefault(item['config']['list'], []).append({**item['config'], '.id': item['id']})
    known.update(getattr(driver, 'address_list_cache', None) or {})
    return known


def remember(driver, name, rows):
    """Record a list's exact members after a verified read or write."""
    cache = getattr(driver, 'address_list_cache', None)
    if cache is None:
        cache = driver.address_list_cache = {}
    cache[name] = [copy.deepcopy(row) for row in rows]


@contextmanager
def reuse(driver):
    """Within one preview or release, reuse what was read until ConfigFlow writes.

    Rule rows are still read fresh every time; only list members proven or
    downloaded in this scope are reused, and any address-list write by
    ConfigFlow (or the start of any release operation) drops them.
    """
    if getattr(driver, 'address_list_reads', None) is not None:
        yield
        return
    driver.address_list_reads = {}
    try:
        yield
    finally:
        driver.address_list_reads = None


def invalidate(driver):
    reads = getattr(driver, 'address_list_reads', None)
    if reads:
        reads.clear()


def read(driver, names):
    """Rows of each named list; known lists are proven by digest, not downloaded."""
    names = sorted(set(names))
    known = known_lists(driver)
    reads = getattr(driver, 'address_list_reads', None)
    reads = reads if reads is not None else {}
    # A read in this scope is reusable when it proved exactly the rows we now
    # expect; lists we know nothing about reuse a download from this scope.
    expected = {name: digest(known[name]) for name in names if name in known}
    reusable = {name for name in names if name in reads and reads[name][0] == expected.get(name, 'downloaded')}
    unproven = [name for name in expected if name not in reusable]
    if unproven:
        actual = router_digests(driver, unproven)
        for name in unproven:
            if actual[name] == expected[name]:
                reads[name] = (expected[name], known[name])
                reusable.add(name)
    rows = {}
    for name in names:
        if name in reusable:
            rows[name] = copy.deepcopy(reads[name][1])
            continue
        rows[name] = filtered(driver, name)
        if name in known:
            remember(driver, name, rows[name])
            reads[name] = (digest(rows[name]), rows[name])
        else:
            reads[name] = ('downloaded', rows[name])
    for value in rows.values():
        value.sort(key=lambda row: str(row.get('.id')))
    return rows


def occupied_names(driver, patterns, exclude):
    """Address-list names matching ``patterns`` that are not in ``exclude``.

    Used to reserve fresh list names without reading every member. Matching
    happens on the router; only the distinct names come back.
    """
    regex = '^(' + '|'.join(patterns) + ')$'
    excluded = ''.join(f' && list!={_script_string(name)}' for name in sorted(exclude))
    script = (':local names ({})\n'
              f':foreach i in=[/ip/firewall/address-list/find where list~{_script_string(regex)}{excluded}] do={{'
              ':set ($names->[/ip/firewall/address-list/get $i list]) 1}\n'
              ':local out ""\n:foreach name,v in=$names do={:set out ($out . $name . "\\n")}\n'
              ':put ("cf-names:\\n" . $out)')
    result = driver._request('POST', '/execute', {'script': script, 'as-string': ''})
    output = str(result.get('ret', '')) if isinstance(result, dict) else ''
    if 'cf-names:' not in output:
        raise RouterError('无法读取已占用的地址列表名称：' + (output.strip()[:200] or 'RouterOS 无输出'))
    return {line.strip() for line in output.split('cf-names:', 1)[1].replace('\r', '').split('\n') if line.strip()}
