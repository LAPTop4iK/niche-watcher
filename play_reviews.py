#!/usr/bin/env python3
"""
Play Reviews — майнинг жалоб (feature-gap raid) по живым лидерам Google Play.

Что делает: берёт топ Play-приложений из niches.db (или конкретные appId),
тянет свежие отзывы, считает долю 1-2★ и самые частые биграммы жалоб —
это готовый фич-лист для «сделать лучше»: боль доказана выручкой лидера.

Использование:
    python3 play_reviews.py com.whatsapp --n 300
    python3 play_reviews.py --from-db 5 --country us    # топ-5 Play-зомби из базы
"""

import argparse
import os
import re
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner as sc

try:
    from google_play_scraper import reviews, app as gp_app
except ImportError:
    sys.exit("Нужен google-play-scraper:  pip3 install --user google-play-scraper")

STOP = set("""the a an and or but is are was were to of in on for with this that it its i you my
me we they he she at as be by from has have had not no do does did can will would should could
приложение это для что так но на как я мне не и в у с по от да же бы очень все всё app очень
все нужно нужно нужна нужен был была были чтобы если когда уже там тут вот только можно
спасибо отлично хорошо крутой лучшая лучший хорошее отличное""".split())


def tokens(text):
    return [w for w in re.split(r"[^a-zA-Zа-яА-ЯёЁ]+", text.lower()) if len(w) > 2 and w not in STOP]


def analyze_app(app_id, country, lang, n):
    meta = gp_app(app_id, lang=lang, country=country)
    result, _ = reviews(app_id, lang=lang, country=country, count=n, sort=2)  # 2 = NEWEST
    if not result:
        return None
    bad = [r for r in result if r["score"] <= 2]
    good_share = 100.0 * sum(1 for r in result if r["score"] >= 4) / len(result)
    grams = Counter()
    for r in bad:
        t = tokens(r["content"])
        for i in range(len(t)):
            grams[t[i]] += 1
            if i + 1 < len(t):
                grams[f"{t[i]} {t[i+1]}"] += 1
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
    con = sc.open_db(db)
    rows = con.execute(
        """SELECT app_id, ratings_count FROM play_snapshots
           WHERE country = ? AND stale_days >= 540 AND ratings_count >= 500
           GROUP BY app_id HAVING ratings_count = MAX(ratings_count)
           ORDER BY ratings_count DESC LIMIT ?""",
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
    args = ap.parse_args()

    ids = list(args.app_ids)
    if args.from_db:
        ids += top_play_zombies(args.db, args.from_db, args.country)
    if not ids:
        sys.exit("нет приложений: передай appId или --from-db (сначала нужен play_scan)")

    for app_id in ids:
        lang = {"us": "en", "gb": "en", "de": "de", "ru": "ru", "jp": "ja", "kr": "ko"}.get(args.country, "en")
        try:
            r = analyze_app(app_id, args.country, lang, args.n)
        except Exception as e:  # noqa: BLE001
            print(f"! {app_id}: {e}", file=sys.stderr)
            continue
        if not r:
            print(f"! {app_id}: отзывов не получено")
            continue
        print(f"\n=== {r['title']} [{args.country}] ({app_id}) ===")
        print(f"установки: {r['installs']} | рейтинг: {r['score']} | "
              f"из {r['n_reviews']} свежих: 1-2★ = {r['bad_share']:.0f}%")
        print("топ-жалобы:", ", ".join(r["top_complaints"][:10]))
        print("самые поддержанные 1-2★:")
        for w in r["worst"]:
            print(f"  [{w['score']}★, 👍{w.get('thumbsUpCount', 0)}] {w['content'][:150]}")
        time.sleep(1.5)


if __name__ == "__main__":
    main()
