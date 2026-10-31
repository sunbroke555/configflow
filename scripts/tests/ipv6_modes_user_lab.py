#!/usr/bin/env python3
"""Evidence/restoration helper for the explicitly authorized VM151/CT152-154 lab.

Run only on 192.168.0.32, from this checkout. Never calls legacy lab main(),
prepare(), or cleanup() (those include out-of-scope inventory/guest reads).
Lifecycle: backup; seed; fixture4; defconf; serve; browser actions; restore.
Use the browser helper for every product setting, prepare, publish and verify.
Private WORK contains credentials indirectly in its DB/backups: publish only
redacted evidence, never WORK itself. defconf/restore are destructive to the
specified lab configuration and require the baseline first. This one-off run
backs up config.yaml, not the Mihomo cache or Agent deployment journal; those
remaining runtime artifacts must be disclosed in the restoration report.
"""
import base64, hashlib, json, os, pathlib, re, shutil, socket, sqlite3, subprocess, sys, time, urllib.request, urllib.error
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/tests'))
import ipv6_readiness_lab as lab
WORK=pathlib.Path('/tmp/cf-modes-e2e-1031')
PREVIEW=pathlib.Path('/home/sunbroke/previews/configflow-ipv6-readiness')
ROUTER='router_d10a2f2c9442'
PATHS=[f'/{fam}/firewall/{part}' for fam in ('ip','ipv6') for part in ('filter','nat','mangle','raw','address-list')]+['/interface/list','/interface/list/member','/ip/settings','/ip/route','/ipv6/route','/ipv6/nd','/ipv6/nd/prefix','/ipv6/nd/prefix/default','/ip/firewall/connection/tracking','/system/script','/system/scheduler','/ip/dns']
def save(name,value):
 p=WORK/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(value,ensure_ascii=False,indent=2));p.chmod(0o600)
def ros(method,path,values=None):
 c=json.loads((lab.ROOT/'private.json').read_text());auth=base64.b64encode(('admin:'+c['router_password']).encode()).decode()
 req=urllib.request.Request('http://127.0.0.1:19151/rest'+path,method=method,headers={'Authorization':'Basic '+auth,'Content-Type':'application/json'},data=None if values is None else json.dumps(values).encode())
 try:
  with urllib.request.urlopen(req,timeout=35) as r:
   b=r.read();return json.loads(b) if b else None
 except urllib.error.HTTPError as e:raise RuntimeError(f'{method} {path}: {e.code} {e.read()[:500]}')
def command(cmd):
 r=lab.ros(cmd);out=r.stdout.decode();
 if re.search(r'failure:|syntax error|expected end of command|bad command name|no such item',out):raise RuntimeError(out)
 return out
def state():return {p:ros('GET',p) for p in PATHS}
def normalize(p,rows):
 if isinstance(rows,dict):rows=[rows]
 skip={'.id','bytes','packets','invalid','active','inactive','dynamic','creation-time','debug.fwp-ptr','hw-offloaded','immediate-gw','used','expires-after','next-run','run-count','last-started','last-link-up-time','last-link-down-time','cache-used'}
 if 'route' in p:skip|={'belongs-to','contribution','fib','suppressed','rpki','nexthop-id','debug.fwp-ptr'}
 if p=='/ip/settings':skip|={'ipv4-fast-path-active','ipv4-fast-path-packets','ipv4-fast-path-bytes','ipv4-fasttrack-active','ipv4-fasttrack-packets','ipv4-fasttrack-bytes'}
 if p=='/ip/firewall/connection/tracking':skip|={'active-ipv4','active-ipv6','total-entries','total-ip4-entries','total-ip6-entries','max-entries'}
 return [{k:v for k,v in r.items() if k not in skip} for r in rows if r.get('dynamic')!='true']
