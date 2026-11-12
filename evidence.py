"""Observable activity, provenance and conservative treatment of missing data."""
from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3
import unicodedata

METHOD = 'activity-v5'
LABELS = {'ALIVE':'Есть свежая активность','WEAK':'Слабый сигнал',
          'UNKNOWN':'Недостаточно данных','NO_RECENT_EVIDENCE':'Нет свежих сигналов'}
LANES = {'stale':'Версия ≥18 мес', 'recent_pain':'Версия <18 мес + жалобы выше базы'}
MAX_AGE_DAYS=14
PAIN_MIN_REVIEWS=3
PAIN_PRIOR=20  # Pseudo-reviews pulling a small sample towards its category share.
# Apple's own apps (Watch, Apple Store, iTunes Remote) ship with the platform: not a niche to enter.
PLATFORM_SELLER=re.compile(r'Apple (Inc\b|Distribution International\b|Pty\b|Services Pte\b)')


def parse(value):
    dt=datetime.fromisoformat(value.replace('Z','+00:00'))
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def run_frames(con):
    frames={}
    for run_id,payload in con.execute('SELECT run_id,settings_json FROM scan_runs'):
        settings=json.loads(payload)
        frames[run_id]=(settings.get('limit',settings.get('top')),settings.get('entity','software'))
    return frames


def coverage_map(con):
    """Latest complete SERP per query; a narrower run cannot replace a current wider one."""
    if not {'scan_runs','scan_queries'} <= tables(con):
        return {}
    frames=run_frames(con);kept={}
    for run_id,geo,cat,query,finished in con.execute('''SELECT q.run_id,q.country,q.category,q.query,q.finished_at
            FROM scan_queries q JOIN scan_runs r USING(run_id) WHERE q.status='OK'
              AND r.source IN ('itunes-search','apple-charts','apple-page-links')
            ORDER BY q.finished_at,r.started_at'''):
        limit,entity=frames[run_id];key=(geo,cat,query);previous=kept.get(key)
        if entity!='software':
            continue  # Another result frame is stored but never replaces this one.
        if previous and limit is not None and previous[1] is not None and limit<previous[1]:
            try:
                if (parse(finished)-parse(previous[2])).total_seconds()<=MAX_AGE_DAYS*86400:
                    continue
            except (TypeError,ValueError,AttributeError):
                continue
        kept[key]=(run_id,limit,finished)
    return {key:value[0] for key,value in kept.items()}


def query_memberships(con,all_queries=False):
    """Use the latest successful completed SERP, including empty SERPs.

    A disappeared result is not kept as a current competitor merely because
    it appeared months ago. Incomplete queries do not erase the previous sample.
    Search results older than MAX_AGE_DAYS before the newest observation expire:
    a query without a journal or one dropped from the seed has no way to be
    superseded. Charts and page links have their own rotation and stay.
    """
    coverage=coverage_map(con)
    columns={r[1] for r in con.execute('PRAGMA table_info(snapshots)')}
    run_column='run_id' if 'run_id' in columns else 'NULL'
    newest=con.execute('SELECT MAX(scanned_at) FROM snapshots').fetchone()[0]
    cutoff=(parse(newest)-timedelta(days=MAX_AGE_DAYS)).strftime('%Y-%m-%dT%H:%M:%SZ') if newest else ''
    memberships={}
    for tid,geo,cat,query,run_id,stamp in con.execute(f'''SELECT track_id,country,category,query,{run_column},scanned_at
            FROM snapshots ORDER BY scanned_at DESC,id DESC'''):
        key=(geo,cat,query)
        if key in coverage and coverage[key]!=run_id:
            continue
        if stamp<cutoff and not cat.startswith('chart:') and cat!='similar-graph':
            continue
        queries=memberships.setdefault((tid,geo),{}).setdefault(cat,[])
        if query not in queries:
            queries.append(query)
    return memberships if all_queries else {key:{cat:queries[0] for cat,queries in cats.items()}
        for key,cats in memberships.items()}


def current_serp(con):
    """Search results of the runs that currently cover each query."""
    coverage=coverage_map(con);runs=sorted(set(coverage.values()))
    if not runs:
        return []
    rows=[]
    for tid,geo,cat,query,rank,run_id,count,name in con.execute(f'''SELECT track_id,country,category,query,rank,run_id,rating_count,app_name
            FROM snapshots WHERE rank IS NOT NULL AND run_id IN ({','.join('?'*len(runs))})''',runs):
        if coverage.get((geo,cat,query))==run_id and not cat.startswith('chart:') and cat!='similar-graph':
            rows.append((tid,geo,cat,query,rank,count,name))
    return rows


