#!/usr/bin/env python3
"""Private TrustMRR watch: verified revenue next to App Store competition and complaints.

TrustMRR terms (Nov 2 2026, s.9.2-9.3) forbid republishing its data and scraping the
site; its llms.txt allows bounded snapshots from the public discovery endpoints. So:
- only the public discovery endpoints are read, or the authenticated API with the
  owner's own key in TRUSTMRR_API_KEY;
- everything lands in a private folder outside the repository (default
  ~/.niche-watcher), never in niches.db, whose checkpoint is a public release asset;
- nothing goes to git, CI, Pages or Telegram.
Trustpilot forbids scraping too, so complaints come from App Store reviews of the
leaders of each app's niche.

    python3 trustmrr_watch.py            # collect, enrich, write the private report
    python3 trustmrr_watch.py collect    # only the TrustMRR snapshot
    python3 trustmrr_watch.py report     # rebuild the report from stored data
"""
import argparse
import html
import json
import math
import os
import re
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE=Path(__file__).resolve().parent
PRIVATE_DIR=Path(os.environ.get('NICHE_PRIVATE_DIR','~/.niche-watcher')).expanduser()
PUBLIC_ENDPOINTS={'api/ai/discovery':('recentlyAddedStartups','fastestGrowingStartups'),
                  'api/ai':('recentlyListedStartups','bestDeals')}
API='https://trustmrr.com/api/v1/startups'
APP_ID=re.compile(r'apps\.apple\.com/(?:[a-z]{2}/)?app/(?:[^/]+/)?id(\d+)')
ENRICH_MAX_AGE_DAYS=7
STALE_MONTHS=18

SCHEMA='''
CREATE TABLE IF NOT EXISTS startups (
 slug TEXT PRIMARY KEY,name TEXT,category TEXT,description TEXT,website TEXT,
 payment_provider TEXT,is_mobile INTEGER,app_store_id INTEGER,app_store_country TEXT,
 first_seen TEXT,last_seen TEXT);
CREATE TABLE IF NOT EXISTS observations (
 slug TEXT NOT NULL,observed_at TEXT NOT NULL,source TEXT NOT NULL,
 revenue_30d REAL,mrr REAL,growth_30d REAL,customers INTEGER,on_sale INTEGER,asking_price REAL,
 store_rating_count INTEGER,PRIMARY KEY(slug,observed_at,source));
CREATE TABLE IF NOT EXISTS niches (
 slug TEXT PRIMARY KEY,checked_at TEXT NOT NULL,term TEXT,country TEXT,payload_json TEXT NOT NULL);
'''


def now():
    return datetime.now(timezone.utc)


def iso(value):
    return value.isoformat(timespec='seconds').replace('+00:00','Z')


def private_path(path):
    """The TrustMRR data must never sit inside the public repository."""
    path=Path(path).expanduser().resolve()
    if path==HERE or HERE in path.parents:
        raise SystemExit(f'Refusing to keep TrustMRR data inside the public repository: {path}')
    return path


def open_db(path):
    path=private_path(path);path.parent.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(path);con.row_factory=sqlite3.Row;con.executescript(SCHEMA)
    return con


def get_json(url,headers=None,attempts=3):
    request=urllib.request.Request(url,headers={'User-Agent':'niche-watcher-research/1.0',**(headers or {})})
    for attempt in range(1,attempts+1):
        try:
            with urllib.request.urlopen(request,timeout=30) as response:
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as error:
            if error.code!=429 or attempt==attempts:
                raise
            time.sleep(60*attempt)


def app_store_id(record):
    store=((record.get('mobileApp') or {}).get('appStore') or {})
    match=APP_ID.search(store.get('url') or '')
    return (int(match.group(1)),store.get('country') or 'us',store.get('ratingCount')) if match else (None,None,None)


def money(value):
    return None if value is None else float(value)


