#!/usr/bin/env python3
"""Fetch recent activity evidence. ALIVE does not establish paid demand.

Use --cached-only to reclassify legacy results without network calls or DB writes.
RSS is a capped sample; empty responses and failures remain UNKNOWN.
"""

import argparse
import db_store
import os
import sqlite3
import sys
import time
import json
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner as sc
import evidence
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))

DEMAND_SCHEMA = """
CREATE TABLE IF NOT EXISTS demand (
    track_id    INTEGER NOT NULL,
    country     TEXT NOT NULL,
    checked_at  TEXT NOT NULL,
    last_review TEXT,
    r60         INTEGER,
    r90         INTEGER,
    bad_share   INTEGER,
    vel_30d     REAL,
    verdict     TEXT,
    PRIMARY KEY (track_id, country)
);
"""


class DemandFetch(tuple):
    def __new__(cls, values, dates=None, scores=None, error=None, ids=None, versions=None, raw_ref=None):
        result=super().__new__(cls,values)
        result.dates=dates or []
        result.scores=scores or []
        result.error=error
        result.ids=ids or []
        result.versions=versions or []
        result.raw_ref=raw_ref
        return result


def fetch_demand(track_id, country):
    """One dated RSS sample; failures and malformed feeds remain unknown."""
    try:
        data=sc.fetch_json(f"https://itunes.apple.com/{country}/rss/customerreviews/id={track_id}/sortby=mostrecent/page=1/json")
        raw_ref=getattr(data,'raw_ref',None)
        if not isinstance(data,dict) or not isinstance(data.get('feed'),dict):
            raise ValueError('Malformed RSS feed')
        entries=data['feed'].get('entry') or []
        if isinstance(entries,dict):
            entries=[entries]
        if not isinstance(entries,list):
            raise ValueError('Malformed RSS entries')
        dates,scores,ids,versions,seen=[],[],[],[],set()
        now=sc.now_utc()
        for e in entries:
            if not isinstance(e,dict) or 'im:rating' not in e:
                continue
            score=int(e['im:rating']['label'])
            stamp=e.get('updated',{}).get('label','')
            date=evidence.parse(stamp)
            if not 1 <= score <= 5 or date > now:
                raise ValueError('Invalid review date or rating')
            identity=e.get('id',{}).get('label') or json.dumps(e,sort_keys=True)
            if identity in seen:
                continue
            seen.add(identity);dates.append(date.isoformat());scores.append(score)
            ids.append(e.get('id',{}).get('label'))
            versions.append(e.get('im:version',{}).get('label'))
        if not dates:
            return DemandFetch((None,None,None,None,'EMPTY'),raw_ref=raw_ref)
        r60=sum((now-evidence.parse(d)).total_seconds() <= 60*86400 for d in dates)
        r90=sum((now-evidence.parse(d)).total_seconds() <= 90*86400 for d in dates)
        return DemandFetch((max(dates,key=evidence.parse),r60,r90,round(100*sum(v<=2 for v in scores)/len(scores)),'OK'),dates,scores,ids=ids,versions=versions,raw_ref=raw_ref)
    except Exception as exc:
        import raw_archive
        if isinstance(exc,raw_archive.ArchiveError):raise
        return DemandFetch((None,None,None,None,'ERROR'),error=f'{type(exc).__name__}: {exc}',raw_ref=locals().get('raw_ref'))


def prepare(con):
    con.executescript(DEMAND_SCHEMA)
    fields={'fetch_status':'TEXT','method_version':'TEXT','review_dates_json':'TEXT',
            'review_scores_json':'TEXT','sample_size':'INTEGER','sample_capped':'INTEGER','fetch_error':'TEXT',
            'review_ids_json':'TEXT','review_versions_json':'TEXT','collection_policy':'TEXT'}
    existing={r[1] for r in con.execute('PRAGMA table_info(demand)')}
    for name,kind in fields.items():
        if name not in existing:
            con.execute(f'ALTER TABLE demand ADD COLUMN {name} {kind}')
    con.commit()


def store_result(con,tid,country,result,policy=None):
    import demand_history
    con.execute('SAVEPOINT review_measurement')
    try:
        demand_history.preserve_current(con,tid,country)
        _store_result(con,tid,country,result,policy)
        payload=demand_history.as_dict(con.execute('SELECT * FROM demand WHERE track_id=? AND country=?',(tid,country)))
        demand_history.append(con,payload,'live-fetch',getattr(result,'raw_ref',None))
        con.execute('RELEASE review_measurement')
    except Exception:
        con.execute('ROLLBACK TO review_measurement');con.execute('RELEASE review_measurement')
        raise
    con.commit()


