#!/usr/bin/env python3
"""
Play Scan — зомби-скан Google Play (второй по важности источник после App Store).

Нужен пакет:  pip3 install --user google-play-scraper
(бесплатный скрапер без ключей; отдаёт дату обновления, установки, рейтинги, отзывы)

Использование:
    python3 play_scan.py                                  # все категории из seed
    python3 play_scan.py --categories construction --countries us,de
    python3 play_scan.py --limit 15 --min-rating-count 300
"""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner as sc

def search(*args,**kwargs):
    from google_play_scraper import search as fetch
    return fetch(*args,**kwargs)


def gp_app(*args,**kwargs):
    from google_play_scraper import app as fetch
    return fetch(*args,**kwargs)


PLAY_SCHEMA = """
CREATE TABLE IF NOT EXISTS play_apps (
    app_id     TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    developer  TEXT,
    installs   TEXT,
    price      TEXT,
    genre      TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS play_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id     TEXT NOT NULL REFERENCES play_apps(app_id),
    country    TEXT NOT NULL,
    category   TEXT NOT NULL,
    query      TEXT NOT NULL,
    scanned_at TEXT NOT NULL,
    updated_at TEXT,
    stale_days INTEGER,
    rating     REAL,
    ratings_count INTEGER,
    installs   TEXT
);
CREATE INDEX IF NOT EXISTS idx_psnap ON play_snapshots(app_id, scanned_at);
"""


RETRY_PAUSE=60


def months_since(ts):
    if not ts:
        return None
    return (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromtimestamp(ts, dt.timezone.utc)).days / 30.44


