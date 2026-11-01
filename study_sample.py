#!/usr/bin/env python3
"""Freeze the comparison sample of one complete search, then fill missing RSS.

The control sample is selected without looking at review outcomes. Sampling
features are retained so later scans cannot silently change the study cohort.
"""
import argparse
import db_store
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from datetime import timedelta
import evidence
import demand_check as demand

SCHEMA='''CREATE TABLE IF NOT EXISTS study_members (
 study_id TEXT NOT NULL,track_id INTEGER NOT NULL,country TEXT NOT NULL,role TEXT NOT NULL,
 selected_at TEXT NOT NULL,version_date TEXT,release_date TEXT,rating_count INTEGER,
 PRIMARY KEY(study_id,track_id,country)
)'''


def freeze(con,run_id,controls=20):
    if not isinstance(controls,int) or controls<1:
        raise ValueError('Control count must be a positive integer')
    run=con.execute('SELECT status,finished_at FROM scan_runs WHERE run_id=?',(run_id,)).fetchone()
    if not run or run[0]!='COMPLETE':
        raise ValueError('A study requires a complete search run')
    con.execute(SCHEMA)
    if con.execute('SELECT 1 FROM study_members WHERE study_id=? LIMIT 1',(run_id,)).fetchone():
        return
    con.row_factory=sqlite3.Row
    rows=con.execute('''WITH ranked AS (SELECT *,ROW_NUMBER() OVER
        (PARTITION BY track_id,country ORDER BY scanned_at DESC,id DESC) rn
        FROM snapshots WHERE run_id=?) SELECT * FROM ranked WHERE rn=1 AND rating_count>=500''',(run_id,)).fetchall()
    reference=evidence.parse(run[1]);old=[];fresh={}
    for record in rows:
        r=dict(record)
        try:age=(reference-evidence.parse(r['version_date'])).total_seconds()/86400/30.44
        except (ValueError,TypeError,AttributeError):continue
        if age>=18:old.append(r)
        elif 0<=age<12:fresh.setdefault(r['country'],[]).append(r)
    selected=[('CANDIDATE',r) for r in old]
    for geo,items in fresh.items():
        items.sort(key=lambda r:hashlib.sha256(f"{r['track_id']}:{geo}".encode()).hexdigest())
        selected.extend(('CONTROL',r) for r in items[:controls])
    for role,r in selected:
        con.execute('INSERT INTO study_members VALUES (?,?,?,?,?,?,?,?)',
            (run_id,r['track_id'],r['country'],role,run[1],r['version_date'],r['release_date'],r['rating_count']))
    con.commit()


def freeze_reviews(con,run_id):
    """Retain the audit's raw outcomes; future weekly refreshes cannot rewrite it."""
    con.execute("""CREATE TABLE IF NOT EXISTS study_reviews (
        study_id TEXT NOT NULL,track_id INTEGER NOT NULL,country TEXT NOT NULL,
        evidence_json TEXT NOT NULL,PRIMARY KEY(study_id,track_id,country))""")
    run=con.execute('SELECT started_at,finished_at FROM scan_runs WHERE run_id=?',(run_id,)).fetchone()
    if not run or not run[1]:
        raise ValueError('Review capture requires a finished search')
    start,end=evidence.parse(run[0]),evidence.parse(run[1])+timedelta(days=1)
    cache=evidence.demand_map(con)
    for tid,geo in con.execute('SELECT track_id,country FROM study_members WHERE study_id=?',(run_id,)):
        row=cache.get((tid,geo))
        if row and start<=evidence.parse(row['checked_at'])<=end:
            con.execute('INSERT OR IGNORE INTO study_reviews VALUES (?,?,?,?)',
                        (run_id,tid,geo,json.dumps(row,ensure_ascii=False)))
    con.commit()


def boundary_keys(con,run_id):
    """Legacy date-only samples need exact timestamps near the 90d boundary."""
    cutoff=(sc_now()-timedelta(days=90)).date()
    cache=evidence.demand_map(con);keys=[]
    for tid,geo in con.execute('SELECT track_id,country FROM study_members WHERE study_id=?',(run_id,)):
        row=cache.get((tid,geo)) or {}
        dates=json.loads(row.get('review_dates_json') or '[]')
        if any(len(stamp)==10 and abs((evidence.parse(stamp).date()-cutoff).days)<=1 for stamp in dates):
            keys.append((tid,geo))
    return keys


def sc_now():
    return demand.sc.now_utc()


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--db',default='niches.db');ap.add_argument('--run-id',required=True);ap.add_argument('--controls',type=int,default=20);ap.add_argument('--fill-missing',action='store_true');ap.add_argument('--delay',type=float,default=1.5);args=ap.parse_args()
    db_store.ensure(args.db)
    con=sqlite3.connect(args.db,timeout=30);freeze(con,args.run_id,args.controls);demand.prepare(con)
    rows=con.execute('''SELECT s.track_id,s.country FROM study_members s JOIN scan_runs r ON r.run_id=s.study_id
        LEFT JOIN demand d ON d.track_id=s.track_id AND d.country=s.country
        WHERE s.study_id=? AND (d.checked_at IS NULL OR d.checked_at<r.started_at)''',(args.run_id,)).fetchall()
    print('Study members:',[tuple(r) for r in con.execute('SELECT role,COUNT(*) FROM study_members WHERE study_id=? GROUP BY role',(args.run_id,))])
    boundary=boundary_keys(con,args.run_id)
    print('Missing current review checks:',len(rows),'· date-only boundary samples:',len(boundary))
    rows=list(dict.fromkeys([tuple(row) for row in rows]+boundary))
    if args.fill_missing:
        for tid,geo in rows:
            demand.store_result(con,tid,geo,demand.fetch_demand(tid,geo));time.sleep(args.delay)
    freeze_reviews(con,args.run_id)
    print('Frozen review samples:',con.execute('SELECT COUNT(*) FROM study_reviews WHERE study_id=?',(args.run_id,)).fetchone()[0])
    con.close()


if __name__=='__main__':main()
