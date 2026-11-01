#!/usr/bin/env python3
"""
Geo Gap — различия локальных оценок: лидер есть в одном рынке, аналога нет в другом.

Гипотеза: популярное в одном гео приложение может не иметь аналога в другом гео
той же языковой группы, при том что спрос там существует. Наша база сканов
24 гео позволяет это измерить: для каждого запроса сравниваем «силу» лучшего
местного приложения по странам одной языковой группы.

Наблюдение: в гео A лучший ответ на запрос имеет 2000+ оценок, а в гео B (same lang)
в топ-10 всё слабее 300 оценок. Сравнение не засчитывается, если лидер сам есть в
выдаче гео B: это то же приложение на меньшем рынке. Счётчики делятся на масштаб
страны, запросы на чужом языке не сравниваются. Объём поиска и спрос не измерены.

Использование:
    python3 geo_gap.py                        # все категории, текущая база
    python3 geo_gap.py --min-rich 3000 --max-poor 500
"""

import argparse
import db_store
import json
import os
from pathlib import Path
import sqlite3
import statistics
from collections import defaultdict
from relevance import relevant

HERE = os.path.dirname(os.path.abspath(__file__))
GAP_RATIO = 1/3  # Масштабированный счётчик ниже этой доли от лидера считается разрывом.


def localized(con, rows, seed, lang_of):
    """Отсеять запросы на чужом языке: страны сравнимы только на языке рынка."""
    frames = {}
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='query_frames'").fetchone():
        frames = {(r[0], r[1], r[2]): r[3] for r in con.execute("SELECT country,category,query,query_lang FROM query_frames")}
    queries = {c["id"]: c["queries"] for c in seed["categories"]}
    for row in rows:
        _, geo, cat, query = row[:4]
        market = lang_of.get(geo)
        known = frames.get((geo, cat, query))
        if market and (known == market if known else query in (queries.get(cat, {}).get(market) or [])):
            yield row


def find_gaps(rows, lang_of, min_rich=2000, max_poor=300):
    """rows: (track_id, country, category, query, rank, rating_count, name)."""
    grid = defaultdict(lambda: defaultdict(list))
    for row in rows:
        _, geo, cat, query, *_ = row
        grid[(cat, query, lang_of[geo])][geo].append(row)
    # Один и тот же лидер на большом рынке набирает больше оценок: масштаб страны
    # — медиана лучшего счётчика её запросов, из наших же наблюдений.
    strongest = defaultdict(list)
    for by_geo in grid.values():
        for geo, hits in by_geo.items():
            strongest[geo].append(max((h[5] or 0) for h in hits))
    scale = {geo: max(1, statistics.median(values)) for geo, values in strongest.items()}
    gaps = []
    for (cat, query, lang), by_geo in grid.items():
        leaders = [h for hits in by_geo.values() for h in hits if (h[5] or 0) >= min_rich and relevant(h[6] or "", query)]
        if not leaders:
            continue
        leader = max(leaders, key=lambda h: h[5])
        poor = {}
        for geo, hits in by_geo.items():
            # Заголовочный фильтр скрывает конкурентов, поэтому здесь весь топ-10.
            local = max(((h[5] or 0) for h in hits if h[4] <= 10), default=0)
            if geo == leader[1] or local >= max_poor or any(h[0] == leader[0] for h in hits):
                continue
            if local / scale[geo] < GAP_RATIO * leader[5] / scale[leader[1]]:
                poor[geo] = local
        if poor:
            gaps.append(dict(category=cat, query=query, lang=lang, leader=leader, poor=poor, scale=scale))
    return sorted(gaps, key=lambda g: -g["leader"][5])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(HERE, "niches.db"))
    ap.add_argument("--markets", default=os.path.join(HERE, "markets.json"))
    ap.add_argument("--seed", default=os.path.join(HERE, "seed_queries.json"))
    ap.add_argument("--min-rich", type=int, default=2000, help="оценок у лидера в богатом гео")
    ap.add_argument("--max-poor", type=int, default=300, help="потолок топ-10 в гео-вакууме")
    ap.add_argument("--out", default=os.path.join(HERE,"geo_gap_report.md"))
    args = ap.parse_args()
    db_store.ensure(args.db)

    markets = json.load(open(args.markets, encoding="utf-8"))["markets"]
    seed = json.load(open(args.seed, encoding="utf-8"))
    lang_of = {g: v["lang"] for g, v in markets.items()}

    import evidence
    con=sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro',uri=True)
    con.execute('BEGIN')
    rows=list(localized(con,evidence.current_serp(con),seed,lang_of))
    con.close()
    gaps = find_gaps(rows, lang_of, args.min_rich, args.max_poor)

    lines = [
        "# Geo Gap — различия локальных оценок\n",
        f"Пороги: лидер >= {args.min_rich} оценок с совпадением слов в названии, весь топ-10 другой страны < {args.max_poor}. "
        f"Лидер не встречается в выдаче этой страны, а её счётчик с поправкой на масштаб рынка ниже {GAP_RATIO:.0%} от лидера. Запросы на чужом языке исключены.\n",
        "Различия оценок не измеряют спрос: проверьте полноту скана, альтернативы и поисковый трафик.\n",
        "| Категория | Запрос | Lang | Лидер | Оценок | Масштаб рынка лидера | Малый счётчик топ-10 (страна, оценок, масштаб) |",
        "|---|---|---|---|---:|---:|---|",
    ]
    for gap in gaps:
        tid, geo, *_, count, name = gap["leader"]
        poor = ", ".join(f"{g} ({v}, {gap['scale'][g]:g})" for g, v in sorted(gap["poor"].items(), key=lambda kv: kv[1]))
        title = (name or "?")[:30].replace("|", "/")
        lines.append(f"| {gap['category']} | {gap['query']} | {gap['lang']} | [{title}](https://apps.apple.com/{geo}/app/id{tid}) [{geo}] | {count:,} | {gap['scale'][geo]:g} | {poor} |")

    out = args.out
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"geo-gap кандидатов: {len(gaps)} → {out}")
    for ln in lines[5:15]:
        print(ln)


if __name__ == "__main__":
    main()
