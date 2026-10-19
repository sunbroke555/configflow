"""Optional real-core acceptance for the bytes shipped in Agent bundles.

Set OXIDNS_TEST_BINARY and/or MOSDNS_TEST_BINARY to isolated core executables.
Every listener and upstream in these tests is local and uses a temporary port.
"""
from collections import Counter
from copy import deepcopy
import io
import os
from pathlib import Path
import socket
import struct
import subprocess
import tarfile
import threading
import time

import pytest
import yaml

from backend.agents.deployment_bundle import build_deployment_bundle
from backend.converters.mosdns import generate_mosdns_config
from backend.converters.oxidns import generate_oxidns_config


WATCHDOG_NAME = 'configflow-watchdog.invalid'


def _question_name(packet):
    labels, offset = [], 12
    while packet[offset]:
        size = packet[offset]
        labels.append(packet[offset + 1:offset + 1 + size].decode('ascii').lower())
        offset += size + 1
    return '.'.join(labels)


class BlackholeUpstream:
    """Receive and count real upstream traffic without ever answering it."""

    def __enter__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(('127.0.0.1', 0))
        self.socket.settimeout(.05)
        self.address = 'udp://127.0.0.1:' + str(self.socket.getsockname()[1])
        self.counts = Counter()
        self.condition = threading.Condition()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._receive, daemon=True)
        self.thread.start()
        return self

    def _receive(self):
        while not self.stopped.is_set():
            try:
                packet, _ = self.socket.recvfrom(65535)
            except socket.timeout:
                continue
            with self.condition:
                self.counts[_question_name(packet)] += 1
                self.condition.notify_all()

    def received(self, name, timeout=2):
        with self.condition:
            return self.condition.wait_for(lambda: self.counts[name] > 0, timeout)

    def __exit__(self, *_):
        self.stopped.set()
        self.thread.join(timeout=1)
        self.socket.close()


def _free_dns_port():
    # Require the same free port for the UDP and TCP listeners.
    for _ in range(20):
        with socket.socket() as tcp, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            tcp.bind(('127.0.0.1', 0))
            port = tcp.getsockname()[1]
            try:
                udp.bind(('127.0.0.1', port))
            except OSError:
                continue
            return port
    pytest.fail('Could not reserve a temporary UDP/TCP port')


def _configuration(engine, mode, port, upstream):
    listen = f'127.0.0.1:{port}'
    if mode == 'template':
        settings = {'local_dns': upstream, 'remote_dns': upstream, 'fallback_dns': upstream}
        if engine == 'oxidns':
            settings.update(listen=listen, log={'level': 'error'})
        else:
            settings.update(listen_address=listen, log_enabled=False, cache_dump_enabled=False,
                            custom_config='api:\n  http: ""\n')
        config = {engine: settings}
    else:
        entry = 'existing_entry' if mode == 'native_sequence' else 'existing_forward'
        plugins = [{'tag': 'existing_forward', 'type': 'forward',
                    'args': {'upstreams': [{'addr': upstream}]}}]
        if mode == 'native_sequence':
            plugins.append({'tag': entry, 'type': 'sequence',
                            'args': [{'exec': '$existing_forward'}]})
        plugins.extend({'tag': f'server_{transport}', 'type': f'{transport}_server',
                        'args': {'listen': listen, 'entry': entry}} for transport in ('udp', 'tcp'))
        document = {'log': {'level': 'error'}, 'plugins': plugins}
        # OxiDNS uses its real native-config validation/rendering path. MosDNS
        # accepts the same already-rendered plugin document at bundle creation.
        config = {engine: {'config_mode': 'native', 'native_config': yaml.safe_dump(document)}}
        if engine == 'mosdns':
            return config, yaml.safe_dump(document)
    render = generate_oxidns_config if engine == 'oxidns' else generate_mosdns_config
    return config, render(config)


def _packet(name, qtype=1):
    question = b''.join(bytes([len(label)]) + label.encode('ascii')
                        for label in name.rstrip('.').split('.'))
    return struct.pack('!6H', 54321, 0x0100, 1, 0, 0, 0) + question + b'\0' + struct.pack('!HH', qtype, 1)


