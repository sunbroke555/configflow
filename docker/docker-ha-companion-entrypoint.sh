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
import re
import stat
import time
from pathlib import Path


def mount_path(value):
    for escaped, literal in ((r'\040', ' '), (r'\011', '\t'), (r'\012', '\n'), (r'\134', '\\')):
        value = value.replace(escaped, literal)
    return value


def readonly_mount(path, persistent=False):
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        left, separator, right = line.partition(' - ')
        fields, filesystem = left.split(), right.split()
        if separator and len(fields) >= 6 and mount_path(fields[4]) == str(path):
            return ('ro' in fields[5].split(',') and filesystem
                    and (not persistent or filesystem[0] not in ('tmpfs', 'ramfs')))
    return False


def read_regular(path, limit=65536):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise SystemExit('The installation proof must be a regular file, not a symlink or device.')
        value = stream.read(limit + 1)
        if len(value) > limit:
            raise SystemExit('The installation proof is too large.')
        return value


def installation_identity():
    scope = os.environ.get('INSTALL_SCOPE', '')
    if not re.fullmatch(r'[0-9a-f]{32}', scope):
        raise SystemExit('INSTALL_SCOPE must identify this Compose installation.')
    agent_directory = Path('/opt/configflow-agent')
    if (not readonly_mount(agent_directory, persistent=True)
            or not stat.S_ISDIR(agent_directory.lstat().st_mode)):
        raise SystemExit('The Agent data directory must be an independent persistent read-only mount.')
    marker = agent_directory / '.configflow-compose-compact.json'
    deadline = time.monotonic() + 30
    while True:
        # Atomic publication of ready=true follows all three data-directory
        # ownership records. Do not write HA/run while initialization is pending.
        if marker.exists() or marker.is_symlink():
            try:
                record = json.loads(read_regular(marker))
            except (ValueError, UnicodeError):
                raise SystemExit('The installation record is invalid.')
            if (not isinstance(record, dict) or type(record.get('protocol')) is not int
                    or record['protocol'] != 2 or not isinstance(record.get('installation_id'), str)
                    or not re.fullmatch(r'[0-9a-f]{32}', record['installation_id'])
                    or not isinstance(record.get('binding'), dict)
                    or record['binding'].get('session') != scope
                    or not isinstance(record.get('owner'), str)
                    or not re.fullmatch(r'[0-9a-f]{64}', record['owner'])
                    or record['owner'] != hashlib.sha256(json.dumps(
                        record['binding'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()):
                raise SystemExit('The installation record does not belong to this Compose installation.')
            if record.get('ready') is True:
                # A helper may have been given a different core/HA bind path
                # than the Agent. Check every owner before creating HA/run.
                for data_directory in (Path('/etc/oxidns'), Path('/var/lib/configflow-ha')):
                    if not stat.S_ISDIR(data_directory.lstat().st_mode):
                        raise SystemExit('The data directory must not be a symlink.')
                    try:
                        data_record = json.loads(read_regular(data_directory / marker.name))
                    except (ValueError, UnicodeError):
                        raise SystemExit('The data directory ownership record is invalid.')
                    if (not isinstance(data_record, dict)
                            or type(data_record.get('protocol')) is not int
                            or data_record['protocol'] != 2
                            or data_record.get('owner') != record['owner']
                            or data_record.get('installation_id') != record['installation_id']):
                        raise SystemExit('The data directory belongs to a different Compose installation.')
                identity = read_regular(agent_directory / '.configflow-ha-identity', 128).strip()
                if identity != record['installation_id'].encode():
                    raise SystemExit('The installation identity does not match its record.')
                return identity
            if record.get('ready') is not False:
                raise SystemExit('The installation readiness record is invalid.')
        if time.monotonic() >= deadline:
            raise SystemExit('Waiting for the Agent to initialize its data timed out; inspect the Agent container log.')
        time.sleep(0.25)


identity_mode = os.environ.get('CONFIGFLOW_DNS_HA_IDENTITY', 'file') or 'file'
identity_path = Path('/run/configflow-host-machine-id')
if identity_mode == 'installation':
    identity = installation_identity()
elif identity_mode == 'volume':
    identity_directory = Path('/run/configflow-host-identity')
    if not readonly_mount(identity_directory) or not stat.S_ISDIR(identity_directory.lstat().st_mode):
        raise SystemExit('The host identity volume must be an independent read-only mount.')
    identity_path = identity_directory / 'host-id'
    if not stat.S_ISREG(identity_path.lstat().st_mode):
        raise SystemExit('The host identity must be a regular file, not a symlink.')
    identity = identity_path.read_bytes().strip()
elif identity_mode == 'file':
    identity = identity_path.read_bytes().strip()
else:
    raise SystemExit('CONFIGFLOW_DNS_HA_IDENTITY must be file, volume or installation.')
if not 16 <= len(identity) <= 128 or b'\n' in identity or b'\r' in identity:
    raise SystemExit('The host identity file is invalid.')
directory = Path('/var/lib/configflow-ha/run')
directory.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(directory, 0o700)
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