def store(con,records,source,stamp):
    """Both endpoints report dollars. The v1 API documents cents, but on Nov 23 2026
    the same startup came back with MRR 1840.63 from the public snapshot and the API."""
    seen=set()
    for r in records:
        slug=r.get('slug')
        if not slug or slug in seen or r.get('stealthMode'):
            continue
        seen.add(slug);revenue=r.get('revenue') or {}
        app_id,country,ratings=app_store_id(r)
        con.execute('''INSERT INTO startups VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET
            name=excluded.name,category=excluded.category,description=excluded.description,website=excluded.website,
            payment_provider=excluded.payment_provider,is_mobile=excluded.is_mobile,
            app_store_id=coalesce(excluded.app_store_id,startups.app_store_id),
            app_store_country=coalesce(excluded.app_store_country,startups.app_store_country),last_seen=excluded.last_seen''',
            (slug,r.get('name'),r.get('category'),r.get('description'),r.get('website'),r.get('paymentProvider'),
             int(bool(r.get('isMobileApp') or app_id)),app_id,country,stamp,stamp))
        con.execute('INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,?,?,?,?,?)',
            (slug,stamp,source,money(revenue.get('last30Days')),money(revenue.get('mrr')),r.get('growth30d'),
             r.get('customers'),int(bool(r.get('onSale'))),money(r.get('askingPrice')),ratings))
    con.commit()
    return len(seen)


KEYCHAIN_SERVICE='trustmrr-api'


def api_key():
    """The owner's key from TRUSTMRR_API_KEY or the macOS Keychain item 'trustmrr-api'.
    It is passed to TrustMRR only and never printed."""
    key=os.environ.get('TRUSTMRR_API_KEY')
    if key:
        return key.strip()
    if sys.platform!='darwin':
        return None
    import subprocess
    found=subprocess.run(['security','find-generic-password','-s',KEYCHAIN_SERVICE,'-w'],capture_output=True,text=True,check=False)
    if found.returncode!=0:
        return None
    return found.stdout.strip() or None


def collect(con,api_pages=0):
    stamp=iso(now());total=0
    for path,groups in PUBLIC_ENDPOINTS.items():
        data=get_json(f'https://trustmrr.com/{path}')
        for group in groups:
            total+=store(con,data.get(group) or [],f'public:{group}',stamp)
    key=api_key() if api_pages else None
    pages=0
    if key:
        # Standard keys allow 10 requests a minute and 10 startups a page.
        for page in range(1,api_pages+1):
            query=urllib.parse.urlencode({'category':'mobile-apps','page':page,'limit':10,'sort':'revenue-desc'})
            try:
                data=get_json(f'{API}?{query}',{'Authorization':f'Bearer {key}'})
            except urllib.error.HTTPError as error:
                print(f'TrustMRR API answered {error.code} on page {page}; public snapshot kept'+(' (key rejected)' if error.code in (401,403) else ''))
                break
            total+=store(con,data.get('data') or [],'api:mobile-apps',stamp);pages+=1
            if not (data.get('meta') or {}).get('hasMore'):
                break
            time.sleep(6.5)
    print(f'TrustMRR: {total} records stored at {stamp}; '+(f'API pages: {pages}' if key else 'public snapshot only, no API key'))
    return stamp


def term_candidates(name):
    """App titles put the brand and the job on either side of a separator:
    "DocPhoto: passport photo maker", "Volume Booster: Louder Sound"."""
    parts=[re.sub(r'[^\w\s]',' ',p).strip().lower() for p in re.split(r'\s*(?::|\s-\s|\s–\s|\|)\s*',name or '')]
    parts=[re.sub(r'\s+',' ',p) for p in parts if p]
    return list(dict.fromkeys(p for p in parts if len(p.split())>=2)) or parts[:1]


def months_since(value,today):
    if not value:
        return None
    stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
    return (today-stamp).days/30.44


def competition(results,today):
    """Who already answers this search, by public rating counts."""
    top=results[:10];counts=sorted((r.get('userRatingCount') or 0 for r in top),reverse=True)
    ages=[a for r in top if (a:=months_since(r.get('currentVersionReleaseDate'),today)) is not None]
    total=sum(counts)
    return dict(results=len(results),top10_ratings=total,leader_ratings=counts[0] if counts else 0,
                leader_share=round(counts[0]/total,2) if total else None,
                median_ratings=statistics.median(counts) if counts else 0,
                apps_over_1k=sum(c>=1000 for c in counts),
                stale_share=round(sum(a>=STALE_MONTHS for a in ages)/len(ages),2) if ages else None,
                paid_share=round(sum((r.get('price') or 0)>0 for r in top)/len(top),2) if top else None,
                leaders=[dict(id=r.get('trackId'),name=r.get('trackName'),ratings=r.get('userRatingCount') or 0,
                              price=r.get('price'),age_months=round(months_since(r.get('currentVersionReleaseDate'),today) or 0,1))
                         for r in top[:5]])


