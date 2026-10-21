#!/usr/bin/python3.11
"""Unpublished launcher candidate. Review/publish separately; no writes or signals here."""
from pathlib import Path
import argparse,hashlib,json,os,stat,sys

def protected_read(path,limit):
    if not path.is_absolute():raise ValueError('ABSOLUTE_PATH_REQUIRED')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC)
    try:
        for part in path.parts[1:-1]:
            nextfd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=fd)
            os.close(fd);fd=nextfd;s=os.fstat(fd)
            if s.st_uid!=0 or s.st_mode&0o022:raise ValueError('UNPROTECTED_PARENT')
        last=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK,dir_fd=fd)
        try:
            s=os.fstat(last)
            if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:raise ValueError('UNPROTECTED_FILE')
            raw=os.read(last,limit+1)
            if len(raw)>limit:raise ValueError('SOURCE_SIZE')
            return raw
        finally:os.close(last)
    finally:os.close(fd)

def execute_snapshot(raw,path,expected,launcher_path,launcher_hash):
    # Hash and compile exactly the SAME immutable in-memory bytes, not another reopen.
    if hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('CANDIDATE_HASH_MISMATCH')
    namespace={'__name__':'__main__','__file__':str(path),'__builtins__':__builtins__,
               '__r2a_loaded_sha256__':expected,'__r2a_loader_path__':str(launcher_path),
               '__r2a_loader_sha256__':launcher_hash}
    exec(compile(raw,str(path),'exec'),namespace)
    return namespace

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--candidate',required=True)
    parser.add_argument('--expected-sha256',required=True)
    parser.add_argument('--expected-launcher-sha256',required=True)
    parser.add_argument('candidate_args',nargs=argparse.REMAINDER)
    args=parser.parse_args()
    if not(sys.flags.isolated and sys.dont_write_bytecode and sys.version_info[:2]==(3,11)):
        raise ValueError('SYSTEM_PYTHON311_I_B_REQUIRED')
    path=Path(args.candidate);launcher=Path(__file__).absolute()
    if path.name!='r2a_retire_final_candidate.py' or path.parent.name!=args.expected_sha256:
        raise ValueError('CONTENT_ADDRESSED_RELEASE_REQUIRED')
    if path.parent.parent!=Path('/opt/aee-r2a/releases') or launcher.parent!=path.parent:
        raise ValueError('FIXED_RELEASE_ROOT_REQUIRED')
    loader_raw=protected_read(launcher,32768)
    if hashlib.sha256(loader_raw).hexdigest()!=args.expected_launcher_sha256:
        raise ValueError('LAUNCHER_HASH_MISMATCH')
    manifest=json.loads(protected_read(path.parent/'READY.json',4096))
    if manifest!={'candidate_sha256':args.expected_sha256,'launcher_sha256':args.expected_launcher_sha256}:
        raise ValueError('RELEASE_NOT_READY')
    raw=protected_read(path,131072)
    rest=args.candidate_args
    if rest[:1]==['--']:rest=rest[1:]
    sys.argv=[str(path),*rest]
    execute_snapshot(raw,path,args.expected_sha256,launcher,args.expected_launcher_sha256)
if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('LAUNCHER_BLOCKED:'+type(exc).__name__,flush=True);sys.exit(2)
