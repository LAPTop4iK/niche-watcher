#!/usr/bin/env python3
"""Read-only recheck behind docs/SEARCH_DEMAND_PLAN_2026-11-01.md. Needs numpy+pandas.

    python3 docs/audit_recheck_2026-11-01.py --db niches.db --out docs/audit_recheck_2026-11-01.json
    python3 docs/audit_recheck_2026-11-01.py --db niches.db --live   # ~45 public GET, 3.3 с пауза

Database opens with mode=ro. Nothing is sent anywhere. Rerun after fixes and compare.
"""
import argparse
import collections
import json
import os
import random
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import warnings
from datetime import datetime,timezone,timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
import evidence
from relevance import relevant

warnings.filterwarnings('ignore')
STUDY='audit-20261031'
ASOF=evidence.parse('2026-11-01T00:11:56+00:00')
BASE_RUN=(STUDY,)
UA={'User-Agent':'niche-decay-scanner/1.0'}


def study_frame(con):
    con.row_factory=sqlite3.Row
    rows=[]
    for m in con.execute('''SELECT s.*,r.evidence_json FROM study_members s JOIN study_reviews r
        USING(study_id,track_id,country) WHERE s.study_id=?''',(STUDY,)):
        e=json.loads(m['evidence_json'])
        status=e.get('fetch_status') or ('OK' if e.get('last_review') else 'UNKNOWN')
        if status!='OK' or not e.get('review_dates_json'):
            continue
        dates=json.loads(e['review_dates_json']);scores=json.loads(e['review_scores_json'] or '[]')
        recent=[i for i,s in enumerate(dates) if 0<=(ASOF-evidence.parse(s)).total_seconds()<=90*86400]
        ref=evidence.parse(m['selected_at'])
        rows.append(dict(track_id=m['track_id'],country=m['country'],old=float(m['role']=='CANDIDATE'),
            age=(ref-evidence.parse(m['version_date'])).total_seconds()/86400/30.44,
            vintage=(ref-evidence.parse(m['release_date'])).days/365.25 if m['release_date'] else None,
            rc=m['rating_count'],r90=len(recent),dates=dates,version_date=m['version_date']))
    df=pd.DataFrame(rows)
    genres={r['track_id']:r['genre'] for r in con.execute('SELECT track_id,genre FROM apps')}
    df['genre']=df.track_id.map(genres)
    top=df.genre.value_counts().index[:14];df['g']=df.genre.where(df.genre.isin(top),'Other')
    price=pd.read_sql_query('SELECT track_id,country,MAX(price_amount) price FROM snapshots WHERE run_id=? GROUP BY 1,2',con,params=(STUDY,))
    df=df.merge(price,how='left',on=['track_id','country'])
    df['y']=(df.r90>=2).astype(float);df['lrc']=np.log10(df.rc)
    df['paid']=(df.price.fillna(0)>0).astype(float);df['vint']=df.vintage.fillna(df.vintage.median())
    return df


def ols(X,y,clusters):
    inverse=np.linalg.pinv(X.T@X);beta=inverse@X.T@y;e=y-X@beta;meat=np.zeros((X.shape[1],)*2)
    for key in np.unique(clusters):
        i=clusters==key;s=X[i].T@e[i];meat+=np.outer(s,s)
    G=len(np.unique(clusters));n,p=X.shape
    return beta,np.sqrt(np.diag(inverse@meat@inverse*G/(G-1)*(n-1)/(n-p)))


def lpm(df,columns,effects,outcome='y',term='old'):
    parts=[pd.Series(1.0,index=df.index,name='const')]+[df[c] for c in columns]
    parts+=[pd.get_dummies(df[f],prefix=f,drop_first=True).astype(float) for f in effects]
    X=pd.concat(parts,axis=1);beta,se=ols(X.values,df[outcome].values,df.track_id.values)
    i=list(X.columns).index(term)
    return dict(pp=round(100*beta[i],2),ci95=[round(100*(beta[i]-1.96*se[i]),2),round(100*(beta[i]+1.96*se[i]),2)],terms=X.shape[1])


def matched(df):
    binned=df.rc.map(lambda c:0 if c<1000 else 1 if c<5000 else 2 if c<20000 else 3)
    vintage=np.where(df.vintage<2,0,np.where(df.vintage<5,1,2))
    numerator=weight=0
    for _,g in df.groupby([df.country,binned,vintage]):
        old=g[g.old==1].y;fresh=g[g.old==0].y
        if len(old) and len(fresh):
            k=min(len(old),len(fresh));numerator+=k*(old.mean()-fresh.mean());weight+=k
    return 100*numerator/weight


