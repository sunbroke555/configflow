#!/bin/sh
set -e

if [ "${CONFIGFLOW_DNS_HA:-}" != 1 ] || [ "${CONFIGFLOW_DNS_HA_NETWORK:-}" != host ]; then
    echo "The HA companion requires CONFIGFLOW_DNS_HA=1 and CONFIGFLOW_DNS_HA_NETWORK=host." >&2
    exit 1
fi

# The main Agent reads this evidence and verifies the live Unix socket peer.
# Identity/config/core data are read-only here; only the dedicated HA directory
# is writable. A journal never causes this entrypoint to start keepalived.
python3 - <<'PYCOMPANION'
import hashlib
import json
import os
from pathlib import Path

directory = Path('/var/lib/configflow-ha/run')
directory.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(directory, 0o700)
identity = Path('/run/configflow-host-machine-id').read_bytes().strip()
if not identity:
    raise SystemExit('The existing host identity file is empty.')
cap_eff = next(line.split(':', 1)[1].strip() for line in Path('/proc/self/status').read_text().splitlines()
               if line.startswith('CapEff:'))

def namespace(name):
    info = os.stat('/proc/self/ns/' + name)
    return {'device': info.st_dev, 'inode': info.st_ino}

evidence = {'protocol': 1, 'mode': 'host', 'host_id': hashlib.sha256(identity).hexdigest(),
            'cap_eff': cap_eff, 'network_namespace': namespace('net'), 'pid_namespace': namespace('pid')}
path = directory / 'companion.json'
temporary = directory / ('.companion-' + str(os.getpid()))
with temporary.open('w') as output:
    os.chmod(temporary, 0o600)
    json.dump(evidence, output)
    output.flush()
    os.fsync(output.fileno())
os.replace(temporary, path)
parent = os.open(directory, os.O_RDONLY)
try:
    os.fsync(parent)
finally:
    os.close(parent)
PYCOMPANION

exec /usr/bin/supervisord -c /etc/supervisor/ha-companion.conf