def ios_reviews(track_id,country,n=100,pages=2):
    """Recent 1-3 star App Store reviews. A missing feed answers 404 for good, so
    it is not retried (the shared fetcher spent about a minute on each, Nov 23)."""
    out=[]
    for page in range(1,pages+1):
        url=f'https://itunes.apple.com/{country}/rss/customerreviews/id={track_id}/sortby=mostrecent/page={page}/json'
        try:
            data=get_json(url)
        except urllib.error.HTTPError as error:
            if error.code in (400,404):
                break
            raise
        entries=(data.get('feed') or {}).get('entry') or []
        entries=[entries] if isinstance(entries,dict) else entries
        rated=[e for e in entries if 'im:rating' in e]
        out+=[dict(text=e.get('content',{}).get('label',''),score=int(e['im:rating']['label'])) for e in rated
              if int(e['im:rating']['label'])<=3]
        if not rated or len(out)>=n:
            break
        time.sleep(1)
    return out[:n]


def complaints(leaders,country,fetch,limit=3):
    """1-3 star themes across the niche leaders' recent App Store reviews."""
    import review_clusters
    bad=[]
    for leader in sorted(leaders,key=lambda l:-l['ratings'])[:limit]:
        if leader['ratings']<20:
            continue
        try:
            bad+=fetch(leader['id'],country,100)
        except Exception as error:
            print(f'  reviews of {leader["id"]}: {error}',file=sys.stderr)
    clusters,_=review_clusters.cluster([r for r in bad if r['score']<=2])
    return dict(bad_reviews=sum(r['score']<=2 for r in bad),
                themes=[dict(word=word,count=len(members),example=members[0]['text'][:160]) for word,members in clusters[:4]])


def due(con,overrides,today,max_age_days=ENRICH_MAX_AGE_DAYS,limit=None):
    """Apps whose niche is missing, older than a week or has a new search phrase,
    highest verified revenue first: about nine App Store requests each."""
    rows=con.execute('''SELECT s.* FROM startups s LEFT JOIN observations o ON o.slug=s.slug
        AND o.observed_at=(SELECT MAX(observed_at) FROM observations WHERE slug=s.slug)
        WHERE s.app_store_id IS NOT NULL GROUP BY s.slug ORDER BY MAX(coalesce(o.revenue_30d,0)) DESC,s.slug''').fetchall()
    out=[]
    for row in rows:
        old=con.execute('SELECT checked_at,term FROM niches WHERE slug=?',(row['slug'],)).fetchone()
        wanted=overrides.get(row['slug'])
        if old and (today-datetime.fromisoformat(old['checked_at'].replace('Z','+00:00'))).days<max_age_days and (not wanted or wanted==old['term']):
            continue
        out.append(row)
    return out[:limit] if limit else out


