#!/usr/bin/env python3
"""
Reviews — свежие отзывы App Store по track_id через официальный RSS (бесплатно).

Использование:
    python3 reviews.py 1494386815                 # последние 10 отзывов
    python3 reviews.py 1494386815,1504997190 -n 5 # по 5 отзывов на приложение
    python3 reviews.py 1494386815 --max-rating 2  # только 1-2★ (майнинг фич-гэпов)

Свежий отзыв — пользовательское событие. Низкая оценка — тема для ручной
проверки, а не доказанная боль, готовность платить или готовый фич-лист.
"""

import argparse
import json
import sys

import scanner as sc


def fetch_reviews(track_id, country="us"):
    url = f"https://itunes.apple.com/{country}/rss/customerreviews/id={track_id}/sortby=mostrecent/page=1/json"
    data = sc.fetch_json(url)
    entries = (data.get("feed") or {}).get("entry") or []
    if isinstance(entries, dict):
        entries = [entries]
    out = []
    for e in entries:
        if "im:rating" not in e:  # первая entry — метаданные страницы
            continue
        out.append({
            "date": e.get("updated", {}).get("label", "")[:10],
            "rating": int(e["im:rating"]["label"]),
            "version": e.get("im:version", {}).get("label", "?"),
            "title": e.get("title", {}).get("label", ""),
            "body": e.get("content", {}).get("label", "").replace("\n", " ")[:180],
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("track_ids", help="id,id,id — track id из scanner report")
    ap.add_argument("--country", default="us")
    ap.add_argument("-n", type=int, default=10)
    ap.add_argument("--max-rating", type=int, default=5, help="показывать только отзывы с рейтингом <= N")
    args = ap.parse_args()

    for tid in [t.strip() for t in args.track_ids.split(",") if t.strip()]:
        try:
            revs = fetch_reviews(tid, args.country)
        except RuntimeError as e:
            print(f"! {tid}: {e}", file=sys.stderr)
            continue
        print(f"\n=== {tid} ({args.country}) — {len(revs)} последних отзывов ===")
        shown = 0
        for r in revs:
            if r["rating"] > args.max_rating or shown >= args.n:
                continue
            print(f"[{r['date']}] {'★' * r['rating']} (v{r['version']}) {r['title']}")
            print(f"    {r['body']}")
            shown += 1


if __name__ == "__main__":
    main()