def release_event_study(df):
    rows=[]
    for r in df.itertuples():
        release=evidence.parse(r.version_date);post=(ASOF-release).total_seconds()/86400
        if not 5<=post<=45 or not r.dates:
            continue
        dates=sorted(map(evidence.parse,r.dates));window=min(30,post)
        if dates[0]>release-timedelta(days=window):
            continue  # pre-window must be observed in full
        rows.append((r.track_id,sum(release-timedelta(days=window)<=d<release for d in dates),
                     sum(release<=d<release+timedelta(days=window) for d in dates)))
    e=pd.DataFrame(rows,columns=['track_id','pre','post']);groups=dict(tuple(e.groupby('track_id')))
    rng=np.random.default_rng(7);ids=e.track_id.unique();values=[]
    for _ in range(2000):
        s=pd.concat([groups[k] for k in rng.choice(ids,len(ids))]);values.append(s.post.sum()/max(s.pre.sum(),1))
    return dict(rows=len(e),apps=int(e.track_id.nunique()),pre=int(e.pre.sum()),post=int(e.post.sum()),
        ratio=round(e.post.sum()/e.pre.sum(),3),ci95=[round(float(x),2) for x in np.percentile(values,[2.5,97.5])],
        note='Только приложения, чей RSS покрывает окно до выпуска, то есть низкая активность.')


def study(con):
    df=study_frame(con);rng=np.random.default_rng(20261101)
    groups=dict(tuple(df.groupby('track_id')));ids=df.track_id.unique()
    boot=np.sort([matched(pd.concat([groups[k] for k in rng.choice(ids,len(ids))])) for _ in range(400)])
    bins=[0,3,6,12,18,24,36,60,84,400];labels=['<3','3-6','6-12','12-18','18-24','24-36','36-60','60-84','84+']
    df['ab']=pd.cut(df.age,bins,right=False,labels=labels)
    dose=df.groupby('ab',observed=True).agg(n=('y','size'),apps=('track_id','nunique'),rate=('y','mean'),mean_r90=('r90','mean')).round(3)
    old=df[df.old==1].copy();old['late']=(old.age>=36).astype(float)
    thresholds={}
    for t in (1,2,5,10):
        df['t']=(df.r90>=t).astype(float);thresholds[t]=lpm(df,['old','lrc'],['country','g'],'t')
    country=df.groupby(['country','old']).size().unstack(fill_value=0)
    return dict(n=dict(old=int(df.old.sum()),fresh=int((1-df.old).sum())),
        ratings=dict(old_median=float(df[df.old==1].rc.median()),fresh_median=float(df[df.old==0].rc.median()),
            old_p90=float(df[df.old==1].rc.quantile(.9)),fresh_p90=float(df[df.old==0].rc.quantile(.9))),
        paid_share=dict(old=round(float(df[df.old==1].paid.mean()),3),fresh=round(float(df[df.old==0].paid.mean()),3)),
        us_rows=dict(old=int(country.loc['us',1.0]),fresh=int(country.loc['us',0.0])),
        control_age_months=df[df.old==0].age.quantile([.1,.5,.9]).round(2).to_dict(),
        control_under_3mo=round(float((df[df.old==0].age<3).mean()),3),
        adjusted=dict(raw=lpm(df,['old'],[]),ratings=lpm(df,['old','lrc'],[]),country=lpm(df,['old','lrc'],['country']),
            genre=lpm(df,['old','lrc'],['country','g']),full=lpm(df,['old','lrc','paid','vint'],['country','g'])),
        repo_matched_pp=round(matched(df),2),repo_matched_cluster_ci95=[round(float(boot[10]),1),round(float(boot[389]),1)],
        dose_response=dose.reset_index().astype({'ab':str}).to_dict('records'),
        within_old_36plus_vs_18_36=lpm(old,['late','lrc'],[],term='late'),thresholds=thresholds,
        release_event_study=release_event_study(df))


