"""Exercise the opt-in core installer against a fully isolated fake host."""
from copy import deepcopy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest

from backend.agents import gateway_install_script as installer
from backend.agents.go_install_script import generate_go_aio_install_script


def generate(**kwargs):
    params = dict(server_url='https://cf.test', agent_name='gateway', agent_ip='192.168.8.2',
                  agent_checksums={'linux-amd64': 'a' * 64, 'linux-arm64': 'b' * 64},
                  installation_id='install-session-0123456789', enrollment_url='https://cf.test/api/enroll',
                  enrollment_token='session-token-01234567890123456789')
    params.update(kwargs)
    return installer.generate_gateway_install_script(**params)


@pytest.mark.parametrize('params', [
    {'agent_name': 'name;touch /tmp/bad'}, {'agent_ip': '127.0.0.1'}, {'agent_ip': 'bad'},
    {'server_url': 'file:///etc/passwd'}, {'server_url': 'https://user:pass@cf.test'},
    {'enrollment_url': 'https://other.test/enroll'}, {'enrollment_token': 'x\nfoo'},
    {'installation_id': 'bad'}, {'agent_checksums': {}}, {'agent_checksums': {'linux-amd64': 'invalid'}},
    {'agent_checksums': {'linux-armv7': 'a' * 64}}, {'mihomo_agent_port': 8082},
])
def test_invalid_input_rejected(params):
    with pytest.raises(ValueError):
        generate(**params)


