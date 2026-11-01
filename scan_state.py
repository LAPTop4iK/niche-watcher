"""Durable query coverage, resumable runs, and an exclusive scanner lock."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import uuid
from datetime import datetime, timezone

SCHEMA = '''
CREATE TABLE IF NOT EXISTS scan_runs (
 run_id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at TEXT NOT NULL,
 finished_at TEXT, config_hash TEXT NOT NULL, settings_json TEXT NOT NULL,
 expected_queries INTEGER NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_queries (
 run_id TEXT NOT NULL REFERENCES scan_runs(run_id), country TEXT NOT NULL,
 category TEXT NOT NULL, query TEXT NOT NULL, started_at TEXT NOT NULL,
 finished_at TEXT, status TEXT NOT NULL, result_count INTEGER, stored_count INTEGER,
 error TEXT, PRIMARY KEY(run_id,country,category,query)
);
CREATE TABLE IF NOT EXISTS query_frames (
 country TEXT NOT NULL, category TEXT NOT NULL, query TEXT NOT NULL,
 query_lang TEXT NOT NULL, market_lang TEXT NOT NULL,
 PRIMARY KEY(country,category,query)
);
'''


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')


@contextmanager
def writer_lock(db):
    if db == ':memory:':
        yield
        return
    with open(str(Path(db).resolve())+'.scan.lock','a') as handle:
        try:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another scanner is already writing this database')
        try:
            yield
        finally:
            fcntl.flock(handle,fcntl.LOCK_UN)


def start_run(con, jobs, settings, run_id=None):
    con.executescript(SCHEMA)
    settings=dict(settings,jobs=jobs)
    payload=json.dumps(settings,sort_keys=True,ensure_ascii=False)
    digest=hashlib.sha256(payload.encode()).hexdigest()
    run_id=run_id or str(uuid.uuid4())
    existing=con.execute('SELECT config_hash,status FROM scan_runs WHERE run_id=?',(run_id,)).fetchone()
    if existing and existing[0]!=digest:
        raise ValueError('Resume configuration differs from the original run')
    if not existing:
        con.execute('INSERT INTO scan_runs VALUES (?,?,?,?,?,?,?,?)',
                    (run_id,settings.get('source','itunes-search'),now(),None,digest,payload,len(jobs),'RUNNING'))
    else:
        con.execute("UPDATE scan_runs SET status='RUNNING',finished_at=NULL WHERE run_id=?",(run_id,))
    con.commit()
    return run_id


def record_frames(con,frames):
    con.executemany('INSERT OR IGNORE INTO query_frames VALUES (?,?,?,?,?)',frames)
    con.commit()


def begin_query(con,run_id,geo,cat,query):
    con.execute('''INSERT INTO scan_queries (run_id,country,category,query,started_at,status)
        VALUES (?,?,?,?,?,'RUNNING') ON CONFLICT(run_id,country,category,query)
        DO UPDATE SET started_at=excluded.started_at,finished_at=NULL,status='RUNNING',error=NULL''',
        (run_id,geo,cat,query,now()))
    con.commit()


def end_query(con,run_id,geo,cat,query,status,result_count=None,stored_count=None,error=None):
    con.execute('''UPDATE scan_queries SET finished_at=?,status=?,result_count=?,stored_count=?,error=?
        WHERE run_id=? AND country=? AND category=? AND query=?''',
        (now(),status,result_count,stored_count,error,run_id,geo,cat,query))
    con.commit()


def finish_run(con,run_id):
    expected=con.execute('SELECT expected_queries FROM scan_runs WHERE run_id=?',(run_id,)).fetchone()[0]
    ok=con.execute("SELECT COUNT(*) FROM scan_queries WHERE run_id=? AND status='OK'",(run_id,)).fetchone()[0]
    status='COMPLETE' if ok==expected else 'PARTIAL'
    con.execute('UPDATE scan_runs SET status=?,finished_at=? WHERE run_id=?',(status,now(),run_id))
    con.commit()
    return status,ok,expected


def query_results(con,run_id,jobs,search,delay=3.1,workers=2):
    """Bounded network concurrency; request starts remain globally spaced."""
    from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
    import threading
    import time
    gate=threading.Lock();next_start=[0.0]
    def fetch(job):
        geo,cat,query=job
        with gate:
            pause=max(0,next_start[0]-time.monotonic())
            if pause:
                time.sleep(pause)
            next_start[0]=time.monotonic()+delay
        try:
            return job,search(query,geo),None
        except RuntimeError as error:
            return job,None,str(error)
    iterator=iter(jobs);pending=set();exhausted=False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while pending or not exhausted:
            while len(pending)<workers and not exhausted:
                try:
                    job=next(iterator)
                except StopIteration:
                    exhausted=True;break
                if con:
                    begin_query(con,run_id,*job)
                pending.add(pool.submit(fetch,job))
            if pending:
                ready,_=wait(pending,return_when=FIRST_COMPLETED)
                for task in ready:
                    pending.remove(task)
                    yield task.result()
