#!/usr/bin/env python3
"""Clean replay of persisted observations, plus an optional bounded live matrix.

Replay is offline and reconstructs persisted fields, not the original HTTP body.
It never writes the reference DB or merges validation observations into it.
"""
import argparse
from collections import defaultdict
import contextlib
from datetime import datetime,timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
import db_store
import evidence
import hypotheses
import scan_state
import scanner as sc
import validate_data

ROOT=Path(__file__).resolve().parent
FIELDS=('track_id','country','category','query','scanned_at','version','version_date','rating_count','avg_rating','rank','app_name','seller_name','storefront_url','run_id','price_amount','currency','release_date')


def digest(con,run):
    # Sorting by observation key excludes surrogate row IDs from identity.
    rows=con.execute('SELECT '+','.join(FIELDS)+' FROM snapshots WHERE run_id=? ORDER BY track_id,country,category,query',(run,))
    result=hashlib.sha256();count=0
    for row in rows:
        result.update((json.dumps(tuple(row),ensure_ascii=False,separators=(',',':'))+'\n').encode());count+=1
    return count,result.hexdigest()


def replay(reference,folder,run_id=None):
    with sqlite3.connect(reference) as source:
        source.row_factory=sqlite3.Row
        if run_id is None:
            row=source.execute("SELECT run_id FROM scan_runs WHERE source='itunes-search' AND status='COMPLETE' ORDER BY expected_queries DESC,finished_at DESC LIMIT 1").fetchone()
            if not row:raise ValueError('No complete reference search')
            run_id=row[0]
        run=dict(source.execute('SELECT * FROM scan_runs WHERE run_id=?',(run_id,)).fetchone())
        if run['status']!='COMPLETE':raise ValueError('Reference search is partial')
        settings=json.loads(run['settings_json']);jobs=[tuple(job) for job in settings['jobs']]
        queries=[dict(row) for row in source.execute('SELECT * FROM scan_queries WHERE run_id=?',(run_id,))]
        observed=defaultdict(list)
        for record in source.execute('''SELECT s.*,a.bundle_id,a.genre,a.first_country,a.first_seen,a.last_seen
                FROM snapshots s JOIN apps a USING(track_id) WHERE run_id=? ORDER BY s.id''',(run_id,)):
            r=dict(record);observed[(r['country'],r['category'],r['query'])].append(r)
        reference_identity=digest(source,run_id)
        expected_hypotheses=hypotheses.build(source)
        query_by_key={(q['country'],q['category'],q['query']):q for q in queries}
        seed=sc.load_seed(ROOT/'seed_queries.json');markets=json.loads((ROOT/'markets.json').read_text())['markets']
        planned=list(dict.fromkeys((geo,c['id'],q) for c in seed['categories'] if c.get('enabled',True)
            for geo in markets for q in (c['queries'].get(markets[geo]['lang']) or c['queries']['en'])))
        if set(planned)!=set(jobs):raise ValueError('Current seed differs from the reference; a new full scan is needed')
        normalize=sc.normalize
        def normalize_saved(raw,geo,cat,query,rank=None):
            app=normalize(raw,geo,cat,query,rank)
            r=raw['_persisted']
            for key in ('scanned_at','first_seen','last_seen','first_country'):
                app[key]=r[key]
            return app
        network_calls=[0]
        def no_network(*args):
            network_calls[0]+=1
            raise AssertionError('Replay attempted a real network request')
        def results(con,run,pending,search,delay):
            # Preserve source insertion order for equal timestamps.
            ordered=sorted(pending,key=lambda key:observed[key][0]['id'] if observed[key] else -1)
            for key in ordered:
                scan_state.begin_query(con,run,*key)
                original=query_by_key[key]
                hits=[{} for _ in range(original['result_count'])]
                for r in observed[key]:
                    hits[r['rank']-1]=dict(trackId=r['track_id'],trackName=r['app_name'],sellerName=r['seller_name'],
                        bundleId=r['bundle_id'],primaryGenreName=r['genre'],trackViewUrl=r['storefront_url'],
                        price=r['price_amount'],currency=r['currency'],releaseDate=r['release_date'],
                        currentVersionReleaseDate=r['version_date'],version=r['version'],
                        userRatingCount=r['rating_count'],averageUserRating=r['avg_rating'],_persisted=r)
                yield key,hits,None
        db=Path(folder)/'replayed.db'
        args=SimpleNamespace(db=str(db),dry_run=False,limit=settings['limit'],run_id=run_id,
            min_stale_months=18,min_rating_count=500,skip_reviews=True,no_alerts=True,
            print_alert=False,quiet=True,report=str(Path(folder)/'replay.md'))
        with patch.object(scan_state,'query_results',results),patch.object(sc,'normalize',normalize_saved),patch.object(sc,'fetch_json',no_network),contextlib.redirect_stdout(io.StringIO()):
            sc.run_scan(args,seed,markets,list(markets),None)
            # A complete resume must save no additional observations.
            sc.run_scan(args,seed,markets,list(markets),None)
        with sqlite3.connect(db) as target:
            target.execute('UPDATE scan_runs SET started_at=?,finished_at=? WHERE run_id=?',(run['started_at'],run['finished_at'],run_id))
            for table in ('study_members','study_reviews'):
                schema=source.execute('SELECT sql FROM sqlite_master WHERE type=\'table\' AND name=?',(table,)).fetchone()
                if schema:
                    target.execute(schema[0])
                    rows=source.execute(f'SELECT * FROM {table} WHERE study_id=?',(run_id,)).fetchall()
                    if rows:target.executemany(f'INSERT INTO {table} VALUES ({",".join("?" for _ in rows[0])})',[tuple(r) for r in rows])
            target.commit()
            _,errors=validate_data.validate(target,True,run_id)
            if errors:raise ValueError(errors)
            identity=digest(target,run_id)
            if identity!=reference_identity:raise ValueError('Replayed observation payloads differ')
            actual=hypotheses.build(target)
            fields=('activity_comparison','old_minus_fresh_pp','matched_old_minus_fresh_pp','age_share_correlation',
                'lifetime_ratings_vs_recent_reviews_spearman','sensitivity','discovery_vs_launch')
            for field in fields:
                if actual[field]!=expected_hypotheses[field]:raise ValueError('Clean replay changed hypothesis: '+field)
        return dict(reference_run=run_id,queries=len(jobs),observations=identity[0],sha256=identity[1],
            empty_database=True,resume_without_duplicates=True,network_requests=network_calls[0],
            hypothesis_fields_equal=list(fields))


