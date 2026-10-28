#!/usr/bin/env python3
"""Real Shell HA installs in disposable PVE guests; run only on 192.168.0.32.

CT190-193 are created by this task, connected only to the existing cfgt test
network. Retained CT132 is snapshot-protected and restored afterwards.
Private scripts/tokens/logs never enter the public report.
"""
from __future__ import annotations
import argparse, concurrent.futures, hashlib, io, json, os, secrets, shlex, socket, subprocess, sys, tarfile, threading, time, urllib.request, urllib.error
from pathlib import Path
ROOT = Path('/home/sunbroke/configflow-shell-ha-prepare-lab')
REPO = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / 'private'
GROOT = '/opt/cf-shell-ha-prepare'
SNAPSHOT = 'cf-shell-ha-prepare-20261027'
UNIT = 'cf-shell-ha-prepare-backend'
SERVER = 'http://10.97.0.74:18080'
LOCAL = 'http://127.0.0.1:19290'
GUESTS = {
190: ('debian', 'debian-12-standard_12.12-1_amd64.tar.zst', 'debian'),
191: ('alpine', 'alpine-3.22-default_20250617_amd64.tar.xz', 'alpine'),
192: ('arch', 'archlinux-base_20260420-1_amd64.tar.zst', 'archlinux'),
193: ('fedora', 'fedora-43-default_20260115_amd64.tar.xz', 'fedora'),
}
CREATED_TEMPLATES = [value[1] for cid,value in GUESTS.items() if cid != 190]
REPORT_LOCK = threading.Lock()

def run(args, data=None, check=True, timeout=120):
    p = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError('command failed '+shlex.join([str(a) for a in args[:6]])+': '+p.stderr.decode(errors='replace')[-1200:])
    return p

def pve(*args, **kw):
    return run(['ssh','-o','BatchMode=yes','pve',shlex.join([str(a) for a in args])],**kw)

def guest(cid,*args,**kw):
    if cid not in GUESTS and cid != 132: raise RuntimeError('Guest outside explicit allowlist')
    return pve('pct','exec',cid,'--',*args,**kw)

def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');path.chmod(0o600)

def report(name,**values):
    with REPORT_LOCK:
        path=ROOT/'report.json'; data=json.loads(path.read_text()) if path.exists() else {'scenarios':[]}
        data['scenarios'].append({'name':name,'at':time.time(),**values});save(path,data)
    print(json.dumps({'scenario':name,'passed':True},ensure_ascii=False),flush=True)

def put(cid,path,data,mode='600'):
    if not path.startswith((GROOT+'/', '/root/cf-shell-ha-', '/etc/keepalived/', '/etc/oxidns/rules/', '/srv/cf-shell-ha-custom/')): raise RuntimeError('Unexpected guest write')
    parent=str(Path(path).parent)
    guest(cid,'mkdir','-p',parent)
    guest(cid,'sh','-c','cat > '+shlex.quote(path)+' && chmod '+mode+' '+shlex.quote(path),data=data if isinstance(data,bytes) else data.encode())

def guard():
    if sys.platform!='linux' or socket.gethostname()=='pve': raise RuntimeError('Run from .32 Linux driver')
    manifest=json.loads((PRIVATE/'manifest.json').read_text())
    for cid in manifest['created']:
        text=pve('pct','config',cid).stdout.decode()
        if f'hostname: cf-shell-ha-{GUESTS[cid][0]}\n' not in text or 'bridge=cfgt' not in text or 'bridge=vmbr' in text or manifest['owner'] not in text:
            raise RuntimeError(f'Guest {cid} ownership/network changed')
    if SNAPSHOT not in pve('pct','listsnapshot',132).stdout.decode(): raise RuntimeError('Backend recovery snapshot missing')
    return manifest