def pain_base_rate(con,now):
    cached=evidence.demand_map(con);rows=[]
    for r in evidence.latest(con):
        if (r['rating_count'] or 0)<500 or not r['categories']:
            continue
        d=cached.get((r['track_id'],r['country']))
        try:age=(now-evidence.parse(r['version_date'])).total_seconds()/86400/30.44
        except (TypeError,ValueError,AttributeError):continue
        if not d or age<0:
            continue
        x=evidence.assess(con,r,d,now)
        if x['fetch_status']=='OK':
            rows.append(dict(fresh=age<18,r90=x['r90'],bad=x['bad_recent_count']))
    b=pd.DataFrame(rows);b=b[b.fresh&(b.r90>0)]
    share=b.bad.sum()/b.r90.sum()
    b['band']=pd.cut(b.r90,[0,1,2,4,9,19,49,1000],labels=['1','2','3-4','5-9','10-19','20-49','50+'])
    t=b.groupby('band',observed=True).agg(n=('bad','size'),has_bad=('bad',lambda s:(s>0).mean()),bad=('bad','sum'),reviews=('r90','sum'),mid=('r90','median'))
    t['bad_share']=t.bad/t.reviews;t['binomial_expected']=1-(1-share)**t.mid
    return dict(pooled_bad_share=round(float(share),3),table=t.round(3).reset_index().astype({'band':str}).to_dict('records'))


def velocity_blocking(con):
    series=collections.defaultdict(list)
    for tid,geo,stamp,low,high in con.execute('''SELECT track_id,country,scanned_at,MIN(rating_count),MAX(rating_count)
        FROM snapshots WHERE rating_count IS NOT NULL GROUP BY 1,2,3'''):
        series[(tid,geo)].append((stamp,low,high))
    by=collections.defaultdict(lambda:[0,0])
    for rows in series.values():
        rows.sort(reverse=True)
        if (evidence.parse(rows[0][0])-evidence.parse(rows[-1][0])).total_seconds()<86400:
            continue
        count=rows[0][1];previous=count;blocked=False
        for _,low,high in rows:   # same rule as evidence.rating_velocity
            if low!=high or high>previous:
                blocked=True;break
            previous=low
        band='<500' if count<500 else '500-5k' if count<5000 else '5k-50k' if count<50000 else '50k+'
        by[band][0]+=1;by[band][1]+=blocked
    d=pd.read_sql_query('SELECT run_id,track_id,country,scanned_at,rating_count FROM snapshots WHERE run_id IS NOT NULL AND rating_count>=500',con)
    d=d.sort_values(['run_id','track_id','country','scanned_at'])
    diff=d.groupby(['run_id','track_id','country']).rating_count.diff()
    return dict(blocked_by_size={k:dict(series=v[0],blocked=v[1],share=round(v[1]/v[0],3)) for k,v in sorted(by.items())},
        within_run=dict(decreases=int((diff<0).sum()),increases=int((diff>0).sum())))


def truncation(con):
    total=con.execute('SELECT COUNT(*) FROM scan_queries WHERE run_id=?',BASE_RUN).fetchone()[0]
    counts={str(n):con.execute('SELECT COUNT(*) FROM scan_queries WHERE run_id=? AND result_count>=?',(STUDY,n)).fetchone()[0] for n in (35,45,50)}
    return dict(queries=total,at_or_above=counts,share_ge35=round(counts['35']/total,3),reported_at_limit=counts['50'])


def serp_noise(con):
    frame=pd.read_sql_query("""SELECT run_id,country,category,query,track_id,rank FROM snapshots WHERE run_id IN (?,?)""",con,
        params=(STUDY,'705c4788-49f3-4407-b1f4-f0c78678fa58'))
    a=frame[frame.run_id==STUDY];b=frame[frame.run_id!=STUDY];rows=[]
    for key,y in b.groupby(['country','category','query']):
        x=a[(a.country==key[0])&(a.category==key[1])&(a['query']==key[2])]
        if len(x)<20 or len(y)<20:
            continue
        j=x.merge(y,on='track_id')
        rows.append((len(set(x[x['rank']<=10].track_id)&set(y[y['rank']<=10].track_id))/10,
            set(x[x['rank']<=3].track_id)==set(y[y['rank']<=3].track_id),(j.rank_x-j.rank_y).abs().median()))
    r=pd.DataFrame(rows,columns=['top10','top3_same','shift'])
    return dict(queries=len(r),hours_apart='~17',top10_overlap_mean=round(r.top10.mean(),3),top3_changed=round(1-r.top3_same.mean(),3),median_abs_rank_shift=float(r['shift'].median()))


