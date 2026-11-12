#!/usr/bin/env python3
"""
Review Clusters — лексические группы жалоб для ручной проверки.

Без LLM, детерминированно: 1-3★ отзывы делятся на кластеры по общим жалобным
токенам; кластеры дают темы для ручной проверки, не готовый фич-лист. Работает по
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
import time
import math
import unicodedata
import json
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner as sc
from relevance import CJK_RUN,REVIEW_STOP,cjk,theme_word

STOP = REVIEW_STOP


def tokens(text):
    found=[]
    for word in re.findall(r'[^\W\d_]+',unicodedata.normalize('NFKC',text).casefold(),re.UNICODE):
        # Latin and CJK run together without spaces ("app使用"): split them first.
        for part in CJK_RUN.findall(word):
            if cjk(part):
                found.extend(b for b in (part[i:i+2] for i in range(len(part)-1)) if theme_word(b))  # No word segmentation: bigrams.
            elif len(part)>3 and theme_word(part):
                found.append(part)
    return found


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
    seen=set()
    total_seen=0
    holes=0
    for page in range(1, min(10, max(1, (n + 49) // 50)) + 1):
        try:
            data = sc.fetch_json(
                f"https://itunes.apple.com/{country}/rss/customerreviews/id={track_id}"
                f"/sortby=mostrecent/page={page}/json")
        except RuntimeError as error:
            print(f'RSS page {page} unavailable; review sample incomplete: {error}',file=sys.stderr)
            if not out:
                raise
            break
        entries = (data.get("feed") or {}).get("entry") or []
        if isinstance(entries, dict):
            entries = [entries]
        rated = 0
        for e in entries:
            if "im:rating" not in e:
                continue
            identity=e.get('id',{}).get('label') or str(e)
            if identity in seen or total_seen>=n:
                continue
            seen.add(identity);total_seen+=1
            rated += 1
            if int(e["im:rating"]["label"]) <= 3:
                out.append({"text": e.get("content", {}).get("label", ""),
                            "review_id":identity,"platform":"ios","app_id":str(track_id),"country":country,
                            "app_version":e.get('im:version',{}).get('label'),
                            "source_url":f"https://itunes.apple.com/{country}/rss/customerreviews/id={track_id}/sortby=mostrecent/page={page}/json",
                            "score": int(e["im:rating"]["label"]),
                            "thumbs": 0,"date":e.get("updated",{}).get("label","")})
        if total_seen>=n:
            break
        # An empty page can be temporary; the following page may still hold reviews.
        holes=0 if rated else holes+1
        if holes==2:
            break
        time.sleep(1)
    return out


def fetch_play(app_id, country, n):
    from google_play_scraper import reviews, Sort
    lang = {"us": "en", "gb": "en", "de": "de", "ru": "ru", "jp": "ja", "kr": "ko"}.get(country, "en")
    result, _ = reviews(app_id, lang=lang, country=country, count=n, sort=Sort.NEWEST)
    return [{"text": r["content"], "score": r["score"], "thumbs": r.get("thumbsUpCount", 0),"date":str(r.get("at", "")),
             "review_id":r.get('reviewId'),"platform":"play","app_id":app_id,"country":country,
             "source_url":f'https://play.google.com/store/apps/details?id={app_id}&gl={country}'}
            for r in result if r["score"] <= 3]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ios", help="iOS track id")
    ap.add_argument("--play", help="Play appId")
    ap.add_argument("--country", default="us")
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--days",type=int,default=90)
    ap.add_argument('--out',help='сохранить датированные тексты и источники в JSON для ручного/LLM-разбора')
    args = ap.parse_args()
    if args.n<1 or args.days<1:ap.error('n и days должны быть положительными')

    if not args.ios and not args.play:
        sys.exit("укажи --ios <trackId> и/или --play <appId>")

    bad = []
    if args.ios:
        bad += fetch_ios(args.ios, args.country, args.n)
    if args.play:
        bad += fetch_play(args.play, args.country, args.n)
    import evidence
    captured=sc.now_utc();seen=set();recent=[]
    for r in bad:
        try:age=(captured-evidence.parse(r['date'])).total_seconds()/86400
        except (ValueError,TypeError,KeyError):continue
        key=(r['platform'],r['app_id'],r.get('review_id') or (r['date'],r['text']))
        if r['text'] and 0<=age<=args.days and key not in seen:
            recent.append(r);seen.add(key)
    bad=recent
    if args.out:
        output=Path(args.out);output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(dict(captured_at=captured.isoformat(),country=args.country,
            window_days=args.days,max_latest_reviews_requested_per_store=args.n,score_max=3,
            method='lexical; not LLM',scope='Capped latest-review sample, not all users; fetch may stop early on a page error',
            reviews=bad),ensure_ascii=False,indent=2)+'\n')
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
    print("Словарная группировка: не измеряет долю проблем среди всех пользователей; CJK делится на биграммы, а не на слова.")
    print("→ Кластеры — гипотезы: проверь свежесть, контекст, повторяемость и готовность платить. Частота слова не равна приоритету MVP.")


if __name__ == "__main__":
    main()
