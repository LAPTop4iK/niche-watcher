#!/usr/bin/env python3
"""
Review Clusters — группировка жалоб на лидера в кластеры → фич-лист MVP.

Без LLM, детерминированно: 1-2★ отзывы делятся на кластеры по общим жалобным
токенам; каждый кластер = потенциальная фича твоего приложения. Работает по
iOS (RSS отзывов) и Google Play (google-play-scraper).

Использование:
    python3 review_clusters.py --ios 1494386815 --country us
    python3 review_clusters.py --play com.plaincode.xclinometer --country us
    python3 review_clusters.py --ios idX --play com.Y --n 300
"""

import argparse
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner as sc

STOP = set("""the a an and or but is are was were to of in on for with this that it its i you my me we
they he she at as be by from has have had not no do does did can will would should could very just
so there their about out up down when what which who all more most other some such only own same
than too при этом для что так как это оно они его её мне тебя не и в на с по у же бы очень все
нужно нужна нужен был была были чтобы если когда уже там вот только можно спасибо приложение
""".split())


def tokens(text):
    return [w.lower() for w in re.split(r"[^a-zA-Zа-яА-ЯёЁ]+", text) if len(w) > 3 and w.lower() not in STOP]


def cluster(reviews_bad, max_clusters=6):
    """Жадная кластеризация: самый частый токен → все отзывы с ним → повторить."""
    pools = [r for r in reviews_bad if tokens(r["text"])]
    clusters = []
    while pools and len(clusters) < max_clusters:
        cnt = defaultdict(int)
        for r in pools:
            for t in set(tokens(r["text"])):
                cnt[t] += 1
        if not cnt:
            break
        top_token, _ = max(cnt.items(), key=lambda kv: kv[1])
        members = [r for r in pools if top_token in tokens(r["text"])]
        if len(members) < 2:
            break
        clusters.append((top_token, members))
        pools = [r for r in pools if r not in members]
    return clusters, pools


def fetch_ios(track_id, country, n):
    out = []
    for page in range(1, max(1, n // 50) + 1):
        try:
            data = sc.fetch_json(
                f"https://itunes.apple.com/{country}/rss/customerreviews/id={track_id}"
                f"/sortby=mostrecent/page={page}/json")
        except RuntimeError:
            break
        entries = (data.get("feed") or {}).get("entry") or []
        if isinstance(entries, dict):
            entries = [entries]
        added = 0
        for e in entries:
            if "im:rating" not in e:
                continue
            if int(e["im:rating"]["label"]) <= 3:
                out.append({"text": e.get("content", {}).get("label", ""),
                            "score": int(e["im:rating"]["label"]),
                            "thumbs": 0})
                added += 1
        if added == 0:
            break
        time.sleep(1)
    return out


def fetch_play(app_id, country, n):
    from google_play_scraper import reviews, Sort
    lang = {"us": "en", "gb": "en", "de": "de", "ru": "ru", "jp": "ja", "kr": "ko"}.get(country, "en")
    result, _ = reviews(app_id, lang=lang, country=country, count=n, sort=Sort.NEWEST)
    return [{"text": r["content"], "score": r["score"], "thumbs": r.get("thumbsUpCount", 0)}
            for r in result if r["score"] <= 3]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ios", help="iOS track id")
    ap.add_argument("--play", help="Play appId")
    ap.add_argument("--country", default="us")
    ap.add_argument("--n", type=int, default=300)
    args = ap.parse_args()

    if not args.ios and not args.play:
        sys.exit("укажи --ios <trackId> и/или --play <appId>")

    bad = []
    if args.ios:
        bad += fetch_ios(args.ios, args.country, args.n)
    if args.play:
        bad += fetch_play(args.play, args.country, args.n)
    bad = [r for r in bad if r["text"]]
    if not bad:
        sys.exit("1-3★ отзывов не найдено")

    clusters, rest = cluster(bad)
    total = len(bad)
    print(f"Проанализировано {total} негативных отзывов (1-3★)\n")
    for token, members in clusters:
        top = sorted(members, key=lambda r: -r["thumbs"])[:2]
        print(f"▪ КЛАСТЕР «{token}» — {len(members)} отз. ({100*len(members)/total:.0f}%)")
        for t in top:
            txt = re.sub(r"\s+", " ", t["text"])[:140]
            print(f"   [{t['score']}★ 👍{t['thumbs']}] {txt}")
        print()
    print(f"Некластеризованный остаток: {len(rest)}")
    print("→ Каждый кластер = фича/фикс твоего MVP; размер = приоритет.")


if __name__ == "__main__":
    import time
    main()
