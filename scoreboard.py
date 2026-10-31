#!/usr/bin/env python3
"""
Scoreboard — сводный рейтинг возможностей (категория × гео): «что делать» одним списком.

Сливает все сигналы системы в один скор (0..100):
  35 × доля зомби           — открыто ли окно (версия >18 мес, ≥500 оценок)
  20 × вакуум (100−fresh%)  — не занято ли активными конкурентами
  15 × плотность quality-gap (популярное+плохое на 100 приложений)
  10 × свежий приток (новые за 7 дней на 100) — живой, но не перенасыщенный рынок
  20 × денежность гео (индекс аномалии revenue/CPI, нормированный)

Плюс лучшая модель монетизации для гео. Фильтр n≥8 (биномиальные ДИ).
Использование:
    python3 scoreboard.py                  # топ-25 в stdout + docs/scoreboard.md
    python3 scoreboard.py --min-n 15 --top 40
"""

import argparse
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner as sc
import revenue_model as rm

HERE = os.path.dirname(os.path.abspath(__file__))


def stale_months(version_date):
    if not version_date:
        return None
    d = datetime.strptime(version_date[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - d).days / 30.44


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(HERE, "niches.db"))
    ap.add_argument("--out", default=os.path.join(HERE, "docs", "scoreboard.md"))
    ap.add_argument("--min-n", type=int, default=8)
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()

    markets, defaults = rm.load()
    anomaly = {}
    for geo, mk in markets.items():
        revs = {m: rm.model_revenue(geo, mk, defaults, m, 10000, 4.99)
                for m in ["ads", "paid", "sub", "hybrid"]}
        best = max(revs, key=revs.get)
        # денежность в скоре — по АБСОЛЮТНОЙ organic-выручке (не аномалии/CPI:
        # деление на дешёвый CPI ложно возводило RU в топ — ре-аудит §24.4)
        anomaly[geo] = (revs[best], best, revs[best] / mk["cpi_usd"])
    a_max = max(v[0] for v in anomaly.values())

    con = sqlite3.connect(args.db)
    rows = con.execute(
        """SELECT s.category, s.country, s.rating_count, s.avg_rating, s.version_date,
                  a.first_seen, a.name, a.store_url
           FROM snapshots s JOIN apps a ON a.track_id = s.track_id
           WHERE s.scanned_at = (SELECT MAX(s2.scanned_at) FROM snapshots s2
                                 WHERE s2.track_id = s.track_id AND s2.country = s.country)"""
    ).fetchall()
    new7 = con.execute(
        "SELECT COUNT(*) FROM apps WHERE first_seen >= datetime('now','-7 days')"
    ).fetchone()[0]
    con.close()

    agg = defaultdict(lambda: {"total": 0, "zombi": 0, "fresh": 0, "qg": 0, "new": 0})
    for cat, country, rc, ar, vd, first_seen, name, url in rows:
        if rc < 10 or cat.startswith("chart:") is False and False:
            continue
        key = (cat, country)
        a = agg[key]
        a["total"] += 1
        sm = stale_months(vd)
        if sm is not None and sm >= 18 and rc >= 500:
            a["zombi"] += 1
        if sm is not None and sm < 12:
            a["fresh"] += 1
        if rc >= 1000 and ar and ar < 4.0 and sm is not None and sm < 12:
            a["qg"] += 1
        if first_seen and first_seen >= datetime.now(timezone.utc).strftime("%Y-%m-%dT") + "00:00:00Z":
            a["new"] += 1

    # demand-alive: у скольких зомби ячейки растёт счётчик оценок (2+ снапшота ≥6 дней)
    vel_live = defaultdict(lambda: [0, 0])  # alive, stale_total
    con2 = sqlite3.connect(args.db)
    vrows = con2.execute(
        """SELECT s.track_id, s.country, s.category,
                  (SELECT rating_count FROM snapshots s2 WHERE s2.track_id = s.track_id AND s2.country = s.country
                    ORDER BY scanned_at DESC LIMIT 1),
                  (SELECT rating_count FROM snapshots s3 WHERE s3.track_id = s.track_id AND s3.country = s.country
                    ORDER BY scanned_at ASC LIMIT 1),
                  julianday(MAX(s.scanned_at)) - julianday(MIN(s.scanned_at)),
                  s.version_date
           FROM snapshots s GROUP BY s.track_id, s.country"""
    ).fetchall()
    for tid, country, cat, rc_now, rc_first, days, vd in vrows:
        sm = stale_months(vd)
        if sm is None or sm < 18 or (rc_now or 0) < 500 or days is None or days < 6:
            continue
        key = (cat, country)
        vel_live[key][1] += 1
        if rc_now is not None and rc_first is not None and rc_now > rc_first:
            vel_live[key][0] += 1

    scored = []
    for (cat, geo), a in agg.items():
        if a["total"] < args.min_n:
            continue
        z = 100.0 * a["zombi"] / a["total"]
        f = 100.0 * a["fresh"] / a["total"]
        qg100 = 100.0 * a["qg"] / a["total"]
        new100 = 100.0 * a["new"] / a["total"]
        alive = vel_live.get((cat, geo), [0, 0])
        demand = 100.0 * alive[0] / alive[1] if alive[1] else 0.0
        # Вакуум (100−fresh%) НЕ входит в скор: эмпирически r(зомби%,fresh%) = −0.33
        # на наших 132 ячейках — это та же переменная, что и зомби% (двойной счёт).
        score = (0.35 * z + 0.15 * min(100.0, qg100 * 10)
                 + 0.10 * min(100.0, new100 * 5) + 0.20 * demand
                 + 0.20 * 100.0 * anomaly[geo][0] / a_max)
        scored.append((score, cat, geo, a["total"], z, f, a["qg"], a["new"],
                       f"{demand:.0f}%", anomaly[geo][1], anomaly[geo][2]))
    scored.sort(reverse=True)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    head = (f"# Scoreboard — куда смотреть ({datetime.now(timezone.utc):%Y-%m-%d})\n\n"
            "Свод: 35% декей + 15% quality-gap + 10% приток + 20% спрос(velocity) "
            "+ 20% денежность гео. Вакуум исключён (r=−0.33 с декеем). n≥" + str(args.min_n) + ".\n\n"
            "| Скор | Категория | Гео | n | Зомби% | Fresh% | QG | New | Спрос | Модель | Аномалия |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|\n")
    trs = [f"| {s:5.1f} | {cat} | {geo} | {n} | {z:.0f}% | {f:.0f}% | {qg} | {nw} | {demand} | {model} | {an:,.0f} |"
           for s, cat, geo, n, z, f, qg, nw, demand, model, an in scored[:args.top]]
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(head + "\n".join(trs) + "\n")
    print("\n".join([head] + trs[:15]))


if __name__ == "__main__":
    main()
