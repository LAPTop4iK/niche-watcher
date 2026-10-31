#!/usr/bin/env python3
"""
Geo Gap — региональный арбитраж: лидер есть в одном рынке, аналога нет в другом.

Гипотеза: популярное в одном гео приложение может не иметь аналога в другом гео
той же языковой группы, при том что спрос там существует. Наша база сканов
24 гео позволяет это измерить: для каждого запроса сравниваем «силу» лучшего
местного приложения по странам одной языковой группы.

Сигнал: в гео A лучший ответ на запрос имеет 2000+ оценок, а в гео B (same lang)
лучший — меньше 300. Это вакуум: спрос формируется (запрос ищут), предложения нет.

Использование:
    python3 geo_gap.py                        # все категории, текущая база
    python3 geo_gap.py --min-rich 3000 --max-poor 500
"""

import argparse
import json
import os
import sqlite3
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))


STOP = set("""for with and the your app my pro free plus hd lite best simple easy all one new
мобильный приложение""".split())


def tokens(text):
    return {w for w in __import__("re").split(r"[^a-zа-яёäöüß0-9]+", text.lower()) if len(w) > 2 and w not in STOP}


def relevant(name, query):
    """Ложные корреляции: iTunes-поиск втягивает гигантов в длинные запросы
    (Capital One Mobile по «frost dates»). Лидер ниши обязан делить токен с запросом."""
    return bool(tokens(name) & tokens(query))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(HERE, "niches.db"))
    ap.add_argument("--markets", default=os.path.join(HERE, "markets.json"))
    ap.add_argument("--min-rich", type=int, default=2000, help="оценок у лидера в богатом гео")
    ap.add_argument("--max-poor", type=int, default=300, help="лучший результат в гео-вакууме")
    args = ap.parse_args()

    markets = json.load(open(args.markets, encoding="utf-8"))["markets"]
    lang_of = {g: v["lang"] for g, v in markets.items()}

    con = sqlite3.connect(args.db)
    rows = con.execute(
        """SELECT s.category, s.query, s.country, MAX(s.rating_count), a.name, a.store_url
           FROM snapshots s JOIN apps a ON a.track_id = s.track_id
           GROUP BY s.category, s.query, s.country"""
    ).fetchall()
    con.close()

    # (category, query, lang) -> {country: (max_ratings, app_name, url)}
    grid = defaultdict(dict)
    for cat, q, country, rc, name, url in rows:
        lang = lang_of.get(country)
        if not lang or not q or not relevant(name, q):
            continue
        key = (cat, q, lang)
        if country not in grid[key] or rc > grid[key][country][0]:
            grid[key][country] = (rc or 0, name, url)

    lines = [
        "# Geo Gap — региональный арбитраж\n",
        f"Пороги: лидер >= {args.min_rich} оценок, вакуум <= {args.max_poor} у лучшего в стране.\n"
        "Сравнение внутри языковых групп (en/de/ja/...): спрос переносим, нужен локальный аналог.\n",
        "| Категория | Запрос | Lang | Лидер | Оценок | Гео-вакуумы (лучший там) |",
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

    out = os.path.join(HERE, "geo_gap_report.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"geo-gap кандидатов: {found} → {out}")
    for ln in lines[5:15]:
        print(ln)


if __name__ == "__main__":
    main()
