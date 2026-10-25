"""RouterOS batch endpoints for the in-memory REST fakes in tests.

The fakes keep their own rows; these helpers interpret what the driver sends
to /execute (route and NAT add scripts), POST .../print queries and POST
.../remove the way RouterOS 7.24 does.
"""
import ipaddress
import re

ADD_LINE = re.compile(r':if \(\$f=0\) do=\{:onerror e in=\{:put \("cf-id:(?P<index>\d+):" \. '
                      r'\[(?P<path>/ip/route|/ip/firewall/nat)/add (?P<params>.*)\]\)\} '
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
