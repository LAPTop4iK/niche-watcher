#!/usr/bin/env python3
"""
Chart Decay Scan — скан ТОП-чартов App Store через официальные RSS (бесплатно, без ключей).

Ищет НИШИ, а не ключевые слова: в каждой категории и гео смотрит топ-200
и считает, сколько из них устарели (версия старше N месяцев).
Категория, где 20–50% чарта — зомби, это категория-кандидат, даже если ты про неё не знал.

Использование:
    python3 charts_scan.py --countries us,de --top 200
    python3 charts_scan.py --countries us --genres 6002,6007    # только Utilities, Productivity

Пишет в ту же niches.db (velocity копится) и в charts_report.md.
"""

import argparse
import json
import statistics
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import scanner as sc

# Эмпирически проверенные genre ID RSS-чартов (проверено curl-ом, октябрь 2026)
GENRES = {
    6001: "Weather", 6002: "Utilities", 6003: "Travel", 6004: "Sports",
    6005: "Social Networking", 6006: "Reference", 6007: "Productivity",
    6008: "Photo & Video", 6010: "Navigation", 6011: "Music",
    6012: "Lifestyle", 6013: "Health & Fitness", 6015: "Finance",
    6017: "Education", 6018: "Books", 6023: "Food & Drink",
}

# top_paid = прямое доказательство платёжеспособности гео (культура разовых покупок)
FEEDS = ["topfreeapplications", "topgrossingapplications", "toppaidapplications"]


def fetch_rss_chart(feed, country, genre, limit):
    url = f"https://itunes.apple.com/{country}/rss/{feed}/limit={limit}/genre={genre}/json"
    data = sc.fetch_json(url)
    entries = (data.get("feed") or {}).get("entry") or []
    if isinstance(entries, dict):  # limit=1 возвращает dict, не list
        entries = [entries]
    ids = []
    for e in entries:
        eid = e.get("id", {})
        tid = (eid.get("attributes") or {}).get("im:id")
        if tid:
            ids.append(int(tid))
    return ids


def lookup_ids(track_ids, country):
    """Батч-lookup до 100 id за запрос."""
    out = []
    for i in range(0, len(track_ids), 100):
        chunk = track_ids[i:i + 100]
        url = ("https://itunes.apple.com/lookup?" +
               urllib.parse.urlencode({"id": ",".join(map(str, chunk)), "country": country, "entity": "software"}))
        data = sc.fetch_json(url)
        out.extend(data.get("results", []))
        time.sleep(sc.REQUEST_DELAY)
    return out