def best_positions(con):
    best={}
    for tid,geo,cat,query,rank,_,_ in current_serp(con):
        if (tid,geo) not in best or rank<best[(tid,geo)][0]:
            best[(tid,geo)]=(rank,query)
    return best


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


def counter_noise(value):
    """Scan maxima of one counter fell by more than max(2; 0.1%) in 4 of 5051
    week-long tier-1 series (Nov 2026): larger moves are real changes."""
    return max(2,0.001*value)


def rating_series(con,track_id,country):
    """One point per scan, newest first: the scan's highest count. An app appears
    once per query it matched; replicas lag behind and never run ahead, so the
    maximum is the freshest value of the scan."""
    scans={};columns={r[1] for r in con.execute('PRAGMA table_info(snapshots)')}
    for run_id,stamp,count in con.execute(f'''SELECT {'run_id' if 'run_id' in columns else 'NULL'},scanned_at,rating_count
            FROM snapshots WHERE track_id=? AND country=? AND rating_count IS NOT NULL''',(track_id,country)):
        scan=scans.setdefault(run_id or stamp,dict(stamp=stamp,value=count))
        scan['stamp']=max(scan['stamp'],stamp);scan['value']=max(scan['value'],count)
    return sorted(((parse(s['stamp']),s['value']) for s in scans.values()),reverse=True)


# A weekly scan can fire minutes short of seven days (slot 3: 6.994 days, Nov 11).
WEEK_DAYS=6.5


def rating_velocity(con,track_id,country,min_days=WEEK_DAYS,max_days=45):
    """Growth per 30 days; 0.0 means within counter noise, None means unusable.

    The baseline is the highest count seen up to it, so a lagging value or a
    dip before it cannot pass for growth during recovery. A counter that ends
    below that baseline, or drops twice, was reset or is unstable."""
    series=rating_series(con,track_id,country)
    if not series:
        return None,0.0
    end,count=series[0]
    observed=(end-series[-1][0]).total_seconds()/86400
    window=[(stamp,value) for stamp,value in series if (end-stamp).total_seconds()<=max_days*86400]
    older=[(stamp,value) for stamp,value in window if (end-stamp).total_seconds()>=min_days*86400]
    if not older:
        return None,observed
    days=(end-older[0][0]).total_seconds()/86400
    baseline=max(value for _,value in older)
    values=[value for _,value in reversed(window)]
    drops=sum(later<earlier-counter_noise(earlier) for earlier,later in zip(values,values[1:]))
    if drops>1 or count<baseline-counter_noise(baseline):
        return None,observed
    return ((count-baseline)/days*30 if count-baseline>counter_noise(baseline) else 0.0),days


def activity(vel,last_review,r90,fetch_status='OK',now=None):
    """Verdict and its evidence path. The paths differ in sensitivity, so periods
    are comparable only within one path."""
    now=now or datetime.now(timezone.utc)
    grew=vel is not None and vel>=1
    verdict=None
    if fetch_status!='OK' or not isinstance(r90,int) or r90<0:
        verdict='UNKNOWN'
    else:
        try:
            age=(now-parse(last_review)).total_seconds()/86400
        except (TypeError,ValueError,AttributeError):
            verdict='UNKNOWN'
        else:
            verdict=('UNKNOWN' if age<0 else 'ALIVE' if age<=90 and r90>=2 else
                     'WEAK' if age<=90 and r90>=1 else 'NO_RECENT_EVIDENCE')
    if verdict=='ALIVE':
        return 'ALIVE','rss+velocity' if grew else 'rss'
    return ('ALIVE','velocity') if grew else (verdict,None)


def classify(vel,last_review,r90,fetch_status='OK',now=None):
    return activity(vel,last_review,r90,fetch_status,now)[0]


def demand_map(con):
    con.row_factory=sqlite3.Row
    if 'demand' not in tables(con):
        return {}
    return {(r['track_id'],r['country']):dict(r) for r in con.execute('SELECT * FROM demand')}


