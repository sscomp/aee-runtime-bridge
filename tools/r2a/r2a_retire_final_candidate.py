#!/usr/bin/python3.11
"""Review candidate. Default is read-only; apply needs an operator record."""
from pathlib import Path
from datetime import datetime, timezone
import argparse, base64, fcntl, hashlib, json, os, re, select, signal, stat, subprocess, sys, time, urllib.request
# Fixed trust location; host parameters are never accepted from caller input.
CONFIG_FILE = Path('/etc/aee-r2a-runtime.json')
ROOT = HOME = TUNNEL = None
TARGETS = {}; FORMAL = {}
STUB = WRAPPER = APPSERVER = GATEWAY = FORMAL_TUNNEL = None
EXECUTOR_UID = None
SUPERVISOR_CONFIG = KEY_FILE = None
STUB_PORT = APP_PORT = GATEWAY_PORT = FORMAL_PORT = None
OWNER_SUBJECT = None
CONFIG_SHA256 = None
SIX = {'aee_status','aee_agents','aee_dispatch','aee_job_cancel','aee_job_status','aee_job_result'}
class GateError(Exception): pass

def need(ok, code):
    if not ok: raise GateError(code)
def emit(**data):
    print(json.dumps({'utc':datetime.now(timezone.utc).isoformat(), **data}), flush=True)
def read_private(path):
    fd=os.open(path, os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK)
    with os.fdopen(fd, 'r') as f:
        s=os.fstat(f.fileno())
        need(stat.S_ISREG(s.st_mode) and s.st_uid==EXECUTOR_UID and s.st_mode&0o777==0o600, 'PRIVATE_FILE_MODE')
        data=f.read(16385)
    need(len(data)<=16384, 'PRIVATE_FILE_TOO_LARGE')
    return data

def readonly_guard(event, args):
    if event in ('os.kill','os.killpg','signal.pidfd_send_signal'):
        raise GateError('DRY_RUN_SIGNAL_DENIED')
    if event=='subprocess.Popen':
        need(args[1]==['supervisorctl','-c',SUPERVISOR_CONFIG,'status'], 'DRY_RUN_SUBPROCESS_DENIED')
    if event=='open':
        mode,flags=args[1],args[2]
        need(not (isinstance(mode,str) and any(c in mode for c in 'wax+')), 'DRY_RUN_WRITE_DENIED')
        need(not (isinstance(flags,int) and flags&(os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND)), 'DRY_RUN_WRITE_DENIED')

def pstat(pid): return Path('/proc',str(pid),'stat').read_text().split()
def penv(pid):
    return dict(x.decode().split('=',1) for x in Path('/proc',str(pid),'environ').read_bytes().split(bytes([0])) if b'=' in x)
def identity(pid):
    tick,ppid,exe,role=TARGETS[pid];p=Path('/proc')/str(pid);s=pstat(pid)
    need(int(s[21])==tick and int(s[3])==ppid and s[2] not in ('T','t','Z'), 'TARGET_IDENTITY_CHANGED')
    need(str(p.joinpath('exe').resolve())==exe, 'TARGET_EXEC_CHANGED')
    need(role.encode() in p.joinpath('cmdline').read_bytes().split(bytes([0])), 'TARGET_ROLE_CHANGED')
    lines=p.joinpath('status').read_text().splitlines()
    need(next(x.split(':')[1].split()[0] for x in lines if x.startswith('Uid:'))==str(EXECUTOR_UID), 'TARGET_UID_CHANGED')
def descendants():
    ppids={}
    for p in Path('/proc').iterdir():
        if p.name.isdigit():
            try: ppids[int(p.name)]=int(pstat(int(p.name))[3])
            except OSError: pass
    result=set();queue=[STUB]
    while queue:
        parent=queue.pop()
        for pid,ppid in ppids.items():
            if ppid==parent and pid not in result: result.add(pid);queue.append(pid)
    return result

def fetch(port,path):
    with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}',timeout=3) as r:
        need(r.status==200,'HEALTH_HTTP'); return r.read()
def healthy():
    for pid,tick in FORMAL.items():
        s=pstat(pid);need(int(s[21])==tick and s[2] not in ('T','t','Z'),'FORMAL_IDENTITY_CHANGED')
    fetch(GATEWAY_PORT,'/health');fetch(FORMAL_PORT,'/readyz')
