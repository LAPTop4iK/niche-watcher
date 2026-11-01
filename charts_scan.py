#!/usr/bin/env python3
"""Chart observations by feed; age is not proof of neglect or a market opening."""
import argparse
from datetime import datetime,timezone,timedelta
import os
import statistics
import sys
import time
import urllib.parse
import scanner as sc
import scan_state

GENRES={6001:'Weather',6002:'Utilities',6003:'Travel',6004:'Sports',6005:'Social Networking',6006:'Reference',6007:'Productivity',6008:'Photo & Video',6010:'Navigation',6011:'Music',6012:'Lifestyle',6013:'Health & Fitness',6015:'Finance',6017:'Education',6018:'Books',6023:'Food & Drink'}
FEEDS=['topfreeapplications','topgrossingapplications','toppaidapplications']


def fetch_rss_chart(feed,country,genre,limit):
    data=sc.fetch_json(f'https://itunes.apple.com/{country}/rss/{feed}/limit={limit}/genre={genre}/json')
    if not isinstance(data,dict) or not isinstance(data.get('feed'),dict):
        raise RuntimeError('Malformed chart RSS')
    entries=data['feed'].get('entry') or []
    if isinstance(entries,dict):
        entries=[entries]
    ids=[]
    for e in entries:
        tid=(e.get('id',{}).get('attributes') or {}).get('im:id')
        if tid and int(tid) not in ids:
            ids.append(int(tid))
    return ids


def lookup_ids(track_ids,country):
    out=[]
    for i in range(0,len(track_ids),100):
        params=urllib.parse.urlencode({'id':','.join(map(str,track_ids[i:i+100])),'country':country,'entity':'software'})
        data=sc.fetch_json('https://itunes.apple.com/lookup?'+params)
        if not isinstance(data.get('results'),list):
            raise RuntimeError('Malformed chart lookup')
        out.extend(data['results']);time.sleep(sc.REQUEST_DELAY)
    return out


def summarize(raw,min_stale=18,min_ratings=500,now=None):
    now=now or sc.now_utc();ages=[];candidates=0
    unique={r['trackId']:r for r in raw if r.get('trackId')}
    for r in unique.values():
        try:
            age=(now-sc.parse_iso(r['currentVersionReleaseDate'])).total_seconds()/86400/30.44
        except (KeyError,ValueError,TypeError):
            continue
        ages.append(age)
        candidates+=age>=min_stale and (r.get('userRatingCount') or 0)>=min_ratings
    return dict(resolved=len(unique),dated=len(ages),candidates=candidates,
                candidate_pct=100*candidates/len(ages) if ages else None,
                median_age=statistics.median(ages) if ages else None)


def previous_ids(con,country,category,feed,cutoff,top):
    # One completed baseline, never the union of all historic chart occupants.
    rows=con.execute('''SELECT q.run_id,r.settings_json FROM scan_queries q JOIN scan_runs r USING(run_id)
        WHERE q.country=? AND q.category=? AND q.query=? AND q.status='OK'
          AND q.finished_at<=? ORDER BY q.finished_at DESC''',
        (country,category,feed,cutoff)).fetchall()
    import json
    row=next((r for r in rows if json.loads(r[1]).get('top')==top),None)
    if not row:
        return None
    return {r[0] for r in con.execute('SELECT DISTINCT track_id FROM snapshots WHERE run_id=? AND country=? AND category=? AND query=?',(row[0],country,category,feed))}


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--countries',default='us,de');ap.add_argument('--genres');ap.add_argument('--top',type=int,default=200);ap.add_argument('--db',default='niches.db');ap.add_argument('--out',default='charts_report.md');ap.add_argument('--min-stale-months',type=int,default=18);ap.add_argument('--min-rating-count',type=int,default=500);args=ap.parse_args()
    countries=args.countries.split(',');genres={int(g):GENRES[int(g)] for g in args.genres.split(',')} if args.genres else GENRES
    jobs=[(geo,'chart:'+name.lower(),feed) for geo in countries for name in genres.values() for feed in FEEDS]
    rows=[]
    with scan_state.writer_lock(args.db):
        con=sc.open_db(args.db)
        run_id=scan_state.start_run(con,jobs,{'source':'apple-charts','top':args.top})
        con.execute("UPDATE scan_runs SET source='apple-charts' WHERE run_id=?",(run_id,));con.commit()
        for country,category,feed in jobs:
            gid=next(k for k,v in genres.items() if 'chart:'+v.lower()==category)
            scan_state.begin_query(con,run_id,country,category,feed)
            try:
                ids=fetch_rss_chart(feed,country,gid,args.top)
                raw=lookup_ids(ids,country);positions={tid:i+1 for i,tid in enumerate(ids)}
                baseline=previous_ids(con,country,category,feed,sc.iso(sc.now_utc()-timedelta(days=7)),args.top)
                for r in raw:
                    app=sc.normalize(r,country,category,feed,positions.get(r['trackId']));app['run_id']=run_id
                    sc.upsert_app(con,app);sc.insert_snapshot(con,app)
                missing=set(ids)-{r['trackId'] for r in raw}
                stats=summarize(raw,args.min_stale_months,args.min_rating_count)
                stats.update(country=country,category=category,feed=feed,missing=len(missing),
                             entered=len(set(ids)-baseline) if baseline is not None else None,
                             exited=len(baseline-set(ids)) if baseline is not None else None)
                rows.append(stats)
                scan_state.end_query(con,run_id,country,category,feed,'ERROR' if missing else 'OK',len(ids),len(raw),f'{len(missing)} unresolved IDs' if missing else None)
            except RuntimeError as error:
                scan_state.end_query(con,run_id,country,category,feed,'ERROR',error=str(error))
                print(f'Chart error {country}/{category}/{feed}: {error}',file=sys.stderr)
            time.sleep(sc.REQUEST_DELAY)
        status,ok,total=scan_state.finish_run(con,run_id);con.close()
    lines=['# Наблюдения чартов по отдельным фидам',f'\nПокрытие: {status} {ok}/{total}. Старые версии ≥{args.min_stale_months} мес и ≥{args.min_rating_count} оценок. Фиды не смешиваются; оплачиваемая загрузка не измеряет прибыль.',
           '\n| Гео | Категория | Фид | Карточек | Датированы | Кандидаты | Доля | Не разрешены | Вошли | Вышли |','|---|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for r in rows:
        pct=f"{r['candidate_pct']:.1f}%" if r['candidate_pct'] is not None else '—'
        lines.append(f"| {r['country']} | {r['category']} | {r['feed']} | {r['resolved']} | {r['dated']} | {r['candidates']} | {pct} | {r['missing']} | {r['entered'] if r['entered'] is not None else '—'} | {r['exited'] if r['exited'] is not None else '—'} |")
    lines.append('\nРотация сравнивается с одним завершённым наблюдением ≥7 дней назад и не доказывает лёгкость входа. Не разрешённые lookup IDs не считаются мёртвыми приложениями.')
    with open(args.out,'w',encoding='utf-8') as f:
        f.write('\n'.join(lines)+'\n')
    print('Chart scan:',status,ok,total)
    if status!='COMPLETE':
        raise SystemExit('Chart scan partial; see query ledger')


if __name__=='__main__':
    main()