SYS_SCRIPT=r'''import json,pathlib,subprocess
r=subprocess.run(['sysctl','-a'],capture_output=True,text=True)
sysctls=dict(x.split(' = ',1) for x in r.stdout.splitlines() if ' = ' in x)
files={}
for base in ['/etc/sysctl.conf','/etc/sysctl.d','/opt/configflow-agent/ipv6-readiness.json','/etc/resolv.conf','/etc/mihomo/config.yaml']:
 p=pathlib.Path(base)
 for f in ([p] if p.is_file() else p.rglob('*') if p.is_dir() else []):
  if f.is_file():files[str(f)]={'data':f.read_bytes().hex(),'mode':f.stat().st_mode&511}
print(json.dumps({'sysctls':sysctls,'files':files}))'''
def guest_state(cid):return json.loads(lab.guest(cid,'python3','-c',SYS_SCRIPT).stdout)
def backup():
 lab.guard();WORK.mkdir(mode=0o700,exist_ok=True)
 assert not (WORK/'backup/ros.json').exists(),'Backup already exists; refusing overwrite'
 s=state();save('backup/ros.json',s)
 for p in ['/ip/firewall','/ipv6/firewall','/interface/list','/ip/settings','/ip/route','/ipv6/route','/ipv6/nd','/ip/firewall/connection/tracking','/ip/dns','/system/script','/system/scheduler']:
  (WORK/'backup'/('ros'+p.replace('/','_')+'.rsc')).write_text(command(p.replace('/',' ')+' export terse'))
 for cid in (152,153):
  save(f'backup/ct{cid}.json',guest_state(cid))
  save(f'backup/ct{cid}-network.json',{a:lab.guest(cid,*a.split(),check=False).stdout.decode() for a in ('ip -j addr','ip -j route show table all','ip -j -6 route show table all','systemctl is-active mihomo','systemctl is-active configflow-agent')})
 (WORK/'backup/ros-default.txt').write_text(command('/system resource print; /system default-configuration print'))
 save('backup/complete.json',{'time':time.time(),'paths':PATHS,'guests':[151,152,153,154]})
 print(json.dumps({'backup_complete':True,'ros':{p:len(v) for p,v in s.items()},'root':str(WORK)}))
def inspect():
 lab.guard()
 for cid in (152,153):
  print(cid,lab.guest(cid,'sh','-c','ip -br addr; ip route; ip -6 route; cat /etc/resolv.conf',check=False).stdout.decode())
 print('filters',json.dumps({p:ros('GET',p) for p in ['/ip/firewall/filter','/ipv6/firewall/filter','/interface/list/member']},ensure_ascii=False))
def clear(p):
 rows=ros('GET',p)
 for r in rows:
  if r.get('dynamic')=='true' or r.get('builtin')=='true' or r.get('default')=='true':continue
  ros('DELETE',p+'/'+r['.id'])
