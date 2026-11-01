#!/usr/bin/env python3
"""Read-only audit. Reproduce baseline and revised classification separately."""
import argparse
import db_store
from collections import Counter
import json
from pathlib import Path
import sqlite3
import evidence


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--db',default='niches.db')
    ap.add_argument('--out',default='docs/audit_metrics.json')
    args=ap.parse_args()
    db_store.ensure(args.db)
    with sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row
        cached=evidence.demand_map(c)
        counts=Counter();transitions=Counter()
        candidates=[r for r in evidence.latest(c) if evidence.is_candidate(r)]
        for r in candidates:
            d=cached.get((r['track_id'],r['country']),{})
            a=evidence.assess(c,r,d)
            counts[a['verdict']]+=1
            transitions[d.get('verdict','MISSING')+' → '+a['verdict']]+=1
        data=dict(method=evidence.METHOD,candidate_storefronts=len(candidates),counts=counts,transitions=transitions,
                  apps=c.execute('SELECT COUNT(*) FROM apps').fetchone()[0],
                  snapshots=c.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0],
                  coverage=[dict(r) for r in c.execute('SELECT country,COUNT(DISTINCT track_id) apps FROM snapshots GROUP BY country')],
                  history=dict(c.execute('SELECT MIN(scanned_at) first,MAX(scanned_at) last FROM snapshots').fetchone()),
                  histories_7d=c.execute('''SELECT COUNT(*) FROM (SELECT track_id,country FROM snapshots GROUP BY track_id,country
                    HAVING julianday(MAX(scanned_at))-julianday(MIN(scanned_at))>=7)''').fetchone()[0],
                  adjacent_counter_drops=c.execute('''WITH t AS (SELECT rating_count,LAG(rating_count) OVER
                    (PARTITION BY track_id,country ORDER BY scanned_at,id) previous FROM snapshots)
                    SELECT COUNT(*) FROM t WHERE rating_count<previous''').fetchone()[0])
        queue,review_coverage=evidence.research_queue(c)
        data['research_lanes']=Counter(r['lane'] for r in queue)
        data['recent_version_review_coverage']=review_coverage
    Path(args.out).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(data,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
