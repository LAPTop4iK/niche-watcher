#!/usr/bin/env python3
"""Collect App Store page links as discovery leads, never a verified similarity graph.

Public pages can mix recommendations, a seller's portfolio and other links.
Version age among these links does not measure niche weakness or buyer demand.
"""
import argparse
from pathlib import Path
import re
import statistics
import sys
import time
import urllib.request
import scanner as sc
import scan_state

PAGE_RE=re.compile(r'/app/(?:[^\s"<>/?#]+/)?id(\d+)',re.I)


def extract_ids(html,exclude=None):
    return list(dict.fromkeys(tid for tid in PAGE_RE.findall(html) if tid!=str(exclude)))


def page_ids(track_id,country='us'):
    req=urllib.request.Request(f'https://apps.apple.com/{country}/app/id{track_id}',headers={'User-Agent':'Mozilla/5.0'})
    with urllib.request.urlopen(req,timeout=25) as response:
        return extract_ids(response.read().decode('utf-8','replace'),track_id)


def report_seeds(text,country,limit):
    # New reports contain /app/id123; older ones may contain /app/title/id123.
    pattern=re.compile(r'https://apps\.apple\.com/'+re.escape(country)+r'/app/(?:[^\s"<>/?#)]+/)?id(\d+)',re.I)
    return list(dict.fromkeys(pattern.findall(text)))[:limit]


def stale_months(value):
    try:
        return (sc.now_utc()-sc.parse_iso(value)).total_seconds()/86400/30.44
    except (ValueError,TypeError,AttributeError):
        return None


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--seeds');ap.add_argument('--from-report');ap.add_argument('--top-seeds',type=int,default=5);ap.add_argument('--depth',type=int,default=1);ap.add_argument('--country',default='us');ap.add_argument('--db',default='niches.db');ap.add_argument('--out',default='clusters_report.md');ap.add_argument('--min-stale-months',type=float,default=18);args=ap.parse_args()
    if not 1<=args.depth<=2:
        raise SystemExit('depth must be 1 or 2')
    seeds=[s.strip() for s in (args.seeds or '').split(',') if s.strip()]
    if any(not s.isdigit() for s in seeds):
        raise SystemExit('Seeds must be numeric app IDs')
    if args.from_report:
        seeds+=report_seeds(Path(args.from_report).read_text(),args.country,args.top_seeds)
    frontier=list(dict.fromkeys(seeds));visited=set();results=[];coverage=[];failed=False
    if not frontier:
        raise SystemExit('No seed IDs for this storefront')
    with scan_state.writer_lock(args.db):
        con=sc.open_db(args.db)
        for depth in range(args.depth):
            jobs=[(args.country,'similar-graph',f'links-of-{tid}') for tid in frontier]
            if not jobs:
                break
            run=scan_state.start_run(con,jobs,{'source':'apple-page-links','depth':depth+1})
            nxt=[]
            for country,category,query in jobs:
                tid=query.removeprefix('links-of-');visited.add(tid)
                scan_state.begin_query(con,run,country,category,query)
                try:
                    ids=page_ids(tid,country);raw=[];time.sleep(sc.REQUEST_DELAY)
                    for i in range(0,len(ids),100):
                        data=sc.fetch_json('https://itunes.apple.com/lookup?id='+','.join(ids[i:i+100])+f'&country={country}&entity=software')
                        if not isinstance(data.get('results'),list):
                            raise RuntimeError('Malformed lookup')
                        raw.extend(data['results']);time.sleep(sc.REQUEST_DELAY)
                    unique={str(r['trackId']):r for r in raw if r.get('trackId')}
                    missing=set(ids)-set(unique)
                    for r in unique.values():
                        app=sc.normalize(r,country,category,query);app['run_id']=run
                        sc.upsert_app(con,app);sc.insert_snapshot(con,app)
                    ages=[age for r in unique.values() if (age:=stale_months(r.get('currentVersionReleaseDate'))) is not None and age>=0]
                    stale=sum(age>=args.min_stale_months for age in ages)
                    results.append((tid,len(ids),len(unique),len(ages),stale,statistics.median(ages) if ages else None,len(missing)))
                    scan_state.end_query(con,run,country,category,query,'ERROR' if missing else 'OK',len(ids),len(unique),f'{len(missing)} unresolved IDs' if missing else None)
                    nxt+=ids
                except Exception as error:
                    con.rollback();scan_state.end_query(con,run,country,category,query,'ERROR',error=str(error))
                    print(f'Page links {tid}: {error}',file=sys.stderr)
            status,ok,total=scan_state.finish_run(con,run);coverage.append(f'{status} {ok}/{total}');failed|=status!='COMPLETE'
            frontier=[tid for tid in dict.fromkeys(nxt) if tid not in visited][:40]
        con.close()
    lines=['# Ссылки со страниц App Store — идеи для проверки',f'\nГео {args.country}; глубина {args.depth}; покрытие: '+', '.join(coverage)+'.',
        '\nЭто все обнаруженные ссылки страницы, а не проверенный блок похожих приложений. Состав зависит от рекомендаций и портфеля продавца. Доля старых версий не доказывает слабую конкуренцию, спрос или прибыль.',
        '\n| Seed | Ссылок | Разрешены | Датированы | Версия ≥18 мес | Медиана возраста, мес | Не разрешены |','|---|---:|---:|---:|---:|---:|---:|']
    lines[3]=lines[3].replace('≥18',f'≥{args.min_stale_months:g}')
    for tid,n,resolved,dated,stale,median,missing in results:
        lines.append(f'| {tid} | {n} | {resolved} | {dated} | {stale} | {median:.1f} | {missing} |' if median is not None else f'| {tid} | {n} | {resolved} | 0 | 0 | — | {missing} |')
    Path(args.out).write_text('\n'.join(lines)+'\n');print('Page-link observations:',len(results),'→',args.out)
    if failed:
        raise SystemExit('Page-link scan partial; see query ledger')


if __name__=='__main__':main()