def prepare():
    PRIVATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    if (PRIVATE/'manifest.json').exists(): raise RuntimeError('Existing manifest; refusing new ownership')
    ids=json.loads(pve('pvesh','get','/cluster/resources','--type','vm','--output-format','json').stdout)
    assert not set(GUESTS)&{int(item['vmid']) for item in ids}, 'Temporary IDs already used'
    backend=pve('pct','config',132).stdout.decode(); status=pve('pct','status',132).stdout.decode()
    assert 'hostname: cf-app-retest\n' in backend and 'bridge=cfgt' in backend and 'stopped' in status
    manifest={'owner':'cf-shell-ha-prepare-'+secrets.token_hex(8),'created':[],'backend_config':backend,'backend_status':status,'started_at':time.time(),'templates_created':CREATED_TEMPLATES}
    save(PRIVATE/'manifest.json',manifest)
    pve('pct','snapshot',132,SNAPSHOT,'--description','Before one-click Shell HA install test')
    pve('pct','start',132)
    for cid,(name,template,ostype) in GUESTS.items():
        pve('pct','create',cid,'local:vztmpl/'+template,'--hostname','cf-shell-ha-'+name,'--description',manifest['owner'],
            '--arch','amd64','--ostype',ostype,'--cores',1,'--memory',768,'--swap',0,'--rootfs','local-lvm:4',
            '--unprivileged',1,'--onboot',0,'--features','nesting=1','--net0',f'name=eth0,bridge=cfgt,ip=10.97.0.{cid-100}/24,gw=10.97.0.1',
            '--nameserver','1.1.1.1',timeout=180)
        manifest['created'].append(cid);save(PRIVATE/'manifest.json',manifest)
        pve('pct','start',cid)
    for cid in GUESTS:
        for _ in range(30):
            probe=guest(cid,'sh','-c','test -f /etc/os-release && cat /etc/os-release',check=False)
            if probe.returncode==0: break
            time.sleep(1)
        (PRIVATE/f'{cid}-os-release.txt').write_bytes(probe.stdout)
        baseline=guest(cid,'sh','-c','command -v keepalived || true; command -v ip || true; test ! -d /etc/oxidns && test ! -d /opt/configflow-agent && echo no_previous_install').stdout.decode()
        save(PRIVATE/f'{cid}-baseline.json',{'os_release':probe.stdout.decode(),'tools':baseline})
    report('isolated-disposable-guests-prepared',guests=list(GUESTS),backend_snapshot=SNAPSHOT,network='cfgt 10.97.0.0/24; production LAN blocked by existing nft rules')

def reset_guests():
    manifest=guard()
    archive=PRIVATE/('previous-round-'+str(int(time.time())))
    archive.mkdir(mode=0o700)
    for name in ('source-hashes.json','report.json'):
        item=ROOT/name
        if item.exists(): (archive/name).write_bytes(item.read_bytes())
    for item in PRIVATE.glob('*-install.log'):
        (archive/item.name).write_bytes(item.read_bytes()); item.unlink()
    # A retained evidence round is never mistaken for final-version coverage.
    if guest(193,'systemctl','is-active','systemd-resolved',check=False).returncode==0:
        guest(193,'sh','-c','test ! -d /etc/oxidns && test ! -d /opt/configflow-agent')
        report('fedora-existing-dns-port-safely-refused',existing_dns_still_active=True,core_agent_not_written=True)
    for cid,(name,template,ostype) in GUESTS.items():
        pve('pct','stop',cid,check=False)
        pve('pct','destroy',cid,'--purge',1,'--destroy-unreferenced-disks',1)
        pve('pct','create',cid,'local:vztmpl/'+template,'--hostname','cf-shell-ha-'+name,'--description',manifest['owner'],
            '--arch','amd64','--ostype',ostype,'--cores',1,'--memory',768,'--swap',0,'--rootfs','local-lvm:4',
            '--unprivileged',1,'--onboot',0,'--features','nesting=1','--net0',f'name=eth0,bridge=cfgt,ip=10.97.0.{cid-100}/24,gw=10.97.0.1',
            '--nameserver','1.1.1.1',timeout=180)
        pve('pct','start',cid)
    # This old PVE kernel lacks Landlock. Keep the workaround only in this
    # disposable Arch fixture; the product never disables package sandboxing.
    guest(192,'sh','-c',r"sed -i '/^\[options\]/a DisableSandbox' /etc/pacman.conf")
    # Fedora's template has resolved listening on :53. The prior attempt must
    # refuse; an operator-selected fixture adjustment makes a separate success case.
    guest(193,'sh','-c',"mkdir -p /etc/systemd/resolved.conf.d; printf '[Resolve]\nDNSStubListener=no\n' > /etc/systemd/resolved.conf.d/cf-shell-ha-lab.conf; systemctl restart systemd-resolved")
    guest(193,'systemctl','is-active','systemd-resolved')
    for cid in GUESTS:
        guest(cid,'sh','-c','test ! -d /etc/oxidns && test ! -d /opt/configflow-agent && ! command -v keepalived')
        pve('pct','snapshot',cid,'cf-shell-ha-clean','--description','Clean isolated OS before final generated installer')
    report('clean-final-round-fixtures',manual_ha_dependency_install=False,
           arch_fixture_only='DisableSandbox for PVE kernel without Landlock; no host or product change',
           fedora_fixture_only='resolved remains active; DNSStubListener=no releases 53 after refusal case')

