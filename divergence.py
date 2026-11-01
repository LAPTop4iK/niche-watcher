#!/usr/bin/env python3
"""
Divergence — динамика между прогонами: кросс-гео velocity и rank-дельты.

1) Кросс-гео divergence: одно приложение набирает оценки в гео A и стоит в гео B
   (та же языковая группа) = гипотеза для проверки; разные размеры рынков не доказывают geo-gap.
2) Rank-дельты: кто поднялся/упал в выдаче одного запроса между двумя последними
   прогонами (rank пишется scanner'ом с 31.10.2026).

Нужны сопоставимые наблюдения с интервалом ≥7 дней; календарная дата не гарантирует результат.

Использование:
    python3 divergence.py                 # оба анализа
    python3 divergence.py --min-ratings 300
"""

import argparse
import db_store
from pathlib import Path
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner as sc


def rank_movers(con,min_days=7):
    """Compare complete query responses, with a baseline at least min_days old.

    Missing baseline membership is unknown, rather than an invented low rank.
    Duplicate snapshots and same-day retries cannot become separate endpoints.
    """
    import evidence
    if not {'scan_runs','scan_queries'}<=evidence.tables(con):
        return []
    queries=defaultdict(list)
    for run,geo,cat,query,finished,settings in con.execute("""SELECT q.run_id,q.country,q.category,q.query,q.finished_at,r.settings_json
            FROM scan_queries q JOIN scan_runs r USING(run_id)
            WHERE q.status='OK' AND r.source='itunes-search' ORDER BY q.finished_at DESC"""):
        import json
        limit=json.loads(settings).get('limit')
        if limit is not None:
            queries[(geo,cat,query,limit)].append((run,evidence.parse(finished)))
    movers=[]
    for (geo,cat,query,limit),runs in queries.items():
        newest,stamp=runs[0]
        baseline=next((run for run,date in runs[1:] if (stamp-date).total_seconds()>=min_days*86400),None)
        if baseline is None:
            continue
        rows={}
        for run in (newest,baseline):
            rows[run]={r[0]:r[1:] for r in con.execute("""SELECT track_id,rank,app_name
                FROM snapshots WHERE run_id=? AND country=? AND category=? AND query=?
                AND rank IS NOT NULL""",(run,geo,cat,query))}
        for tid,(rank,name) in rows[newest].items():
            old=rows[baseline].get(tid)
            if old and old[0]>20 and rank<=20 and rank<old[0]:
                movers.append((old[0]-rank,geo,query,old[0],rank,name or str(tid),
                               f'https://apps.apple.com/{geo}/app/id{tid}'))
    return sorted(movers,reverse=True)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(here, "niches.db"))
    ap.add_argument("--markets", default=os.path.join(here, "markets.json"))
    ap.add_argument("--min-ratings", type=int, default=300)
    ap.add_argument("--min-days", type=float, default=7.0)
    ap.add_argument("--out", default=os.path.join(here, "docs", "divergence.md"))
    args = ap.parse_args()
    db_store.ensure(args.db)

    import json
    markets = json.load(open(args.markets, encoding="utf-8"))["markets"]
    lang_of = {g: v.get("lang", "en") for g, v in markets.items()}

    con = sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro',uri=True)
    import evidence
    by_app = defaultdict(dict)
    for r in evidence.latest(con):
        tid, country, rc = r["track_id"], r["country"], r["rating_count"]
        if rc is None or rc < args.min_ratings:
            continue
        vel, days = evidence.rating_velocity(con,tid,country,min_days=max(7,args.min_days))
        if vel is None:
            continue
        by_app[tid][country] = dict(rc_first=rc-vel*days/30,rc_last=rc,days=days,
            cat=r["category"],q=r["query"],name=r["name"],url=r["store_url"],last=r["scanned_at"])
    con.close()

    lines = [f"# Divergence — {datetime.now(timezone.utc):%Y-%m-%d}\n",
             "Разница прироста оценок между странами — наблюдение, не доказательство свободного рынка "
             "или миграции спроса. Окна могут различаться, страны имеют разный размер. "
             "Rank — позиция в ответе iTunes API, не проверенная позиция на устройстве. Baseline одного запроса — не менее 7 дней назад.\n"]

    # 1) кросс-гео divergence
    div = []
    for tid, geos in by_app.items():
        for gA, a in geos.items():
            if a["days"] < args.min_days or not a["rc_last"] or a["rc_last"] <= a["rc_first"]:
                continue
            vA = (a["rc_last"] - a["rc_first"]) / max(1, a["days"]) * 30.0
            for gB, b in geos.items():
                if gB == gA or lang_of.get(gA) != lang_of.get(gB) or b["days"] < args.min_days:
                    continue
                if abs((sc.parse_iso(a["last"]) - sc.parse_iso(b["last"])).total_seconds()) > 86400 or abs(a["days"]-b["days"]) > 1:
                    continue
                vB = (b["rc_last"] - b["rc_first"]) / max(1, b["days"]) * 30.0
                if 0 <= vB <= vA * 0.3:  # в B растёт меньше трети от A
                    div.append((vA, vB, gA, gB, a, b, tid))
    div.sort(key=lambda x: -(x[0] - x[1]))
    lines.append("## Кросс-гео divergence (растёт в A, стоит в B, same lang)\n")
    lines.append("| Рост A/30д | Стояние B/30д | A→B | Приложение | Категория |")
    lines.append("|---|---|---|---|---|")
    if div:
        for vA, vB, gA, gB, a, b, tid in div[:15]:
            lines.append(f"| +{vA:.0f} | {vB:+.0f} | {gA}→{gB} | "
                         f"[{a['name'][:34]}]({a['url']}) | {a['cat']} |")
    else:
        lines.append("_Пока пусто: нужны пригодные серии по странам с интервалом ≥7 дней. Дата появления результата не гарантирована._")

    # 2) rank-дельты между двумя последними прогонами одного запроса
    con = sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro',uri=True)
    movers=rank_movers(con,max(7,args.min_days))
    con.close()
    lines.append("\n## Rank-дельты (ворвались в топ-20 выдачи)\n")
    lines.append("| Гео | Запрос | Было | Стало | Приложение |")
    lines.append("|---|---|---|---|---|")
    if movers:
        for d, country, q, r_old, r_new, name, url in movers[:15]:
            lines.append(f"| {country} | {q} | {r_old} | **{r_new}** | [{name[:34]}]({url}) |")
    else:
        lines.append("_Нет сопоставимых пересечений топ-20: нужны успешные ответы одного запроса с интервалом ≥7 дней._")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"divergence: {len(div)} кросс-гео, {len(movers)} rank-движений → {args.out}")


if __name__ == "__main__":
    main()