def defconf():
 assert (WORK/'backup/complete.json').exists()
 lab.guard()
 for fam in ('ip','ipv6'):
  for part in ('filter','nat','mangle','raw','address-list'):clear(f'/{fam}/firewall/{part}')
 clear('/interface/list/member');clear('/interface/list')
 for name in ('LAN','WAN'):ros('PUT','/interface/list',{'name':name,'comment':'defconf'})
 for interface,name in [('ether1','LAN'),('ether2','LAN'),('ether3','WAN')]:ros('PUT','/interface/list/member',{'interface':interface,'list':name})
 rules4=[
 ('input',dict(action='accept',**{'connection-state':'established,related,untracked','comment':'accept established,related,untracked'})),
 ('input',dict(action='drop',**{'connection-state':'invalid','comment':'drop invalid'})),
 ('input',dict(action='accept',protocol='icmp',comment='accept ICMP')),
 ('input',dict(action='accept',**{'dst-address':'127.0.0.1','comment':'accept to local loopback (for CAPsMAN)'})),
 ('input',dict(action='drop',**{'in-interface-list':'!LAN','comment':'drop all not coming from LAN'})),
 ('forward',dict(action='accept',**{'ipsec-policy':'in,ipsec','comment':'accept in ipsec policy'})),
 ('forward',dict(action='accept',**{'ipsec-policy':'out,ipsec','comment':'accept out ipsec policy'})),
 ('forward',dict(action='fasttrack-connection',**{'connection-state':'established,related','comment':'fasttrack'})),
 ('forward',dict(action='accept',**{'connection-state':'established,related,untracked','comment':'accept established,related, untracked'})),
 ('forward',dict(action='drop',**{'connection-state':'invalid','comment':'drop invalid'})),
 ('forward',dict(action='drop',**{'connection-state':'new','connection-nat-state':'!dstnat','in-interface-list':'WAN','comment':'drop all from WAN not DSTNATed'}))]
 for chain,r in rules4:ros('PUT','/ip/firewall/filter',{'chain':chain,**r,'comment':'defconf: '+r['comment']})
 ros('PUT','/ip/firewall/nat',{'chain':'srcnat','action':'masquerade','out-interface-list':'WAN','ipsec-policy':'out,none','comment':'defconf: masquerade'})
 bad=[('::/128','unspecified address'),('::1/128','lo'),('fec0::/10','site-local'),('::ffff:0.0.0.0/96','ipv4-mapped'),('::/96','ipv4 compat'),('100::/64','discard only'),('2001:10::/28','ORCHID'),('3ffe::/16','6bone')]
 for addr,c in bad:ros('PUT','/ipv6/firewall/address-list',{'list':'bad_ipv6','address':addr,'comment':'defconf: '+c})
 rules6=[
 ('input',{'action':'accept','connection-state':'established,related,untracked','comment':'accept established,related,untracked'}),
 ('input',{'action':'drop','connection-state':'invalid','comment':'drop invalid'}),
 ('input',{'action':'accept','protocol':'icmpv6','comment':'accept ICMPv6'}),
 ('input',{'action':'accept','protocol':'udp','port':'33434-33534','comment':'accept UDP traceroute'}),
 ('input',{'action':'accept','protocol':'udp','dst-port':'546','src-address':'fe80::/10','comment':'accept DHCPv6-Client prefix delegation.'}),
 ('input',{'action':'accept','protocol':'udp','dst-port':'500,4500','comment':'accept IKE'}),
 ('input',{'action':'accept','protocol':'ipsec-ah','comment':'accept ipsec AH'}),
 ('input',{'action':'accept','protocol':'ipsec-esp','comment':'accept ipsec ESP'}),
 ('input',{'action':'accept','ipsec-policy':'in,ipsec','comment':'accept all that matches ipsec policy'}),
 ('input',{'action':'drop','in-interface-list':'!LAN','comment':'drop everything else not coming from LAN'}),
 ('forward',{'action':'accept','connection-state':'established,related,untracked','comment':'accept established,related,untracked'}),
 ('forward',{'action':'drop','connection-state':'invalid','comment':'drop invalid'}),
 ('forward',{'action':'drop','src-address-list':'bad_ipv6','comment':'drop packets with bad src ipv6'}),
 ('forward',{'action':'drop','dst-address-list':'bad_ipv6','comment':'drop packets with bad dst ipv6'}),
 ('forward',{'action':'drop','protocol':'icmpv6','hop-limit':'equal:1','comment':'rfc4890 drop hop-limit=1'}),
 ('forward',{'action':'accept','protocol':'icmpv6','comment':'accept ICMPv6'}),
 ('forward',{'action':'accept','protocol':'139','comment':'accept HIP'}),
 ('forward',{'action':'accept','protocol':'udp','dst-port':'500,4500','comment':'accept IKE'}),
 ('forward',{'action':'accept','protocol':'ipsec-ah','comment':'accept ipsec AH'}),
 ('forward',{'action':'accept','protocol':'ipsec-esp','comment':'accept ipsec ESP'}),
 ('forward',{'action':'accept','ipsec-policy':'in,ipsec','comment':'accept all that matches ipsec policy'}),
 ('forward',{'action':'drop','in-interface-list':'!LAN','comment':'drop everything else not coming from LAN'})]
 for chain,r in rules6:ros('PUT','/ipv6/firewall/filter',{'chain':chain,**r,'comment':'defconf: '+r['comment']})
 time.sleep(2);save('defconf.json',state());print('defconf installed, IPv4/IPv6 forward drop invalid active')
