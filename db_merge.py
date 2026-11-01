#!/usr/bin/env python3
"""Union observations by complete payload, including country/category/query.

Only merge quiescent SQLite files or files made with sqlite3.Connection.backup.
Do not copy/rebase a live database. Demand results are merged by checked_at.
"""
import argparse
import os
import sqlite3
import db_store
import demand_history


def columns(con, schema, table):
    return {r[1]:r[2] for r in con.execute(f'PRAGMA {schema}.table_info({table})')}


def merge(main_path, other_path):
    db_store.ensure(main_path);db_store.ensure(other_path)
    if not os.path.exists(other_path):
        raise FileNotFoundError(other_path)
    with sqlite3.connect(main_path, timeout=30) as con:
        con.execute('ATTACH DATABASE ? AS src',(os.fspath(other_path),))
        source_tables={r[0]:r[1] for r in con.execute("SELECT name,sql FROM src.sqlite_master WHERE type='table'")}
        # Preserve the destination cache before the latest-value merge can replace it.
        demand_history.seed_existing(con)
        counts={}
        for table in ('apps','scan_runs','scan_queries','query_frames','snapshots','play_apps','play_snapshots','demand_samples','demand','study_members','study_reviews','notification_outbox'):
            if table not in source_tables:
                continue
            if not columns(con,'main',table):
                con.execute(source_tables[table])
            src=columns(con,'src',table)
            dst=columns(con,'main',table)
            for name,kind in src.items():
                if name not in dst:
                    con.execute(f'ALTER TABLE main.{table} ADD COLUMN "{name}" {kind}')
            cols=[k for k in src if k!='id']
            names=','.join(f'"{k}"' for k in cols)
            if table in ('snapshots','play_snapshots'):
                equals=' AND '.join(f'm."{k}" IS s."{k}"' for k in cols)
                counts[table]=con.execute(f'''INSERT INTO main.{table} ({names})
                    SELECT {','.join('s.'+chr(34)+k+chr(34) for k in cols)} FROM src.{table} s
                    WHERE NOT EXISTS (SELECT 1 FROM main.{table} m WHERE {equals})''').rowcount
            elif table in ('study_members','study_reviews','demand_samples','query_frames'):
                con.execute(f'INSERT OR IGNORE INTO main.{table} ({names}) SELECT {names} FROM src.{table}')
            elif table in ('scan_runs','scan_queries','notification_outbox'):
                keys={'scan_runs':['run_id'],'scan_queries':['run_id','country','category','query'],
                      'notification_outbox':['event_key']}[table]
                if table=='scan_runs':
                    conflict=con.execute('SELECT 1 FROM main.scan_runs m JOIN src.scan_runs s USING(run_id) WHERE m.config_hash!=s.config_hash LIMIT 1').fetchone()
                    if conflict:
                        raise ValueError('Conflicting scan configurations with the same run ID')
                update=','.join(f'"{k}"=excluded."{k}"' for k in cols if k not in keys)
                if table=='scan_runs':
                    condition="excluded.status='COMPLETE' OR (scan_runs.status!='COMPLETE' AND COALESCE(excluded.finished_at,excluded.started_at)>=COALESCE(scan_runs.finished_at,scan_runs.started_at))"
                elif table=='scan_queries':
                    condition="(excluded.status='OK' AND (scan_queries.status!='OK' OR excluded.finished_at>=scan_queries.finished_at)) OR (scan_queries.status!='OK' AND COALESCE(excluded.finished_at,excluded.started_at)>=COALESCE(scan_queries.finished_at,scan_queries.started_at))"
                else:
                    condition="excluded.status='SENT' OR (notification_outbox.status!='SENT' AND COALESCE(excluded.last_attempt_at,excluded.created_at)>=COALESCE(notification_outbox.last_attempt_at,notification_outbox.created_at))"
                con.execute(f'INSERT INTO main.{table} ({names}) SELECT {names} FROM src.{table} WHERE 1 ON CONFLICT({",".join(keys)}) DO UPDATE SET {update} WHERE {condition}')
            elif table=='demand':
                cursor=con.execute('SELECT * FROM src.demand');fields=[d[0] for d in cursor.description]
                for row in cursor.fetchall():
                    demand_history.append(con,dict(zip(fields,row)),'merge-cache')
                update=','.join(f'"{k}"=excluded."{k}"' for k in cols if k not in ('track_id','country'))
                con.execute(f'INSERT INTO main.demand ({names}) SELECT {names} FROM src.demand WHERE 1 ON CONFLICT(track_id,country) DO UPDATE SET {update} WHERE excluded.checked_at>=demand.checked_at')
            else:
                pk='track_id' if table=='apps' else 'app_id'
                updates=[]
                for k in cols:
                    if k==pk:
                        continue
                    if k=='first_seen':
                        updates.append('first_seen=MIN(first_seen,excluded.first_seen)')
                    elif k=='first_country':
                        updates.append('first_country=CASE WHEN excluded.first_seen<first_seen THEN excluded.first_country ELSE first_country END')
                    elif k=='last_seen':
                        updates.append('last_seen=MAX(last_seen,excluded.last_seen)')
                    else:
                        updates.append(f'"{k}"=CASE WHEN excluded.last_seen >= last_seen THEN excluded."{k}" ELSE "{k}" END')
                con.execute(f'''INSERT INTO main.{table} ({names}) SELECT {names} FROM src.{table} WHERE 1
                    ON CONFLICT({pk}) DO UPDATE SET {','.join(updates)}''')
        con.commit()
        con.execute('DETACH DATABASE src')
    print('merge OK:',counts)


if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--main',default='niches.db')
    ap.add_argument('--other',required=True)
    args=ap.parse_args()
    db_store.ensure(args.other)
    if not os.path.exists(args.other):
        ap.error('нет файла '+args.other)
    merge(args.main,args.other)
