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
import db_store
import json
import os
from pathlib import Path
import evidence
import unicodedata
import re
import sqlite3
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))

STOP = set("""for with and the your app my pro free plus hd lite best simple easy all one new
timer time app calculator calculators конвертер калькулятор приложение для""".split())


def tokens(text):
    return [w for w in re.findall(r'[^\W_]+',text.lower(),re.UNICODE) if len(w)>2 and w not in STOP]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(HERE, "niches.db"))
    ap.add_argument("--seed", default=os.path.join(HERE, "seed_queries.json"))
    ap.add_argument("--min-count", type=int, default=4)
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()
    db_store.ensure(args.db)

    seed = json.loads(Path(args.seed).read_text(encoding='utf-8'))
    existing = set()
    for cat in seed["categories"]:
        for lang_qs in cat["queries"].values():
            for q in lang_qs:
                existing.add(" ".join(sorted(tokens(q))))

    con=sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro',uri=True)
    con.execute('BEGIN')
    rows=evidence.latest(con,by_category=True)
    con.close()
    by_cat={};seen=set()
    for row in rows:
        cat,name=row['category'],row['name']
        if cat.startswith('chart:') or cat=='similar-graph':
            continue
        key=(row['track_id'],cat)
        if key in seen:
            continue
        seen.add(key)
        toks=tokens(name);grams=set()
        for i in range(len(toks)-1):
            bigram=f'{toks[i]} {toks[i+1]}'
            if ' '.join(sorted(toks[i:i+2])) not in existing:
                grams.add(bigram)
        by_cat.setdefault(cat,Counter()).update(grams)

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