def prepare_app():
 assert (WORK/'backup/complete.json').exists()
 dest=WORK/'data'
 if not dest.exists():
  dest.mkdir();src=sqlite3.connect(f'file:{PREVIEW}/data/configflow.sqlite3?mode=ro',uri=True);db=sqlite3.connect(dest/'configflow.sqlite3');src.backup(db);db.close();src.close()
 c=json.loads((PREVIEW/'credentials.json').read_text())
 os.environ.update(DATA_DIR=str(dest),LOG_FILE=str(WORK/'server.log'),LOG_LEVEL='WARNING',ADMIN_USERNAME=c['username'],ADMIN_PASSWORD=c['password'],JWT_SECRET_KEY=c['jwt_secret'],AGENT_REGISTRATION_KEY=c['registration_key'],SUB_STORE_BUILTIN_DIR=str(WORK/'disabled-sub-store'))
 for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):os.environ.pop(key,None)
 sys.path.insert(0,str(ROOT))
 from backend.common import config as store
 from backend.common.config_repository import ProfileRepository
 store.set_repository(ProfileRepository(dest,default_config_factory=store.get_default_config,initial_config_factory=store.get_default_config))
 import backend.agents.rule_sync as sync
 sync.start_loop=lambda:None
 import backend.agents.dns_follow as dns
 dns.start_loop=lambda *a:None
 import backend.agents.oxidns_ha_publish as ha
 ha.start_loop=lambda *a:None
 from backend.common.agent_manager import get_agent_manager
 manager=get_agent_manager()
 agents=manager.get_all_agents()
 allowed=[a for a in agents if a.get('host') in ('192.0.2.3','192.0.2.100')]
 assert len(allowed)==2,[(a.get('id'),a.get('host')) for a in agents]
 manager._update_agents(lambda rows:rows.__setitem__(slice(None),allowed))
 repo=store.get_repository();cfg=store.get_config('default')
 print(json.dumps({'agents':[{k:a.get(k) for k in ('id','name','host','service_type')} for a in allowed], 'config':{k:cfg.get(k) for k in ('mihomo','nodes','rule_configs')}, 'routers':[{k:r.get(k) for k in ('id','endpoint','name','ip_rule_sets','manage_return_routing','dns_agent_id','proxy_gateway_agent_id')} for r in cfg['routers']['items']]},ensure_ascii=False),flush=True)
 return store,repo,manager,allowed
def serve():
 store,repo,manager,allowed=prepare_app()
 def isolate(cfg):
  cfg['routers']['items']=[r for r in cfg['routers']['items'] if r['id']==ROUTER]
  r=cfg['routers']['items'][0];assert r['endpoint']=='http://127.0.0.1:19151'
  r['dns_agent_id']=''
 repo.update_profile_transaction('default',isolate);store.reset_config_context()
 # No substituted route/probe responses; only heartbeat timestamps for this private DB.
 from upstream_router_pending_lab import keep_online
 for a in allowed:keep_online(manager,a['id'])
 from backend.app import app
 from waitress import serve
 print('READY 15931',flush=True);serve(app,host='127.0.0.1',port=15931,threads=12)
