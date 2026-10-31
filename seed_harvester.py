#!/usr/bin/env python3
"""
Seed Harvester — добыча новых запросов-ниш из собственной базы сканера.

Идея: 3 800+ названий приложений, собранных сканером, — это карта лексики ниш.
Частотные биграммы, которых ещё нет в seed_queries.json — кандидаты в новые
запросы (новые категории/под-ниши). Так система расширяет сама себя без
платных сервисов (их публичные страницы закрыты Cloudflare — проверено).

Использование:
    python3 seed_harvester.py                 # топ биграмм по категориям
    python3 seed_harvester.py --min-count 5   # порог частоты
"""

import argparse
import json
import os
import re
import sqlite3
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))

STOP = set("""for with and the your app my pro free plus hd lite best simple easy all one new
timer time app calculator calculators конвертер калькулятор приложение для""".split())


def tokens(text):
    return [w for w in re.split(r"[^a-zа-яёäöüß0-9]+", text.lower()) if len(w) > 2 and w not in STOP]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(HERE, "niches.db"))
    ap.add_argument("--seed", default=os.path.join(HERE, "seed_queries.json"))
    ap.add_argument("--min-count", type=int, default=4)
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()

    seed = json.load(open(args.seed, encoding="utf-8"))
    existing = set()
    for cat in seed["categories"]:
        for lang_qs in cat["queries"].values():
            for q in lang_qs:
                existing.add(" ".join(sorted(tokens(q))))

    con = sqlite3.connect(args.db)
    rows = con.execute(
        """SELECT s.category, a.name FROM apps a
           JOIN snapshots s ON a.track_id = s.track_id
           GROUP BY a.track_id"""
    ).fetchall()
    con.close()

    by_cat = {}
    for cat, name in rows:
        toks = tokens(name)
        for i in range(len(toks) - 1):
            bigram = f"{toks[i]} {toks[i+1]}"
            key = " ".join(sorted(toks[i : i + 2]))
            if key in existing:
                continue
            by_cat.setdefault(cat, Counter())[bigram] += 1

    print("# Кандидаты в seed (частотные биграммы, которых нет в seed_queries.json)\n")
    for cat, counter in sorted(by_cat.items()):
        top = [(b, c) for b, c in counter.most_common(30) if c >= args.min_count][: args.top]
        if not top:
            continue
        print(f"## {cat}")
        for b, c in top:
            print(f"  - {b} (x{c})")
        print()


if __name__ == "__main__":
    main()
