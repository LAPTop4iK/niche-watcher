"""Observable activity, provenance and conservative treatment of missing data."""
from datetime import datetime, timezone
import json
import sqlite3

METHOD = 'activity-v3'
LABELS = {'ALIVE':'Есть свежая активность','WEAK':'Слабый сигнал',
          'UNKNOWN':'Недостаточно данных','NO_RECENT_EVIDENCE':'Нет свежих сигналов'}
LANES = {'stale':'Версия ≥18 мес', 'recent_pain':'Версия <18 мес + жалобы'}
MAX_AGE_DAYS=14


def parse(value):
    dt=datetime.fromisoformat(value.replace('Z','+00:00'))
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def query_memberships(con,all_queries=False):
    """Use the latest successful completed SERP, including empty SERPs.

    A disappeared result is not kept as a current competitor merely because
    it appeared months ago. Incomplete queries do not erase the previous sample.
    """
    coverage={}
    if {'scan_runs','scan_queries'} <= tables(con):
        for row in con.execute('''SELECT q.run_id,q.country,q.category,q.query FROM scan_queries q
            JOIN scan_runs r USING(run_id) WHERE q.status='OK'
              AND r.source IN ('itunes-search','apple-charts','apple-page-links')
            ORDER BY q.finished_at,r.started_at'''):
            coverage[(row[1],row[2],row[3])]=row[0]
    columns={r[1] for r in con.execute('PRAGMA table_info(snapshots)')}
    run_column='run_id' if 'run_id' in columns else 'NULL'
    memberships={}
    for tid,geo,cat,query,run_id in con.execute(f'''SELECT track_id,country,category,query,{run_column}
            FROM snapshots ORDER BY scanned_at DESC,id DESC'''):
        key=(geo,cat,query)
        if key in coverage and coverage[key]!=run_id:
            continue
        queries=memberships.setdefault((tid,geo),{}).setdefault(cat,[])
        if query not in queries:
            queries.append(query)
    return memberships if all_queries else {key:{cat:queries[0] for cat,queries in cats.items()}
        for key,cats in memberships.items()}


def latest(con, by_category=False):
    con.row_factory=sqlite3.Row
    columns={r[1] for r in con.execute('PRAGMA table_info(snapshots)')}
    name='COALESCE(r.app_name,a.name)' if 'app_name' in columns else 'a.name'
    seller='COALESCE(r.seller_name,a.seller)' if 'seller_name' in columns else 'a.seller'
    url='COALESCE(r.storefront_url,a.store_url)' if 'storefront_url' in columns else 'a.store_url'
    price='r.price_amount' if 'price_amount' in columns else 'NULL'
    rows=con.execute(f'''WITH ranked AS (
        SELECT s.*,ROW_NUMBER() OVER (PARTITION BY track_id,country ORDER BY scanned_at DESC,id DESC) rn
        FROM snapshots s)
        SELECT r.*,{name} AS name,{seller} AS seller,{url} AS store_url,
               {price} AS observed_price,a.first_seen
        FROM ranked r JOIN apps a USING(track_id) WHERE rn=1''').fetchall()
    memberships=query_memberships(con,all_queries=True)
    output=[]
    for record in rows:
        r=dict(record);r['price']=r.pop('observed_price')
        sources=memberships.get((r['track_id'],r['country']),{})
        members={cat:queries[0] for cat,queries in sources.items()}
        r['source_queries']=sources
        r['categories']=sorted(members)
        r['category_queries']=members
        if by_category:
            for cat,query in members.items():
                output.append(dict(r,category=cat,query=query))
        else:
            # Show a keyword source ahead of chart/similarity collection methods.
            keyword_members=[cat for cat in members if not cat.startswith('chart:') and cat!='similar-graph']
            if keyword_members and r['category'] not in keyword_members:
                r['category']=keyword_members[0];r['query']=members[r['category']]
            output.append(r)
    return output


def is_candidate(row,now=None,min_stale=18,min_ratings=500):
    if 'categories' in row and not row['categories']:
        return False
    try:
        age=((now or datetime.now(timezone.utc))-parse(row['version_date'])).total_seconds()/86400/30.44
        return age>=min_stale and (row['rating_count'] or 0)>=min_ratings
    except (AttributeError,TypeError,ValueError,KeyError):
        return False


