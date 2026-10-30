"""RouterOS batch endpoints for the in-memory REST fakes in tests.

The fakes keep their own rows; these helpers interpret what the driver sends
to /execute (route and NAT add scripts), POST .../print queries and POST
.../remove the way RouterOS 7.24 does.
"""
import ipaddress
import re

ADD_LINE = re.compile(r':if \(\$f=0\) do=\{:onerror e in=\{:put \("cf-id:(?P<index>\d+):" \. '
                      r'\[(?P<path>/ip/route|/ipv6/route|/ip/firewall/nat)/add (?P<params>.*)\]\)\} '
                      r'do=\{:set f 1; :put \("cf-err:(?P=index):" \. \$e\)\}\}')
FIELD = re.compile(r'([\w.-]+)=(?:"((?:[^"\\]|\\.)*)"|(yes|no))')


def _unescape(text):
    return re.sub(r'\\(.)', lambda match: {'n': '\n', 'r': '\r', 't': '\t'}.get(match[1], match[1]), text)


def is_add_script(script):
    return script.startswith(':local f 0\n') and script.endswith('\n:put "cf-done"')


def run_add_script(script, add, fatal=()):
    """Run a batch of console adds; ``add(path, fields)`` returns the new ID or raises.

    Like the console, the script stops at the first failed add and reports it.
    ``fatal`` errors (a lost reply) abort the whole request instead.
    """
    lines, output = script.split('\n')[1:-1], []
    for line in lines:
        match = ADD_LINE.fullmatch(line)
        assert match, line
        fields = {key: {'yes': 'true', 'no': 'false'}[bare] if bare else _unescape(quoted)
                  for key, quoted, bare in FIELD.findall(match['params'])}
        try:
            row_id = add(match['path'], fields)
        except fatal:
            raise
        except Exception as error:  # noqa: BLE001 - the console prints any failure
            output.append(f'cf-err:{match["index"]}:{error}')
            break
        output.append(f'cf-id:{match["index"]}:{row_id}')
    return {'ret': '\r\n'.join(output + ['cf-done'])}


def _matches(row, term):
    key, _, value = term.partition('=')
    if key == 'dst-address':
        try:
            return ipaddress.ip_network(row.get(key, ''), strict=False) == ipaddress.ip_network(value, strict=False)
        except ValueError:
            return False
    return str(row.get(key, '')) == value


def print_rows(rows, body):
    """POST .../print with an OR'ed .query of key=value terms."""
    terms = [term for term in (body or {}).get('.query', []) if not term.startswith('#')]
    return [row for row in rows if any(_matches(row, term) for term in terms)]


ADDRESS_BATCH = re.compile(r':local n 0\n:local m ""\n:foreach a in=\{(?P<items>.*)\} do=\{:onerror e in=\{'
                           r'/ip/firewall/address-list/add (?P<params>.*) address=\$a\} do=\{:set n \(\$n\+1\); '
                           r':if \(\$n=1\) do=\{:set m \(\$a \. " " \. \$e\)\}\}\}\n:put \("cf-done:" \. \$n \. ":" \. \$m\)',
                           re.DOTALL)
DIGEST_LIST = re.compile(r'\(\$groups->"((?:[^"\\]|\\.)*)"\)')
FETCH_LIST = re.compile(r'print as-value where list="((?:[^"\\]|\\.)*)"')
FETCH_RANGE = re.compile(r'\$i >= (\d+) && \$i < (\d+)')
QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')


def run_list_script(script, rows, add):
    """Address-list console scripts: digests, paged prints and batch adds.

    ``rows`` are the fake's address-list rows; ``add(fields)`` stores one
    member and returns its ID or raises with the console's message.
    """
    from backend.routers import ros_address_lists
    if 'cf-digest:' in script:
        lines = []
        for index, name in enumerate(_unescape(name) for name in DIGEST_LIST.findall(script)):
            count, value = ros_address_lists.digest([row for row in rows if row.get('list') == name])
            lines.append(f'cf-digest:{index}:{count}:{value}')
        return {'ret': '\r\n'.join(lines)}
    if 'cf-rows:' in script:
        name = _unescape(FETCH_LIST.search(script)[1])
        start, end = map(int, FETCH_RANGE.search(script).groups())
        members = [row for row in rows if row.get('list') == name]
        lines = [f'cf-row:{ros_address_lists._encode(row).decode().rstrip(chr(10))}' for row in members[start:end]]
        return {'ret': '\r\n'.join(lines + [f'cf-rows:{len(members)}'])}
    match = ADDRESS_BATCH.fullmatch(script)
    assert match, script
    fields = {key: {'yes': 'true', 'no': 'false'}[bare] if bare else _unescape(quoted)
              for key, quoted, bare in FIELD.findall(match['params'])}
    failed, first = 0, ''
    for address in (_unescape(item) for item in QUOTED.findall(match['items'])):
        try:
            add({**fields, 'address': address})
        except Exception as error:  # noqa: BLE001 - the console prints any failure
            failed += 1
            first = first or f'{address} {error}'
    return {'ret': f'cf-done:{failed}:{first}'}


def stored_address(address):
    """RouterOS prints a host member without /32."""
    network = ipaddress.ip_network(address, strict=False)
    return str(network.network_address) if network.prefixlen == network.max_prefixlen else str(network)