def parse_managed(output):
    """Keep only Program/State/PID. Descriptions and uptime are volatile."""
    result={}
    states={'STOPPED','STARTING','RUNNING','BACKOFF','STOPPING','EXITED','FATAL','UNKNOWN'}
    for line in output.splitlines():
        parts=line.split(maxsplit=2)
        need(len(parts)>=2,'SUPERVISOR_ROW')
        program,state=parts[:2]
        need(program not in result and state in states,'SUPERVISOR_ROW')
        description=parts[2] if len(parts)==3 else ''
        match=re.match(r'^pid ([1-9][0-9]*)(?:,|$)',description)
        need(state!='RUNNING' or match is not None,'SUPERVISOR_RUNNING_PID_MISSING')
        result[program]={'state':state,'pid':int(match[1]) if state=='RUNNING' else None}
    need(bool(result),'SUPERVISOR_EMPTY')
    for program,pid in [('aee-v2-gateway',GATEWAY),('aee-v2-tunnel-client',FORMAL_TUNNEL)]:
        need(result.get(program)=={'state':'RUNNING','pid':pid},'FORMAL_SUPERVISOR_IDENTITY')
    return result

def assert_managed_unchanged(current,baseline):
    need(current==baseline,'SUPERVISOR_STATE_OR_PID_CHANGED')
    return True

def managed():
    r=subprocess.run(['supervisorctl','-c',SUPERVISOR_CONFIG,'status'],capture_output=True,text=True,timeout=5)
    need(r.returncode in (0,3),'SUPERVISOR_QUERY')
    return parse_managed(r.stdout)

# This is an executor trust anchor, not an A2 policy or runtime credential.
TRUST_FILE = Path('/etc/aee-r2a-approval-trust.json')
LOCK_FILE = Path('/run/aee-r2a-apply.lock')
DOMAIN = b'AEE-R2A-RETIRE-V4|'
OBSERVE_SECONDS = 120
MAX_SHUTDOWN_SECONDS = 30



def strict_json(raw):
    def pairs(items):
        result={}
        for k,v in items:
            need(k not in result,'DUPLICATE_JSON_KEY');result[k]=v
        return result
    def invalid(value): raise GateError('NONFINITE_JSON_VALUE')
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=invalid)

def canonical(payload):
    return json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()

def root_protected_read(path,limit=16384):
    """No caller-selectable trust root, no writable or symlink path components."""
    need(path.is_absolute(),'TRUST_PATH')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC)
    try:
        for component in path.parts[1:-1]:
            nextfd=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=fd)
            os.close(fd);fd=nextfd;s=os.fstat(fd)
            need(s.st_uid==0 and not(s.st_mode&0o022),'TRUST_PARENT_WRITABLE')
        last=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK,dir_fd=fd)
        try:
            s=os.fstat(last);need(stat.S_ISREG(s.st_mode) and s.st_uid==0 and not(s.st_mode&0o022),'TRUST_FILE_NOT_PROTECTED')
            raw=os.read(last,limit+1);need(len(raw)<=limit,'TRUST_FILE_SIZE');return raw
        finally:os.close(last)
    finally:os.close(fd)