def network():
    manifest=guard()
    if manifest.get('network_rule'): raise RuntimeError('Network exception already recorded')
    before=pve('nft','list','table','inet','cfgt').stdout.decode()
    comment=manifest['owner']+'-isolated-guests'
    addresses='{ 10.97.0.74, 10.97.0.90, 10.97.0.91, 10.97.0.92, 10.97.0.93 }'
    pve('nft','insert','rule','inet','cfgt','forward','iifname','cfgt','oifname','cfgt','ip','saddr',addresses,'ip','daddr',addresses,'accept','comment','"'+comment+'"')
    rules=json.loads(pve('nft','-j','-a','list','chain','inet','cfgt','forward').stdout)['nftables']
    matches=[r['rule'] for r in rules if r.get('rule',{}).get('comment')==comment]
    assert len(matches)==1
    manifest.update(network_before=before,network_rule={'comment':comment,'handle':matches[0]['handle']});save(PRIVATE/'manifest.json',manifest)
    backend_before=guest(132,'nft','list','table','inet','cf_retest_egress',check=False)
    if backend_before.returncode==0:
        reply_comment=manifest['owner']+'-reply'
        guest(132,'nft','insert','rule','inet','cf_retest_egress','output','ip','daddr','{ 10.97.0.90, 10.97.0.91, 10.97.0.92, 10.97.0.93 }','accept','comment','"'+reply_comment+'"')
        manifest.update(backend_firewall_before=backend_before.stdout.decode(),backend_firewall_comment=reply_comment);save(PRIVATE/'manifest.json',manifest)
    report('isolated-guest-connectivity',allowed_ips=['10.97.0.74','10.97.0.90','10.97.0.91','10.97.0.92','10.97.0.93'],production_network_access=False)