def assess(con,row,cached=None,now=None):
    from relevance import theme_word
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
    r90=d.get('r90');bad_recent=None;r7=r30=None;bad_after_version=None;bad_current_version=None
    pain_theme=None;theme_known=bool(d.get('review_themes_json'))
    try:rss_after_version=parse(d['checked_at'])>=parse(row['version_date'])
    except (KeyError,ValueError,TypeError,AttributeError):rss_after_version=None
    version_known=id_known=sample_span=None;capped_recent=None
    if d.get('review_dates_json'):
        try:
            dates=json.loads(d['review_dates_json'])
            scores=json.loads(d.get('review_scores_json') or '[]')
            if not isinstance(dates,list) or not isinstance(scores,list):
                raise ValueError('Invalid review arrays')
            recent=[i for i,stamp in enumerate(dates) if 0<=(now-parse(stamp)).total_seconds()<=90*86400]
            r90=len(recent)
            r7=sum(0<=(now-parse(stamp)).total_seconds()<=7*86400 for stamp in dates)
            r30=sum(0<=(now-parse(stamp)).total_seconds()<=30*86400 for stamp in dates)
            sample_span=(max(map(parse,dates))-min(map(parse,dates))).total_seconds()/86400 if dates else None
            capped_recent=bool(d.get('sample_capped')) and len(recent)==len(dates)
            versions=json.loads(d.get('review_versions_json') or '[]')
            ids=json.loads(d.get('review_ids_json') or '[]')
            if isinstance(versions,list) and len(versions)==len(dates):
                version_known=sum(bool(v) for v in versions)
            if isinstance(ids,list) and len(ids)==len(dates):
                id_known=sum(bool(v) for v in ids)
            if len(scores)==len(dates):
                if any(not isinstance(s,(int,float)) or not 1<=s<=5 for s in scores):
                    raise ValueError('Invalid review score')
                bad_recent=sum(scores[i]<=2 for i in recent)
                try:
                    version_date=parse(row['version_date'])
                    bad_after_version=sum(scores[i]<=2 and parse(dates[i])>=version_date for i in recent)
                except (KeyError,ValueError,TypeError,AttributeError):pass
                if version_known and row.get('version'):
                    bad_current_version=sum(scores[i]<=2 and versions[i]==row['version'] for i in recent)
                if theme_known:
                    on_release={i for i in recent if scores[i]<=2 and (versions[i]==row.get('version') if bad_current_version is not None else True)}
                    # Samples stored before the stop list still carry grammar words; the
                    # app's own name ("pizza" for Domino's Pizza) describes the product.
                    title=unicodedata.normalize('NFKC',row.get('name') or '').casefold()
                    repeated=[(sum(i in on_release for i in reviews),word) for word,reviews in json.loads(d['review_themes_json']).items()
                              if theme_word(word) and word not in title]
                    pain_theme=next((word for count,word in sorted(repeated,key=lambda x:(-x[0],x[1])) if count>=2),None)
        except (ValueError,TypeError,AttributeError):
            status='ERROR';r90=None
    expired_complaints=status=='STALE' and (bad_recent or 0)>0
    if status!='OK':
        bad_recent=bad_after_version=bad_current_version=r7=r30=pain_theme=None
    state,path=activity(vel,d.get('last_review'),r90,status,now)
    reasons=[f'{vel:+.1f} оценок/30д за {days:.1f} дней наблюдений' if vel else
             f'Изменение счётчика в пределах шума за {days:.1f} дн.' if vel==0 else
             f'Надёжный прирост пока недоступен; история {days:.1f} дн.']
    if path:
        reasons.append({'rss':'Основание активности: свежие отзывы','velocity':'Основание активности: прирост оценок сверх шума','rss+velocity':'Основание активности: отзывы и прирост оценок'}[path])
    if d.get('last_review'):
        reasons.append(f"Последний отзыв {str(d['last_review'])[:10]}; R90 ≥ {r90 if r90 is not None else 'неизвестно'} (выборка RSS)")
    if status!='OK':
        reasons.append(f'Отзывы: {status}; отсутствие данных не означает отсутствие спроса')
    if snapshot_age is None or snapshot_age>MAX_AGE_DAYS:
        reasons.append('Метаданные требуют обновления')
    if status=='OK' and (bad_recent or 0)>0:
        if rss_after_version is False:
            reasons.append('RSS собран до последнего выпуска; нужен новый срез, прежде чем оценивать текущую версию')
        if bad_after_version==0:
            reasons.append('Все свежие 1–2★ предшествуют последнему обновлению; текущую проблему нужно перепроверить')
        elif bad_after_version is not None:
            reasons.append(f'1–2★ по времени после публикации версии: {bad_after_version}; это ещё не доказательство неисправленной ошибки')
        if bad_current_version is not None:
            reasons.append(f'1–2★ с совпадающей строкой версии: {bad_current_version}; версия известна у {version_known} записей RSS')
    if status=='OK' and capped_recent:
        reasons.append('Все полученные отзывы попали в 90 дней: окно усечено, это нижняя граница числа отзывов')
    return dict(verdict=state,alive_path=path,label=LABELS[state],velocity=vel,days=days,r90=r90,
                last_review=d.get('last_review'),checked_at=d.get('checked_at'),fetch_status=status,
                snapshot_age_days=snapshot_age,sample_size=d.get('sample_size'),
                bad_recent_count=bad_recent,bad_after_version_count=bad_after_version,pain_theme=pain_theme,theme_known=theme_known,
                bad_current_version_count=bad_current_version,review_version_known_count=version_known,
                review_id_known_count=id_known,r7=r7,r30=r30,recent_sample_capped=capped_recent,
                sample_span_days=sample_span,rss_age_days=review_age,expired_complaints=expired_complaints,rss_collected_after_version=rss_after_version,reasons=reasons,method=METHOD)


