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
import stat
from pathlib import Path

directory = Path('/var/lib/configflow-ha/run')
directory.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(directory, 0o700)
identity_mode = os.environ.get('CONFIGFLOW_DNS_HA_IDENTITY', 'file') or 'file'
identity_path = Path('/run/configflow-host-machine-id')
if identity_mode == 'volume':
    identity_directory = Path('/run/configflow-host-identity')
    # A read-only parent directory in the container layer is not a volume.
    # Match an actual, read-only mount before reading its ordinary identity file.
    def mount_path(value):
        for escaped, literal in ((r'\040', ' '), (r'\011', '\t'), (r'\012', '\n'), (r'\134', '\\')):
            value = value.replace(escaped, literal)
        return value
    mounted = False
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        left, separator, right = line.partition(' - ')
        fields = left.split()
        if separator and len(fields) >= 6 and mount_path(fields[4]) == str(identity_directory):
            mounted = 'ro' in fields[5].split(',')
            break
    if not mounted or not stat.S_ISDIR(identity_directory.lstat().st_mode):
        raise SystemExit('The host identity volume must be an independent read-only mount.')
    identity_path = identity_directory / 'host-id'
    if not stat.S_ISREG(identity_path.lstat().st_mode):
        raise SystemExit('The host identity must be a regular file, not a symlink.')
elif identity_mode != 'file':
    raise SystemExit('CONFIGFLOW_DNS_HA_IDENTITY must be file or volume.')
identity = identity_path.read_bytes().strip()
if not 16 <= len(identity) <= 128 or b'\n' in identity or b'\r' in identity:
    raise SystemExit('The host identity file is invalid.')
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