def main():
    import scan_state
    here=Path(__file__).resolve().parent
    ap=argparse.ArgumentParser()
    ap.add_argument('--seed',default=str(here/'seed_queries.json'))
    ap.add_argument('--markets',default=str(here/'markets.json'))
    ap.add_argument('--db',default=str(here/'niches.db'))
    ap.add_argument('--countries',default='us,de')
    ap.add_argument('--categories')
    ap.add_argument('--limit',type=int,default=20)
    ap.add_argument('--max-queries',type=int)
    ap.add_argument('--min-rating-count',type=int,default=500)
    ap.add_argument('--min-stale-months',type=int,default=18)
    ap.add_argument('--run-id')
    args=ap.parse_args()
    if not 1<=args.limit<=30:
        ap.error('limit must be 1..30')
    seed=json.loads(Path(args.seed).read_text(encoding='utf-8'))
    markets=json.loads(Path(args.markets).read_text(encoding='utf-8'))['markets']
    countries=args.countries.split(',');wanted=set(args.categories.split(',')) if args.categories else None
    if wanted and not wanted<={category['id'] for category in seed['categories']}:
        ap.error('Unknown category')
    if args.max_queries is not None and args.max_queries<1:
        ap.error('max-queries must be positive')
    if any(g not in markets for g in countries):
        ap.error('Unknown country')
    jobs=[]
    for category in seed['categories']:
        if not category.get('enabled',True) or (wanted and category['id'] not in wanted):
            continue
        for geo in countries:
            queries=category['queries'];lang=markets[geo].get('lang','en')
            if isinstance(queries,dict):
                queries=queries.get(lang) or queries.get('en',[])
            jobs.extend((geo,category['id'],query) for query in queries)
    jobs=list(dict.fromkeys(jobs))
    if args.max_queries is not None:
        jobs=jobs[:args.max_queries]
    if not jobs:
        ap.error('No enabled query jobs')
    cache={};candidates={};analyzed=0
    with scan_state.writer_lock(args.db):
        con=sc.open_db(args.db);con.executescript(PLAY_SCHEMA)
        existing={r[1] for r in con.execute('PRAGMA table_info(play_snapshots)')}
        for name,kind in {'run_id':'TEXT','currency':'TEXT','price_amount':'REAL'}.items():
            if name not in existing:
                con.execute(f'ALTER TABLE play_snapshots ADD COLUMN {name} {kind}')
        import importlib.metadata
        try:
            library_version=importlib.metadata.version('google-play-scraper')
        except importlib.metadata.PackageNotFoundError:
            library_version='not-installed'
        run_id=scan_state.start_run(con,jobs,{'source':'google-play','limit':args.limit,'library_version':library_version},args.run_id)
        # Play can fail one query and answer it normally minutes later (KeyError 'ds:4'
        # on 1 of 537 queries, Nov 12 2026); one more pass keeps that from failing the week.
        for attempt in (1,2):
            done={(r[0],r[1],r[2]) for r in con.execute("SELECT country,category,query FROM scan_queries WHERE run_id=? AND status='OK'",(run_id,))}
            pending=[job for job in jobs if job not in done]
            if attempt==2 and pending:
                errors=con.execute("SELECT country,query,error FROM scan_queries WHERE run_id=? AND status!='OK'",(run_id,)).fetchall()
                print(f'Retrying {len(pending)} failed Play queries: '+'; '.join(f'{c} {q}: {e}' for c,q,e in errors[:3]))
                time.sleep(RETRY_PAUSE)
            for country,category,query in pending:
                scan_state.begin_query(con,run_id,country,category,query)
                lang=markets[country].get('lang','en');lang={'nb':'no'}.get(lang,lang)
                try:
                    hits=search(query,lang=lang,country=country,n_hits=args.limit)
                    if not isinstance(hits,list) or any(not isinstance(h,dict) or not isinstance(h.get('appId'),str) or not h['appId'] for h in hits):
                        raise RuntimeError('Malformed Play search entries; not a successful empty result')
                    resolved=[];failed=[];seen=set()
                    for hit in hits:
                        app_id=hit['appId']
                        if app_id in seen:
                            continue
                        seen.add(app_id);key=(app_id,country,lang)
                        try:
                            if key not in cache:
                                cache[key]=gp_app(app_id,lang=lang,country=country);time.sleep(.7)
                            resolved.append((app_id,cache[key]))
                        except Exception as error:
                            failed.append(f'{app_id}: {type(error).__name__}: {error}'[:220])
                    stored=0;new_candidates={}
                    con.execute('BEGIN')
                    for app_id,app in ([] if failed else resolved):
                        now=sc.iso(sc.now_utc());age=months_since(app.get('updated'))
                        updated=dt.datetime.fromtimestamp(app['updated'],dt.timezone.utc).strftime('%Y-%m-%d') if app.get('updated') else None
                        con.execute('''INSERT INTO play_apps (app_id,title,developer,installs,price,genre,first_seen,last_seen)
                            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(app_id) DO UPDATE SET title=excluded.title,
                            developer=excluded.developer,installs=excluded.installs,price=excluded.price,
                            genre=excluded.genre,last_seen=excluded.last_seen''',
                            (app_id,app.get('title') or app_id,app.get('developer'),app.get('installs'),app.get('price'),app.get('genre'),now,now))
                        con.execute('''INSERT INTO play_snapshots (app_id,country,category,query,scanned_at,updated_at,
                            stale_days,rating,ratings_count,installs,run_id,currency,price_amount) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                            (app_id,country,category,query,now,updated,round(age*30.44) if age is not None else None,
                             app.get('score'),app.get('ratings'),app.get('installs'),run_id,app.get('currency'),app.get('price')))
                        stored+=1
                        if age is not None and age>=args.min_stale_months and (app.get('ratings') or 0)>=args.min_rating_count:
                            new_candidates[(app_id,country)]=app.get('title')
                    scan_state.end_query(con,run_id,country,category,query,'ERROR' if failed else 'OK',len(hits),stored,
                                         f'{len(failed)} metadata failures: '+ '; '.join(failed[:2]) if failed else None)
                    analyzed+=stored;candidates.update(new_candidates)
                except Exception as error:
                    con.rollback()
                    scan_state.end_query(con,run_id,country,category,query,'ERROR',error=f'{type(error).__name__}: {error}')
                time.sleep(1.2)
        status,ok,total=scan_state.finish_run(con,run_id)
        analyzed=con.execute('SELECT COUNT(*) FROM play_snapshots WHERE run_id=?',(run_id,)).fetchone()[0]
        candidate_count=con.execute('''WITH ranked AS (SELECT *,ROW_NUMBER() OVER
            (PARTITION BY app_id,country ORDER BY scanned_at DESC,id DESC) rn
            FROM play_snapshots WHERE run_id=?) SELECT COUNT(*) FROM ranked
            WHERE rn=1 AND stale_days>=? AND ratings_count>=?''',
            (run_id,args.min_stale_months*30.44,args.min_rating_count)).fetchone()[0]
        con.close()
    print(f'Play: {status}, queries {ok}/{total}, observations {analyzed}, candidates {candidate_count}')
    print('Installs/ratings are store listing counters, not verified local demand or revenue.')
    if status!='COMPLETE':
        raise SystemExit('Play scan partial; results and errors are saved')


if __name__=='__main__':
    main()
