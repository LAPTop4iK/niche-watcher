#!/usr/bin/env python3
"""
Divergence — динамика между прогонами: кросс-гео velocity и rank-дельты.

1) Кросс-гео divergence: одно приложение набирает оценки в гео A и стоит в гео B
   (та же языковая группа) = geo-gap с доказательством спроса фактом роста.
2) Rank-дельты: кто поднялся/упал в выдаче одного запроса между двумя последними
   прогонами (rank пишется scanner'ом с 31.10.2026).

Данные появляются со вторых прогонов (≈6–7 ноября). До этого отчёт пуст — норма.

Использование:
    python3 divergence.py                 # оба анализа
    python3 divergence.py --min-ratings 300
"""

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner as sc


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(here, "niches.db"))
    ap.add_argument("--markets", default=os.path.join(here, "markets.json"))
    ap.add_argument("--min-ratings", type=int, default=300)
    ap.add_argument("--min-days", type=float, default=6.0)
    ap.add_argument("--out", default=os.path.join(here, "docs", "divergence.md"))
    args = ap.parse_args()

    import json
    markets = json.load(open(args.markets, encoding="utf-8"))["markets"]
    lang_of = {g: v.get("lang", "en") for g, v in markets.items()}

    con = sqlite3.connect(args.db)
    # (track_id, country) -> (first_rc, last_rc, days, version_date, name, url, category)
    series = con.execute(
        """SELECT s.track_id, s.country, s.category, s.query,
                  (SELECT rating_count FROM snapshots s2 WHERE s2.track_id = s.track_id
                     AND s2.country = s.country ORDER BY scanned_at ASC LIMIT 1),
                  (SELECT rating_count FROM snapshots s3 WHERE s3.track_id = s.track_id
                     AND s3.country = s.country ORDER BY scanned_at DESC LIMIT 1),
                  (julianday(MAX(s.scanned_at)) - julianday(MIN(s.scanned_at))),
                  (SELECT version_date FROM snapshots s4 WHERE s4.track_id = s.track_id
                     AND s4.country = s.country ORDER BY scanned_at DESC LIMIT 1),
                  a.name, a.store_url
           FROM snapshots s JOIN apps a ON a.track_id = s.track_id
           GROUP BY s.track_id, s.country"""
    ).fetchall()
    con.close()

    by_app = defaultdict(dict)
    for tid, country, cat, q, rc_first, rc_last, days, vd, name, url in series:
        if rc_first is None or rc_last is None or (rc_last or 0) < args.min_ratings:
            continue
        by_app[tid][country] = dict(rc_first=rc_first, rc_last=rc_last, days=days or 0,
                                    cat=cat, q=q, name=name, url=url)

    lines = [f"# Divergence — {datetime.now(timezone.utc):%Y-%m-%d}\n"]

    # 1) кросс-гео divergence
    div = []
    for tid, geos in by_app.items():
        for gA, a in geos.items():
            if a["days"] < args.min_days or not a["rc_last"] or a["rc_last"] <= a["rc_first"]:
                continue
            vA = (a["rc_last"] - a["rc_first"]) / max(1, a["days"]) * 30.0
            for gB, b in geos.items():
                if gB == gA or lang_of.get(gA) != lang_of.get(gB) or b["days"] < args.min_days:
                    continue
                vB = (b["rc_last"] - b["rc_first"]) / max(1, b["days"]) * 30.0
                if vB <= vA * 0.3:  # в B растёт меньше трети от A
                    div.append((vA, vB, gA, gB, a, b, tid))
    div.sort(key=lambda x: -(x[0] - x[1]))
    lines.append("## Кросс-гео divergence (растёт в A, стоит в B, same lang)\n")
    lines.append("| Рост A/30д | Стояние B/30д | A→B | Приложение | Категория |")
    lines.append("|---|---|---|---|---|")
    if div:
        for vA, vB, gA, gB, a, b, tid in div[:15]:
            lines.append(f"| +{vA:.0f} | {vB:+.0f} | {gA}→{gB} | "
                         f"[{a['name'][:34]}]({a['url']}) | {a['cat']} |")
    else:
        lines.append("_Пока пусто: нужно ≥2 прогона по гео (≈6–7 ноября)._")

    # 2) rank-дельты между двумя последними прогонами одного запроса
    con = sqlite3.connect(args.db)
    rank_rows = con.execute(
        """SELECT s.category, s.query, s.country, s.track_id,
                  (SELECT rank FROM snapshots s2 WHERE s2.track_id = s.track_id
                     AND s2.category = s.category AND s2.query = s.query AND s2.country = s.country
                     ORDER BY scanned_at DESC LIMIT 1 OFFSET 1),
                  (SELECT rank FROM snapshots s3 WHERE s3.track_id = s.track_id
                     AND s3.category = s.category AND s3.query = s.query AND s3.country = s.country
                     ORDER BY scanned_at DESC LIMIT 1),
                  a.name, a.store_url
           FROM snapshots s JOIN apps a ON a.track_id = s.track_id
           WHERE s.rank IS NOT NULL
           GROUP BY s.category, s.query, s.country, s.track_id"""
    ).fetchall()
    con.close()
    movers = []
    for cat, q, country, tid, r_old, r_new, name, url in rank_rows:
        if r_old is None or r_new is None:
            continue
        if r_old > 50 and r_new <= 20 and r_new < r_old:  # ворвался в топ-20
            movers.append((r_old - r_new, country, q, r_old, r_new, name, url))
    movers.sort(reverse=True)
    lines.append("\n## Rank-дельты (ворвались в топ-20 выдачи)\n")
    lines.append("| Гео | Запрос | Было | Стало | Приложение |")
    lines.append("|---|---|---|---|---|")
    if movers:
        for d, country, q, r_old, r_new, name, url in movers[:15]:
            lines.append(f"| {country} | {q} | {r_old} | **{r_new}** | [{name[:34]}]({url}) |")
    else:
        lines.append("_Пока пусто: нужен второй прогон с rank (с 31.10.2026)._")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"divergence: {len(div)} кросс-гео, {len(movers)} rank-движений → {args.out}")


if __name__ == "__main__":
    main()
