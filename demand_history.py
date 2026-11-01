"""Append-only review measurements. `demand` remains a replaceable latest-value cache."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sqlite3

SCHEMA='''CREATE TABLE IF NOT EXISTS demand_samples (
 sample_id TEXT PRIMARY KEY,track_id INTEGER NOT NULL,country TEXT NOT NULL,
 checked_at TEXT NOT NULL,recorded_at TEXT NOT NULL,origin TEXT NOT NULL,
 payload_json TEXT NOT NULL,snapshot_json TEXT,raw_ref_json TEXT
)'''


def prepare(con):
    con.execute(SCHEMA)
    con.execute('CREATE INDEX IF NOT EXISTS idx_demand_samples_time ON demand_samples(track_id,country,checked_at)')


def canonical(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)


def sample_identity(payload):
    # SQL NULL and an absent legacy column both mean unknown. JSON formatting is
    # not part of the measurement identity; array order/pairing is retained.
    normalized={}
    for key,value in payload.items():
        if value is None:continue
        if key.endswith('_json') and isinstance(value,str):
            try:value=json.loads(value)
            except ValueError:pass
        normalized[key]=value
    return hashlib.sha256(canonical(normalized).encode()).hexdigest()


def as_dict(cursor):
    row=cursor.fetchone()
    return dict(zip([d[0] for d in cursor.description],row)) if row is not None else None


def append(con,payload,origin,raw_ref=None):
    prepare(con)
    if not payload or not payload.get('checked_at'):return 0
    snapshot=None
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='snapshots'").fetchone():
        snapshot=as_dict(con.execute('''SELECT * FROM snapshots WHERE track_id=? AND country=?
            AND julianday(scanned_at)<=julianday(?) ORDER BY julianday(scanned_at) DESC,id DESC LIMIT 1''',
            (payload['track_id'],payload['country'],payload['checked_at'])))
        if snapshot:snapshot.pop('id',None)
    return con.execute('''INSERT OR IGNORE INTO demand_samples VALUES (?,?,?,?,?,?,?,?,?)''',
        (sample_identity(payload),payload['track_id'],payload['country'],payload['checked_at'],
         datetime.now(timezone.utc).isoformat(),origin,canonical(payload),
         canonical(snapshot) if snapshot else None,canonical(raw_ref) if raw_ref else None)).rowcount


def preserve_current(con,track_id,country,origin='legacy-cache'):
    payload=as_dict(con.execute('SELECT * FROM demand WHERE track_id=? AND country=?',(track_id,country)))
    return append(con,payload,origin) if payload else 0


def seed_existing(con):
    """Import only measurements which still exist; never invent lost HTTP bodies."""
    prepare(con);added=0
    names={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'demand' in names:
        cursor=con.execute('SELECT * FROM demand');keys=[d[0] for d in cursor.description]
        for row in cursor.fetchall():added+=append(con,dict(zip(keys,row)),'legacy-cache')
    if 'study_reviews' in names:
        for payload, in con.execute('SELECT evidence_json FROM study_reviews').fetchall():
            added+=append(con,json.loads(payload),'frozen-study')
    return added


def stats(con):
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='demand_samples'").fetchone():return dict(samples=0)
    row=con.execute('SELECT COUNT(*),COUNT(DISTINCT track_id),MIN(checked_at),MAX(checked_at) FROM demand_samples').fetchone()
    return dict(samples=row[0],apps=row[1],first=row[2],last=row[3],
                origins=dict(con.execute('SELECT origin,COUNT(*) FROM demand_samples GROUP BY origin')))


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--db',default='niches.db');ap.add_argument('--seed-existing',action='store_true');args=ap.parse_args()
    if args.seed_existing:
        import scan_state
        with scan_state.writer_lock(args.db),sqlite3.connect(args.db) as con:
            added=seed_existing(con);con.commit();print(json.dumps(dict(imported=added,**stats(con)),ensure_ascii=False))
    else:
        with sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro',uri=True) as con:print(json.dumps(stats(con),ensure_ascii=False))


if __name__=='__main__':main()