def stage_backend():
    guard()
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w:gz') as arc:
        for name in ('backend','scripts','config_template.json','version.json'):
            p=REPO/name
            if p.exists(): arc.add(p,arcname=name,filter=lambda item: None if '__pycache__' in item.name or item.name.endswith('.pyc') else item)
    guest(132,'mkdir','-p',GROOT+'/source',GROOT+'/static-agents')
    guest(132,'tar','-xzf','-','-C',GROOT+'/source',data=stream.getvalue())
    binary=ROOT/'configflow-agent'
    assert binary.is_file()
    put(132,GROOT+'/static-agents/configflow-agent-linux-amd64',binary.read_bytes(),'755')
    creds={'username':'shell-ha-lab','password':secrets.token_urlsafe(28),'jwt_secret':secrets.token_urlsafe(40),'registration_key':''}
    save(PRIVATE/'credentials.json',creds)
    put(132,GROOT+'/credentials.json',json.dumps(creds))
    guest(132,'systemctl','stop',UNIT,check=False)
    guest(132,'systemctl','reset-failed',UNIT,check=False)
    guest(132,'systemd-run','--unit='+UNIT,'--property=WorkingDirectory='+GROOT,'/opt/cf-retest/venv/bin/python',GROOT+'/source/scripts/tests/oxidns_shell_ha_prepare_lab.py','server')
    pidfile=PRIVATE/'tunnel.pid'
    if pidfile.exists():
        pid=int(pidfile.read_text()); proc=Path(f'/proc/{pid}/cmdline')
        if proc.exists() and b'19290:10.97.0.74:18080' in proc.read_bytes():
            os.kill(pid,15); time.sleep(0.5)
    tunnel=subprocess.Popen(['ssh','-N','-o','BatchMode=yes','-o','ExitOnForwardFailure=yes','-L','127.0.0.1:19290:10.97.0.74:18080','pve'],stdout=subprocess.DEVNULL,stderr=(PRIVATE/'tunnel.log').open('ab'))
    (PRIVATE/'tunnel.pid').write_text(str(tunnel.pid))
    for _ in range(40):
        try:
            login=api('POST','/api/auth/login',{'username':creds['username'],'password':creds['password']},authenticated=False)
            creds['admin_token']=login['token'];save(PRIVATE/'credentials.json',creds);break
        except (OSError,KeyError,RuntimeError):time.sleep(1)
    else:raise RuntimeError('Temporary backend unavailable')
    sources={str(p.relative_to(REPO)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((REPO/'backend/agents/scripts').glob('*.sh'))}
    for name in ('backend/agents/go_install_script.py','backend/agents/core_install_script.py','backend/agents/install_output.py','backend/routes/agents.py'):
        sources[name]=hashlib.sha256((REPO/name).read_bytes()).hexdigest()
    save(ROOT/'source-hashes.json',sources)
    report('temporary-real-backend-ready',product_source_sha256=sources,agent_binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),agent_binary_version=run([str(binary),'-version']).stdout.decode().strip())

def server():
    root=Path(GROOT);creds=json.loads((root/'credentials.json').read_text())
    os.environ.update(DATA_DIR=str(root/'data'),LOG_FILE=str(root/'backend.log'),LOG_LEVEL='WARNING',ADMIN_USERNAME=creds['username'],ADMIN_PASSWORD=creds['password'],JWT_SECRET_KEY=creds['jwt_secret'],AGENT_REGISTRATION_KEY='',AGENTS_STATIC_DIR=str(root/'static-agents'),SUB_STORE_BUILTIN_DIR=str(root/'no-sub-store'),NO_PROXY='*')
    for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):os.environ.pop(key,None)
    os.chdir(root);sys.path.insert(0,str(REPO))
    from backend.common import config as store
    from backend.common.config_repository import ProfileRepository
    store.set_repository(ProfileRepository(root/'data',default_config_factory=store.get_default_config,initial_config_factory=store.get_default_config))
    from backend.app import app
    from werkzeug.serving import make_server
    make_server('0.0.0.0',18080,app,threaded=True).serve_forever()

def api(method,path,body=None,authenticated=True):
    headers={'Content-Type':'application/json'}
    creds=json.loads((PRIVATE/'credentials.json').read_text())
    if authenticated:headers['Authorization']='Bearer '+creds['admin_token']
    req=urllib.request.Request(LOCAL+path,method=method,headers=headers,data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req,timeout=120) as r:return json.load(r)
    except urllib.error.HTTPError as e:raise RuntimeError('Backend '+path+' HTTP '+str(e.code)) from e