def _store_result(con,tid,country,result,policy):
    lr,r60,r90,bad,status=result
    vel,days=velocity(con,tid,country)
    state=evidence.classify(vel,lr,r90,status)
    dates=getattr(result,'dates',[]);scores=getattr(result,'scores',[])
    con.execute('''INSERT INTO demand
        (track_id,country,checked_at,last_review,r60,r90,bad_share,vel_30d,verdict,
         fetch_status,method_version,review_dates_json,review_scores_json,sample_size,sample_capped,fetch_error,
         review_ids_json,review_versions_json,collection_policy)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(track_id,country) DO UPDATE SET
        checked_at=excluded.checked_at,last_review=excluded.last_review,r60=excluded.r60,r90=excluded.r90,
        bad_share=excluded.bad_share,vel_30d=excluded.vel_30d,verdict=excluded.verdict,
        fetch_status=excluded.fetch_status,method_version=excluded.method_version,
        review_dates_json=excluded.review_dates_json,review_scores_json=excluded.review_scores_json,
        sample_size=excluded.sample_size,sample_capped=excluded.sample_capped,fetch_error=excluded.fetch_error,
        review_ids_json=excluded.review_ids_json,review_versions_json=excluded.review_versions_json,
        collection_policy=excluded.collection_policy''',
        (tid,country,sc.iso(sc.now_utc()),lr,r60,r90,bad,vel,state,status,evidence.METHOD,
         json.dumps(dates) if dates else None,json.dumps(scores) if scores else None,
         len(dates) if dates else None,int(len(dates)>=50),getattr(result,'error',None),
         json.dumps(getattr(result,'ids',[])) or None,json.dumps(getattr(result,'versions',[])) or None,policy))


def follow_run(con,run_id,delay,workers=2,controls=20):
    """Consume new candidates while the keyword scan runs; commits are short."""
    started=con.execute('SELECT started_at FROM scan_runs WHERE run_id=?',(run_id,)).fetchone()
    if not started:
        raise ValueError('Unknown scan run')
    started=started[0]
    processed=set();pending={};last_discovery=0
    gate=threading.Lock();next_request=[0.0]
    def fetch(key):
        with gate:
            wait_for=max(0,next_request[0]-time.monotonic())
            if wait_for:
                time.sleep(wait_for)
            next_request[0]=time.monotonic()+delay
        return fetch_demand(*key)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            if time.monotonic()-last_discovery >= 15 or not pending:
                rows=evidence.latest(con)
                candidates={(r['track_id'],r['country']) for r in rows if evidence.is_candidate(r)}
                by_geo={}
                for r in rows:
                    if (r['rating_count'] or 0)>=500 and not evidence.is_candidate(r):
                        by_geo.setdefault(r['country'],[]).append(r)
                controls_set=set()
                for geo,items in by_geo.items():
                    items.sort(key=lambda r:hashlib.sha256(f"{r['track_id']}:{geo}".encode()).hexdigest())
                    controls_set.update((r['track_id'],geo) for r in items[:controls])
                needed=candidates|controls_set
                for r in con.execute('SELECT track_id,country FROM demand WHERE checked_at>=? AND method_version=?',(started,evidence.METHOD)):
                    processed.add((r[0],r[1]))
                queue=sorted(needed-processed-set(pending.values()))
                last_discovery=time.monotonic()
            while queue and len(pending)<workers:
                key=queue.pop(0);pending[pool.submit(fetch,key)]=key
            if pending:
                ready,_=wait(pending,timeout=1,return_when=FIRST_COMPLETED)
                for task in ready:
                    key=pending.pop(task);store_result(con,*key,task.result());processed.add(key)
                    if len(processed)%25==0:
                        print(f'REVIEWS checked {len(processed)} · pending {len(queue)}',flush=True)
            else:
                state=con.execute('SELECT status FROM scan_runs WHERE run_id=?',(run_id,)).fetchone()[0]
                if state in ('COMPLETE','PARTIAL'):
                    print(f'REVIEWS complete: {len(processed)} checked; source scan {state}',flush=True)
                    break
                time.sleep(5)


