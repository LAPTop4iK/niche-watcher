#!/usr/bin/env python3
"""Keep the mutable SQLite file out of Git; Git versions a checkpoint pointer.

A checkpoint is an xz archive without derived indexes, named by its SHA-256 and
stored as a GitHub Release asset, so a data commit adds a few bytes rather than
a new binary blob. Restoring never overwrites an existing working database.
Packing uses SQLite's backup API so the checkpoint is coherent even when a
collector is running. `niches.db.gz` remains readable for the first migration.
"""
import argparse
import gzip
import hashlib
import json
import lzma
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.request

RELEASE_TAG='data-checkpoints'
DEFAULT_REPOSITORY='LAPTop4iK/niche-watcher'
ASSET_LIMIT=1900*1024*1024  # GitHub rejects Release assets of 2 GiB or more.
KEEP_ASSETS=30
POINTER_SUFFIX='.checkpoint.json'
FILTERS=[{'id':lzma.FILTER_LZMA2,'preset':9}]  # 64 MiB window spans yesterday's scan.
INDEXES={'idx_snap_track_time':('snapshots','track_id,scanned_at'),
         'idx_snap_store_time':('snapshots','track_id,country,scanned_at,id'),
         'idx_snap_run':('snapshots','run_id'),
         'idx_demand_samples_time':('demand_samples','track_id,country,checked_at')}


def backup(db,out):
    db,out=Path(db).resolve(),Path(out).resolve()
    if db==out:
        raise ValueError('Backup destination must differ from the source')
    with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as source,sqlite3.connect(out) as destination:
        source.backup(destination)
        if destination.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise ValueError('SQLite checkpoint failed integrity check')
    return out


def repository():
    return os.environ.get('GITHUB_REPOSITORY') or DEFAULT_REPOSITORY


def local_store():
    """Tests and offline recovery use a folder instead of the Release."""
    value=os.environ.get('NICHE_CHECKPOINT_DIR')
    return Path(value) if value else None


def gh(*args):
    return subprocess.run(['gh',*args],check=True,capture_output=True,text=True).stdout


def upload(path):
    path=Path(path);folder=local_store()
    if folder:
        folder.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,folder/path.name)
        return
    repo=repository()
    try:
        gh('release','view',RELEASE_TAG,'-R',repo)
    except subprocess.CalledProcessError:
        try:
            gh('release','create',RELEASE_TAG,'-R',repo,'--title','Data checkpoints','--latest=false',
               '--notes','Machine-written SQLite checkpoints. niches.db.checkpoint.json in Git names the current one.')
        except subprocess.CalledProcessError:
            gh('release','view',RELEASE_TAG,'-R',repo)  # A parallel job created it first.
    gh('release','upload',RELEASE_TAG,str(path),'-R',repo,'--clobber')


def download(name,destination):
    folder=local_store()
    if folder:
        shutil.copyfile(folder/name,destination)
        return
    url=f'https://github.com/{repository()}/releases/download/{RELEASE_TAG}/{name}'
    for attempt in range(1,4):
        try:
            with urllib.request.urlopen(url,timeout=120) as response,open(destination,'wb') as output:
                shutil.copyfileobj(response,output)
            return
        except OSError:
            if attempt==3:
                raise
            time.sleep(5*attempt)


def digest(path):
    result=hashlib.sha256()
    with open(path,'rb') as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b''):
            result.update(chunk)
    return result.hexdigest()


def restore_indexes(path):
    with sqlite3.connect(path) as con:
        tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for name,(table,columns) in INDEXES.items():
            if table in tables:
                con.execute(f'CREATE INDEX IF NOT EXISTS {name} ON {table}({columns})')


def decompress(archive,output):
    with open(archive,'rb') as handle:
        magic=handle.read(6)
    opener=lzma.open if magic==b'\xfd7zXZ\x00' else gzip.open
    with opener(archive,'rb') as source:
        shutil.copyfileobj(source,output)


def materialize(archive,db):
    with tempfile.NamedTemporaryFile(dir=db.parent,prefix='.niche-restore-',delete=False) as output:
        temporary=Path(output.name)
    try:
        with temporary.open('wb') as output:
            decompress(archive,output)
        with sqlite3.connect(temporary.as_uri()+'?mode=ro',uri=True) as con:
            if con.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                raise ValueError('Packed database failed integrity check')
        restore_indexes(temporary)
        # A simultaneous first start must not overwrite a newly created DB.
        try:
            os.link(temporary,db)
        except FileExistsError:
            return False
    finally:
        temporary.unlink(missing_ok=True)
    return True


def ensure(db):
    if str(db)==':memory:':
        return False
    db=Path(db).resolve()
    if db.exists():
        return False
    pointer=Path(str(db)+POINTER_SUFFIX)
    if pointer.exists():
        entry=json.loads(pointer.read_text())
        with tempfile.TemporaryDirectory(dir=db.parent,prefix='.niche-restore-') as folder:
            archive=Path(folder)/entry['asset']
            download(entry['asset'],archive)
            if digest(archive)!=entry['sha256']:
                raise ValueError('Checkpoint does not match its pointer')
            return materialize(archive,db)
    for suffix in ('.xz','.gz'):
        archive=Path(str(db)+suffix)
        if archive.exists():
            return materialize(archive,db)
    return False


