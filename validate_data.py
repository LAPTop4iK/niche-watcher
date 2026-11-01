#!/usr/bin/env python3
"""Validate SQLite integrity and exact coverage of every recorded query."""
import argparse
import db_store
import json
from pathlib import Path
import sqlite3
import evidence
import demand_history


def validate(con,require_complete=False,required_run=None):
    errors=[];checks=[]
    result=con.execute('PRAGMA quick_check').fetchall()
    if [r[0] for r in result]!=['ok']:
        errors.append('SQLite quick_check: '+str(result))
    else:
        checks.append('SQLite integrity')
    if con.execute('PRAGMA foreign_key_check').fetchall():
        errors.append('Foreign-key violations')
    else:
        checks.append('Foreign keys')
    if con.execute('SELECT 1 FROM snapshots WHERE country IS NULL OR track_id IS NULL OR rating_count<0 LIMIT 1').fetchone():
        errors.append('Invalid snapshot keys or negative count')
    if 'demand_samples' in evidence.tables(con):
        before=len(errors)
        for identity,tid,geo,checked,payload,context in con.execute('SELECT sample_id,track_id,country,checked_at,payload_json,snapshot_json FROM demand_samples'):
            try:
                d=json.loads(payload);stamp=evidence.parse(checked)
                if (d['track_id'],d['country'],d['checked_at'])!=(tid,geo,checked) or demand_history.sample_identity(d)!=identity:
                    raise ValueError('Payload does not match immutable sample identity')
                if context:
                    snapshot=json.loads(context)
                    if (snapshot['track_id'],snapshot['country'])!=(tid,geo) or evidence.parse(snapshot['scanned_at'])>stamp:
                        raise ValueError('Context is from another record or the future')
            except (ValueError,TypeError,KeyError,AttributeError) as exc:
                errors.append(f'Review history {identity}: {exc}')
        if len(errors)==before:checks.append('Review history identities and as-of context')
    if {'scan_runs','scan_queries'}<=evidence.tables(con):
        for run_id,expected,status,payload in con.execute('SELECT run_id,expected_queries,status,settings_json FROM scan_runs'):
            try:
                jobs=[tuple(job) for job in json.loads(payload)['jobs']]
                actual={tuple(row) for row in con.execute('SELECT country,category,query FROM scan_queries WHERE run_id=?',(run_id,))}
                if len(jobs)!=len(set(jobs)) or len(jobs)!=expected or not actual<=set(jobs):
                    errors.append(f'{run_id}: query keys differ from configured jobs')
            except (ValueError,KeyError,TypeError):
                errors.append(f'{run_id}: invalid query configuration')
            rows=con.execute('SELECT status,COUNT(*) FROM scan_queries WHERE run_id=? GROUP BY status',(run_id,)).fetchall()
            counts={r[0]:r[1] for r in rows}
            if status=='COMPLETE' and (counts.get('OK',0)!=expected or sum(counts.values())!=expected):
                errors.append(f'{run_id}: COMPLETE does not match query coverage')
            if (require_complete or required_run==run_id) and status!='COMPLETE':
                errors.append(f'{run_id}: {status}, {counts.get("OK",0)}/{expected}')
        has_run='run_id' in {r[1] for r in con.execute('PRAGMA table_info(snapshots)')}
        if has_run:
            mismatch=con.execute('''SELECT q.run_id,q.country,q.category,q.query,q.stored_count,COUNT(s.id)
                FROM scan_queries q LEFT JOIN snapshots s ON s.run_id=q.run_id AND s.country=q.country
                  AND s.category=q.category AND s.query=q.query
                JOIN scan_runs r ON r.run_id=q.run_id WHERE q.status='OK' AND r.source!='google-play' GROUP BY q.run_id,q.country,q.category,q.query
                HAVING COALESCE(q.stored_count,0)!=COUNT(s.id) LIMIT 10''').fetchall()
            if mismatch:
                errors.append('Stored query counts differ from observations: '+str([tuple(r) for r in mismatch]))
            duplicates=con.execute('''SELECT 1 FROM snapshots WHERE run_id IS NOT NULL
                GROUP BY run_id,track_id,country,category,query HAVING COUNT(*)>1 LIMIT 1''').fetchone()
            if duplicates:
                errors.append('Duplicate application/query observations within a run')
        if 'play_snapshots' in evidence.tables(con) and 'run_id' in {r[1] for r in con.execute('PRAGMA table_info(play_snapshots)')}:
            bad=con.execute("""SELECT q.run_id,q.country,q.query FROM scan_queries q JOIN scan_runs r USING(run_id)
                LEFT JOIN play_snapshots p ON p.run_id=q.run_id AND p.country=q.country AND p.category=q.category AND p.query=q.query
                WHERE r.source='google-play' AND q.status='OK' GROUP BY q.run_id,q.country,q.category,q.query
                HAVING q.stored_count!=COUNT(p.id) LIMIT 1""").fetchone()
            if bad:
                errors.append('Play stored counts differ from coverage ledger')
        if required_run and not con.execute('SELECT 1 FROM scan_runs WHERE run_id=?',(required_run,)).fetchone():
            errors.append('Required run not found: '+required_run)
        checks.append('Recorded query counts and run states')
    if required_run and 'scan_runs' not in evidence.tables(con):
        errors.append('Required run not found: '+required_run)
    return checks,errors


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--db',default='niches.db');ap.add_argument('--require-complete',action='store_true');ap.add_argument('--require-run');args=ap.parse_args()
    db_store.ensure(args.db)
    with sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro',uri=True) as con:
        con.execute('BEGIN');checks,errors=validate(con,args.require_complete,args.require_run)
    for check in checks:
        print('PASS:',check)
    for error in errors:
        print('FAIL:',error)
    if errors:
        raise SystemExit(1)


if __name__=='__main__':
    main()