def serp_trace(con):
    frame=pd.read_sql_query("""WITH last AS (SELECT query,country,MAX(scanned_at) t FROM snapshots WHERE rank IS NOT NULL GROUP BY query,country)
        SELECT s.query,s.country,s.track_id,s.rating_count,d.fetch_status,d.r90,d.sample_capped FROM snapshots s
        JOIN last l ON l.query=s.query AND l.country=s.country AND l.t=s.scanned_at
        LEFT JOIN demand d ON d.track_id=s.track_id AND d.country=s.country WHERE s.rank<=10""",con).drop_duplicates(['query','country','track_id'])
    frame['rss']=(frame.fetch_status=='OK')&frame.r90.notna()
    pairs=frame.groupby(['query','country'])
    full=frame[pairs.track_id.transform('size')>=10].groupby(['query','country']).rating_count
    median=full.median();top1=full.max()/full.sum().clip(lower=1);usable=frame[frame.rss]
    return dict(pairs=pairs.ngroups,pairs_with_10=len(median),rows=len(frame),rating_known=round(frame.rating_count.notna().mean(),3),
        rss_usable_rows=round(frame.rss.mean(),3),pairs_without_rss=round((pairs.rss.sum()==0).mean(),3),usable_capped=round(usable.sample_capped.fillna(0).astype(bool).mean(),3),
        top10_median_ratings=dict(zip(('p10','p25','p50','p75','p90'),median.quantile([.1,.25,.5,.75,.9]).round(1).tolist())),
        pairs_median_below_50=round((median<50).mean(),3),pairs_median_below_500=round((median<500).mean(),3),top1_share_of_sum_median=round(top1.median(),3))


def seed_fallback():
    seed=json.loads((ROOT/'seed_queries.json').read_text());markets=json.loads((ROOT/'markets.json').read_text())['markets']
    out={}
    for lang in sorted({m.get('lang') for m in markets.values()}):
        own=sum(len(c['queries'][lang]) for c in seed['categories'] if c['queries'].get(lang))
        fallback=sum(len(c['queries']['en']) for c in seed['categories'] if not c['queries'].get(lang))
        out[lang]=dict(local=own,english_fallback=fallback,fallback_share=round(fallback/(own+fallback),2))
    return out


def funnel(con,now):
    queue,_=evidence.research_queue(con,now);q=pd.DataFrame(queue);stale=q[q.lane=='stale'].copy()
    ranks=pd.read_sql_query("""SELECT track_id,country,MIN(rank) best_rank FROM snapshots WHERE run_id IS NOT NULL
        AND category NOT LIKE 'chart:%' AND category!='similar-graph' GROUP BY 1,2""",con)
    stale=stale.merge(ranks,how='left',on=['track_id','country'])
    step=lambda f:dict(rows=len(f),apps=int(f.track_id.nunique()))
    search=stale[stale.sources.map(lambda x:'search' in x)];alive=search[search.verdict=='ALIVE']
    top=alive[alive.best_rank<=10];title=top[top.title_match]
    grossing=pd.read_sql_query("SELECT track_id,country,version_date,rank,rating_count,scanned_at FROM snapshots WHERE query='topgrossingapplications'",con)
    keys=set(zip(grossing.track_id,grossing.country))
    search=search.assign(grossing=[k in keys for k in zip(search.track_id,search.country)])
    in_charts=search[search.country.isin(grossing.country.unique())]
    grossing['age']=(pd.Timestamp(now)-pd.to_datetime(grossing.version_date,utc=True)).dt.total_seconds()/86400/30.44
    pool=[r for r in evidence.latest(con) if (r['rating_count'] or 0)>=500 and r['version_date'] and any(not c.startswith('chart:') and c!='similar-graph' for c in r['categories'])]
    pool_age=[(now-evidence.parse(r['version_date'])).total_seconds()/86400/30.44 for r in pool]
    return dict(lanes=q.lane.value_counts().to_dict(),funnel=dict(stale=step(stale),search=step(search),alive=step(alive),rank_top10=step(top),title_match=step(title)),
        recent_pain_title_match=round(float(q[q.lane=='recent_pain'].title_match.mean()),3),stale_title_match=round(float(stale.title_match.mean()),3),
        stale_search_rank=dict(top10=round(float((search.best_rank<=10).mean()),3),top3=round(float((search.best_rank<=3).mean()),3),median=float(search.best_rank.median())),
        shortlist=title.sort_values('r90',ascending=False)[['name','country','rating_count','r90','best_rank']].head(28).to_dict('records'),
        grossing=dict(rows=len(grossing),countries=sorted(grossing.country.unique()),genres=16,observed=[grossing.scanned_at.min(),grossing.scanned_at.max()],
            stale_share=round(float((grossing.age>=18).mean()),3),stale_share_top50=round(float((grossing[grossing['rank']<=50].age>=18).mean()),3),
            under_3mo_share=round(float((grossing.age<3).mean()),3),search_pool_stale_share=round(float(np.mean(np.array(pool_age)>=18)),3)),
        verdict_vs_grossing=in_charts.groupby('verdict').grossing.agg(['size','sum']).astype(int).to_dict('index'),
        stale_in_grossing=in_charts[in_charts.grossing][['name','country','rating_count','r90','verdict']].to_dict('records'))