def test_opt_in_does_not_change_ordinary_aio_and_sh_syntax(tmp_path):
    ordinary = generate_go_aio_install_script('https://cf.test', 'ordinary')
    assert 'read -r choice < /dev/tty' in ordinary
    # The report helper recognizes secret names; ordinary config has no enrollment key.
    assert '"installation_token":' not in ordinary
    assert 'configflow-gateway' not in ordinary
    assert '-prepare-tun' in ordinary
    script = generate()
    assert 'read -r choice < /dev/tty' not in script
    assert '"installation_id": "install-session-0123456789"' in script
    assert '"installation_token": "session-token-01234567890123456789"' in script
    path = tmp_path / 'script.sh'
    path.write_text(script)
    result = subprocess.run(['sh', '-n', path], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'releases/latest' not in script
    assert 'sysctl -w' not in script
    assert 'enable-ipv4-forwarding.sh' not in script
    assert '99-configflow-aio-forward.conf' not in script
    assert '-prepare-tun' not in script
    assert 'nft add' not in script
    assert 'curl | sh' not in script


def test_hashes_exact_served_agent_files(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENTS_STATIC_DIR', str(tmp_path))
    (tmp_path / 'configflow-agent-linux-amd64').write_bytes(b'new binary')
    assert installer.local_agent_checksums() == {'linux-amd64': hashlib.sha256(b'new binary').hexdigest()}


def host(tmp_path, monkeypatch, manager='systemd', architecture='x86_64'):
    events = tmp_path / 'events'
    assets = tmp_path / 'assets'
    commands = tmp_path / 'commands'
    assets.mkdir()
    commands.mkdir()
    # The fake commands use only stdlib; skip site initialization per process.
    shebang = '#!' + sys.executable + ' -S\n'
    core = shebang + '''import os,sys
from pathlib import Path
kind=Path(sys.argv[0]).name
args=sys.argv[1:]
if '-v' in args:
 version=os.environ.get('EXISTING_MIHOMO_VERSION','v1.19.32') if '/usr/local/bin/' in sys.argv[0] else 'v1.19.32'
 print('Mihomo Meta '+version+' linux amd64'); raise SystemExit()
if '--version' in args: print('oxidns 1.6.1'); raise SystemExit()
with open(os.environ['EVENTS'],'a') as f: f.write('core-check '+kind+' '+' '.join(args)+'\\n')
if os.environ.get('FAIL_CHECK') == kind: raise SystemExit(41)
if '-f' in args: p=args[args.index('-f')+1]
else: p=args[args.index('-c')+1]
assert Path(p).is_file()
'''
    agent = shebang + '''import json,os,sys
args=sys.argv[1:]
if args == ['-h']: raise SystemExit()
if args == ['-version']: print(os.environ.get('AGENT_VERSION','1.8.25-go')); raise SystemExit()
cfg=json.load(open(args[args.index('-config')+1]))
with open(os.environ['EVENTS'],'a') as f: f.write('agent-check '+cfg['service_type']+' '+args[-1]+'\\n')
if '-prepare-tun' in args:
 with open(os.environ['EVENTS'],'a') as f: f.write('prepare-tun:'+cfg['service_type']+'\\n')
 raise SystemExit('Gateway installation must not prepare TUN')
if cfg['service_type'] == os.environ.get('FAIL_AGENT_CHECK'): raise SystemExit(42)
'''
    raw_agent = agent.encode()
    (assets / 'agent').write_bytes(raw_agent)
    (assets / 'mihomo.gz').write_bytes(gzip.compress(core.encode(), mtime=0))
    with tarfile.open(assets / 'oxidns.tar.gz', 'w:gz') as archive:
        member = tarfile.TarInfo('oxidns')
        member.size = len(core.encode())
        member.mode = 0o755
        archive.addfile(member, io.BytesIO(core.encode()))
    releases = deepcopy(installer.CORE_RELEASES)
    for service, file in [('mihomo','mihomo.gz'),('oxidns','oxidns.tar.gz')]:
        for arch in ('linux-amd64','linux-arm64'):
            releases[service][arch]['sha256'] = hashlib.sha256((assets / file).read_bytes()).hexdigest()
    monkeypatch.setattr(installer, 'CORE_RELEASES', releases)
    hashes = {arch: hashlib.sha256(raw_agent).hexdigest() for arch in ('linux-amd64','linux-arm64')}
    source = generate(agent_checksums=hashes)
    # No command sees a real host path: all writable paths, logs, proc reads,
    # service definitions and Agent/core roots are confined to this temp root.
    replacements = [('/etc/', str(tmp_path / 'etc') + '/'), ('/etc ', str(tmp_path / 'etc') + ' '),
                    ('/usr/local', str(tmp_path / 'usr/local')), ('/opt/', str(tmp_path / 'opt') + '/'),
                    ('/opt ', str(tmp_path / 'opt') + ' '), ('/run/', str(tmp_path / 'run') + '/'),
                    ('/run;', str(tmp_path / 'run') + ';'), ('/var/log', str(tmp_path / 'logs')),
                    ('/tmp/configflow-gateway.', str(tmp_path / 'stage.')),
                    ('/dev/net/tun', '/dev/null'), ('/proc/sys/net/ipv4/ip_forward', str(tmp_path / 'ip_forward'))]
    for old,new in replacements:
        source=source.replace(old,new)
    if sys.platform == 'darwin':
        source=source.replace('sed -i ', "sed -i '' ")
    for directory in ['etc','usr/local/bin','opt','run/systemd/system','logs']:
        (tmp_path / directory).mkdir(parents=True,exist_ok=True)
    (tmp_path / 'ip_forward').write_text('0\n')
    (tmp_path / 'active').write_text('')
    manager_code = shebang + '''import json,os,sys
from pathlib import Path
root=Path(os.environ['FAKE_ROOT']); name=Path(sys.argv[0]).name; args=sys.argv[1:]
with open(os.environ['EVENTS'],'a') as f: f.write(name+' '+' '.join(args)+'\\n')
active=set((root/'active').read_text().split())
service=args[1] if name=='systemctl' and len(args)>1 else (args[0] if args else '')
if service.endswith('.service'): service=service[:-8]
if name=='systemctl' and args[0]=='show':
 if '--property=LoadState' in args: print('loaded' if (root/'etc/systemd/system'/f'{service}.service').exists() else 'not-found')
 else:
  text=(root/'etc/systemd/system'/f'{service}.service').read_text()
  command=[l[10:] for l in text.splitlines() if l.startswith('ExecStart=')][0]
  print('{ path='+command.split()[0]+' ; argv[]='+command+' ; }')
 raise SystemExit()
if (name=='systemctl' and args[0]=='is-active'):
 service=args[-1]; raise SystemExit(0 if service in active else 3)
action=args[0] if name=='systemctl' else (args[1] if len(args)>1 else '')
if name=='rc-service' and action=='status': raise SystemExit(0 if service in active else 3)
if action=='start':
 if os.environ.get('FAIL_START')==service: raise SystemExit(43)
 active.add(service); (root/'active').write_text(' '.join(sorted(active)))
 if service.startswith('configflow-agent-'):
  kind=service.removeprefix('configflow-agent-'); p=root/'opt/configflow-agent'/kind/'config.json'
  cfg=json.loads(p.read_text()); cfg.update(agent_id='registered-'+kind,token='agent-token-'+kind)
  p.write_text(json.dumps(cfg,indent=2))
if action=='stop':
 active.discard(service); (root/'active').write_text(' '.join(sorted(active)))
'''
    curl = shebang + '''import json,os,shutil,sys
from pathlib import Path
args=sys.argv[1:]; url=args[-1]; out=Path(args[args.index('-o')+1]); assets=Path(os.environ['ASSETS'])
with open(os.environ['EVENTS'],'a') as f: f.write('download '+url+'\\n')
if '--data-binary' in args:
 body=Path(args[args.index('--data-binary')+1][1:]); headers=Path(args[args.index('-H')+1][1:])
 data=json.loads(body.read_text()); assert data['agents']['mihomo']['token']=='agent-token-mihomo'; assert data['agents']['oxidns']['id']=='registered-oxidns'
 assert headers.read_text().startswith('Authorization: Bearer session-token-')
 out.write_text('{"success":true}'); raise SystemExit()
if os.environ.get('FAIL_GITHUB') == 'yes' and 'github.com/' in url: raise SystemExit(45)
if 'mihomo-linux-' in url: src=assets/'mihomo.gz'
elif 'oxidns-' in url: src=assets/'oxidns.tar.gz'
else: src=assets/'agent'
if os.environ.get('FAIL_DOWNLOAD')==src.name: raise SystemExit(44)
shutil.copyfile(src,out)
if os.environ.get('CORRUPT')==src.name: out.write_bytes(b'bad checksum')
'''
    mocks={'id':'#!/bin/sh\necho 0\n','uname':f'#!/bin/sh\nif [ "$1" = -s ]; then echo Linux; else echo {architecture}; fi\n',
           'curl':curl,'ip':'#!/bin/sh\nexit 0\n','nft':'#!/bin/sh\n[ "${FAIL_NFT:-}" != yes ]\n',
           'ss':'#!/bin/sh\nif [ -n "${BUSY_PORT:-}" ]; then echo "udp UNCONN 0 0 0.0.0.0:$BUSY_PORT 0.0.0.0:*"; fi\n',
           'sysctl':'#!/bin/sh\nprintf "forbidden-sysctl %s\\n" "$*" >> "$EVENTS"\nexit 98\n',
           'modprobe':'#!/bin/sh\nprintf "forbidden-modprobe %s\\n" "$*" >> "$EVENTS"\nexit 97\n',
           'mknod':'#!/bin/sh\nprintf "forbidden-mknod %s\\n" "$*" >> "$EVENTS"\nexit 97\n',
           'sleep':'#!/bin/sh\nexit 0\n'}
    mocks.update({'systemctl':manager_code} if manager=='systemd' else {'rc-service':manager_code,'rc-update':manager_code})
    for name,body in mocks.items():
        p=commands/name;p.write_text(body);p.chmod(0o755)
    script=tmp_path/'install.sh';script.write_text(source)
    environment=dict(os.environ,PATH=str(commands)+os.pathsep+os.environ['PATH'],FAKE_ROOT=str(tmp_path),EVENTS=str(events),ASSETS=str(assets))
    def run(**overrides):
        return subprocess.run(['sh',str(script)],env=dict(environment,**overrides),capture_output=True,text=True,timeout=30)
    return run,events,source


@pytest.mark.parametrize('manager,architecture',[('systemd','x86_64'),('openrc','aarch64')])
def test_new_install_validates_before_mutation_registers_and_never_applies_forwarding(tmp_path,monkeypatch,manager,architecture):
    run,events,source=host(tmp_path,monkeypatch,manager,architecture)
    result=run()
    assert result.returncode==0,result.stdout+result.stderr
    log=events.read_text()
    assert log.index('core-check mihomo')<log.index('core-check oxidns')<log.index('enable' if manager=='systemd' else 'rc-update -u')
    assert (tmp_path/'ip_forward').read_text()=='0\n'
    assert 'forbidden-sysctl' not in log
    assert 'prepare-tun:' not in log
    assert 'forbidden-modprobe' not in log and 'forbidden-mknod' not in log
    assert not (tmp_path/'etc/sysctl.d/99-configflow-aio-forward.conf').exists()
    assert not (tmp_path/'opt/configflow-agent/mihomo/enable-ipv4-forwarding.sh').exists()
    assert 'enable: false' in (tmp_path/'etc/mihomo/config.yaml').read_text()
    assert '127.0.0.1:53' in (tmp_path/'etc/oxidns/config.yaml').read_text()
    assert '0.0.0.0:53' not in (tmp_path/'etc/oxidns/config.yaml').read_text()
    assert log.count('configflow-agent-linux-')==1
    assert 'session-token' not in result.stdout+result.stderr
    assert 'agent-token' not in result.stdout+result.stderr
    for kind in ('mihomo','oxidns'):
        cfg=json.loads((tmp_path/f'opt/configflow-agent/{kind}/config.json').read_text())
        assert cfg['installation_id']=='install-session-0123456789'
        assert cfg['service_binary']==str(tmp_path/f'usr/local/bin/{kind}')
    assert not (tmp_path/'run/configflow-gateway-install.lock').exists()


@pytest.mark.parametrize('failure',[{'CORRUPT':'oxidns.tar.gz'},{'FAIL_DOWNLOAD':'agent'}, {'FAIL_CHECK':'oxidns'},
                                  {'AGENT_VERSION':'1.8.24-go'}, {'AGENT_VERSION':'invalid'},
                                  {'AGENT_VERSION':'1.8.25-beta'}, {'BUSY_PORT':'53'}, {'FAIL_NFT':'yes'}])
def test_preflight_download_and_validation_failures_leave_host_untouched(tmp_path,monkeypatch,failure):
    run,events,source=host(tmp_path,monkeypatch)
    result=run(**failure)
    assert result.returncode!=0
    assert not (tmp_path/'etc/mihomo').exists()
    assert not (tmp_path/'etc/oxidns').exists()
    assert not (tmp_path/'opt/configflow-agent').exists()
    assert (tmp_path/'ip_forward').read_text()=='0\n'


def test_rerun_preserves_published_configs_and_existing_agent_identity(tmp_path,monkeypatch):
    run,events,source=host(tmp_path,monkeypatch)
    assert run().returncode==0
    for kind in ('mihomo','oxidns'):
        (tmp_path/f'etc/{kind}/config.yaml').write_text('# user published configuration\n')
    events.write_text('')
    result=run(FAIL_GITHUB='yes')
    assert result.returncode==0,result.stdout+result.stderr
    assert 'download https://github.com/' not in events.read_text()
    for kind in ('mihomo','oxidns'):
        assert (tmp_path/f'etc/{kind}/config.yaml').read_text()=='# user published configuration\n'
        cfg=json.loads((tmp_path/f'opt/configflow-agent/{kind}/config.json').read_text())
        assert cfg['agent_id']=='registered-'+kind


def test_partial_start_failure_can_resume_without_rewriting_configs(tmp_path,monkeypatch):
    run,events,source=host(tmp_path,monkeypatch)
    failed=run(FAIL_START='oxidns')
    assert failed.returncode!=0
    before=(tmp_path/'etc/mihomo/config.yaml').read_bytes()
    assert '重新运行同一命令' in failed.stderr
    result=run()
    assert result.returncode==0,result.stdout+result.stderr
    assert (tmp_path/'etc/mihomo/config.yaml').read_bytes()==before


def test_partial_foreign_layout_is_rejected_without_overwrite(tmp_path,monkeypatch):
    run,events,source=host(tmp_path,monkeypatch)
    path=tmp_path/'etc/mihomo/config.yaml';path.parent.mkdir();path.write_text('external config')
    result=run()
    assert result.returncode!=0
    assert path.read_text()=='external config'
    assert not (tmp_path/'etc/oxidns').exists()
    assert not (path.parent/'.configflow-gateway-core-v1').exists()


@pytest.mark.parametrize('manager',['systemd','openrc'])
def test_complete_existing_layout_attaches_without_mutating_cores(tmp_path,monkeypatch,manager):
    run,events,source=host(tmp_path,monkeypatch,manager)
    assert run().returncode==0
    snapshots={}
    for kind in ('mihomo','oxidns'):
        (tmp_path/f'etc/{kind}/.configflow-gateway-core-v1').unlink()
        path=tmp_path/f'etc/{kind}/config.yaml';path.write_text('# existing external config\n')
        snapshots[path]=path.read_bytes()
        unit=tmp_path/(f'etc/systemd/system/{kind}.service' if manager=='systemd' else f'etc/init.d/{kind}')
        snapshots[unit]=unit.read_bytes()
        binary=tmp_path/f'usr/local/bin/{kind}'
        snapshots[binary]=binary.read_bytes()
    events.write_text('')
    result=run(FAIL_GITHUB='yes')
    assert result.returncode==0,result.stdout+result.stderr
    assert '保留并接入已有' in result.stdout
    for path,data in snapshots.items(): assert path.read_bytes()==data
    log=events.read_text()
    assert 'download https://github.com/' not in log
    assert log.count('configflow-agent-linux-')==1
    assert 'systemctl start mihomo' not in log
    assert 'rc-service oxidns start' not in log


@pytest.mark.parametrize('existing', ['mihomo', 'oxidns'])
def test_one_external_core_is_preserved_while_only_missing_peer_is_installed(tmp_path, monkeypatch, existing):
    run, events, source = host(tmp_path, monkeypatch)
    initial = run()
    assert initial.returncode == 0, initial.stdout + initial.stderr
    missing = 'oxidns' if existing == 'mihomo' else 'mihomo'
    (tmp_path / f'etc/{existing}/.configflow-gateway-core-v1').unlink()
    paths = [tmp_path / f'etc/{existing}/config.yaml', tmp_path / f'usr/local/bin/{existing}',
             tmp_path / f'etc/systemd/system/{existing}.service']
    original = {path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode) for path in paths}
    shutil.rmtree(tmp_path / f'etc/{missing}')
    (tmp_path / f'usr/local/bin/{missing}').unlink()
    (tmp_path / f'etc/systemd/system/{missing}.service').unlink()
    active = tmp_path / 'active'
    active.write_text(' '.join(service for service in active.read_text().split() if service != missing))
    events.write_text('')
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    log = events.read_text()
    downloads = [line for line in log.splitlines() if line.startswith('download https://github.com/')]
    assert len(downloads) == 1
    assert ('mihomo-linux-' if missing == 'mihomo' else 'oxidns-') in downloads[0]
    for path, expected in original.items():
        assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode) == expected
    assert f'systemctl start {existing}' not in log
    assert (tmp_path / f'etc/{missing}/.configflow-gateway-core-v1').is_file()
    assert (tmp_path / 'ip_forward').read_text() == '0\n'