def zombie_tracks(con, min_stale=18.0, min_ratings=500, limit=None, track=None):
    rows = evidence.latest(con)
    candidates = [(r["track_id"],r["country"],r["rating_count"],r["version_date"],r["name"],r["seller"],r["store_url"])
                  for r in rows if evidence.is_candidate(r) and (not track or r["track_id"] == int(track))]
    return candidates[:limit] if limit else candidates


def velocity(con, track_id, country):
    return evidence.rating_velocity(con, track_id, country)


def exploration_stratum(row,now):
    ratings=row.get('rating_count')
    rating_band='unknown' if ratings is None else '0-49' if ratings<50 else '50-499' if ratings<500 else '500+'
    try:
        age=(now-evidence.parse(row['version_date'])).total_seconds()/86400/30.44
        age_band='unknown' if age<0 else '<12mo' if age<12 else '12-18mo' if age<18 else '18mo+'
    except (ValueError,TypeError,KeyError,AttributeError):age_band='unknown'
    price=row.get('price');price_band='unknown' if price is None else 'paid' if price>0 else 'free'
    return '|'.join((rating_band,age_band,price_band))


def wide_review_sample(rows,cached,per_country,countries=None,now=None,exclude=()):
    """Explore observed search results without selecting on review outcomes.

    Round-robin rating/age/price strata and query families, with daily rotation.
    This deliberately overrepresents small strata; it is not a market census.
    """
    now=now or sc.now_utc();pools={};excluded=set(exclude)
    for row in rows:
        geo=row['country'];key=(row['track_id'],geo)
        if key in excluded or (countries is not None and geo not in countries):continue
        categories=[c for c in row.get('categories',[]) if not c.startswith('chart:') and c!='similar-graph']
        if not categories:continue
        try:
            if not 0<=(now-evidence.parse(row['scanned_at'])).total_seconds()<=14*86400:continue
        except (ValueError,TypeError,KeyError,AttributeError):continue
        previous=cached.get(key,{})
        try:new_version=evidence.parse(previous['checked_at'])<evidence.parse(row['version_date'])
        except (ValueError,TypeError,KeyError,AttributeError):new_version=False
        try:
            if (now-evidence.parse(previous['checked_at'])).total_seconds()<(1 if previous.get('fetch_status')=='ERROR' else 7)*86400 and not new_version:continue
        except (ValueError,TypeError,KeyError,AttributeError):pass
        # Assign multiply discovered apps to one family independently of outcomes.
        category=min(categories,key=lambda c:hashlib.sha256(f'{now:%Y-%m-%d}:{key}:{c}'.encode()).digest())
        group=(bool(previous),exploration_stratum(row,now),category)
        pools.setdefault(geo,{}).setdefault(group,[]).append(key)
    selected=[]
    for geo,groups in sorted(pools.items()):
        rotations={}
        for known in (False,True):
            strata={};served={}
            for (has_cache,stratum,category),keys in groups.items():
                if has_cache!=known:continue
                keys.sort(key=lambda k:hashlib.sha256(f'{now:%Y-%m-%d}:{k}'.encode()).digest())
                strata.setdefault(stratum,{})[category]=keys
            order=sorted(strata,key=lambda s:hashlib.sha256(f'{now:%Y-%m-%d}:{geo}:{s}'.encode()).digest())
            rotation=[]
            while any(any(c.values()) for c in strata.values()):
                for stratum in order:
                    categories=strata[stratum]
                    available=[c for c,keys in categories.items() if keys]
                    if not available:continue
                    # Within each stratum, rotate query families before repeating one.
                    category=min(available,key=lambda c:(served.get((stratum,c),0),
                        hashlib.sha256(f'{now:%Y-%m-%d}:{geo}:{c}'.encode()).digest()))
                    rotation.append(categories[category].pop())
                    served[(stratum,category)]=served.get((stratum,category),0)+1
            rotations[known]=rotation
        for i in range(per_country):
            pool=rotations.get(bool(i%2)) or rotations.get(not bool(i%2))
            if not pool:break
            selected.append(pool.pop(0))
    return selected