def pain_excess(rows):
    """1–2★ share above the category×storefront base, shrunk towards that base.

    A share of low ratings is about 38% at any review volume, so a bare complaint
    measures activity. Only a share above the base says something about the app.
    """
    groups={};bad=reviews=0
    for row in rows:
        group=groups.setdefault((row['category'],row['country']),[0,0])
        group[0]+=row['bad_recent_count'] or 0;group[1]+=row['r90'] or 0
        bad+=row['bad_recent_count'] or 0;reviews+=row['r90'] or 0
    overall=bad/reviews if reviews else 0
    for row in rows:
        k,n=groups[(row['category'],row['country'])]
        base=(k+PAIN_PRIOR*overall)/(n+PAIN_PRIOR)
        shrunk=((row['bad_recent_count'] or 0)+PAIN_PRIOR*base)/((row['r90'] or 0)+PAIN_PRIOR)
        row.update(pain_base=base,pain_excess=shrunk-base)


def research_queue(con,now=None):
    """Two discovery routes; the frozen old/fresh study still uses is_candidate.

    A recent version qualifies through complaints above its category's base on
    the current release, not through its age or lifetime rating. Coverage
    describes the selected RSS sample, not complaint prevalence in the market.
    Order: verdict, then best search position, then complaints; never earnings.
    """
    from relevance import relevant
    now=now or datetime.now(timezone.utc)
    cached=demand_map(con);queue=[];reviewed=[];positions=best_positions(con)
    coverage={kind:dict(eligible=0,rss_usable=0,any_complaint=0,complaint_records=0,lost_to_stale_rss=0) for kind in ('search','charts','links','all')}
    for row in latest(con):
        if not row['categories'] or (row['rating_count'] or 0)<500 or PLATFORM_SELLER.match(row.get('seller') or ''):
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
        row['best_rank'],row['best_query']=positions.get((row['track_id'],row['country']),(None,None))
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
            for kind in kinds:
                coverage[kind]['rss_usable']+=int(usable)
                coverage[kind]['lost_to_stale_rss']+=int(row['expired_complaints'])
            if not usable:
                continue
            row['lane']='recent_pain'
            reviewed.append(row)
            continue
        row['store_url']=f"https://apps.apple.com/{row['country']}/app/id{row['track_id']}"
        queue.append(row)
    pain_excess(reviewed)
    complaints=[r for r in reviewed if (r['bad_recent_count'] or 0)>0]
    searched=[r for r in complaints if 'search' in r['sources']]
    coverage['pain_timing']=dict(total=len(searched),after_release=sum((r['bad_after_version_count'] or 0)>0 for r in searched),
        only_before_release=sum(r['bad_after_version_count']==0 for r in searched),
        timing_unknown=sum(r['bad_after_version_count'] is None for r in searched),
        rss_before_release=sum(r['rss_collected_after_version'] is False for r in searched),
        same_version_positive=sum((r['bad_current_version_count'] or 0)>0 for r in searched),
        version_unknown=sum(r['bad_current_version_count'] is None for r in searched))
    for row in complaints:
        for kind in [*row['sources'],'all']:coverage[kind]['any_complaint']+=1
        # Without review versions the release date is the closest available filter.
        count=row['bad_current_version_count'] if row['bad_current_version_count'] is not None else row['bad_after_version_count']
        if (count or 0)<PAIN_MIN_REVIEWS or row['pain_excess']<=0 or (row['theme_known'] and not row['pain_theme']):
            continue
        # A recent release qualifies through its own complaints, so an unrelated
        # search hit (Instagram for "tide chart") would bring only its size here.
        sources=[s for s in row['sources'] if s!='search' or row['title_match']]
        if not sources:
            continue
        row['sources']=sources
        for kind in [*row['sources'],'all']:coverage[kind]['complaint_records']+=1
        row['store_url']=f"https://apps.apple.com/{row['country']}/app/id{row['track_id']}"
        queue.append(row)
    queue.sort(key=lambda r:(list(LABELS).index(r['verdict']),r['best_rank'] or 1000,
        -(r.get('pain_excess') or 0),-int((r.get('bad_recent_count') or 0)>0),-(r['r90'] or 0),r['track_id'],r['country']))
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