@pytest.mark.parametrize('kind', ['mihomo', 'oxidns'])
def test_owned_missing_binary_downloads_only_that_core_and_checks_preserved_config(tmp_path, monkeypatch, kind):
    run, events, source = host(tmp_path, monkeypatch)
    initial = run()
    assert initial.returncode == 0, initial.stdout + initial.stderr
    binary = tmp_path / f'usr/local/bin/{kind}'
    binary.unlink()
    config = tmp_path / f'etc/{kind}/config.yaml'
    config.write_text('# retained published config\n')
    events.write_text('')
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    log = events.read_text()
    downloads = [line for line in log.splitlines() if line.startswith('download https://github.com/')]
    assert len(downloads) == 1
    assert ('mihomo-linux-' if kind == 'mihomo' else 'oxidns-') in downloads[0]
    assert config.read_text() == '# retained published config\n'
    assert binary.is_file()
    checks = [line for line in log.splitlines() if line.startswith('core-check ' + kind + ' ')]
    assert len(checks) == 1 and str(config) in checks[0]


@pytest.mark.parametrize('kind', ['mihomo', 'oxidns'])
def test_owned_missing_config_checks_bootstrap_with_existing_binary_without_github(tmp_path, monkeypatch, kind):
    run, events, source = host(tmp_path, monkeypatch)
    initial = run()
    assert initial.returncode == 0, initial.stdout + initial.stderr
    config = tmp_path / f'etc/{kind}/config.yaml'
    config.unlink()
    binary = tmp_path / f'usr/local/bin/{kind}'
    original = binary.read_bytes()
    events.write_text('')
    result = run(FAIL_GITHUB='yes')
    assert result.returncode == 0, result.stdout + result.stderr
    log = events.read_text()
    assert 'download https://github.com/' not in log
    assert binary.read_bytes() == original
    assert config.is_file()
    checks = [line for line in log.splitlines() if line.startswith('core-check ' + kind + ' ')]
    assert len(checks) == 1 and f'/{kind}.yaml' in checks[0] and str(config) not in checks[0]


