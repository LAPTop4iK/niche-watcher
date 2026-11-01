"""Opt-in local HTTP-body archive for public research endpoints, never Telegram.

Objects are gzip-compressed and content-addressed. Every successful HTTP fetch
has its own event, even when its body is identical. This is not a cloud backup.
"""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from urllib.parse import urlsplit, parse_qsl
import uuid

ENV_DIR='NICHE_RAW_ARCHIVE_DIR'
ENV_LIMIT='NICHE_RAW_ARCHIVE_MAX_MIB'
ALLOWED_HOSTS={'itunes.apple.com','rss.marketingtools.apple.com','web.archive.org'}
HEADERS={'content-type','date','etag','cache-control','last-modified','content-encoding'}
SCHEMA='''CREATE TABLE IF NOT EXISTS objects (
 sha256 TEXT PRIMARY KEY,body_bytes INTEGER NOT NULL,gzip_bytes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fetches (
 event_id TEXT PRIMARY KEY,fetched_at TEXT NOT NULL,url TEXT NOT NULL,status INTEGER,
 sha256 TEXT NOT NULL REFERENCES objects(sha256),headers_json TEXT NOT NULL,
 collector_version TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS fetches_time ON fetches(fetched_at);'''


class ArchiveError(Exception):
    """Stop collection on storage failure; retrying HTTP cannot repair it."""


class CapturedDict(dict):pass
class CapturedList(list):pass


def attach_ref(value,ref):
    if ref is None:return value
    if isinstance(value,dict):value=CapturedDict(value)
    elif isinstance(value,list):value=CapturedList(value)
    else:return value
    value.raw_ref=ref
    return value


def public_url(url):
    parsed=urlsplit(url)
    if parsed.scheme!='https' or parsed.hostname not in ALLOWED_HOSTS or parsed.username or parsed.password or parsed.port not in (None,443):
        raise ArchiveError('Raw archive accepts only the configured public research endpoints')
    if any(any(token in key.lower() for token in ('token','secret','password','key','auth')) for key,_ in parse_qsl(parsed.query)):
        raise ArchiveError('Credential-like query parameters are not archived')


def capture(url,body,status=200,headers=None,directory=None,max_bytes=None):
    directory=directory or os.environ.get(ENV_DIR)
    if not directory:return None
    public_url(url)
    try:
        limit=max_bytes if max_bytes is not None else int(os.environ.get(ENV_LIMIT,'5120'))*1024*1024
        if limit<=0:raise ValueError('Archive capacity must be positive')
        root=Path(directory).expanduser().resolve();root.mkdir(parents=True,exist_ok=True,mode=0o700)
        digest=hashlib.sha256(body).hexdigest();relative=Path('objects')/digest[:2]/(digest+'.gz')
        path=root/relative;compressed=gzip.compress(body,mtime=0)
        index=root/'index.sqlite3'
        with sqlite3.connect(index,timeout=30) as con:
            con.executescript(SCHEMA);con.execute('BEGIN IMMEDIATE')
            existing=con.execute('SELECT 1 FROM objects WHERE sha256=?',(digest,)).fetchone()
            used=con.execute('SELECT COALESCE(SUM(gzip_bytes),0) FROM objects').fetchone()[0]
            if used+(0 if existing else len(compressed))+index.stat().st_size+16384>limit:
                raise ArchiveError('Local raw archive is full; collection stopped without deleting history')
            if not path.exists():
                path.parent.mkdir(parents=True,exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as handle:
                    temporary=Path(handle.name)
                    try:
                        handle.write(compressed);handle.flush();os.fsync(handle.fileno())
                        os.replace(temporary,path)
                    finally:temporary.unlink(missing_ok=True)
            con.execute('INSERT OR IGNORE INTO objects VALUES (?,?,?)',(digest,len(body),len(compressed)))
            event=uuid.uuid4().hex;stamp=datetime.now(timezone.utc).isoformat()
            safe_headers={k.lower():str(v) for k,v in (headers or {}).items() if k.lower() in HEADERS}
            con.execute('INSERT INTO fetches VALUES (?,?,?,?,?,?,?)',
                (event,stamp,url,status,digest,json.dumps(safe_headers,sort_keys=True),'raw-http-v1'))
        return dict(event_id=event,sha256=digest,object=str(relative),fetched_at=stamp)
    except ArchiveError:raise
    except (OSError,sqlite3.Error,ValueError) as exc:
        raise ArchiveError(f'Could not preserve raw response: {exc}') from exc


def stats(directory):
    root=Path(directory).expanduser().resolve();index=root/'index.sqlite3'
    if not index.exists():return dict(fetches=0,objects=0,gzip_bytes=0)
    with sqlite3.connect(index.as_uri()+'?mode=ro',uri=True) as con:
        count,body,size=con.execute('SELECT COUNT(*),COALESCE(SUM(body_bytes),0),COALESCE(SUM(gzip_bytes),0) FROM objects').fetchone()
        return dict(fetches=con.execute('SELECT COUNT(*) FROM fetches').fetchone()[0],objects=count,body_bytes=body,gzip_bytes=size,
                    first=con.execute('SELECT MIN(fetched_at) FROM fetches').fetchone()[0],last=con.execute('SELECT MAX(fetched_at) FROM fetches').fetchone()[0])


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--directory',default=str(Path(__file__).resolve().parent/'.data/raw'));args=ap.parse_args()
    print(json.dumps(stats(args.directory),ensure_ascii=False))