def enrich(con,niches_db=None,max_age_days=ENRICH_MAX_AGE_DAYS,limit=40):
    import scanner as sc
    overrides_path=PRIVATE_DIR/'terms.json'
    overrides=json.loads(overrides_path.read_text()) if overrides_path.exists() else {}
    known=None
    if niches_db and Path(niches_db).exists():
        known=sqlite3.connect(Path(niches_db).resolve().as_uri()+'?mode=ro',uri=True)
    today=now();done=0
    rows=due(con,overrides,today,max_age_days)
    if limit and len(rows)>limit:
        print(f'{len(rows)} niches are due; checking the {limit} with the highest revenue now, the rest on the next run')
        rows=rows[:limit]
    for row in rows:
        wanted=overrides.get(row['slug'])
        country=row['app_store_country'] or 'us'
        found=sc.fetch_json(f'https://itunes.apple.com/lookup?id={row["app_store_id"]}&country={country}').get('results') or []
        if not found:
            continue
        app=found[0];time.sleep(sc.REQUEST_DELAY)
        terms=[wanted] if wanted else term_candidates(app.get('trackName'))[:2]
        best=None
        for term in terms:
            results=sc.search_apps(term,country,50);time.sleep(sc.REQUEST_DELAY)
            profile=competition(results,today)
            if best is None or profile['top10_ratings']>best[1]['top10_ratings']:
                best=(term,profile)
        term,profile=best
        payload=dict(app=dict(name=app.get('trackName'),genre=app.get('primaryGenreName'),price=app.get('price'),
                              ratings=app.get('userRatingCount') or 0,released=app.get('releaseDate'),
                              updated=app.get('currentVersionReleaseDate'),url=app.get('trackViewUrl')),
                     competition=profile,complaints=complaints(profile['leaders'],country,ios_reviews))
        if known:
            seen=known.execute('SELECT COUNT(*),MAX(rating_count) FROM snapshots WHERE track_id=?',(row['app_store_id'],)).fetchone()
            payload['our_scanner']=dict(snapshots=seen[0],max_ratings=seen[1])
        con.execute('INSERT OR REPLACE INTO niches VALUES (?,?,?,?,?)',(row['slug'],iso(today),term,country,json.dumps(payload,ensure_ascii=False)))
        con.commit();done+=1
        print(f'  {row["name"]}: "{term}" in {country}, top-10 {profile["top10_ratings"]} ratings')
    print(f'App Store niches refreshed: {done}')


def latest(con):
    return [dict(r) for r in con.execute('''SELECT s.*,o.observed_at,o.revenue_30d,o.mrr,o.growth_30d,o.customers,o.on_sale,
        o.asking_price,n.term,n.payload_json FROM startups s
        JOIN observations o ON o.slug=s.slug AND o.observed_at=(SELECT MAX(observed_at) FROM observations WHERE slug=s.slug)
        LEFT JOIN niches n ON n.slug=s.slug GROUP BY s.slug ORDER BY o.revenue_30d DESC''')]


def spearman(xs,ys):
    def ranks(values):
        order=sorted(range(len(values)),key=lambda i:values[i]);out=[0]*len(values)
        for rank,i in enumerate(order):out[i]=rank
        return out
    if len(xs)<4:
        return None
    rx,ry=ranks(xs),ranks(ys);n=len(xs)
    return 1-6*sum((a-b)**2 for a,b in zip(rx,ry))/(n*(n*n-1))


def compact(value):
    for size,suffix in ((1e6,'M'),(1e3,'k')):
        if value>=size:
            return f'{value/size:g}{suffix}'
    return f'{value:g}'


def count(value):
    return '—' if value is None else f'{value:,.0f}'


def scatter(points):
    """Log-log ratings vs verified 30-day revenue, inline SVG."""
    if not points:
        return ''
    w,h,pad=640,320,44
    lx=lambda v:math.log10(v+1);xs=[lx(p[0]) for p in points];ys=[lx(p[1]) for p in points]
    x0,x1=0,max(xs+[1]);y0,y1=0,max(ys+[1])
    sx=lambda v:pad+(v-x0)/(x1-x0)*(w-2*pad);sy=lambda v:h-pad-(v-y0)/(y1-y0)*(h-2*pad)
    dots=''.join(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="5"><title>{html.escape(name)}: {r:,} оценок, ${v:,.0f}/30д</title></circle>'
                 for (r,v,name),x,y in zip(points,xs,ys))
    ticks=''.join(f'<text x="{sx(lx(10**t)):.0f}" y="{h-pad+18}" class="tick" text-anchor="middle">{compact(10**t)}</text>' for t in range(0,int(x1)+1))
    ticks+=''.join(f'<text x="{pad-8}" y="{sy(lx(10**t))+4:.0f}" class="tick" text-anchor="end">${compact(10**t)}</text>' for t in range(0,int(y1)+1))
    return (f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="Оценки и выручка">'
            f'<line x1="{pad}" y1="{h-pad}" x2="{w-pad}" y2="{h-pad}" class="axis"/><line x1="{pad}" y1="{pad}" x2="{pad}" y2="{h-pad}" class="axis"/>'
            f'{ticks}<text x="{w/2}" y="{h-6}" class="label" text-anchor="middle">оценок в App Store (лог.)</text>{dots}</svg>')