def rating_velocity(con,track_id,country,min_days=7,max_days=45):
    # Group equal timestamps before choosing either endpoint. Otherwise a
    # conflicting second row at the baseline could be hidden by an early return.
    rows=con.execute('''SELECT scanned_at,MIN(rating_count),MAX(rating_count) FROM snapshots
        WHERE track_id=? AND country=? AND rating_count IS NOT NULL GROUP BY scanned_at
        ORDER BY scanned_at DESC''',(track_id,country)).fetchall()
    if not rows:
        return None,0.0
    end,count=parse(rows[0][0]),rows[0][1]
    observed=(end-parse(rows[-1][0])).total_seconds()/86400
    previous=count;baseline=None
    for stamp,low,high in rows:
        days=(end-parse(stamp)).total_seconds()/86400
        if days>max_days:
            break
        if low!=high or high>previous:
            return None,observed
        previous=low
        if days>=min_days and baseline is None:
            baseline=(low,days)
    # A reset just before the chosen baseline can masquerade as new ratings
    # during recovery. Inspect the entire recent window before returning growth.
    if baseline is not None:
        low,days=baseline
        return (count-low)/days*30,days
    return None,observed


def classify(vel,last_review,r90,fetch_status='OK',now=None):
    now=now or datetime.now(timezone.utc)
    if vel is not None and vel>=1:
        return 'ALIVE'
    if fetch_status!='OK' or not isinstance(r90,int) or r90<0:
        return 'UNKNOWN'
    try:
        age=(now-parse(last_review)).total_seconds()/86400
    except (TypeError,ValueError,AttributeError):
        return 'UNKNOWN'
    if age<0:
        return 'UNKNOWN'
    if age<=90 and r90>=2:
        return 'ALIVE'
    if age<=90 and r90>=1:
        return 'WEAK'
    return 'NO_RECENT_EVIDENCE'


def demand_map(con):
    con.row_factory=sqlite3.Row
    if 'demand' not in tables(con):
        return {}
    return {(r['track_id'],r['country']):dict(r) for r in con.execute('SELECT * FROM demand')}


def assess(con,row,cached=None,now=None):
    now=now or datetime.now(timezone.utc)
    vel,days=rating_velocity(con,row['track_id'],row['country'])
    d=cached or {}
    status=d.get('fetch_status') or ('OK' if d.get('last_review') else 'UNKNOWN')
    # Snapshot and RSS freshness are checked independently: missing RSS must not
    # accidentally make an old positive velocity current.
    try:
        snapshot_age=(now-parse(row['scanned_at'])).total_seconds()/86400
        if not 0<=snapshot_age<=MAX_AGE_DAYS:
            vel=None
    except (KeyError,ValueError,AttributeError,TypeError):
        snapshot_age=None;vel=None
    try:
        review_age=(now-parse(d['checked_at'])).total_seconds()/86400
        if not 0<=review_age<=MAX_AGE_DAYS:
            status='STALE'
    except (KeyError,ValueError,AttributeError,TypeError):
        review_age=None;status='UNKNOWN'
    r90=d.get('r90');bad_recent=None
    if d.get('review_dates_json'):
        try:
            dates=json.loads(d['review_dates_json'])
            scores=json.loads(d.get('review_scores_json') or '[]')
            recent=[i for i,stamp in enumerate(dates) if 0<=(now-parse(stamp)).total_seconds()<=90*86400]
            r90=len(recent)
            if len(scores)==len(dates):
                bad_recent=sum(scores[i]<=2 for i in recent)
        except (ValueError,TypeError,AttributeError):
            status='ERROR';r90=None
    if status!='OK':
        bad_recent=None  # stale/error samples cannot qualify a current pain signal
    state=classify(vel,d.get('last_review'),r90,status,now)
    reasons=[f'{vel:+.1f} оценок/30д за {days:.1f} дней наблюдений' if vel is not None else
             f'Надёжный прирост пока недоступен; история {days:.1f} дн.']
    if d.get('last_review'):
        reasons.append(f"Последний отзыв {str(d['last_review'])[:10]}; R90 ≥ {r90 if r90 is not None else 'неизвестно'} (выборка RSS)")
    if status!='OK':
        reasons.append(f'Отзывы: {status}; отсутствие данных не означает отсутствие спроса')
    if snapshot_age is None or snapshot_age>MAX_AGE_DAYS:
        reasons.append('Метаданные требуют обновления')
    return dict(verdict=state,label=LABELS[state],velocity=vel,days=days,r90=r90,
                last_review=d.get('last_review'),checked_at=d.get('checked_at'),fetch_status=status,
                snapshot_age_days=snapshot_age,sample_size=d.get('sample_size'),
                bad_recent_count=bad_recent,reasons=reasons,method=METHOD)