def prepare_script(cid):
    name=GUESTS[cid][0]
    request={'name':'cf-shell-ha-'+name,'type':'oxidns','port':18081,'agent_ip':f'10.97.0.{cid-100}',
             'server_url':SERVER,'install_core':True,'prepare_dns_ha':True}
    data=api('POST','/api/agents/install-command',request)
    script=data['script']
    assert '# ConfigFlow DNS HA preparation interface: 1' in script.splitlines()[:5]
    assert script.splitlines()[1]=='# ConfigFlow Agent + core installer interface: 1'
    url=SERVER+data['download_path']
    # Use the real short-link route and the guest's standard downloader.
    target='/root/cf-shell-ha-install.sh'
    downloader='if command -v curl >/dev/null 2>&1; then curl --connect-timeout 5 --max-time 30 -fsSL '+shlex.quote(url)+' -o '+target+'; else wget -T 15 -q -O '+target+' '+shlex.quote(url)+'; fi'
    guest(cid,'sh','-c',downloader)
    actual=guest(cid,'cat',target).stdout
    assert actual==script.encode()
    (PRIVATE/f'{cid}-installer.sh').write_bytes(actual)
    downloader_name=guest(cid,'sh','-c','if command -v curl >/dev/null 2>&1; then echo curl; else echo wget; fi').stdout.decode().strip()
    return {'download_tool':downloader_name,'sha256':hashlib.sha256(actual).hexdigest(),'download_equal':True,'request':request}

def preserve_state(cid):
    program=r"""set -eu
jq -c '{agent_id,token,agent_name,service_type,config_path,service_manager,service_unit,service_binary,restart_command,start_command,stop_command,status_command,agent_port}' /opt/configflow-agent/config-oxidns.json
config_path=$(jq -r .config_path /opt/configflow-agent/config-oxidns.json)
sha256sum "$config_path"
workdir=$(dirname "$config_path")
find "$workdir/rules" -type f -exec sha256sum '{}' \; 2>/dev/null || true
sha256sum /etc/keepalived/keepalived.conf 2>/dev/null || true
"""
    raw=guest(cid,'sh','-c',program).stdout.decode().splitlines()
    identity=json.loads(raw[0]); hashes=raw[1:]
    return {'identity':identity,'hashes':hashes}

def dns_probe(cid):
    program=r"""import json,os,socket,struct,sys
host=sys.argv[1];results=[]
for transport in ('udp','tcp'):
 ident=os.urandom(2); q=ident+b'\x01\x00\x00\x01'+b'\0'*6+b'\x07example\x03com\0\0\x01\0\x01'
 s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM if transport=='udp' else socket.SOCK_STREAM);s.settimeout(5)
 try:
  if transport=='udp':s.sendto(q,(host,53));r=s.recv(8192)
  else:
   s.connect((host,53));s.sendall(struct.pack('!H',len(q))+q);n=struct.unpack('!H',s.recv(2))[0];r=b''
   while len(r)<n:r+=s.recv(n-len(r))
  assert r[:2]==ident and r[3]&15==0 and struct.unpack('!H',r[6:8])[0]>0
  results.append({'transport':transport,'rcode':0,'answers':struct.unpack('!H',r[6:8])[0]})
 finally:s.close()
print(json.dumps(results))
"""
    return json.loads(guest(132,'/opt/cf-retest/venv/bin/python','-c',program,f'10.97.0.{cid-100}').stdout)

def check_live(cid):
    name='cf-shell-ha-'+GUESTS[cid][0]
    for _ in range(35):
        items=api('GET','/api/agents')
        matches=[x for x in items if x['name']==name]
        if len(matches)==1 and matches[0].get('status')=='online':break
        time.sleep(1)
    else:raise RuntimeError(name+' did not register online')
    data=matches[0]
    versions=guest(cid,'sh','-c','keepalived --version 2>&1 | head -n 1; /usr/local/bin/oxidns --version; /usr/local/bin/configflow-agent -version; ip -j -4 addr show').stdout.decode()
    (PRIVATE/f'{cid}-versions.txt').write_text(versions)
    links=json.loads(guest(cid,'ip','-j','-4','addr','show').stdout)
    addresses=sorted(address['local'] for link in links for address in link.get('addr_info',[]))
    assert addresses==['10.97.0.'+str(cid-100),'127.0.0.1'], addresses
    assert not (data.get('oxidns_ha') or {}).get('enabled',False)
    service_script = 'test ! -e /run/openrc/started/keepalived && test ! -e /etc/runlevels/default/keepalived && ! pidof keepalived && echo inactive-disabled' if cid == 191 else 'systemctl is-active keepalived.service; systemctl is-enabled keepalived.service; test ! -L /run/systemd/system/keepalived.service'
    service=guest(cid,'sh','-c',service_script,check=False)
    state=service.stdout.decode().strip()
    assert service.returncode == 0 and ((state=='inactive-disabled') if cid == 191 else (state.splitlines()==['inactive','disabled'])), state
    return {'agent_online':True,'agent_id':data['id'],'dns':dns_probe(cid),'ipv4_addresses':addresses,
            'ha_enabled':False,'keepalived_inactive_disabled':True,'owned_runtime_mask_removed':True,'system':GUESTS[cid][0]}

