"""Measurement coverage and censoring, never a niche profitability score."""
import argparse
from collections import Counter,defaultdict
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3
import evidence
import demand_history
import scanner as sc

ROOT=Path(__file__).resolve().parent


def build(con,now=None,rows=None,queue=None):
    now=now or datetime.now(timezone.utc);con.row_factory=sqlite3.Row
    rows=evidence.latest(con) if rows is None else rows
    queue=evidence.research_queue(con,now)[0] if queue is None else queue
    base=[r for r in rows if any(not c.startswith('chart:') and c!='similar-graph' for c in r['categories'])]
    eligible=[r for r in base if evidence.is_candidate(r,now,min_stale=0)]
    cache=evidence.demand_map(con);assessed={(r['track_id'],r['country']):r for r in queue}
    counts=Counter();id_geos=defaultdict(set);app_geos=defaultdict(set)
    for r in eligible:
        key=(r['track_id'],r['country']);d=cache.get(key)
        try:meta_age=(now-evidence.parse(r['scanned_at'])).total_seconds()/86400
        except (ValueError,TypeError,AttributeError):meta_age=None
        counts['metadata_fresh']+=int(meta_age is not None and 0<=meta_age<=evidence.MAX_AGE_DAYS)
        if not d:
            counts['rss_missing']+=1;continue
        a=assessed.get(key) or evidence.assess(con,r,d,now)
        counts['rss_usable' if a['fetch_status']=='OK' else 'rss_'+a['fetch_status'].lower()]+=1
        if a['fetch_status']!='OK':continue
        counts['rss_recent_capped']+=int(bool(a.get('recent_sample_capped')))
        counts['rss_with_review_versions']+=int(bool(a.get('review_version_known_count')))
        counts['rss_with_review_ids']+=int(bool(a.get('review_id_known_count')))
        try:ids=json.loads(d.get('review_ids_json') or '[]')
        except (ValueError,TypeError):ids=[]
        if isinstance(ids,list) and len(ids)==(d.get('sample_size') or 0):
            for identity in ids:
                if identity:
                    app_geos[r['track_id']].add(r['country'])
                    id_geos[(r['track_id'],str(identity))].add(r['country'])
    counts['eligible']=len(eligible);counts['metadata_not_fresh']=len(eligible)-counts['metadata_fresh']
    counts['rss_not_usable']=len(eligible)-counts['rss_usable']
    broad_counts=Counter();strata=Counter()
    # Full observed search frame, deliberately separate from the >=500 UI queue.
    from demand_check import exploration_stratum
    for r in base:
        strata[exploration_stratum(r,now)]+=1
        d=cache.get((r['track_id'],r['country']))
        broad_counts['checked_at_least_once']+=int(bool(d))
        if d:
            a=evidence.assess(con,r,d,now)
            broad_counts['rss_usable_now']+=int(a['fetch_status']=='OK')
    for name in ('rss_usable','rss_missing','rss_error','rss_empty','rss_stale','rss_unknown','rss_recent_capped','rss_with_review_versions','rss_with_review_ids'):
        counts.setdefault(name,0)
    pain=[r for r in queue if r['lane']=='recent_pain' and 'search' in r['sources']]
    pain_timing=dict(total=len(pain),after_release=sum((r.get('bad_after_version_count') or 0)>0 for r in pain),
        only_before_release=sum(r.get('bad_after_version_count')==0 for r in pain),
        timing_unknown=sum(r.get('bad_after_version_count') is None for r in pain),
        rss_before_release=sum(r.get('rss_collected_after_version') is False for r in pain),
        same_version_positive=sum((r.get('bad_current_version_count') or 0)>0 for r in pain),
        version_unknown=sum(r.get('bad_current_version_count') is None for r in pain))
    seed=sc.load_seed(ROOT/'seed_queries.json');markets=json.loads((ROOT/'markets.json').read_text())['markets']
    planned=set(sc.planned_jobs(seed,markets,list(markets)));latest={};limits={};query_counts=Counter()
    if {'scan_runs','scan_queries'}<=evidence.tables(con):
        for row in con.execute("SELECT run_id,settings_json FROM scan_runs WHERE source='itunes-search'"):
            limits[row[0]]=json.loads(row[1]).get('limit')
        for row in con.execute("""SELECT q.* FROM scan_queries q JOIN scan_runs r USING(run_id)
                WHERE r.source='itunes-search' ORDER BY COALESCE(q.finished_at,q.started_at),r.started_at"""):
            key=(row['country'],row['category'],row['query'])
            if key in planned:latest[key]=dict(row)
    for row in latest.values():
        query_counts[row['status'].lower()]+=1
        if row['status']!='OK':continue
        age=(now-evidence.parse(row['finished_at'])).total_seconds()/86400
        if not 0<=age<=evidence.MAX_AGE_DAYS:continue
        query_counts['fresh_ok']+=1;limit=limits.get(row['run_id'])
        if isinstance(limit,(int,float)) and limit>0:
            query_counts['limit_known']+=1
            query_counts['at_limit']+=int((row['result_count'] or 0)>=limit)
    query_counts['expected']=len(planned);query_counts['missing']=len(planned)-len(latest)
    for name in ('ok','error','running','fresh_ok','limit_known','at_limit'):query_counts.setdefault(name,0)
    multi_geo=sum(len(geos)>=2 for geos in app_geos.values())
    return dict(computed_at=now.isoformat(),scope='Current keyword-search memberships; counts before the optional title filter',
        unit='app×storefront, not independent users',search_records=len(base),search_unique_apps=len({r['track_id'] for r in base}),
        queries=dict(query_counts),freshness_days=evidence.MAX_AGE_DAYS,review_coverage=dict(counts),
        collection=dict(search_records=len(base),below_500_ratings=sum((r.get('rating_count') or 0)<500 for r in base),
            strata=dict(strata),**dict(broad_counts),history=demand_history.stats(con),
            raw_capture='Opt-in local archive only; complete HTTP bodies are not persisted by CI',
            note='Exploration strata are not weighted estimates of the App Store population'),
        last_search_query_at=max((r['finished_at'] for r in latest.values() if r['status']=='OK'),default=None),
        recent_pain_timing=pain_timing,
        cross_geo_review_ids=dict(apps_comparable=multi_geo,shared_ids=sum(len(geos)>=2 for geos in id_geos.values()) if multi_geo else None,
            note='Only collected review IDs; repeated countries do not establish independent people or demand'),
        competitor_revenue_observed=False,
        limitations=['Reaching the result limit signals truncation; successful queries are not full market coverage.',
            'Reviews after a release date may still refer to an older installed version; matching version strings is a separate count.',
            'Absence of complaints after a release does not prove a fix. RSS dates can be review updates.',
            'Legacy RSS lacks review IDs/versions. Those fields are unknown until fetched; frozen studies are not backfilled.',
            'Counts with all 50 reviews inside 90 days are censored lower bounds; negative shares are not population rates.'])


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--db',default='niches.db');ap.add_argument('--out',default='docs/data_quality.json');args=ap.parse_args()
    with sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro',uri=True) as con:
        con.execute('BEGIN');data=build(con)
    Path(args.out).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(data,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