def seed():
 store,repo,manager,allowed=prepare_app()
 rule={'id':'lab-modes-ipv6','name':'实验 IPv6 网段','source_type':'content','behavior':'classical','format':'text','content':'IP-CIDR6,2001:b28:f23d::/48,no-resolve\nIP-CIDR6,2001:67c:4e8::/48,no-resolve\n','enabled':True}
 def shared(s):
  s['rule_library']=[r for r in s['rule_library'] if r['id']!='lab-modes-ipv6']+[rule]
 repo.update_shared_transaction(shared)
 def cfg(c):
  c['rule_configs']=[{'id':'lab-modes-direct','itemType':'rule','rule_type':'IP-CIDR','value':'198.51.100.10/32','policy':'DIRECT','no_resolve':True,'enabled':True},{'id':'lab-modes-6','itemType':'ruleset','name':'实验 IPv6 网段','library_rule_id':'lab-modes-ipv6','policy':'IPv6 测试代理','enabled':True}]+[r for r in c['rule_configs'] if r['id']=='ipv6-test-match']
  c['routers']['items']=[r for r in c['routers']['items'] if r['id']==ROUTER]
  c['routers']['items'][0].update(ip_rule_sets=['lab-modes-6'],auto_rule_sync=False,dns_agent_id='')
 repo.update_profile_transaction('default',cfg);store.reset_config_context()
 save('seed.json',{'rule':rule,'note':'Private preview DB fixture only. Product operations begin in browser.'})
 print('seeded private rule fixture')
def fixture4():
 assert (WORK/'backup/complete.json').exists()
 original=WORK/'backup/ct154-fixture.py'
 if not original.exists():
  original.write_bytes(lab.guest(154,'cat','/opt/cf-ipv6-fixture/fixture.py').stdout);original.chmod(0o600)
 script=original.read_text().replace('servers=[IPv6Server', 'socketserver.ThreadingTCPServer.allow_reuse_address=True\nservers=[socketserver.ThreadingTCPServer(("198.51.100.10",1080),Socks),IPv6Server')
 assert script!=original.read_text()
 lab.write_remote('/opt/cf-ipv6-fixture/fixture.py',script,cid=154);lab.guest(154,'systemctl','restart','cf-ipv6-fixture')
 store,repo,manager,allowed=prepare_app()
 repo.update_system_transaction(lambda c:c['system_config'].update(server_domain='http://10.242.150.254:15932'))
 def shared(c):
  c['nodes']=[n for n in c['nodes'] if n['id']!='ipv4-test-node']+[{'id':'ipv4-test-node','name':'IPv4 测试节点','enabled':True,'type':'socks5','server':'198.51.100.10','port':1080,'params':{'udp':False}}]
 repo.update_shared_transaction(shared)
 def config(c):
  for group in c['proxy_groups']:
   if group['name']=='IPv6 测试代理' and 'ipv4-test-node' not in group['manual_nodes']:group['manual_nodes'].append('ipv4-test-node')
 repo.update_profile_transaction('default',config)
 print('IPv4 fixture ready; expose private frontend with the documented lab-only reverse tunnel')