def package_failure():
    guard();cid=190
    info=prepare_script(cid)
    put(cid,'/root/cf-shell-ha-failure/apt-get','#!/bin/sh\nprintf "fixture package repository failure\\n" >&2\nexit 42\n','755')
    result=guest(cid,'sh','-c','PATH=/root/cf-shell-ha-failure:$PATH timeout 90 sh /root/cf-shell-ha-install.sh',check=False,timeout=100)
    (PRIVATE/'package-failure.log').write_bytes(result.stdout+result.stderr)
    assert result.returncode!=0
    guest(cid,'sh','-c','test ! -L /run/systemd/system/keepalived.service && test ! -d /opt/configflow-agent && test ! -d /etc/oxidns && test ! -e /usr/local/bin/oxidns')
    assert not api('GET','/api/agents')
    guest(cid,'rm','-rf','/root/cf-shell-ha-failure')
    report('package-manager-failure-stops-before-core-or-agent',exit_code=result.returncode,installer=info)

def install_one(cid):
    guard(); info=prepare_script(cid)
    result=guest(cid,'timeout','600','sh','/root/cf-shell-ha-install.sh',check=False,timeout=620)
    (PRIVATE/f'{cid}-first-install.log').write_bytes(result.stdout+result.stderr)
    if result.returncode:raise RuntimeError(f'CT{cid} first install exited {result.returncode}; private log retained')
    live=check_live(cid)
    put(cid,'/etc/oxidns/rules/cf-shell-ha-retained.txt','full:retained-shell-ha.invalid\n')
    put(cid,'/etc/keepalived/keepalived.conf','# cf-shell-ha existing operator configuration retained\n')
    before=preserve_state(cid)
    result=guest(cid,'timeout','600','sh','/root/cf-shell-ha-install.sh',check=False,timeout=620)
    (PRIVATE/f'{cid}-repeat-install.log').write_bytes(result.stdout+result.stderr)
    if result.returncode:raise RuntimeError(f'CT{cid} repeat install exited {result.returncode}; private log retained')
    after=preserve_state(cid)
    assert before==after, f'CT{cid} existing identity/config/rules changed'
    final=check_live(cid)
    report('full-install-and-idempotent-repeat-'+GUESTS[cid][0],installer=info,initial=live,final=final,
           identity_preserved=True,config_rules_keepalived_preserved=True,manual_dependency_install=False,
           scripts_run_without_tty=True)
    return cid