def report(con,path):
    rows=latest(con);mobile=[r for r in rows if r['app_store_id']];web=[r for r in rows if not r['app_store_id']]
    esc=lambda v:html.escape(str(v)) if v is not None else '—'
    usd=lambda v:'—' if v is None else f'${v:,.0f}'
    pct=lambda v:'—' if v is None else f'{v:+.0f}%'
    points=[];body=[]
    for r in mobile:
        p=json.loads(r['payload_json']) if r['payload_json'] else {}
        app=p.get('app') or {};c=p.get('competition') or {};pain=p.get('complaints') or {}
        ratings=app.get('ratings')
        per=(r['revenue_30d']/ratings) if ratings and r['revenue_30d'] else None
        if ratings and r['revenue_30d']:points.append((ratings,r['revenue_30d'],r['name']))
        themes='; '.join(f'<span title="{esc(t["example"])}">{esc(t["word"])} ×{t["count"]}</span>' for t in pain.get('themes') or [])
        body.append(f'''<tr><td><a href="{esc(app.get("url") or "")}">{esc(r["name"])}</a><div class="sub">{esc(r["category"])} · {esc(app.get("genre"))}</div></td>
<td data-v="{r["revenue_30d"] or 0}">{usd(r["revenue_30d"])}</td><td data-v="{r["mrr"] or 0}">{usd(r["mrr"])}</td>
<td data-v="{r["growth_30d"] or 0}">{pct(r["growth_30d"])}</td><td data-v="{ratings or 0}">{count(ratings)}</td>
<td data-v="{per or 0}">{"—" if per is None else f"${per:,.1f}"}</td><td>{esc(r["term"])}</td>
<td data-v="{c.get("leader_ratings",0)}">{count(c.get("leader_ratings"))}</td><td data-v="{c.get("median_ratings",0)}">{count(c.get("median_ratings"))}</td>
<td data-v="{c.get("apps_over_1k",0)}">{esc(c.get("apps_over_1k"))}</td><td data-v="{c.get("stale_share") or 0}">{esc(c.get("stale_share"))}</td>
<td class="pain">{themes or "—"}</td></tr>''')
    rho=spearman([p[0] for p in points],[p[1] for p in points])
    web_rows=''.join(f'<tr><td><a href="{esc(r["website"])}">{esc(r["name"])}</a><div class="sub">{esc((r["description"] or "")[:140])}</div></td>'
                     f'<td>{esc(r["category"])}</td><td data-v="{r["revenue_30d"] or 0}">{usd(r["revenue_30d"])}</td>'
                     f'<td data-v="{r["mrr"] or 0}">{usd(r["mrr"])}</td><td data-v="{r["growth_30d"] or 0}">{pct(r["growth_30d"])}</td></tr>' for r in web)
    observed=con.execute('SELECT MIN(observed_at),MAX(observed_at),COUNT(DISTINCT observed_at) FROM observations').fetchone()
    page=f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TrustMRR watch</title><style>