def research_queue(con,now=None):
    """Two discovery routes; the frozen old/fresh study still uses is_candidate.

    Recent-version records qualify through observed complaints, not their age or
    lifetime average rating. Coverage describes the selected RSS sample; it is
    not an estimate of complaint prevalence in the market.
    """
    from relevance import relevant
    now=now or datetime.now(timezone.utc)
    cached=demand_map(con);queue=[]
    coverage={kind:dict(eligible=0,rss_usable=0,complaint_records=0) for kind in ('search','charts','links','all')}
    for row in latest(con):
        if not row['categories'] or (row['rating_count'] or 0)<500:
            continue
        try:
            age=(now-parse(row['version_date'])).total_seconds()/86400/30.44
        except (ValueError,TypeError,AttributeError):
            continue
        if age<0:
            continue
        row['sources']=sorted({'charts' if c.startswith('chart:') else 'links' if c=='similar-graph' else 'search' for c in row['categories']})
        row['title_match_categories']=sorted(c for c,queries in row['source_queries'].items()
            if not c.startswith('chart:') and c!='similar-graph' and any(relevant(row['name'],q) for q in queries))
        row['title_match']=bool(row['title_match_categories'])
        d=cached.get((row['track_id'],row['country']))
        if age>=18:
            row.update(assess(con,row,d,now));row['lane']='stale'
        else:
            kinds=[*row['sources'],'all']
            for kind in kinds:coverage[kind]['eligible']+=1
            if not d:
                continue
            row.update(assess(con,row,d,now))
            usable=row['fetch_status']=='OK' and row['snapshot_age_days'] is not None and 0<=row['snapshot_age_days']<=MAX_AGE_DAYS
            for kind in kinds:coverage[kind]['rss_usable']+=int(usable)
            if not usable or not (row['bad_recent_count'] or 0)>0:
                continue
            for kind in kinds:coverage[kind]['complaint_records']+=1
            row['lane']='recent_pain'
        row['store_url']=f"https://apps.apple.com/{row['country']}/app/id{row['track_id']}"
        queue.append(row)
    queue.sort(key=lambda r:(list(LABELS).index(r['verdict']),-int(r['title_match']),
        -int((r.get('bad_recent_count') or 0)>0 and (r.get('price') or 0)>0),
        -int((r.get('bad_recent_count') or 0)>0),-(r['r90'] or 0),r['track_id'],r['country']))
    return queue,coverage


def coverage(con):
    con.row_factory=sqlite3.Row
    if not {'scan_runs','scan_queries'}<=tables(con):
        return []
    rows=[dict(r) for r in con.execute('''SELECT r.run_id,r.source,r.started_at,r.finished_at,r.status,r.settings_json,
        r.expected_queries,COUNT(q.query) attempted,SUM(q.status='OK') succeeded,
        SUM(q.status='ERROR') errors,SUM(q.status='OK' AND q.result_count=0) empty
        FROM scan_runs r LEFT JOIN scan_queries q USING(run_id) GROUP BY r.run_id ORDER BY r.started_at DESC''')]
    for row in rows:
        settings=json.loads(row.pop('settings_json'));jobs=settings.get('jobs',[])
        geos=sorted({job[0].upper() for job in jobs});categories={job[1] for job in jobs}
        limit=settings.get('limit',settings.get('top'))
        scope=', '.join(geos) if len(geos)<=4 else str(len(geos))
        row['scope']=f'Страны: {scope} · категорий: {len(categories)}'+(f' · до {limit} карточек/запрос' if limit is not None else '')
    return rows