@pytest.mark.parametrize('kind', ['mihomo', 'oxidns'])
def test_partial_recovery_native_check_failure_preserves_missing_core(tmp_path, monkeypatch, kind):
    run, events, source = host(tmp_path, monkeypatch)
    initial = run()
    assert initial.returncode == 0, initial.stdout + initial.stderr
    binary = tmp_path / f'usr/local/bin/{kind}'
    binary.unlink()
    config = tmp_path / f'etc/{kind}/config.yaml'
    original = config.read_bytes()
    events.write_text('')
    result = run(FAIL_CHECK=kind)
    assert result.returncode != 0
    assert not binary.exists()
    assert config.read_bytes() == original
    assert 'systemctl enable' not in events.read_text()


@pytest.mark.parametrize('version',['v1.19.32-alpha','v1.19.32-custom','v1.18.9','alpha'])
def test_existing_mihomo_must_be_supported_stable_release_before_any_install_changes(tmp_path,monkeypatch,version):
    run,events,source=host(tmp_path,monkeypatch)
    initial=run()
    assert initial.returncode==0,initial.stdout+initial.stderr
    before={kind:(tmp_path/f'etc/{kind}/config.yaml').read_bytes() for kind in ('mihomo','oxidns')}
    events.write_text('')
    result=run(EXISTING_MIHOMO_VERSION=version)
    assert result.returncode!=0
    assert '稳定版' in result.stderr
    assert 'systemctl enable' not in events.read_text()
    for kind,content in before.items(): assert (tmp_path/f'etc/{kind}/config.yaml').read_bytes()==content