:root{{--bg:#fbfaf7;--fg:#1d1c1a;--muted:#6b6862;--line:#e4e1da;--accent:#2f6f5e;--card:#fff}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecebe7;--muted:#9a978f;--line:#2c2b29;--accent:#7cc4ad;--card:#1c1c1a}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,system-ui,sans-serif}}
main{{max-width:1280px;margin:0 auto;padding:24px 16px 64px}}h1{{font-size:26px;margin:0 0 4px}}h2{{font-size:18px;margin:36px 0 8px}}
.lead,.sub,.note{{color:var(--muted)}}.sub{{font-size:12px}}.note{{font-size:13px;max-width:80ch}}
.wrap{{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--card)}}
table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}th,td{{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}
th{{position:sticky;top:0;background:var(--card);cursor:pointer;font-size:12px;color:var(--muted);white-space:nowrap}}td[data-v]{{text-align:right;white-space:nowrap}}
a{{color:var(--accent);text-decoration:none}}.pain{{font-size:13px;min-width:220px}}.pain span{{border-bottom:1px dotted var(--muted)}}
.chart{{width:100%;max-width:680px;background:var(--card);border:1px solid var(--line);border-radius:10px}}.chart circle{{fill:var(--accent);opacity:.75}}
.axis{{stroke:var(--line)}}.tick,.label{{fill:var(--muted);font-size:11px}}
</style></head><body><main>
<h1>TrustMRR × App Store</h1>
<p class="lead">Подтверждённая выручка из TrustMRR рядом с конкуренцией и жалобами в App Store. Срезов: {observed[2]}, {esc((observed[0] or "")[:10])} — {esc((observed[1] or "")[:16].replace("T"," "))} UTC.</p>
<p class="note">Личный отчёт. По условиям TrustMRR (п. 9.2) эти данные нельзя публиковать и передавать третьим лицам; файл и база лежат вне репозитория. Выручка — из платёжки основателя, расхождение до 30% по их FAQ. Конкуренция — топ-10 поиска App Store по нише; «устаревшие» — версия ≥{STALE_MONTHS} мес. Жалобы — 1–2★ из свежих отзывов лидеров ниши (Trustpilot запрещает сбор).</p>
<h2>Мобильные приложения с подтверждённой выручкой — {len(mobile)}</h2>
<div class="wrap"><table class="sortable"><thead><tr><th>Приложение</th><th>Выручка 30д</th><th>MRR</th><th>Рост 30д</th><th>Оценок</th><th>$ на оценку</th><th>Ниша (поиск)</th><th>Лидер, оценок</th><th>Медиана топ-10</th><th>≥1k оценок</th><th>Доля устаревших</th><th>Жалобы лидеров</th></tr></thead><tbody>{"".join(body)}</tbody></table></div>
<h2>Оценки и деньги</h2>
<p class="note">Связь числа оценок с подтверждённой выручкой: Спирмен {"—" if rho is None else f"{rho:.2f}"} на {len(points)} приложениях. Наведите на точку, чтобы увидеть приложение.</p>
{scatter(points)}
<h2>Веб-стартапы — {len(web)}</h2>
<p class="note">Идеи для переноса в мобильное приложение. Чтобы проверить нишу, допишите поисковую фразу в {esc(PRIVATE_DIR/"terms.json")} как {{"slug": "фраза"}}.</p>
<div class="wrap"><table class="sortable"><thead><tr><th>Стартап</th><th>Категория</th><th>Выручка 30д</th><th>MRR</th><th>Рост 30д</th></tr></thead><tbody>{web_rows}</tbody></table></div>
</main><script>
document.querySelectorAll('table.sortable th').forEach((th,i)=>th.addEventListener('click',()=>{{
 const body=th.closest('table').tBodies[0],asc=th.dataset.asc!=='1';th.dataset.asc=asc?'1':'0';
 const key=r=>{{const c=r.cells[i];return c.dataset.v!==undefined?parseFloat(c.dataset.v):c.textContent.trim().toLowerCase()}};
 [...body.rows].sort((a,b)=>{{const x=key(a),y=key(b);return (x>y?1:x<y?-1:0)*(asc?1:-1)}}).forEach(r=>body.appendChild(r));
}}));
</script></body></html>'''
    path=private_path(path);path.write_text(page,encoding='utf-8')
    print(f'Report: {path}')
    return path


def main():
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('step',nargs='?',default='all',choices=['all','collect','enrich','report'])
    ap.add_argument('--db',default=str(PRIVATE_DIR/'trustmrr.db'))
    ap.add_argument('--out',default=str(PRIVATE_DIR/'trustmrr_report.html'))
    ap.add_argument('--niches-db',default=str(HERE/'niches.db'),help='read-only: which apps our scanner already sees')
    ap.add_argument('--api-pages',type=int,default=20,help='pages of the mobile-apps category when an API key is available')
    ap.add_argument('--enrich-limit',type=int,default=40,help='App Store niches checked per run, highest revenue first')
    args=ap.parse_args()
    con=open_db(args.db)
    if args.step in ('all','collect'):
        collect(con,args.api_pages)
    if args.step in ('all','enrich'):
        enrich(con,args.niches_db,limit=args.enrich_limit)
    if args.step in ('all','report'):
        report(con,args.out)


if __name__=='__main__':
    main()
