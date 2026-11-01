#!/usr/bin/env python3
"""
Play Reviews — майнинг жалоб (feature-gap raid) по живым лидерам Google Play.

Что делает: берёт топ Play-приложений из niches.db (или конкретные appId),
тянет свежие отзывы, считает долю 1-2★ и самые частые биграммы жалоб —
это гипотезы боли для ручной проверки; выручка не измеряется.

Использование:
    python3 play_reviews.py com.whatsapp --n 300
    python3 play_reviews.py --from-db 5 --country us    # топ-5 Play-зомби из базы
"""

import argparse
import db_store
import os
import re
import sys
import time
import sqlite3
from pathlib import Path
from datetime import datetime,timezone
import unicodedata
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner as sc

def gp_app(*args,**kwargs):
    from google_play_scraper import app
    return app(*args,**kwargs)


def fetch_play_reviews(app_id,country,lang,n):
    from google_play_scraper import reviews,Sort
    return reviews(app_id,lang=lang,country=country,count=n,sort=Sort.NEWEST)[0]


STOP = set("""the a an and or but is are was were to of in on for with this that it its i you my
me we they he she at as be by from has have had not no do does did can will would should could
приложение это для что так но на как я мне не и в у с по от да же бы очень все всё app очень
все нужно нужно нужна нужен был была были чтобы если когда уже там тут вот только можно
спасибо отлично хорошо крутой лучшая лучший хорошее отличное""".split())


def tokens(text):
    return [w for w in re.findall(r'[^\W\d_]+',unicodedata.normalize('NFKC',text).casefold(),re.UNICODE) if len(w)>2 and w not in STOP]


def analyze_app(app_id, country, lang, n,days=90):
    meta = gp_app(app_id, lang=lang, country=country)
    fetched=fetch_play_reviews(app_id,country,lang,n)
    result=[];seen=set();now=datetime.now(timezone.utc)
    for row in fetched:
        stamp=row.get('at')
        if not isinstance(stamp,datetime):
            continue
        # The library returns datetime.fromtimestamp: naive values are host local.
        age=(now-stamp.astimezone(timezone.utc)).total_seconds()
        identity=row.get('reviewId') or (str(stamp),row.get('content'))
        if not 0<=age<=days*86400 or identity in seen:
            continue
        seen.add(identity);result.append(row)
    if not result:
        return None
    bad = [r for r in result if r["score"] <= 2]
    good_share = 100.0 * sum(1 for r in result if r["score"] >= 4) / len(result)
    grams = Counter()
    for r in bad:
        t = tokens(r["content"])
        terms=set(t)|{f'{left} {right}' for left,right in zip(t,t[1:])}
        grams.update(terms)
    return {
        "app_id": app_id,
        "title": meta.get("title"),
        "installs": meta.get("installs"),
        "score": meta.get("score"),
        "n_reviews": len(result),
        "bad_share": 100.0 * len(bad) / len(result),
        "good_share": good_share,
        "top_complaints": [f"{g} (x{c})" for g, c in grams.most_common(12)],
        "worst": sorted(bad, key=lambda r: -r["thumbsUpCount"] if r.get("thumbsUpCount") else 0)[:5],
    }


def top_play_zombies(db, k, country):
    db_store.ensure(db)
    con=sqlite3.connect(Path(db).resolve().as_uri()+'?mode=ro',uri=True)
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='play_snapshots'").fetchone():
        con.close();return []
    rows = con.execute(
        """WITH ranked AS (
            SELECT *,ROW_NUMBER() OVER (PARTITION BY app_id,country ORDER BY scanned_at DESC,id DESC) rn
            FROM play_snapshots)
            SELECT app_id,ratings_count FROM ranked WHERE rn=1 AND country=?
              AND stale_days>=547.92 AND ratings_count>=500 ORDER BY ratings_count DESC LIMIT ?""",
        (country, k),
    ).fetchall()
    con.close()
    return [r[0] for r in rows]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("app_ids", nargs="*", help="appId appId ...")
    ap.add_argument("--from-db", type=int, default=0, metavar="K", help="топ-K Play-зомби из niches.db")
    ap.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "niches.db"))
    ap.add_argument("--country", default="us")
    ap.add_argument("--n", type=int, default=300, help="сколько свежих отзывов тянуть на приложение")
    ap.add_argument('--days',type=int,default=90)
    args = ap.parse_args()
    if args.days<1 or not 1<=args.n<=1000:
        ap.error('days must be positive; n must be 1..1000')
    db_store.ensure(args.db)

    ids = list(args.app_ids)
    if args.from_db:
        ids += top_play_zombies(args.db, args.from_db, args.country)
    if not ids:
        sys.exit("нет приложений: передай appId или --from-db (сначала нужен play_scan)")

    for app_id in ids:
        lang = {"us": "en", "gb": "en", "de": "de", "ru": "ru", "jp": "ja", "kr": "ko"}.get(args.country, "en")
        try:
            r = analyze_app(app_id, args.country, lang, args.n,args.days)
        except Exception as e:  # noqa: BLE001
            print(f"! {app_id}: {e}", file=sys.stderr)
            continue
        if not r:
            print(f"! {app_id}: нет пригодных отзывов в выбранном окне")
            continue
        print(f"\n=== {r['title']} [{args.country}] ({app_id}) ===")
        print(f"установки: {r['installs']} | рейтинг: {r['score']} | "
              f"из {r['n_reviews']} доступных за {args.days} дней: 1-2★ = {r['bad_share']:.0f}%")
        print("Ограниченная выборка, не доля всех пользователей и не выручка. Словарные группы требуют ручного чтения.")
        print("слова/фразы по числу отзывов:", ", ".join(r["top_complaints"][:10]))
        print("самые поддержанные 1-2★:")
        for w in r["worst"]:
            print(f"  [{w['score']}★, 👍{w.get('thumbsUpCount', 0)}] {w['content'][:150]}")
        time.sleep(1.5)


if __name__ == "__main__":
    main()