def live_matrix(folder):
    seed=sc.load_seed(ROOT/'seed_queries.json')
    for category in seed['categories']:
        category['queries']={lang:phrases[:1] for lang,phrases in category['queries'].items()}
    active=[c['id'] for c in seed['categories'] if c.get('enabled',True)]
    seed_path=Path(folder)/'matrix.json';seed_path.write_text(json.dumps(seed,ensure_ascii=False))
    db=Path(folder)/'live.db';stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
    common=[sys.executable,str(ROOT/'scanner.py'),'--seed',str(seed_path),'--db',str(db),
        '--limit','10','--no-alerts','--skip-reviews','--quiet','--report',str(Path(folder)/'live.md')]
    subprocess.run([*common,'--all-markets','--categories',active[0],'--run-id','matrix-countries-'+stamp],check=True)
    time.sleep(sc.REQUEST_DELAY)
    subprocess.run([*common,'--countries','us','--categories',','.join(active[1:]),'--run-id','matrix-categories-'+stamp],check=True)
    with sqlite3.connect(db) as con:
        _,errors=validate_data.validate(con,True)
        if errors:raise ValueError(errors)
        row=con.execute('SELECT COUNT(*),COUNT(DISTINCT country),COUNT(DISTINCT category),SUM(result_count),SUM(stored_count) FROM scan_queries').fetchone()
        market_count=len(json.loads((ROOT/'markets.json').read_text())['markets'])
        if row[1]!=market_count or row[2]!=len(active):raise ValueError('Live matrix coverage is incomplete')
        import demand_check
        sample=demand_check.fetch_demand(286799607,'us')
        if sample[-1]=='ERROR':raise ValueError('RSS live check failed')
        return dict(queries=row[0],countries=row[1],categories=row[2],results=row[3],stored=row[4],
                    limit_per_query=10,rss_status=sample[-1],scope='One query per country and per active category; not a new full market survey')


def telegram_readonly():
    import tg_setup
    token=os.environ.get('TG_BOT_TOKEN');chat=os.environ.get('TG_CHAT_ID')
    if not token or not chat:raise ValueError('Telegram credentials missing')
    try:
        me=tg_setup.call(token,'getMe')['result']
        tg_setup.call(token,'getChat',{'chat_id':chat})
        if not me.get('is_bot'):raise ValueError('Unexpected bot identity')
    except Exception as error:
        raise ValueError('Telegram read-only check failed: '+type(error).__name__) from None
    return dict(bot_verified=True,chat_verified=True,messages_sent=0)


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--db',default=str(ROOT/'niches.db'));ap.add_argument('--run-id');ap.add_argument('--live',action='store_true');ap.add_argument('--telegram-readonly',action='store_true');ap.add_argument('--out',default='docs/pipeline_verification.json');args=ap.parse_args()
    db_store.ensure(args.db)
    report=dict(checked_at=datetime.now(timezone.utc).isoformat(),status='RUNNING',
                code_sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip())
    output=Path(args.out);output.parent.mkdir(parents=True,exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='niche-verify-') as folder:
            reference=db_store.backup(args.db,Path(folder)/'reference.db')
            report['replay']=replay(reference,folder,args.run_id)
            if args.live:report['live']=live_matrix(folder)
            if args.telegram_readonly:report['telegram']=telegram_readonly()
        report['status']='PASS'
    except Exception as error:
        report['status']='FAILED';report['error_type']=type(error).__name__
        raise
    finally:
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))



if __name__=='__main__':main()
