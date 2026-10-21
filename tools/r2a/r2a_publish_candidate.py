#!/usr/bin/python3.11
"""Unpublished root-admin publication candidate; default verifies snapshots without writes.
No keys, trust anchors, locks, services or signals are created/changed by this tool.
--publish is a future separately approved root operation; never run this round.
"""
from pathlib import Path
import argparse,hashlib,json,os,re,stat,sys

def checked_snapshot(path,expected,limit,source_uid):
    if type(source_uid) is not int or source_uid<=0:raise ValueError('SOURCE_UID_REQUIRED')
    if not re.fullmatch('[0-9a-f]{64}',expected):raise ValueError('EXPECTED_SHA256_REQUIRED')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK)
    try:
        s=os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=source_uid or s.st_mode&0o777!=0o600:raise ValueError('PRIVATE_CANDIDATE_REQUIRED')
        raw=os.read(fd,limit+1);after=os.fstat(fd)
        if (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):raise ValueError('SOURCE_CHANGED_DURING_READ')
        if len(raw)>limit or len(raw)!=s.st_size or hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('REVIEWED_BYTES_MISMATCH')
        return raw
    finally:os.close(fd)

def root_directory(fd):
    s=os.fstat(fd)
    if not stat.S_ISDIR(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:raise ValueError('ROOT_DIRECTORY_REQUIRED')

def root_child(parent,name,create=False):
    if create:
        try:os.mkdir(name,0o755,dir_fd=parent)
        except FileExistsError:pass
    fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent)
    try:root_directory(fd)
    except BaseException:os.close(fd);raise
    return fd

def write_new(fd,name,raw):
    out=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o444,dir_fd=fd)
    try:
        with os.fdopen(out,'wb',closefd=False) as stream:stream.write(raw);stream.flush();os.fsync(out)
        os.fchmod(out,0o444)
    finally:os.close(out)
    check=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=fd)
    try:
        actual=os.read(check,len(raw)+1)
        if actual!=raw:raise ValueError('PUBLICATION_READBACK_MISMATCH')
    finally:os.close(check)

def publish(candidate,launcher,candidate_hash,launcher_hash):
    if os.geteuid()!=0:raise ValueError('ROOT_ADMIN_ONLY')
    fds=[]
    try:
        root=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC);fds.append(root);root_directory(root)
        opt=root_child(root,'opt');fds.append(opt)
        base=root_child(opt,'aee-r2a',True);fds.append(base)
        releases=root_child(base,'releases',True);fds.append(releases)
        # Never replace an existing release, including a previous incomplete attempt.
        os.mkdir(candidate_hash,0o755,dir_fd=releases)
        release=root_child(releases,candidate_hash);fds.append(release)
        write_new(release,'r2a_retire_final_candidate.py',candidate)
        write_new(release,'r2a_snapshot_launcher_candidate.py',launcher)
        manifest=json.dumps({'candidate_sha256':candidate_hash,'launcher_sha256':launcher_hash},sort_keys=True).encode()
        write_new(release,'READY.json',manifest)  # Completion marker LAST.
        os.fsync(release);os.fsync(releases)
    finally:
        for fd in reversed(fds):os.close(fd)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-uid',type=int,required=True)
    p.add_argument('--candidate',required=True);p.add_argument('--launcher',required=True)
    p.add_argument('--candidate-sha256',required=True);p.add_argument('--launcher-sha256',required=True)
    mode=p.add_mutually_exclusive_group();mode.add_argument('--dry-run',action='store_true');mode.add_argument('--publish',action='store_true')
    args=p.parse_args()
    if not(sys.flags.isolated and sys.dont_write_bytecode and sys.version_info[:2]==(3,11)):raise ValueError('PYTHON311_I_B_REQUIRED')
    candidate=checked_snapshot(args.candidate,args.candidate_sha256,131072,args.source_uid)
    launcher=checked_snapshot(args.launcher,args.launcher_sha256,32768,args.source_uid)
    if args.publish:publish(candidate,launcher,args.candidate_sha256,args.launcher_sha256)
    print(json.dumps({'mode':'publish' if args.publish else 'dry-run','reviewed_snapshots':'PASS','formal_writes':bool(args.publish),'signals_sent':0}))
if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('PUBLICATION_BLOCKED:'+type(exc).__name__,flush=True);sys.exit(2)