def configure_runtime(config, digest):
    """Validate before assigning. Only main's fixed root-protected read supplies live input.

    Tests call this function using fake identities; this is not an OS privilege boundary.
    """
    fields={'kind','executor_uid','runtime_root','codex_home','tunnel_executable',
            'key_file','supervisor_config','owner_subject','targets','formal','ports'}
    need(isinstance(config,dict) and set(config)==fields,'RUNTIME_CONFIG_SCHEMA')
    need(config['kind']=='aee-r2a-runtime-v1','RUNTIME_CONFIG_KIND')
    uid=config['executor_uid']
    need(type(uid) is int and uid>0,'RUNTIME_CONFIG_UID')
    for field in ('runtime_root','codex_home','tunnel_executable','key_file','supervisor_config'):
        value=config[field]
        need(isinstance(value,str) and value.startswith('/') and '..' not in Path(value).parts,'RUNTIME_CONFIG_PATH')
    need(isinstance(config['owner_subject'],str) and bool(config['owner_subject']),'RUNTIME_CONFIG_SUBJECT')
    targets=config['targets'];formal=config['formal'];ports=config['ports']
    need(isinstance(targets,dict) and set(targets)=={'stub','wrapper','appserver'},'RUNTIME_CONFIG_TARGETS')
    need(isinstance(formal,dict) and set(formal)=={'gateway','tunnel'},'RUNTIME_CONFIG_FORMAL')
    for target in targets.values():
        need(isinstance(target,dict) and set(target)=={'pid','start_ticks','ppid','executable','role'},'RUNTIME_CONFIG_IDENTITY')
        need(type(target['ppid']) is int and target['ppid']>0,'RUNTIME_CONFIG_PPID')
        need(isinstance(target['executable'],str) and target['executable'].startswith('/') and '..' not in Path(target['executable']).parts,'RUNTIME_CONFIG_EXECUTABLE')
        need(isinstance(target['role'],str) and bool(target['role']),'RUNTIME_CONFIG_ROLE')
    for target in formal.values():
        need(isinstance(target,dict) and set(target)=={'pid','start_ticks'},'RUNTIME_CONFIG_FORMAL_IDENTITY')
    identities=list(targets.values())+list(formal.values())
    for target in identities:
        need(all(type(target[k]) is int and target[k]>0 for k in ('pid','start_ticks')),'RUNTIME_CONFIG_IDENTITY_NUMBER')
    need(len({t['pid'] for t in identities})==5,'RUNTIME_CONFIG_DISTINCT_PIDS')
    need(targets['wrapper']['ppid']==targets['stub']['pid'] and targets['appserver']['ppid']==targets['wrapper']['pid'],'RUNTIME_CONFIG_TREE')
    need(targets['stub']['executable']==config['tunnel_executable'],'RUNTIME_CONFIG_TUNNEL')
    need(isinstance(ports,dict) and set(ports)=={'stub','appserver','gateway','tunnel'},'RUNTIME_CONFIG_PORTS')
    need(all(type(v) is int and 0<v<65536 for v in ports.values()) and len(set(ports.values()))==4,'RUNTIME_CONFIG_PORT_NUMBER')
    need(isinstance(digest,str) and re.fullmatch('[0-9a-f]{64}',digest),'RUNTIME_CONFIG_HASH')
    global ROOT, HOME, TUNNEL, TARGETS, FORMAL, STUB, WRAPPER, APPSERVER, GATEWAY, FORMAL_TUNNEL
    global EXECUTOR_UID, SUPERVISOR_CONFIG, KEY_FILE, STUB_PORT, APP_PORT, GATEWAY_PORT, FORMAL_PORT, OWNER_SUBJECT, CONFIG_SHA256
    ROOT=Path(config['runtime_root']);HOME=config['codex_home'];TUNNEL=config['tunnel_executable']
    EXECUTOR_UID=uid;SUPERVISOR_CONFIG=config['supervisor_config'];KEY_FILE=config['key_file']
    STUB=targets['stub']['pid'];WRAPPER=targets['wrapper']['pid'];APPSERVER=targets['appserver']['pid']
    GATEWAY=formal['gateway']['pid'];FORMAL_TUNNEL=formal['tunnel']['pid']
    TARGETS={t['pid']:(t['start_ticks'],t['ppid'],t['executable'],t['role']) for t in targets.values()}
    FORMAL={t['pid']:t['start_ticks'] for t in formal.values()}
    STUB_PORT=ports['stub'];APP_PORT=ports['appserver'];GATEWAY_PORT=ports['gateway'];FORMAL_PORT=ports['tunnel']
    OWNER_SUBJECT=config['owner_subject'];CONFIG_SHA256=digest

def load_runtime_config():
    raw=root_protected_read(CONFIG_FILE)
    configure_runtime(strict_json(raw),hashlib.sha256(raw).hexdigest())

def load_trust():
    registry=strict_json(root_protected_read(TRUST_FILE))
    need(set(registry)=={'kind','owner_subject','operator_subject','owner_public_key_b64','operator_public_key_b64'},'TRUST_SCHEMA')
    need(registry['kind']=='aee-r2a-dual-approval-trust-v2','TRUST_KIND')
    need(registry['owner_subject']==OWNER_SUBJECT and bool(registry['operator_subject']),'TRUST_SUBJECT')
    need(registry['owner_public_key_b64']!=registry['operator_public_key_b64'],'DISTINCT_SIGNERS_REQUIRED')
    return registry

def protected_source_hashes():
    """Apply must originate from the snapshot loader; never hash a different reopen."""
    loaded=globals().get('__r2a_loaded_sha256__')
    loader_path=globals().get('__r2a_loader_path__')
    loader_hash=globals().get('__r2a_loader_sha256__')
    need(isinstance(loaded,str) and isinstance(loader_path,str) and isinstance(loader_hash,str),'PROTECTED_SNAPSHOT_LOADER_REQUIRED')
    raw=root_protected_read(Path(__file__).absolute(),131072)
    need(hashlib.sha256(raw).hexdigest()==loaded,'PUBLISHED_SOURCE_CHANGED')
    actual_loader=root_protected_read(Path(loader_path),32768)
    need(hashlib.sha256(actual_loader).hexdigest()==loader_hash,'PUBLISHED_LOADER_CHANGED')
    return loaded,loader_hash