def main():
    ap = argparse.ArgumentParser()
    here = __import__("os").path.dirname(__file__) if "__file__" in globals() else "."
    ap.add_argument("--countries", default="us,de")
    ap.add_argument("--genres", default=None, help="6002,6007 — иначе все из GENRES")
    ap.add_argument("--top", type=int, default=200)
    ap.add_argument("--db", default="niches.db")
    ap.add_argument("--out", default="charts_report.md")
    ap.add_argument("--min-stale-months", type=int, default=18)
    ap.add_argument("--min-rating-count", type=int, default=500)
    args = ap.parse_args()

    genres = {int(g): GENRES[int(g)] for g in args.genres.split(",")} if args.genres else GENRES
    countries = [c.strip() for c in args.countries.split(",")]
    con = sc.open_db(args.db)
    min_age = args.min_stale_months
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30.44 * min_age)).strftime("%Y-%m-%d")  # для ages

    rows = []  # (country, genre_name, resolved, stale, pct, median_age, top_stale_str)
    churn_rows, band_rows = [], []
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=30.44 * min_age)).strftime("%Y-%m-%d")
    for country in countries:
        for gid, gname in genres.items():
            resolved, feed_pos = [], {}
            for feed in FEEDS:
                try:
                    ids = fetch_rss_chart(feed, country, gid, args.top)
                except RuntimeError as e:
                    print(f"! {country}/{gname}/{feed}: {e}", file=sys.stderr)
                    continue
                # позиция в фиде = rank чарта (для churn и бандов глубины)
                for i, tid in enumerate(ids, 1):
                    feed_pos[int(tid)] = i
                got = lookup_ids(ids, country)
                for r in got:
                    r["_feed"] = feed
                    r["_rank"] = feed_pos.get(r.get("trackId"))
                resolved.extend(got)
                time.sleep(sc.REQUEST_DELAY)
            if not resolved:
                continue
            stale, ages, top_stale = [], [], []
            for r in resolved:
                vd = r.get("currentVersionReleaseDate", "")
                if not vd:
                    continue
                date = vd[:10]
                age_m = (datetime.now(timezone.utc) - datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)).days / 30.44
                ages.append(age_m)
                if date < cutoff:
                    stale.append(r)
            if not ages:
                continue
            top_stale = sorted(
                stale, key=lambda r: -(r.get("userRatingCount") or 0)
            )[:3]
            top_str = "; ".join(
                f"{r['trackName'][:30]} ({r.get('userRatingCount') or 0}★, {r['currentVersionReleaseDate'][:10]})"
                for r in top_stale
            )
            rows.append((country, gname, len(ages), len(stale), 100.0 * len(stale) / len(ages),
                         statistics.median(ages), top_str))

            # --- churn: ротация топа vs прошлый прогон (>=6 дней назад)
            today_ids = {r.get("trackId") for r in resolved if r.get("_feed")}
            for feed in FEEDS:
                prev = con.execute(
                    """SELECT DISTINCT s.track_id FROM snapshots s
                       WHERE s.category = ? AND s.country = ? AND s.query = ?
                         AND s.scanned_at <= ?""",
                    (f"chart:{gname.lower()}", country, feed,
                     (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%SZ")),
                ).fetchall()
                prev_ids = {p[0] for p in prev}
                today_feed_ids = {r.get("trackId") for r in resolved if r.get("_feed") == feed}
                if not prev_ids or not today_feed_ids:
                    continue  # baseline ещё не накоплен
                entered = len(today_feed_ids - prev_ids)
                exited = len(prev_ids - today_feed_ids)
                churn_rows.append((country, gname, feed, len(today_feed_ids), entered, exited,
                                   100.0 * (entered + exited) / max(1, len(today_feed_ids))))

            # --- банды глубины: где гниль — в топ-10, 11-50 или хвосте
            for lo, hi, label in [(1, 10, "top-10"), (11, 50, "11-50"), (51, 9999, "51+")]:
                band = [r for r in resolved
                        if r.get("_rank") and lo <= r["_rank"] <= hi and r.get("currentVersionReleaseDate")]
                if band:
                    b_stale = sum(1 for r in band
                                  if r["currentVersionReleaseDate"][:10] < cutoff_date)
                    band_rows.append((country, gname, label, len(band),
                                      100.0 * b_stale / len(band)))
            # зомби из чартов пишем в БД — velocity копится и тут; rank = позиция фида
            for r in resolved:
                rc = r.get("userRatingCount") or 0
                if rc < 10:
                    continue
                app = sc.normalize(r, country, f"chart:{gname.lower()}", r["_feed"], rank=r["_rank"])
                sc.upsert_app(con, app)
                sc.insert_snapshot(con, app)
            con.commit()
            print(f"[{country}] {gname}: {len(ages)} в чарте, зомби {len(stale)} ({rows[-1][4]:.0f}%)")

    con.close()

    lines = [
        f"# Chart Decay Scan — {sc.iso(sc.now_utc())[:10]}\n",
        f"Порог зомби: версия старше {min_age} мес. Топ-{args.top} topfree + topgrossing на жанр.\n",
        "| Гео | Категория | В чарте | Зомби | % | Медианный возраст | Топ-зомби |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in sorted(rows, key=lambda x: -x[4]):
        lines.append(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]:.0f}% | {r[5]:.0f} мес | {r[6]} |")
    if churn_rows:
        lines += [
            "\n## Churn топов (ротация vs прошлый прогон ≥6 дней назад)\n",
            "Высокая ротация = рынок вознаграждает новизну, вход проще; нулевая = места застыли.\n",
            "| Гео | Категория | Фид | В топе | Вошло | Вышло | Ротация% |",
            "|---|---|---|---|---|---|---|",
        ] + [f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} | {r[6]:.0f}% |" for r in
             sorted(churn_rows, key=lambda x: -x[6])]
    if band_rows:
        lines += [
            "\n## Глубина гнили по бандам (top-10 / 11-50 / хвост)\n",
            "| Гео | Категория | Банд | Приложений | Зомби% |",
            "|---|---|---|---|---|",
        ] + [f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]:.0f}% |" for r in
             sorted(band_rows, key=lambda x: -x[4])]
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n== отчёт: {args.out} (churn: {len(churn_rows)}, bands: {len(band_rows)})")


if __name__ == "__main__":
    main()
