"""Offline job-store backup/migration/rollback; never mutate the source store."""
import argparse
import fcntl
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from .packaging import safe_output
from .store import JobStore, JobError, now, ACTIVE, TERMINAL
from .result_contract import seal_record

ID = re.compile(r'A3JOB-[0-9]{8}-[a-f0-9]{32}')


def private_write(path,payload):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as f:
        f.write(payload);f.flush();os.fsync(f.fileno())


def sync_directory(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def inspect_records(source):
    source=Path(source)
    if source.is_symlink() or not source.is_dir():
        raise JobError('MIGRATION_INVALID','Source must be a stopped private job store')
    records={}
    for p in sorted(source.iterdir()):
        if p.name in {'.registry.lock','.active.lock'}:
            if p.is_symlink() or not p.is_file():
                raise JobError('MIGRATION_INVALID','Invalid source lock')
            # Open existing locks only, never create or change baseline files.
            fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise JobError('BUSY','Stop all old workers before migration') from None
            os.close(fd)
            continue
        if p.suffix!='.json' or not ID.fullmatch(p.stem) or p.is_symlink() or not p.is_file():
            raise JobError('MIGRATION_INVALID','Unknown source entry requires operator review')
        fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as f:payload=f.read(65537)
        if len(payload)>65536:
            raise JobError('MIGRATION_INVALID','Oversized source record')
        try:
            body=json.loads(payload)
            if not isinstance(body,dict) or body.get('job_id')!=p.stem or body.get('status') not in ACTIVE | TERMINAL:
                raise ValueError()
        except (ValueError,UnicodeError):
            raise JobError('MIGRATION_INVALID','Invalid source record') from None
        # Validate original content before any trusted state transformation.
        reader = object.__new__(JobStore)
        reader._root = source
        reader._read(p.stem)
        records[p.name]=payload
    return records


def migrate(source,destination,backup,*,apply=False):
    destination=safe_output(destination);backup=safe_output(backup)
    source=Path(source).resolve()
    if (destination==backup or source in {destination,backup} or destination.exists() or backup.exists()
            or destination.is_relative_to(source) or backup.is_relative_to(source)
            or destination.is_relative_to(backup) or backup.is_relative_to(destination)):
        raise JobError('MIGRATION_INVALID','Destination and backup must be new separate paths')
    records=inspect_records(source)
    inventory={name:hashlib.sha256(value).hexdigest() for name,value in records.items()}
    preview={'schema':1,'source':str(source),'records':inventory,
             'interrupted':sum(json.loads(value)['status'] in {'queued','running'} for value in records.values()),
             'source_untouched':True,'applied':apply}
    if not apply:return preview
    # Source is required to be offline; check again and compare exact inventory.
    if inspect_records(source)!=records:
        raise JobError('MIGRATION_INVALID','Source changed during offline migration')
    backup.mkdir(mode=0o700,parents=True)
    for name,value in records.items():
        private_write(backup/name,value)
    private_write(backup/'backup-manifest.json',(json.dumps(preview,sort_keys=True)+'\n').encode())
    sync_directory(backup);sync_directory(backup.parent)
    try:
        destination.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.aee-migration-',dir=destination.parent) as temporary:
            staged=Path(temporary)/'jobs';staged.mkdir(mode=0o700)
            for name,value in records.items():
                body=json.loads(value)
                if body['status'] in {'queued','running'}:
                    body.update(status='failed',error_code='EXECUTION_INTERRUPTED',
                                error='Interrupted during offline store migration',finished_at=now(),updated_at=now())
                    seal_record(body)
                private_write(staged/name,(json.dumps(body,ensure_ascii=False,sort_keys=True)+'\n').encode())
            store=JobStore(staged)
            try:
                for name in records:store.get(Path(name).stem)
            finally:store.close()
            sync_directory(staged);os.rename(staged,destination);sync_directory(destination.parent)
    except Exception:
        # Preserve the backup, source and invalid evidence; do not auto-delete it.
        raise
    return preview


def rollback_copy(backup,destination,*,apply=False):
    backup=Path(backup);destination=safe_output(destination)
    if destination.exists():raise JobError('MIGRATION_INVALID','Rollback destination must be new')
    manifest=json.loads((backup/'backup-manifest.json').read_text())
    records={}
    for name,digest in manifest['records'].items():
        if not ID.fullmatch(Path(name).stem) or Path(name).name!=name:
            raise JobError('MIGRATION_INVALID','Invalid backup manifest path')
        p=backup/name
        if p.is_symlink() or hashlib.sha256(p.read_bytes()).hexdigest()!=digest:
            raise JobError('MIGRATION_INVALID','Backup integrity failed')
        records[name]=p.read_bytes()
    if apply:
        destination.mkdir(mode=0o700,parents=True)
        for name,value in records.items():
            private_write(destination/name,value)
        sync_directory(destination);sync_directory(destination.parent)
    return {'records':len(records),'applied':apply,'backup_verified':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['migrate','rollback'])
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--destination',type=Path,required=True)
    parser.add_argument('--backup',type=Path)
    parser.add_argument('--apply-fixture',action='store_true',help='write only new non-runtime fixture paths')
    args=parser.parse_args()
    if args.action=='migrate':
        if args.backup is None:parser.error('--backup is required for migration')
        result=migrate(args.source,args.destination,args.backup,apply=args.apply_fixture)
    else:result=rollback_copy(args.source,args.destination,apply=args.apply_fixture)
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