def local_scope():
    script_sha,loader_sha=protected_source_hashes()
    # Do not print host fingerprint, runtime key, bearer or Tunnel ID.
    host=hashlib.sha256(Path('/etc/machine-id').read_bytes()+Path('/proc/sys/kernel/random/boot_id').read_bytes()).hexdigest()
    return {'runtime_config_sha256':CONFIG_SHA256,'candidate_sha256':script_sha,'launcher_sha256':loader_sha,'host_boot_sha256':host,
            'targets':{str(pid):data[0] for pid,data in TARGETS.items()},
            'formal_targets':{str(pid):tick for pid,tick in FORMAL.items()}}

def enforce_time_window(payload,now,phase='signal',required_remaining=0):
    begin=datetime.fromisoformat(payload['window_start_utc'])
    cutoff=datetime.fromisoformat(payload['signal_not_after_utc'])
    observation_end=datetime.fromisoformat(payload['observation_not_after_utc'])
    need(all(x.tzinfo is not None and x.utcoffset().total_seconds()==0 for x in (begin,cutoff,observation_end,now)),'UTC_REQUIRED')
    need(0<(cutoff-begin).total_seconds()<=1800,'SIGNAL_WINDOW_BOUNDS')
    # 40s shutdown/reaping + 150s maximum observation + 30s margin.
    need(220<=(observation_end-cutoff).total_seconds()<=300,'OBSERVATION_DEADLINE_BOUNDS')
    need(phase in ('signal','observe'),'AUTHORIZATION_PHASE')
    deadline=cutoff if phase=='signal' else observation_end
    need(begin<=now<deadline,'SIGNAL_WINDOW_CLOSED' if phase=='signal' else 'OBSERVATION_WINDOW_CLOSED')
    need((deadline-now).total_seconds()>=required_remaining,'APPROVAL_TIME_BUDGET')
    return cutoff

