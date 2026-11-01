#!/usr/bin/env python3
"""Research queue by category/storefront. No unvalidated profit score."""
import argparse
import db_store
from pathlib import Path
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
import evidence

HERE = os.path.dirname(os.path.abspath(__file__))


def build(con, min_n=8):
    con.row_factory = sqlite3.Row
    cached = evidence.demand_map(con)
    groups = defaultdict(lambda: dict(n=0, stale=0, alive=0, weak=0, unknown=0, no_recent=0, qg=0))
    for r in evidence.latest(con, by_category=True):
        cat = r['category']
        if cat.startswith('chart:') or cat == 'similar-graph':
            continue  # different sampling frames are never ranked as niches
        a = groups[(cat, r['country'])]
        a['n'] += 1
        if (r['rating_count'] or 0) >= 1000 and 0 < (r['avg_rating'] or 0) < 4:
            a['qg'] += 1  # historical dissatisfaction, not a paid demand estimate
        if evidence.is_candidate(r):
            a['stale'] += 1
            state = evidence.assess(con, r, cached.get((r['track_id'],r['country'])))['verdict']
            a[{'ALIVE':'alive','WEAK':'weak','UNKNOWN':'unknown','NO_RECENT_EVIDENCE':'no_recent'}[state]] += 1
    return sorted(((cat,geo,a) for (cat,geo),a in groups.items() if a['n']>=min_n),
                  key=lambda r: (-r[2]['alive'], -r[2]['weak'], r[0], r[1]))


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--db',default=os.path.join(HERE,'niches.db'))
    ap.add_argument('--out',default=os.path.join(HERE,'docs','scoreboard.md'))
    ap.add_argument('--min-n',type=int,default=8)
    ap.add_argument('--top',type=int,default=25)
    args=ap.parse_args()
    db_store.ensure(args.db)
    with sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro',uri=True) as con:
        groups=build(con,args.min_n)
    lines=[f'# Очередь исследования — {datetime.now(timezone.utc):%Y-%m-%d}',
           '\nПорядок по числу кандидатов со свежей активностью, затем слабых сигналов. '
           'Это не вероятность успеха и не рейтинг доходности. n — приложения в выдаче, '
           'а не случайная выборка рынка. Старые данные и ошибки наблюдений сохранены как неизвестные. '
           '«Новые в базе» не считаются новыми конкурентами. Страны не сравниваются по выдуманному LTV.\n',
           '| Категория | Гео | n | Старые версии ≥500 оценок | Активность | Слабый сигнал | Нет свежих сигналов | Неизвестно | Рейтинг <4 при ≥1000 оценок |',
           '|---|---|---|---|---|---|---|---|---|']
    for cat,geo,a in groups[:args.top]:
        lines.append(f"| {cat} | {geo} | {a['n']} | {a['stale']} | {a['alive']} | {a['weak']} | {a['no_recent']} | {a['unknown']} | {a['qg']} |")
    os.makedirs(os.path.dirname(os.path.abspath(args.out)),exist_ok=True)
    with open(args.out,'w',encoding='utf-8') as f:
        f.write('\n'.join(lines)+'\n')
    print('\n'.join(lines[:15]))


if __name__=='__main__':
    main()