def snapshot(label):save('evidence/'+label+'-ros.json',state());print('snapshot',label)
def restore():
 lab.guard();before=json.loads((WORK/'backup/ros.json').read_text())
 original_fixture=WORK/'backup/ct154-fixture.py'
 if original_fixture.exists():
  lab.write_remote('/opt/cf-ipv6-fixture/fixture.py',original_fixture.read_bytes(),cid=154)
  lab.guest(154,'systemctl','restart','cf-ipv6-fixture')
 # Remove forward rules first; management stays on isolated ether1.
 for fam in ('ip','ipv6'):
  for part in ('filter','nat','mangle','raw','address-list'):clear(f'/{fam}/firewall/{part}')
 clear('/interface/list/member');clear('/interface/list')
 for p in ('/ip/route','/ipv6/route','/system/scheduler','/system/script'):clear(p)
 for p in ('/interface/list','/ip/firewall','/ipv6/firewall','/ip/route','/ipv6/route','/system/script','/system/scheduler','/ipv6/nd','/ip/dns'):
  script=(WORK/'backup'/('ros'+p.replace('/','_')+'.rsc')).read_text()
  lines=[x.strip() for x in script.splitlines() if x.strip() and not x.startswith('#')]
  if lines:command('; '.join(lines))
 # Restore all modified scalar properties, including defaults omitted by export.
 for p in ('/ip/settings','/ip/firewall/connection/tracking','/ip/dns'):
  old=normalize(p,before[p])[0];current=normalize(p,ros('GET',p))[0]
  changes={k:v for k,v in old.items() if current.get(k)!=v}
  if changes:ros('POST',p+'/set',changes)
 for p in ('/ipv6/nd','/ipv6/nd/prefix/default'):
  old=before[p];old=old if isinstance(old,list) else [old]
  now=ros('GET',p);now=now if isinstance(now,list) else [now]
  for o,n in zip(old,now):
   changes={k:v for k,v in normalize(p,[o])[0].items() if normalize(p,[n])[0].get(k)!=v}
   if changes:ros('PATCH',p+'/'+n['.id'],changes) if '.id' in n else ros('POST',p+'/set',changes)
 restore_guest=r'''import json,pathlib,subprocess,sys,time
d=json.load(sys.stdin)
for base in ['/etc/sysctl.d','/opt/configflow-agent/ipv6-readiness.json']:
 p=pathlib.Path(base)
 for f in ([p] if p.is_file() else p.rglob('*') if p.is_dir() else []):
  if f.is_file() and str(f) not in d['files']:f.unlink()
for name,v in d['files'].items():
 p=pathlib.Path(name);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(bytes.fromhex(v['data']));p.chmod(v['mode'])
if '/etc/mihomo/config.yaml' in d['files']:
 subprocess.run(['systemctl','restart','mihomo'],check=True)
 time.sleep(3)  # TUN recreation initializes per-interface IPv6 parameters asynchronously.
now=subprocess.run(['sysctl','-a'],capture_output=True,text=True).stdout
now=dict(x.split(' = ',1) for x in now.splitlines() if ' = ' in x)
errors=[];changed=[]
for k,v in sorted(d['sysctls'].items(),key=lambda item: (not item[0].endswith('.stable_secret'),item[0])):
 if k in now and (v!=now[k] or (k.endswith('.addr_gen_mode') and k.rsplit('.',1)[0]+'.stable_secret' in changed)) and k.startswith('net.') and k!='net.netfilter.nf_conntrack_count':
  r=subprocess.run(['sysctl','-w',k+'='+v],capture_output=True,text=True)
  changed.append(k)
  if r.returncode:errors.append({'key':k,'error':r.stderr})
print(json.dumps({'changed':changed,'errors':errors}))'''
 results={}
 for cid in (152,153):
  d=json.loads((WORK/f'backup/ct{cid}.json').read_text())
  results[str(cid)]=json.loads(lab.guest(cid,'python3','-c',restore_guest,data=json.dumps(d).encode()).stdout)
  now=guest_state(cid)
  results[str(cid)]['files_equal']=d['files']==now['files']
  results[str(cid)]['net_sysctl_diff']={k:('[redacted baseline/difference]' if 'secret' in k else [v,now['sysctls'].get(k)]) for k,v in d['sysctls'].items() if k.startswith('net.') and k!='net.netfilter.nf_conntrack_count' and k in now['sysctls'] and now['sysctls'][k]!=v}
  results[str(cid)]['runtime_counters_excluded']=['net.netfilter.nf_conntrack_count']
 after=state();save('restore-ros.json',after)
 diffs={p:{'before':normalize(p,before[p]),'after':normalize(p,after[p])} for p in PATHS if normalize(p,before[p])!=normalize(p,after[p])}
 fixture_equal=not original_fixture.exists() or lab.guest(154,'cat','/opt/cf-ipv6-fixture/fixture.py').stdout==original_fixture.read_bytes()
 save('restoration.json',{'ros_diffs':diffs,'guests':results,'ct154_fixture_equal':fixture_equal,'at':time.time()});print(json.dumps({'ros_diff_paths':list(diffs),'guests':results,'ct154_fixture_equal':fixture_equal},ensure_ascii=False))