def verify_envelope(envelope,registry,scope,now,required_remaining=0,phase='signal'):
    """Pure verifier; only load_trust() can supply a production registry."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    need(set(envelope)=={'payload','owner_signature_b64','operator_signature_b64'},'ENVELOPE_SCHEMA')
    p=envelope['payload']
    fields={'kind','approval_id','action','owner_subject','operator_subject','owner_released','no_active_turn_or_ui','shared_state_shutdown_acknowledged','window_start_utc','signal_not_after_utc','observation_not_after_utc','observe_seconds','candidate_sha256','launcher_sha256','runtime_config_sha256','host_boot_sha256','targets','formal_targets'}
    need(isinstance(p,dict) and set(p)==fields,'PAYLOAD_SCHEMA')
    need(p['kind']=='aee-r2a-dual-approval-v4' and p['action']=='retire_stub_and_attached_appserver','APPROVAL_SCOPE')
    need(isinstance(p['approval_id'],str) and 1<=len(p['approval_id'])<=128,'APPROVAL_ID')
    need(p['owner_subject']==registry['owner_subject']==OWNER_SUBJECT and p['operator_subject']==registry['operator_subject'],'SIGNER_SUBJECT')
    need(p['owner_released'] is True and p['no_active_turn_or_ui'] is True and p['shared_state_shutdown_acknowledged'] is True,'OWNER_RELEASE_REQUIRED')
    need(type(p['observe_seconds']) is int and p['observe_seconds']==OBSERVE_SECONDS,'OBSERVATION_SCOPE')
    for key,value in scope.items():need(p[key]==value,'APPROVAL_BINDING')
    enforce_time_window(p,now,phase,required_remaining)
    try:
        owner=base64.b64decode(registry['owner_public_key_b64'],validate=True)
        operator=base64.b64decode(registry['operator_public_key_b64'],validate=True)
        need(len(owner)==len(operator)==32 and owner!=operator,'DISTINCT_SIGNERS_REQUIRED')
        for role,key in [('owner',owner),('operator',operator)]:
            signature=base64.b64decode(envelope[role+'_signature_b64'],validate=True)
            need(len(signature)==64,'SIGNATURE_LENGTH')
            Ed25519PublicKey.from_public_bytes(key).verify(signature,DOMAIN+canonical(p))
    except GateError:raise
    except Exception:raise GateError('DUAL_SIGNATURE_INVALID')
    return True

def authorization(envelope_path,remaining=0,phase='signal'):
    if not envelope_path:return False
    # The envelope cannot introduce/override trusted keys or the trust path.
    need(hashlib.sha256(root_protected_read(CONFIG_FILE)).hexdigest()==CONFIG_SHA256,'RUNTIME_CONFIG_CHANGED')
    registry=load_trust()
    envelope=strict_json(read_private(envelope_path))
    verify_envelope(envelope,registry,local_scope(),datetime.now(timezone.utc),remaining,phase)
    return envelope['payload']

def checks():
    need(sys.version_info[:2]==(3,11) and Path(sys.executable).resolve()==Path('/usr/bin/python3.11').resolve(),'USE_SYSTEM_PYTHON311')
    need(sys.dont_write_bytecode and sys.flags.isolated,'USE_ISOLATED_PYTHON311_I_B')
    need(callable(getattr(os,'pidfd_open',None)) and callable(getattr(signal,'pidfd_send_signal',None)) and hasattr(select,'poll'),'PIDFD_UNAVAILABLE')
    need(os.geteuid()==EXECUTOR_UID,'EXECUTOR_UID')
    ancestor=os.getpid();seen=set()
    while ancestor and ancestor not in seen:
        need(ancestor not in TARGETS,'EXECUTOR_DEPENDS_ON_TARGET');seen.add(ancestor);ancestor=int(pstat(ancestor)[3])
    need(descendants()=={WRAPPER,APPSERVER},'DESCENDANTS_CHANGED')
    for pid in TARGETS: identity(pid)
    ge=penv(GATEWAY);te=penv(STUB);fe=penv(FORMAL_TUNNEL)
    need(te.get('CODEX_HOME')==penv(APPSERVER).get('CODEX_HOME')==HOME,'ORIGINAL_PROFILE_CHANGED')
    key=read_private(KEY_FILE).strip()
    need(bool(key) and key==te['CONTROL_PLANE_API_KEY']==fe['CONTROL_PLANE_API_KEY'],'ROLLBACK_KEY_SOURCE_CHANGED')
    need(json.loads(fetch(FORMAL_PORT,'/api/status'))['control_plane_tunnel_id']==te['CONTROL_PLANE_TUNNEL_ID'],'TUNNEL_BINDING_CHANGED')
    sys.path.insert(0,str(ROOT))
    from aee.mcp_runtime.store import read_job_record
    files=list(Path(ge['A3_JOB_STORE_DIR']).glob('A3JOB-*.json'));need(len(files)<10000,'JOB_SCAN_BOUND')
    need(all(read_job_record(f.parent,f.stem)['status'] not in ('queued','running') for f in files),'ACTIVE_JOBS')
    before=managed()
    for sample in range(2):
        healthy()
        for port in (STUB_PORT,FORMAL_PORT):
            d=json.loads(fetch(port,'/health?details=true'));c=d['components']
            need(d['ready'] and c['dispatcher']['details']['active']==0 and c['queue']['details']['depth']==0 and c['response-delivery']['details']['in_progress']==0,'TUNNEL_INFLIGHT')
        if sample==0: time.sleep(1)
    for proto in ('tcp','tcp6'):
        for line in Path('/proc/net',proto).read_text().splitlines()[1:]:
            a=line.split();need(not(a[3]=='01' and int(a[1].rsplit(':',1)[1],16) in (STUB_PORT,APP_PORT)),'STUB_CLIENT_CONNECTED')
    body=json.dumps({'jsonrpc':'2.0','id':'r2a-readiness','method':'tools/list','params':{}}).encode()
    req=urllib.request.Request(f'http://127.0.0.1:{GATEWAY_PORT}/mcp',data=body,headers={'Authorization':'Bearer '+ge['MCP_BRIDGE_API_KEY'],'Content-Type':'application/json','Accept':'application/json, text/event-stream','Mcp-Protocol-Version':'2025-03-26'})
    with urllib.request.urlopen(req,timeout=3) as r: data=json.load(r)
    need({t['name'] for t in data['result']['tools']}==SIX,'RESTRICTED_SURFACE_CHANGED')
    status=json.loads(fetch(STUB_PORT,'/api/codex/status'))
    return before,{'job_records':len(files),'target_pids':sorted(TARGETS),'pidfd_capability':'PASS','executor_ancestry':'PASS','restricted_six':'PASS','appserver_running':status.get('running') is True,'active_turn_idle':'NOT_PROVEN_BY_STATUS','shared_codex_home':True}

def wait_exit(fd,seconds):
    poll=select.poll();poll.register(fd,select.POLLIN);return bool(poll.poll(max(0,int(seconds*1000))))
def final_signal_gate(payload,now):
    # Last userspace check immediately before the pidfd syscall. Strictly before cutoff.
    enforce_time_window(payload,now,'signal')

def send_term(fd,pid,sent,authorize):
    if wait_exit(fd,0): return
    payload=authorize()  # Revalidate both signatures, protected source and scope.
    final_signal_gate(payload,datetime.now(timezone.utc))
    try: signal.pidfd_send_signal(fd,signal.SIGTERM,None,0);sent.append(pid)
    except ProcessLookupError: pass

def listener_rows():
    rows=[]
    for proto in ('tcp','tcp6'):
        for line in Path('/proc/net',proto).read_text().splitlines()[1:]:
            a=line.split()
            if a[3]=='0A':rows.append((proto,int(a[1].rsplit(':',1)[1],16),a[9]))
    return rows

def stub_listener_snapshot():
    owned=set()
    for fd in Path('/proc',str(STUB),'fd').iterdir():
        try:
            link=os.readlink(fd)
            if link.startswith('socket:['):owned.add(link[8:-1])
        except OSError:pass
    rows=[row for row in listener_rows() if row[2] in owned]
    need({row[1] for row in rows}=={STUB_PORT,APP_PORT},'STUB_LISTENER_CHANGED')
    need(all(row[2] in owned for row in listener_rows() if row[1] in (STUB_PORT,APP_PORT)),'STUB_PORT_OWNERSHIP')
    return sorted(rows)

def consumers(tunnel_id):
    same=[];unknown=[];unclassified=0
    installed=os.stat(TUNNEL)
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():continue
        try:
            comm=entry.joinpath('comm').read_text().strip()
            try:exe=str(entry.joinpath('exe').resolve(strict=True))
            except OSError:exe=''
            try:
                actual=entry.joinpath('exe').stat();same_inode=(actual.st_dev,actual.st_ino)==(installed.st_dev,installed.st_ino)
            except OSError:same_inode=False
            if not same_inode and Path(exe).name!='tunnel-client' and comm!='tunnel-client':
                if not exe:unclassified+=1
                continue
            pid=int(entry.name);argv=[x.decode() for x in entry.joinpath('cmdline').read_bytes().split(bytes([0])) if x]
            env=penv(pid);bound=None
            # Flag > environment. Unknown profile/config resolution blocks this inventory.
            for i,arg in enumerate(argv):
                if arg=='--control-plane.tunnel-id' and i+1<len(argv):bound=argv[i+1]
                elif arg.startswith('--control-plane.tunnel-id='):bound=arg.split('=',1)[1]
            if bound is None:bound=env.get('CONTROL_PLANE_TUNNEL_ID')
            if pid==FORMAL_TUNNEL:bound=json.loads(fetch(FORMAL_PORT,'/api/status'))['control_plane_tunnel_id']
            if not bound:unknown.append(pid)
            elif bound==tunnel_id:same.append(pid)
        except FileNotFoundError:continue
        except (OSError,ValueError,KeyError):unknown.append(int(entry.name))
    return {'same_tunnel_pids':sorted(same),'unresolved_candidate_pids':sorted(set(unknown)),'unclassified_proc_count':unclassified,'scope':'visible_local_process_namespace_only'}

def delivery_failures():
    d=json.loads(fetch(FORMAL_PORT,'/health?details=true'))
    need(d['ready'] is True,'FORMAL_NOT_READY')
    return d['components']['response-delivery']['details']['terminal_failures']

def original_state(pid):
    try:
        s=pstat(pid);return (int(s[21]),s[2])
    except FileNotFoundError:return None

def validate_post_snapshot(snapshot,expected_ticks,old_listeners):
    """Pure postconditions, testable without process operations or signals."""
    need(set(snapshot['pidfd_exited'])==set(expected_ticks),'EXIT_EVIDENCE_INCOMPLETE')
    for pid,tick in expected_ticks.items():
        need(snapshot['pidfd_exited'][pid] is True,'TARGET_PIDFD_NOT_EXITED')
        need(set(snapshot['original_states'])==set(expected_ticks),'EXIT_STATE_EVIDENCE_INCOMPLETE')
        current=snapshot['original_states'][pid]
        need(current is None or current[0]!=tick,'ORIGINAL_IDENTITY_STILL_PRESENT_OR_ZOMBIE')
    current=snapshot['listeners']
    need(not any(row[1] in (STUB_PORT,APP_PORT) for row in current),'STUB_PORT_STILL_LISTENING')
    need(not({row[2] for row in old_listeners}&{row[2] for row in current}),'ORIGINAL_LISTENER_INODE_REMAINS')
    inventory=snapshot['consumers']
    need(not inventory['unresolved_candidate_pids'],'CONSUMER_BINDING_UNRESOLVED')
    need(inventory['same_tunnel_pids']==[FORMAL_TUNNEL],'UNEXPECTED_SAME_TUNNEL_CONSUMER')
    need(snapshot['formal_health'] is True and snapshot['supervisor_unchanged'] is True,'FORMAL_SERVICE_CHANGED')
    need(snapshot['delivery_failures']==snapshot['delivery_baseline'],'DELIVERY_FAILURES_INCREASED_OR_RESET')

def post_sample(handles,old_listeners,tunnel_id,baseline,failures):
    healthy()
    snap={'pidfd_exited':{pid:wait_exit(fd,0) for pid,fd in handles.items()},
          'original_states':{pid:original_state(pid) for pid in TARGETS},
          'listeners':listener_rows(),'consumers':consumers(tunnel_id),
          'formal_health':True,'supervisor_unchanged':assert_managed_unchanged(managed(),baseline),
          'delivery_failures':delivery_failures(),'delivery_baseline':failures}
    validate_post_snapshot(snap,{pid:data[0] for pid,data in TARGETS.items()},old_listeners)
    return snap['consumers']['unclassified_proc_count']

def observe(callback,clock=time.monotonic,sleeper=time.sleep,duration=OBSERVE_SECONDS):
    need(duration==OBSERVE_SECONDS,'OBSERVATION_CANNOT_BE_SHORTENED')
    start=clock();samples=0
    while True:
        callback();samples+=1
        elapsed=clock()-start
        need(elapsed<=OBSERVE_SECONDS+30,'OBSERVATION_WALL_TIME_EXCEEDED')
        if elapsed>=OBSERVE_SECONDS:break
        sleeper(min(5,OBSERVE_SECONDS-elapsed))
    need(samples>=20,'INSUFFICIENT_HEALTH_SAMPLES')
    return samples

def open_apply_lock():
    # Root pre-provisions this lock; candidate never creates it.
    root_protected_read(LOCK_FILE,64)
    fd=os.open(LOCK_FILE,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK)
    s=os.fstat(fd);need(s.st_uid==0 and stat.S_ISREG(s.st_mode) and not(s.st_mode&0o022),'APPLY_LOCK_NOT_PROTECTED')
    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BaseException:os.close(fd);raise
    return fd

def main():
    p=argparse.ArgumentParser(description='Final review candidate. Dry-run by default.')
    m=p.add_mutually_exclusive_group();m.add_argument('--dry-run',action='store_true');m.add_argument('--apply',action='store_true')
    p.add_argument('--signed-approval');args=p.parse_args()
    if not args.apply:sys.addaudithook(readonly_guard)
    handles={};sent=[];lockfd=None
    try:
        need(sys.flags.isolated and sys.dont_write_bytecode,'USE_ISOLATED_PYTHON311_I_B')
        load_runtime_config()
        approved=False;auth_code='SIGNED_APPROVAL_NOT_SUPPLIED'
        try:approved=authorization(args.signed_approval)
        except Exception as exc:
            auth_code=str(exc) if isinstance(exc,GateError) else type(exc).__name__
        if args.apply:need(approved,'TRUSTED_OWNER_OPERATOR_APPROVAL_REQUIRED')
        baseline,result=checks()
        for pid in TARGETS:handles[pid]=os.pidfd_open(pid);identity(pid)
        tunnel_id=penv(STUB)['CONTROL_PLANE_TUNNEL_ID']
        old_listeners=stub_listener_snapshot();inv=consumers(tunnel_id)
        need(not inv['unresolved_candidate_pids'] and inv['same_tunnel_pids']==[STUB,FORMAL_TUNNEL],'PRE_RETIREMENT_CONSUMER_SET_CHANGED')
        failures=delivery_failures()
        if not args.apply:
            copy_trusted=False;lock_protected=False
            try:protected_source_hashes();copy_trusted=True
            except Exception:pass
            try:root_protected_read(LOCK_FILE,64);lock_protected=True
            except Exception:pass
            emit(mode='dry-run',technical='PASS',reviewed_copy_trusted=copy_trusted,apply_lock_protected=lock_protected,owner_operator_source='DUAL_SIGNATURE_VERIFIED' if approved else 'BLOCKED',authorization_code=auth_code if not approved else None,trust_anchor_configured=TRUST_FILE.exists(),apply_lock_configured=LOCK_FILE.exists(),signals_sent=0,pre_listener_ports=sorted({r[1] for r in old_listeners}),consumers=inv,post_exit_checks='NOT_RUN_TARGETS_ALIVE',observation_seconds=OBSERVE_SECONDS,apply_location_requires_root_published_copy=True,**result)
            return 0 if approved and copy_trusted and lock_protected else 3
        # Apply only from an operator-published root-protected reviewed copy.
        protected_source_hashes()
        lockfd=open_apply_lock()
        need(authorization(args.signed_approval),'APPROVAL_INVALIDATED')
        need(descendants()=={WRAPPER,APPSERVER},'DESCENDANTS_CHANGED')
        for pid in TARGETS:identity(pid)
        # Refresh every mechanical gate inside the lock immediately before mutation.
        refreshed,_=checks();assert_managed_unchanged(refreshed,baseline)
        need(stub_listener_snapshot()==old_listeners,'LISTENER_BASELINE_CHANGED')
        current=consumers(tunnel_id);need(not current['unresolved_candidate_pids'] and current['same_tunnel_pids']==[STUB,FORMAL_TUNNEL],'CONSUMER_BASELINE_CHANGED')
        def interrupted(signum,frame):raise GateError('CONTROLLER_INTERRUPTED_NO_AUTOMATIC_ROLLBACK')
        signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGINT,interrupted)
        emit(mode='apply',event='TRUSTED_SIGNAL_BOUNDARY')
        authorize_signal=lambda:authorization(args.signed_approval)
        send_term(handles[STUB],STUB,sent,authorize_signal);need(wait_exit(handles[STUB],20),'STUB_EXIT_TIMEOUT_NO_SIGKILL')
        for pid in (WRAPPER,APPSERVER):
            send_term(handles[pid],pid,sent,authorize_signal)
        deadline=time.monotonic()+10
        for pid in TARGETS:need(wait_exit(handles[pid],deadline-time.monotonic()),'TARGET_EXIT_TIMEOUT_NO_SIGKILL')
        # Allow bounded reaping; a zombie is not reported as complete cleanup.
        reap_deadline=time.monotonic()+10
        while True:
            states={pid:original_state(pid) for pid in TARGETS}
            if all(states[pid] is None or states[pid][0]!=TARGETS[pid][0] for pid in TARGETS):break
            need(time.monotonic()<reap_deadline,'TARGET_REAP_PENDING');time.sleep(.2)
        sample_count=[0]
        def sample():
            need(authorization(args.signed_approval,0,phase='observe'),'APPROVAL_INVALIDATED_DURING_OBSERVATION')
            unreadable=post_sample(handles,old_listeners,tunnel_id,baseline,failures)
            sample_count[0]+=1
            emit(mode='apply',event='POST_OBSERVATION_SAMPLE',sequence=sample_count[0],targets_exited=sorted(TARGETS),old_ports_absent=True,local_same_tunnel_pids=[FORMAL_TUNNEL],formal_health='PASS',supervisor_unchanged=True,delivery_failures=failures,unclassified_proc_count=unreadable)
        emit(mode='apply',event='POST_OBSERVATION_STARTED',required_seconds=OBSERVE_SECONDS)
        need(authorization(args.signed_approval,OBSERVE_SECONDS+30,phase='observe'),'OBSERVATION_BUDGET_REQUIRED')
        observation_started=time.monotonic()
        samples=observe(sample)
        observation_elapsed=round(time.monotonic()-observation_started,3)
        emit(mode='apply',retirement_status='PASS_LOCAL_POSTCONDITIONS',targets_exited=sorted(TARGETS),old_listeners_gone=True,local_consumer_set=[FORMAL_TUNNEL],observation_seconds=OBSERVE_SECONDS,observation_elapsed_seconds=observation_elapsed,health_samples=samples,signals_sent=len(sent),pids_signalled=sent,mcp_routing_acceptance='NOT_TESTED',global_consumer_exclusivity='NOT_PROVEN',a2_trusted_dispatch='NOT_ACCEPTED',dispatch_e2e='NOT_TESTED',credential_revocation='NOT_DONE')
        return 0
    except BaseException as exc:
        emit(mode='apply' if args.apply else 'dry-run',retirement_status='BLOCKED_OR_ABORTED',code=str(exc) if isinstance(exc,GateError) else type(exc).__name__,signals_sent=len(sent),pids_signalled=sent,mcp_routing_acceptance='NOT_TESTED',a2_trusted_dispatch='NOT_ACCEPTED',automatic_rollback=False)
        return 2
    finally:
        for fd in handles.values():os.close(fd)
        if lockfd is not None:os.close(lockfd)
if __name__=='__main__':sys.exit(main())