def custom_repeat():
    guard();cid=190;custom='/srv/cf-shell-ha-custom'
    move=r"""set -eu
systemctl stop configflow-agent oxidns
mv /etc/oxidns /srv/cf-shell-ha-custom
sed -i 's|/etc/oxidns|/srv/cf-shell-ha-custom|g' /etc/systemd/system/oxidns.service
jq '.config_path="/srv/cf-shell-ha-custom/config.yaml"' /opt/configflow-agent/config-oxidns.json > /opt/configflow-agent/config-oxidns.json.tmp
chmod 600 /opt/configflow-agent/config-oxidns.json.tmp
mv /opt/configflow-agent/config-oxidns.json.tmp /opt/configflow-agent/config-oxidns.json
systemctl daemon-reload
systemctl start oxidns configflow-agent
"""
    guest(cid,'sh','-c',move)
    check_live(cid)
    before=preserve_state(cid)
    info=prepare_script(cid)
    result=guest(cid,'timeout','600','sh','/root/cf-shell-ha-install.sh',check=False,timeout=620)
    (PRIVATE/'190-custom-repeat.log').write_bytes(result.stdout+result.stderr)
    if result.returncode:raise RuntimeError(f'custom deployment repeat failed {result.returncode}')
    after=preserve_state(cid)
    assert before==after
    guest(cid,'test','!','-e','/etc/oxidns')
    live=check_live(cid)
    report('custom-native-deployment-preserved-on-default-one-click-repeat',installer=info,
           custom_config_path=custom+'/config.yaml',no_duplicate_default_install=True,
           identity_config_rules_preserved=True,final=live)

def tests():
    package_failure()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(install_one,cid) for cid in GUESTS]
        for future in concurrent.futures.as_completed(futures):future.result()
    custom_repeat()


def cleanup():
    manifest=guard()
    for cid in manifest['created']:
        pve('pct','stop',cid,check=False)
        pve('pct','destroy',cid,'--purge',1,'--destroy-unreferenced-disks',1)
    if manifest.get('network_rule'):
        rule=manifest['network_rule']
        rules=json.loads(pve('nft','-j','-a','list','chain','inet','cfgt','forward').stdout)['nftables']
        matches=[r['rule'] for r in rules if r.get('rule',{}).get('comment')==rule['comment']]
        assert len(matches)==1 and matches[0]['handle']==rule['handle']
        pve('nft','delete','rule','inet','cfgt','forward','handle',rule['handle'])
        assert pve('nft','list','table','inet','cfgt').stdout.decode()==manifest['network_before']
    pidfile=PRIVATE/'tunnel.pid'
    if pidfile.exists():
        pid=int(pidfile.read_text()); cmd=Path(f'/proc/{pid}/cmdline')
        if cmd.exists() and b'19290:10.97.0.74:18080' in cmd.read_bytes():os.kill(pid,15)
    pve('pct','stop',132)
    pve('pct','rollback',132,SNAPSHOT)
    pve('pct','delsnapshot',132,SNAPSHOT)
    restored=pve('pct','config',132).stdout.decode()
    assert restored==manifest['backend_config'] and 'stopped' in pve('pct','status',132).stdout.decode()
    # Remove only exact templates this task downloaded; no guest is based on them now.
    state=json.loads(pve('pvesh','get','/cluster/resources','--type','vm','--output-format','json').stdout)
    external_configs=[pve('pct','config',int(item['vmid'])).stdout.decode() for item in state if item.get('type')=='lxc']
    removed=[];retained=[]
    for template in manifest['templates_created']:
        family=next(value[2] for value in GUESTS.values() if value[1]==template)
        if any('ostype: '+family+'\n' in config for config in external_configs):
            retained.append({'template':template,'reason':'An external guest of this OS family exists; preserve shared template conservatively.'})
        else:
            pve('pveam','remove','local:vztmpl/'+template);removed.append(template)
    assert not set(GUESTS)&{int(x['vmid']) for x in state}
    save(ROOT/'cleanup.json',{'temporary_guests_removed':manifest['created'],'backend_restored_stopped':True,'backend_config_equal':True,'original_firewall_restored':True,'snapshot_removed':SNAPSHOT,'templates_removed':removed,'shared_templates_retained':retained,'finished_at':time.time()})
    print(json.dumps({'cleanup':'passed','temporary_guests_removed':manifest['created']}),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['prepare','reset-guests','network','backend','server','test','custom-repeat','cleanup']);args=parser.parse_args()
    {'prepare':prepare,'reset-guests':reset_guests,'network':network,'backend':stage_backend,'server':server,'test':tests,'custom-repeat':custom_repeat,'cleanup':cleanup}[args.command]()