def updated_review_sample(rows,cached,per_country,countries=None,now=None):
    """Budgeted discovery sample, separate from the frozen scientific control.

    Alternate overdue refreshes and unseen records, round-robin by query family.
    Daily hashing rotates choices within a family. This is not a market sample.
    """
    now=now or sc.now_utc();pools={};title_hints={};version_refresh={}
    from relevance import relevant
    for row in rows:
        geo=row['country'];key=(row['track_id'],geo)
        if countries is not None and geo not in countries:
            continue
        if not evidence.is_candidate(row,now,min_stale=0) or evidence.is_candidate(row,now):
            continue
        try:
            if not 0<=(now-evidence.parse(row['scanned_at'])).total_seconds()<=14*86400:
                continue
        except (ValueError,TypeError,KeyError):
            continue
        previous=cached.get(key,{})
        try:version_refresh[key]=evidence.parse(previous['checked_at'])<evidence.parse(row['version_date'])
        except (ValueError,TypeError,KeyError,AttributeError):version_refresh[key]=False
        try:
            age=(now-evidence.parse(previous['checked_at'])).total_seconds()/86400
            if age<(1 if previous.get('fetch_status')=='ERROR' else 7) and not version_refresh[key]:
                continue
        except (ValueError,TypeError,KeyError):
            pass
        category=next((c for c in row['categories'] if not c.startswith('chart:') and c!='similar-graph'),row['categories'][0])
        title_hints[key]=any(relevant(row['name'],q) for c,queries in row.get('source_queries',{}).items()
            if not c.startswith('chart:') and c!='similar-graph' for q in queries)
        bucket=pools.setdefault(geo,{}).setdefault(bool(previous),{})
        bucket.setdefault(category,[]).append(key)
    selected=[]
    for geo,kinds in sorted(pools.items()):
        rotations={}
        for known,categories in kinds.items():
            order=sorted(categories,key=lambda category:hashlib.sha256(f'{now:%Y-%m-%d}:{geo}:{category}'.encode()).hexdigest())
            for category,keys in categories.items():
                keys.sort(key=lambda key:(int(title_hints[key]),int(version_refresh[key]),hashlib.sha256(f'{now:%Y-%m-%d}:{key}'.encode()).hexdigest()))
            rotation=[]
            while any(categories.values()):
                for category in order:
                    if categories[category]:rotation.append(categories[category].pop())
            rotations[known]=rotation
        for i in range(per_country):
            preferred=bool(i%2==0)
            pool=rotations.get(preferred) or rotations.get(not preferred)
            if not pool:break
            selected.append(pool.pop(0))
    return selected


def verdict_for(rc, vel, last_review, r90, fetch_status="OK"):
    return evidence.classify(vel, last_review, r90, fetch_status)