def _read_exact(connection, size):
    data = b''
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        assert chunk, 'Core closed the DNS connection before returning a complete message'
        data += chunk
    return data


def _query(port, name, transport='udp', qtype=1, timeout=1):
    packet = _packet(name, qtype)
    kind = socket.SOCK_STREAM if transport == 'tcp' else socket.SOCK_DGRAM
    with socket.socket(socket.AF_INET, kind) as client:
        client.settimeout(timeout)
        client.connect(('127.0.0.1', port))
        if transport == 'tcp':
            client.sendall(struct.pack('!H', len(packet)) + packet)
            size = struct.unpack('!H', _read_exact(client, 2))[0]
            return _read_exact(client, size)
        client.send(packet)
        return client.recv(65535)


def _wait_until_listening(process, port, log_path):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        assert process.poll() is None, log_path.read_text()
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=.1):
                return
        except OSError:
            time.sleep(.02)
    pytest.fail('DNS listener did not start: ' + log_path.read_text())


@pytest.mark.parametrize('engine', ['oxidns', 'mosdns'])
@pytest.mark.parametrize('mode', ['template', 'native_sequence', 'native_forward'])
def test_packaged_watchdog_answers_locally_while_upstreams_are_unresponsive(tmp_path, engine, mode):
    binary = os.environ.get(engine.upper() + '_TEST_BINARY')
    if not binary:
        pytest.skip(f'Set {engine.upper()}_TEST_BINARY for real-core acceptance')
    binary = str(Path(binary).resolve())
    port = _free_dns_port()
    with BlackholeUpstream() as upstream:
        config, rendered = _configuration(engine, mode, port, upstream.address)
        before = deepcopy(config)
        bundle = build_deployment_bundle({'id': 'watchdog-core-test', 'service_type': engine,
                                          'profile_id': 'default'}, config, rendered)
        assert config == before
        # Run precisely the final published bytes, including all bundled assets.
        with tarfile.open(fileobj=io.BytesIO(bundle.archive), mode='r:gz') as archive:
            for item in bundle.manifest['files']:
                path = tmp_path / item['path']
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.extractfile('files/' + item['path']).read())
        path = tmp_path / bundle.manifest['config_path']
        if engine == 'oxidns':
            checked = subprocess.run([binary, 'check', '-c', str(path), '-d', str(tmp_path)],
                                     capture_output=True, text=True, timeout=15)
            assert checked.returncode == 0, checked.stdout + checked.stderr
        log_path = tmp_path / 'core.log'
        with log_path.open('w') as log:
            process = subprocess.Popen([binary, 'start', '-c', str(path), '-d', str(tmp_path)],
                                       stdout=log, stderr=log, cwd=tmp_path)
            try:
                _wait_until_listening(process, port, log_path)
                for transport in ('udp', 'tcp'):
                    for qtype in (1, 28):
                        started = time.monotonic()
                        reply = _query(port, WATCHDOG_NAME.upper() + '.', transport, qtype)
                        assert time.monotonic() - started < 1
                        identifier, flags, questions, answers, _, _ = struct.unpack('!6H', reply[:12])
                        assert identifier == 54321 and flags & 0x8000
                        assert flags & 15 == 3, reply.hex()  # NXDOMAIN, not an upstream timeout.
                        assert questions == 1 and answers == 0
                assert not upstream.received(WATCHDOG_NAME, timeout=.1), dict(upstream.counts)
                assert not upstream.counts, dict(upstream.counts)
                # The exact guard must preserve normal forwarding and must not
                # swallow subdomains that merely contain the watchdog name.
                for name in ('ordinary.test', 'child.' + WATCHDOG_NAME):
                    with pytest.raises(socket.timeout):
                        _query(port, name, timeout=.15)
                    assert upstream.received(name), log_path.read_text()
                assert upstream.counts[WATCHDOG_NAME] == 0
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