def pack(db,out=None):
    ensure(db);db=Path(db).resolve();out=Path(out or str(db)+'.xz').resolve()
    with tempfile.TemporaryDirectory(prefix='niche-checkpoint-') as folder:
        snapshot=backup(db,Path(folder)/'snapshot.db')
        with sqlite3.connect(snapshot) as con:
            # Indexes hold 25% of the file and compress worst; rebuild them on restore.
            for name in INDEXES:
                con.execute(f'DROP INDEX IF EXISTS {name}')
        with sqlite3.connect(snapshot,isolation_level=None) as con:
            con.execute('VACUUM')
        with tempfile.NamedTemporaryFile(dir=out.parent,prefix='.niche-pack-',delete=False) as handle:
            temporary=Path(handle.name)
        try:
            with snapshot.open('rb') as source,lzma.open(temporary,'wb',format=lzma.FORMAT_XZ,filters=FILTERS) as archive:
                shutil.copyfileobj(source,archive)
            if temporary.stat().st_size>=ASSET_LIMIT:
                raise ValueError('Checkpoint reached the GitHub Release asset limit; split history first')
            os.replace(temporary,out)
        finally:
            temporary.unlink(missing_ok=True)
    return out


def publish(archive,pointer):
    """Upload first: a pointer in Git must never name a missing checkpoint."""
    archive=Path(archive);sha=digest(archive);name=f'niches-{sha[:16]}.db.xz'
    with tempfile.TemporaryDirectory(prefix='niche-publish-') as folder:
        named=Path(folder)/name
        shutil.copyfile(archive,named);upload(named)
    entry=dict(format=1,asset=name,sha256=sha,bytes=archive.stat().st_size)
    Path(pointer).write_text(json.dumps(entry,indent=1)+'\n')
    return entry


def prune(keep=KEEP_ASSETS,protect=()):
    """Every checkpoint contains the full history; old ones only allow rollback."""
    folder=local_store()
    if folder:
        assets=[(p.stat().st_mtime,p.name) for p in folder.iterdir()]
    else:
        data=json.loads(gh('release','view',RELEASE_TAG,'-R',repository(),'--json','assets'))
        assets=[(a['createdAt'],a['name']) for a in data['assets']]
    removed=[]
    for _,name in sorted(assets,reverse=True)[keep:]:
        if name in protect:
            continue
        if folder:
            (folder/name).unlink()
        else:
            gh('release','delete-asset',RELEASE_TAG,name,'-R',repository(),'-y')
        removed.append(name)
    return removed


def sync(db):
    """Explicitly union the published checkpoint into the local working database."""
    import db_merge
    import scan_state
    db=Path(db).resolve()
    sources=[s for s in (POINTER_SUFFIX,'.xz','.gz') if Path(str(db)+s).exists()]
    if not sources:
        raise FileNotFoundError(str(db)+POINTER_SUFFIX)
    with scan_state.writer_lock(str(db)),tempfile.TemporaryDirectory(prefix='niche-sync-') as folder:
        target=Path(folder)/'incoming.db'
        shutil.copyfile(str(db)+sources[0],str(target)+sources[0]);ensure(target)
        if db.exists():
            db_merge.merge(str(db),str(target))
        else:
            backup(target,db)


def main():
    ap=argparse.ArgumentParser();ap.add_argument('action',choices=['restore','pack','publish','prune','backup','sync']);ap.add_argument('--db',default='niches.db');ap.add_argument('--out');ap.add_argument('--archive');ap.add_argument('--pointer',default='niches.db'+POINTER_SUFFIX);args=ap.parse_args()
    if args.action=='sync':
        sync(args.db);print('Archive and local observations merged')
    elif args.action=='restore':
        restored=ensure(args.db)
        if not Path(args.db).exists():
            raise SystemExit('Database and compressed checkpoint are missing')
        print('Database restored' if restored else 'Working database retained')
    elif args.action=='pack':
        out=pack(args.db,args.out);print('Packed checkpoint:',out.name,out.stat().st_size,'bytes')
    elif args.action=='publish':
        if not args.archive:ap.error('publish requires --archive')
        entry=publish(args.archive,args.pointer);print('Published checkpoint:',entry['asset'],entry['bytes'],'bytes')
    elif args.action=='prune':
        protect={json.loads(Path(args.pointer).read_text())['asset']} if Path(args.pointer).exists() else set()
        print('Removed checkpoints:',len(prune(protect=protect)))
    else:
        if not args.out:ap.error('backup requires --out')
        ensure(args.db);backup(args.db,args.out);print('SQLite checkpoint saved')


if __name__=='__main__':main()