CLIENT_PROBE=r'''import json,socket,struct,subprocess,sys,time,pathlib
label,family=sys.argv[1],int(sys.argv[2]);name='fixture.lab.invalid'
resolver=next(x.split()[1] for x in pathlib.Path('/etc/resolv.conf').read_text().splitlines() if x.startswith('nameserver '))
def dns(typ):
 q=b''.join(bytes([len(x)])+x.encode() for x in name.split('.'))+b'\x00'+struct.pack('!HH',typ,1)
 s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(3);s.sendto(b'\x61\x63'+struct.pack('!HHHHH',0x100,1,0,0,0)+q,(resolver,53));d=s.recv(4096);s.close()
 def skip(i):
  while d[i]:
   if d[i]&0xc0==0xc0:return i+2
   i+=d[i]+1
  return i+1
 p=skip(12)+4;out=[]
 for _ in range(struct.unpack('!H',d[6:8])[0]):
  p=skip(p);t,c,ttl,n=struct.unpack('!HHIH',d[p:p+10]);p+=10
  if t==typ:out.append(socket.inet_ntop(socket.AF_INET if t==1 else socket.AF_INET6,d[p:p+n]))
  p+=n
 return out
out={'resolver':resolver,'A':dns(1),'AAAA':dns(28),'family':family,'connections':[]}
addresses=out['A' if family==4 else 'AAAA']
if addresses:
 ip=addresses[0];resolved='['+ip+']' if family==6 else ip
 for i in range(10):
  # Discard learned redirects so every new connection uses the actual upstream route.
  subprocess.run(['ip','-'+str(family),'route','flush','cache'],capture_output=True)
  url=f'http://{name}:18080/{label}-{i}'
  r=subprocess.run(['curl','--noproxy','*','-'+str(family),'-sS','--connect-timeout','3','--max-time','5','--resolve',name+':18080:'+resolved,'-w','\n%{json}',url],capture_output=True,text=True)
  body,_,metrics=r.stdout.rpartition('\n');m=json.loads(metrics) if metrics.startswith('{') else {}
  out['connections'].append({'i':i,'code':r.returncode,'ok':r.returncode==0 and 'IPv6-node-fixture' in body,'connect':m.get('time_connect'),'ttfb':m.get('time_starttransfer'),'total':m.get('time_total'),'remote_ip':m.get('remote_ip'),'error':r.stderr,'body':body[:200]})
  time.sleep(.1)
direct=subprocess.run(['curl','--noproxy','*','-4','-sS','--max-time','5','-w','\n%{json}','http://198.51.100.10:18080/'+label+'-direct'],capture_output=True,text=True)
out['direct']={'code':direct.returncode,'output':direct.stdout,'error':direct.stderr}
out['route']=subprocess.run(['ip','-'+str(family),'route','get',addresses[0] if addresses else 'fc00::1'],capture_output=True,text=True).stdout
print(json.dumps(out))'''
OBSERVER=r'''import json,time,urllib.request,sys
seen={};end=time.monotonic()+float(sys.argv[1])
while time.monotonic()<end:
 try:
  d=json.load(urllib.request.urlopen('http://127.0.0.1:9090/connections',timeout=1))
  for c in d.get('connections',[]):
   if c.get('metadata',{}).get('sourceIP','').startswith(('192.0.2.100','2001:db8:150:1:','fdda:150:')):seen[c['id']]=c
 except:pass
 time.sleep(.04)
print(json.dumps(list(seen.values())))'''
def measure(label,family):
 import threading
 holder={}
 def observer():
  holder['connections']=json.loads(lab.guest(152,'python3','-c',OBSERVER,'62' if label.endswith('before') else '15',timeout=80).stdout)
 t=threading.Thread(target=observer);t.start()
 snapshot(label+'-before')
 result=json.loads(lab.guest(153,'python3','-c',CLIENT_PROBE,label,str(family),timeout=85).stdout)
 save('evidence/'+label+'-timings.json',result)
 t.join();save('evidence/'+label+'-connections.json',holder)
 snapshot(label+'-after')
 print(json.dumps({'label':label,'A':result['A'],'AAAA':result['AAAA'],'success':sum(x['ok'] for x in result['connections']),'total_seconds':[x['total'] for x in result['connections']],'direct_ok':result['direct']['code']==0,'observed_connections':len(holder['connections'])},ensure_ascii=False))