def write_cached_report(con, path):
    con.row_factory = sqlite3.Row
    cached = evidence.demand_map(con)
    rows = []
    for r in evidence.latest(con):
        if evidence.is_candidate(r):
            r.update(evidence.assess(con, r, cached.get((r["track_id"], r["country"]))))
            rows.append(r)
    counts = Counter(r["verdict"] for r in rows)
    lines = ["# Demand Report — свежая активность, не доказательство платежей\n",
             f"Метод {evidence.METHOD}. Пересчёт {sc.iso(sc.now_utc())}. Кандидатов: {len(rows)}.\n",
             " · ".join(f"{k}: {counts[k]}" for k in evidence.LABELS) + "\n",
             "ALIVE: ≥2 свежих письменных отзыва за 90 дней либо надёжный прирост оценок "
             "за ≥7 дней в одной стране. Это эвристика для исследования. NO_RECENT_EVIDENCE "
             "означает отсутствие свежего сигнала у приложения, а не смерть ниши. Ошибки и пустой RSS = UNKNOWN.\n",
             "RSS ограничен последними ~50 отзывами: R90 — нижняя граница; ни отзывы, ни "
             "возраст версии не подтверждают выручку, поломку продукта или возможность перехватить пользователей.\n"]
    for state, label in evidence.LABELS.items():
        lines += [f"\n## {label} ({counts[state]})\n",
                  "| Гео | Приложение | Оценок | Vel/30д | Посл. отзыв | R90 ≥ |",
                  "|---|---|---|---|---|---|"]
        selected = sorted((r for r in rows if r["verdict"] == state), key=lambda r: -(r["r90"] or 0))
        for r in selected:
            vel = f"{r['velocity']:+.1f}" if r['velocity'] is not None else "—"
            name = r['name'].replace('|', '/')
            url = f"https://apps.apple.com/{r['country']}/app/id{r['track_id']}"
            lines.append(f"| {r['country']} | [{name}]({url}) | {r['rating_count']:,} | {vel} | {r['last_review'] or '—'} | {r['r90'] if r['r90'] is not None else '—'} |")
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print(dict(counts), '→', path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(HERE, "niches.db"))
    ap.add_argument("--cached-only", action="store_true", help="пересчитать отчёт без сети и записи в БД")
    ap.add_argument("--follow-run")
    ap.add_argument("--workers",type=int,default=2)
    ap.add_argument("--controls",type=int,default=20)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--track")
    ap.add_argument("--delay", type=float, default=1.2)
    ap.add_argument('--explore-updated',type=int,default=0,metavar='N',help='до N версий <18 мес на страну; отдельная ротационная выборка, не контроль исследования')
    ap.add_argument('--explore-wide',type=int,default=0,metavar='N',help='до N поисковых app×country на страну: малые/крупные, старые/свежие, free/paid; не контроль исследования')
    ap.add_argument('--countries',help='страны для --explore-updated; по умолчанию все')
    ap.add_argument('--tier',type=int,choices=[1,2],help='tier из markets.json для --explore-updated')
    ap.add_argument('--slot',type=int,choices=[1,2,3,4])
    ap.add_argument("--out", default=os.path.join(HERE, "demand_report.md"))
    args = ap.parse_args()
    if not 0<=args.explore_updated<=200 or not 0<=args.explore_wide<=200 or args.explore_updated+args.explore_wide>200:
        ap.error('общий бюджет explore должен быть 0..200 на страну')
    if (args.explore_updated or args.explore_wide) and (args.follow_run or args.cached_only or args.track or args.limit):
        ap.error('explore запускается отдельно от follow/cached/track/limit')
    db_store.ensure(args.db)

    con = sqlite3.connect(__import__("pathlib").Path(args.db).resolve().as_uri()+"?mode=ro",uri=True,timeout=30) if args.cached_only else sqlite3.connect(args.db,timeout=30)
    if args.cached_only:
        write_cached_report(con, args.out)
        con.close()
        return
    if args.explore_updated or args.explore_wide:
        import scan_state
        markets=json.load(open(os.path.join(HERE,'markets.json')))['markets']
        countries=set(args.countries.split(',')) if args.countries else {geo for geo,m in markets.items()
            if (args.tier is None or m.get('tier',2)==args.tier) and (args.slot is None or m.get('week_slot')==args.slot)}
        if not countries or not countries<=set(markets):ap.error('Нужны страны из markets.json')
        with scan_state.writer_lock(args.db):
            prepare(con)
            rows=evidence.latest(con);cached=evidence.demand_map(con);now=sc.now_utc()
            targeted=updated_review_sample(rows,cached,args.explore_updated,countries,now)
            broad=wide_review_sample(rows,cached,args.explore_wide,countries,now,exclude=targeted)
            keys=targeted+broad;targeted=set(targeted);by_key={(r['track_id'],r['country']):r for r in rows}
            print(f'DISCOVERY: {len(keys)} RSS checks, max {args.explore_updated}+{args.explore_wide}/country; not a statistical control',flush=True)
            for i,(tid,geo) in enumerate(keys,1):
                policy='updated-v1' if (tid,geo) in targeted else 'wide-v1:'+exploration_stratum(by_key[(tid,geo)],now)
                store_result(con,tid,geo,fetch_demand(tid,geo),policy=policy)
                if i%25==0:print(f'UPDATED {i}/{len(keys)}',flush=True)
                time.sleep(args.delay)
        con.close()
        return
    prepare(con)
    if args.follow_run:
        follow_run(con,args.follow_run,args.delay,args.workers,args.controls)
        write_cached_report(con,args.out)
        con.close()
        return
    tracks = zombie_tracks(con, limit=args.limit, track=args.track)
    print(f"зомби-треков на проверку: {len(tracks)}")

    now = sc.iso(sc.now_utc())
    results = []
    for i, (tid, country, rc, vd, name, seller, url) in enumerate(tracks, 1):
        vel, days_obs = velocity(con, tid, country)
        result=fetch_demand(tid,country)
        store_result(con,tid,country,result)
        if i % 25 == 0:
            con.commit()
            print(f"  {i}/{len(tracks)}…")
        time.sleep(args.delay)
    con.commit()

    write_cached_report(con, args.out)
    con.close()


if __name__ == "__main__":
    main()
