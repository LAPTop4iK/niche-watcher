#!/usr/bin/env python3
"""Build a self-contained evidence dashboard; all figures come from SQLite."""
import argparse
import db_store
from collections import Counter
from datetime import datetime,timezone
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import evidence
import scoreboard
import quality_metrics

HERE=Path(__file__).resolve().parent


def build_data(db):
    db_store.ensure(db)
    with sqlite3.connect(Path(db).resolve().as_uri()+'?mode=ro',uri=True) as con:
        con.row_factory=sqlite3.Row
        con.execute("BEGIN")
        latest=evidence.latest(con)
        candidates,research_coverage=evidence.research_queue(con)
        source_coverage={kind:set() for kind in ('search','charts','links','all')}
        for r in latest:
            r['sources']=sorted({'charts' if c.startswith('chart:') else 'links' if c=='similar-graph' else 'search' for c in r['categories']})
            for kind in [*r['sources'],'all']:
                source_coverage[kind].add(r['country'])
        coverage=[dict(r) for r in con.execute('SELECT country,COUNT(DISTINCT track_id) apps,MAX(scanned_at) last_seen FROM snapshots GROUP BY country')]
        napps=con.execute('SELECT COUNT(*) FROM apps').fetchone()[0]
        observations=con.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0]
        bounds=con.execute('SELECT MIN(scanned_at),MAX(scanned_at) FROM snapshots').fetchone()
        groups=[dict(category=cat,country=geo,**a) for cat,geo,a in scoreboard.build(con)]
        runs=evidence.coverage(con)
        quality=quality_metrics.build(con,rows=latest,queue=candidates,coverage=research_coverage)
    markets=json.loads((HERE/'markets.json').read_text())['markets']
    seen={r['country'] for r in coverage}
    seed=json.loads((HERE/'seed_queries.json').read_text())['categories']
    directions=dict(configured=len(seed),enabled=sum(c.get('enabled',True) for c in seed),
        observed=sorted({c for r in latest for c in r['categories'] if not c.startswith('chart:') and c!='similar-graph'}),
        note='Поисковые направления в нашем конфиге; не полный перечень категорий App Store.')
    return dict(candidates=candidates,counts=Counter(r['verdict'] for r in candidates),labels=evidence.LABELS,
                lanes=evidence.LANES,research_coverage=research_coverage,directions=directions,quality=quality,
                source_coverage={kind:sorted(geos) for kind,geos in source_coverage.items()},coverage=coverage,missing=sorted(set(markets)-seen),napps=napps,observations=observations,
                first=bounds[0],last=bounds[1],groups=groups,runs=runs,method=evidence.METHOD,
                built=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--db',default=str(HERE/'niches.db'))
    ap.add_argument('--out-dir',default=str(HERE/'dashboard'))
    ap.add_argument('--repo',default='LAPTop4iK/niche-watcher')
    args=ap.parse_args()
    data=build_data(args.db)
    data['repo']=args.repo
    source=HERE/"docs"/"hypotheses.json"
    data["hypotheses"]=json.loads(source.read_text()) if source.exists() else None
    # JSON is embedded as text; escape markup before crossing the HTML boundary.
    payload=json.dumps(data,ensure_ascii=False).replace('&','\\u0026').replace('<','\\u003c').replace('>','\\u003e')
    output=Path(args.out_dir); output.mkdir(parents=True,exist_ok=True)
    template=(HERE/'dashboard_template.html').read_text()
    (output/"style.css").write_text((HERE/"dashboard.css").read_text())
    (output/"app.js").write_text((HERE/"dashboard.js").read_text())
    (output/'index.html').write_text(template.replace('__DATA__',payload).replace('__ASSET_VERSION__',hashlib.sha256(((HERE/'dashboard.js').read_bytes()+(HERE/'dashboard.css').read_bytes())).hexdigest()[:12]))
    (output/'data.json').write_text(json.dumps(data,ensure_ascii=False,indent=2))
    print(f'dashboard → {output}; {len(data["candidates"])} app/storefront candidates; {dict(data["counts"])}')


if __name__=='__main__':
    main()