def hints(label):
 commands=json.loads((WORK/'evidence'/f'{label}-commands.json').read_text());commands=list(dict.fromkeys(commands))
 assert commands,'No page commands captured'
 result=[]
 for cmd in commands:
  assert cmd.startswith(('/ip firewall filter add chain=forward action=accept','/ipv6 firewall filter add chain=forward action=accept')) or cmd.startswith('/ip settings set '),cmd
  result.append({'original_page_command':cmd,'output':command(cmd)})
 save('evidence/'+label+'-executed.json',result);print(json.dumps(result,ensure_ascii=False))
def refault(label,families):
 removed=[]
 for family in families.split(','):
  assert family in ('ip','ipv6')
  p=f'/{family}/firewall/filter'
  for r in ros('GET',p):
   if r.get('comment')=='LAN to Mihomo via router' and r.get('in-interface')=='ether2' and r.get('out-interface')=='ether2' and r.get('action')=='accept':
    ros('DELETE',p+'/'+r['.id']);removed.append({'path':p,'rule':r})
 save('evidence/'+label+'-refault.json',{'purpose':'Repeat before/after comparison only; remove the previous page-generated allow rule, leave defconf drop invalid enabled','removed':removed});print('refault',label,len(removed))
def extras(label):
 import threading
 cap={}
 def capture():cap['output']=lab.guest(152,'timeout','7','tcpdump','-eni','eth1','-c','2','ip6 dst 2001:b28:f23d::1',check=False,timeout=20).stdout.decode()
 t=threading.Thread(target=capture);t.start();time.sleep(1)
 lab.guest(153,'ip','-6','route','flush','cache')
 ping=lab.guest(153,'ping','-6','-c','3','-i','0.3','-W','1','2001:b28:f23d::1',check=False).stdout.decode();t.join()
 host=lab.guest(152,'curl','--noproxy','','-sS','--proxy','socks5h://127.0.0.1:7890','--max-time','6','-w','\n%{json}','http://fixture.lab.invalid:18080/'+label+'-host',check=False)
 logs=lab.guest(154,'tail','-n','80','/opt/cf-ipv6-fixture/requests.jsonl').stdout.decode()
 save('evidence/'+label+'-extras.json',{'ruleset_capture':cap,'ping':ping,'host':{'code':host.returncode,'out':host.stdout.decode(),'error':host.stderr.decode()},'fixture_tail':[json.loads(x) for x in logs.splitlines()]})
 print(json.dumps({'label':label,'capture':cap,'host_ok':host.returncode==0},ensure_ascii=False))
if __name__=='__main__':
 lab.guard()
 action=sys.argv[1]
 if action=='backup':backup()
 elif action=='inspect':inspect()
 elif action=='defconf':defconf()
 elif action=='app-inspect':prepare_app()
 elif action=='serve':serve()
 elif action=='snapshot':snapshot(sys.argv[2])
 elif action=='restore':restore()
 elif action=='seed':seed()
 elif action=='fixture4':fixture4()
 elif action=='measure':measure(sys.argv[2],int(sys.argv[3]))
 elif action=='hints':hints(sys.argv[2])
 elif action=='extras':extras(sys.argv[2])
 elif action=='refault':refault(sys.argv[2],sys.argv[3])