def storage(db_path):
    gz=Path(db_path).with_suffix('.db.gz')
    return dict(db_bytes=os.path.getsize(db_path),gz_bytes=os.path.getsize(gz) if gz.exists() else None,gz_limit_bytes=90*1024**2)


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url,headers=UA),timeout=25) as r:
        return json.loads(r.read())


def rss_count(track,country,page=1):
    entries=get(f'https://itunes.apple.com/{country}/rss/customerreviews/id={track}/sortby=mostrecent/page={page}/json')['feed'].get('entry') or []
    return sum('im:rating' in e for e in ([entries] if isinstance(entries,dict) else entries))


def live(con):
    rng=random.Random(20261101);sample=[];now=datetime.now(timezone.utc);rows=[]
    for low,high,n in [(1,9,3),(10,24,4),(25,34,4),(35,44,6),(45,50,7)]:
        sample+=rng.sample(con.execute('SELECT country,query,result_count FROM scan_queries WHERE run_id=? AND result_count BETWEEN ? AND ?',(STUDY,low,high)).fetchall(),n)
    def stale(x):
        try:age=(now-evidence.parse(x['currentVersionReleaseDate'])).days/30.44
        except (KeyError,ValueError):return False
        return age>=18 and (x.get('userRatingCount') or 0)>=500
    for geo,query,stored in sample:
        results=get('https://itunes.apple.com/search?'+urllib.parse.urlencode({'term':query,'entity':'software','country':geo,'limit':200}))['results']
        head,tail=results[:50],results[50:]
        rows.append(dict(country=geo,query=query,stored_at_50=stored,returned_at_200=len(results),stale_head=sum(map(stale,head)),stale_tail=sum(map(stale,tail)),
            r500_head=sum((x.get('userRatingCount') or 0)>=500 for x in head),r500_tail=sum((x.get('userRatingCount') or 0)>=500 for x in tail)))
        time.sleep(3.3)
    pages=[]
    for page in (1,2,3,4,5,10):
        pages.append(dict(page=page,reviews=rss_count(1252497129,'us',page)));time.sleep(3.3)
    empty=[]
    for tid,geo in con.execute("SELECT track_id,country FROM demand WHERE fetch_status='EMPTY' ORDER BY checked_at LIMIT 8").fetchall():
        empty.append(dict(track_id=tid,country=geo,reviews_now=rss_count(tid,geo)));time.sleep(3.3)
    feeds={}
    for name in ('topgrossingapplications','toppaidapplications'):
        data=get(f'https://itunes.apple.com/us/rss/{name}/limit=10/genre=6007/json');feeds[name]=data['feed']['updated']['label'];time.sleep(3.3)
    return dict(at=now.isoformat(),search_limit_200=rows,rss_pages_picturethis_us=pages,previously_empty=empty,chart_feeds_updated=feeds)


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--db',default=str(ROOT/'niches.db'));ap.add_argument('--out');ap.add_argument('--now',default='2026-11-01T16:40:00Z');ap.add_argument('--live',action='store_true')
    args=ap.parse_args();now=evidence.parse(args.now)
    con=sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro',uri=True)
    data=dict(computed_for=args.now,counts=dict(apps=con.execute('SELECT COUNT(*) FROM apps').fetchone()[0],snapshots=con.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0]),
        study=study(con),pain_base_rate=pain_base_rate(con,now),velocity=velocity_blocking(con),truncation=truncation(con),
        serp_noise=serp_noise(con),serp_trace=serp_trace(con),seed_fallback=seed_fallback(),funnel=funnel(con,now),storage=storage(args.db))
    if args.live:
        data['live']=live(con)
    text=json.dumps(data,ensure_ascii=False,indent=1,default=float)+'\n'
    if args.out:Path(args.out).write_text(text)
    print(text)


if __name__=='__main__':main()
