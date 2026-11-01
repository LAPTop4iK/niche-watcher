#!/usr/bin/env python3
"""Version a compressed checkpoint; keep the mutable SQLite file outside Git.

Restoring never overwrites an existing working database. Packing uses SQLite's
backup API so the checkpoint is coherent even when a collector is running.
"""
import argparse
import gzip
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile


def backup(db,out):
    db,out=Path(db).resolve(),Path(out).resolve()
    if db==out:
        raise ValueError('Backup destination must differ from the source')
    with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as source,sqlite3.connect(out) as destination:
        source.backup(destination)
        if destination.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise ValueError('SQLite checkpoint failed integrity check')
    return out


def ensure(db):
    if str(db)==':memory:':
        return False
    db=Path(db).resolve();archive=Path(str(db)+'.gz')
    if db.exists() or not archive.exists():
        return False
    with tempfile.NamedTemporaryFile(dir=db.parent,prefix='.niche-restore-',delete=False) as output:
        temporary=Path(output.name)
    try:
        with gzip.open(archive,'rb') as source,temporary.open('wb') as output:
            shutil.copyfileobj(source,output)
        with sqlite3.connect(temporary.as_uri()+'?mode=ro',uri=True) as con:
            if con.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                raise ValueError('Packed database failed integrity check')
        # A simultaneous first start must not overwrite a newly created DB.
        try:
            os.link(temporary,db)
        except FileExistsError:
            return False
    finally:
        temporary.unlink(missing_ok=True)
    return True


def pack(db,out=None):
    ensure(db);db=Path(db).resolve();out=Path(out or str(db)+'.gz').resolve()
    with tempfile.TemporaryDirectory(prefix='niche-checkpoint-') as folder:
        snapshot=backup(db,Path(folder)/'snapshot.db')
        with tempfile.NamedTemporaryFile(dir=out.parent,prefix='.niche-pack-',delete=False) as handle:
            temporary=Path(handle.name)
        try:
            with snapshot.open('rb') as source,temporary.open('wb') as destination:
                with gzip.GzipFile(filename='',fileobj=destination,mode='wb',mtime=0,compresslevel=6) as archive:
                    shutil.copyfileobj(source,archive)
            if temporary.stat().st_size>=90*1024*1024:
                raise ValueError('Compressed DB reached 90 MiB; move history to external storage before the GitHub file limit')
            os.replace(temporary,out)
        finally:
            temporary.unlink(missing_ok=True)
    return out


def sync(db):
    """Explicitly union a pulled archive into the local working database."""
    import db_merge
    import scan_state
    db=Path(db).resolve();archive=Path(str(db)+'.gz')
    if not archive.exists():
        raise FileNotFoundError(archive)
    with scan_state.writer_lock(str(db)),tempfile.TemporaryDirectory(prefix='niche-sync-') as folder:
        target=Path(folder)/'incoming.db'
        shutil.copyfile(archive,str(target)+'.gz');ensure(target)
        if db.exists():
            db_merge.merge(str(db),str(target))
        else:
            backup(target,db)


def main():
    ap=argparse.ArgumentParser();ap.add_argument('action',choices=['restore','pack','backup','sync']);ap.add_argument('--db',default='niches.db');ap.add_argument('--out');args=ap.parse_args()
    if args.action=='sync':
        sync(args.db);print('Archive and local observations merged')
    elif args.action=='restore':
        restored=ensure(args.db)
        if not Path(args.db).exists():
            raise SystemExit('Database and compressed checkpoint are missing')
        print('Database restored' if restored else 'Working database retained')
    elif args.action=='pack':
        out=pack(args.db,args.out);print('Packed checkpoint:',out.name,out.stat().st_size,'bytes')
    else:
        if not args.out:ap.error('backup requires --out')
        ensure(args.db);backup(args.db,args.out);print('SQLite checkpoint saved')


if __name__=='__main__':main()
