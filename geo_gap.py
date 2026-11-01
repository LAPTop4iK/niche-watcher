#!/usr/bin/env python3
"""
Geo Gap — различия локальных оценок: лидер есть в одном рынке, аналога нет в другом.

Гипотеза: популярное в одном гео приложение может не иметь аналога в другом гео
той же языковой группы, при том что спрос там существует. Наша база сканов
24 гео позволяет это измерить: для каждого запроса сравниваем «силу» лучшего
местного приложения по странам одной языковой группы.

Наблюдение: в гео A лучший ответ на запрос имеет 2000+ оценок, а в гео B (same lang)
лучший — меньше 300. Это различие накопленных локальных оценок; объём поиска и наличие спроса не измерены.

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
from collections import defaultdict
from relevance import tokens,relevant

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(HERE, "niches.db"))
    ap.add_argument("--markets", default=os.path.join(HERE, "markets.json"))
    ap.add_argument("--min-rich", type=int, default=2000, help="оценок у лидера в богатом гео")
    ap.add_argument("--max-poor", type=int, default=300, help="лучший результат в гео-вакууме")
    ap.add_argument("--out", default=os.path.join(HERE,"geo_gap_report.md"))
    args = ap.parse_args()
    db_store.ensure(args.db)

    markets = json.load(open(args.markets, encoding="utf-8"))["markets"]
    lang_of = {g: v["lang"] for g, v in markets.items()}

    import evidence
    con=sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro',uri=True)
    con.execute('BEGIN')
    memberships=evidence.query_memberships(con,all_queries=True)
    observations={(r['track_id'],r['country']):r for r in evidence.latest(con)}
    # Preserve each topical query membership; select maxima AFTER the title hint.
    rows=[]
    for (tid,geo),categories in memberships.items():
        r=observations[(tid,geo)]
        for cat,queries in categories.items():
            if cat.startswith('chart:') or cat=='similar-graph':
                continue
            for query in queries:
                rows.append((cat,query,geo,r['rating_count'],r['name'],tid))
    con.close()

    # (category, query, lang) -> {country: (max_ratings, app_name, url)}
    grid = defaultdict(dict)
    for cat, q, country, rc, name, tid in rows:
        url = f"https://apps.apple.com/{country}/app/id{tid}"
        lang = lang_of.get(country)
        if not lang or not q or not relevant(name, q):
            continue
        key = (cat, q, lang)
        if country not in grid[key] or rc > grid[key][country][0]:
            grid[key][country] = (rc or 0, name, url)

    lines = [
        "# Geo Gap — различия локальных оценок\n",
        f"Пороги: лидер >= {args.min_rich} оценок, низкий счётчик < {args.max_poor} у лучшего результата с совпадением значимых слов в названии. Это подсказка, не семантическая проверка.\n"
        "Различия оценок не измеряют спрос: проверьте размер страны, полноту скана, язык, альтернативы и поисковый трафик.\n",
        "| Категория | Запрос | Lang | Лидер | Оценок | Малый локальный счётчик (не доказанный гэп) |",
        "|---|---|---|---|---|---|",
    ]
    found = 0
    for (cat, q, lang), by_geo in sorted(grid.items(), key=lambda kv: -max(v[0] for v in kv[1].values())):
        rich = {g: v for g, v in by_geo.items() if v[0] >= args.min_rich}
        if not rich:
            continue
        poor = {g: v for g, v in by_geo.items() if v[0] < args.max_poor}
        if not poor:
            continue
        found += 1
        rg, rv = max(rich.items(), key=lambda kv: kv[1][0])
        poor_s = ", ".join(f"{g} ({v[0]})" for g, v in sorted(poor.items(), key=lambda kv: kv[1][0]))
        lines.append(f"| {cat} | {q} | {lang} | [{rv[1][:30]}]({rv[2]}) [{rg}] | {rv[0]:,} | {poor_s} |")

    out = args.out
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"geo-gap кандидатов: {found} → {out}")
    for ln in lines[5:15]:
        print(ln)


if __name__ == "__main__":
    main()
