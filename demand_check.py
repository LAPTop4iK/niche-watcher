#!/usr/bin/env python3
"""
Demand Check — отделяет живой спрос от мёртвого у зомби-кандидатов.

Проблема (требование заказчика): rating_count накопительный за всю жизнь;
«зомби» с 20k оценок из 2017-го при мёртвом спросе с 2019-го — ловушка, не ниша.
Нужны НЕДАВНИЕ и ЗНАЧИМЫЕ оценки, чтобы верить, что спрос есть; плюс проверять,
не ушёл ли он в аналог (соседей смотрит similar_crawl на живых кластерах).

Три сигнала (триангуляция):
  1) velocity_30d — прирост rating_count между нашими снапшотами (главный;
     письменные отзывы занижают спрос у зомби — они не могут обновить промпт);
  2) r90/r60 — письменные отзывы за 90/60 дней из RSS (консервативный барьер);
  3) last_review — дата последнего отзыва.

Вердикты: ALIVE / WEAK / DEAD / UNKNOWN.
Порог значимости r90: max(2, rating_count/25000) — крупным приложениям
нужна пропорционально большая свежая активность.

Таблица demand(track_id, country, checked_at, last_review, r60, r90, bad_share,
vel_30d, verdict). Отчёт: demand_report.md (чистый список = только ALIVE).

Использование:
    python3 demand_check.py                  # все зомби из БД
    python3 demand_check.py --limit 50       # тест
    python3 demand_check.py --track 1494386815
"""

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner as sc

HERE = os.path.dirname(os.path.abspath(__file__))

DEMAND_SCHEMA = """
CREATE TABLE IF NOT EXISTS demand (
    track_id    INTEGER NOT NULL,
    country     TEXT NOT NULL,
    checked_at  TEXT NOT NULL,
    last_review TEXT,
    r60         INTEGER,
    r90         INTEGER,
    bad_share   INTEGER,
    vel_30d     REAL,
    verdict     TEXT,
    PRIMARY KEY (track_id, country)
);
"""


def fetch_demand(track_id, country):
    """RSS последние ~50 отзывов: (последний отзыв, r60, r90, доля 1-2★)."""
    try:
        data = sc.fetch_json(
            f"https://itunes.apple.com/{country}/rss/customerreviews/id={track_id}"
            "/sortby=mostrecent/page=1/json")
        entries = (data.get("feed") or {}).get("entry") or []
        if isinstance(entries, dict):
            entries = [entries]
        rated, bad, dates = [], 0, []
        for e in entries:
            if "im:rating" not in e:
                continue
            rated.append(e)
            if int(e["im:rating"]["label"]) <= 2:
                bad += 1
            lbl = e.get("updated", {}).get("label", "")
            if lbl:
                dates.append(lbl[:10])
        if not dates:
            return None, None, None, None
        now = sc.now_utc()
        d60 = (now - timedelta(days=60)).strftime("%Y-%m-%d")
        d90 = (now - timedelta(days=90)).strftime("%Y-%m-%d")
        return (dates[0],
                sum(1 for d in dates if d >= d60),
                sum(1 for d in dates if d >= d90),
                round(100 * bad / len(rated)) if rated else None)
    except Exception:  # noqa: BLE001 — RSS нестабилен, это не фатально
        return None, None, None, None


def zombie_tracks(con, min_stale=18.0, min_ratings=500, limit=None, track=None):
    q = f"""
    SELECT s.track_id, s.country, s.rating_count, s.version_date, a.name, a.seller, a.store_url
    FROM snapshots s
    JOIN (SELECT track_id, country, MAX(scanned_at) ms FROM snapshots GROUP BY track_id, country) l
      ON s.track_id = l.track_id AND s.country = l.country AND s.scanned_at = l.ms
    JOIN apps a ON a.track_id = s.track_id
    WHERE CAST((julianday('now') - julianday(s.version_date))/30.44 AS REAL) >= {min_stale}
      AND s.rating_count >= {min_ratings}
    """
    if track:
        q += f" AND s.track_id = {int(track)}"
    if limit:
        q += f" LIMIT {int(limit)}"
    return con.execute(q).fetchall()


def velocity(con, track_id, country):
    row = con.execute(
        """SELECT MIN(rating_count), MAX(rating_count),
                  (julianday(MAX(scanned_at)) - julianday(MIN(scanned_at)))
           FROM snapshots WHERE track_id = ? AND country = ?""", (track_id, country)).fetchone()
    rc_first, rc_last, days = row
    if rc_first is None or rc_last is None or not days or days < 0.5:
        return None, days
    return (rc_last - rc_first) / days * 30.0, days


def verdict_for(rc, vel, last_review, r90):
    thresh = max(2, round(rc / 25000))
    alive = (vel is not None and vel >= 1.0) or (r90 is not None and r90 >= thresh)
    if alive:
        return "ALIVE"
    if last_review:
        lr = (datetime.now(timezone.utc) - datetime.strptime(last_review[:10], "%Y-%m-%d")
              .replace(tzinfo=timezone.utc)).days
        if lr <= 180 and (r90 or 0) >= 1:
            return "WEAK"
        if lr <= 90:
            return "WEAK"
    return "DEAD"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(HERE, "niches.db"))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--track")
    ap.add_argument("--delay", type=float, default=1.2)
    ap.add_argument("--out", default=os.path.join(HERE, "demand_report.md"))
    args = ap.parse_args()

    con = sqlite3.connect(args.db, timeout=30)
    con.executescript(DEMAND_SCHEMA)
    tracks = zombie_tracks(con, limit=args.limit, track=args.track)
    print(f"зомби-треков на проверку: {len(tracks)}")

    now = sc.iso(sc.now_utc())
    results = []
    for i, (tid, country, rc, vd, name, seller, url) in enumerate(tracks, 1):
        vel, days_obs = velocity(con, tid, country)
        last_review, r60, r90, bad = fetch_demand(tid, country)
        v = verdict_for(rc, vel, last_review, r90)
        con.execute(
            """INSERT OR REPLACE INTO demand
               (track_id, country, checked_at, last_review, r60, r90, bad_share, vel_30d, verdict)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (tid, country, now, last_review, r60, r90, bad,
             round(vel, 2) if vel is not None else None, v))
        results.append((v, tid, country, rc, vel, last_review, r60, name, seller, url))
        if i % 25 == 0:
            con.commit()
            print(f"  {i}/{len(tracks)}…")
        time.sleep(args.delay)
    con.commit()

    alive = [r for r in results if r[0] == "ALIVE"]
    weak = [r for r in results if r[0] == "WEAK"]
    dead = [r for r in results if r[0] == "DEAD"]

    def row(r):
        v, tid, country, rc, vel, lr, r60, name, seller, url = r
        vel_s = f"{vel:+.1f}" if vel is not None else "—"
        return (f"| {country} | [{name[:32]}]({url}) | {rc:,} | {vel_s} | "
                f"{(lr or '—')[:10]} | {r60 if r60 is not None else '—'} |")

    head_alive = (
        f"# Demand Report — чистый список зомби ({sc.iso(sc.now_utc())[:10]})\n\n"
        f"Проверено {len(results)} зомби: **ALIVE {len(alive)}** · WEAK {len(weak)} · DEAD {len(dead)}.\n"
        f"ALIVE = спрос доказан: velocity ≥ +1/30д ИЛИ свежих отзывов ≥ max(2, оценки/25000).\n"
        f"DEAD = ловушки «умер и спрос»: НЕ делать. Если у DEAD-зомби живой сосед\n"
        f"(similar_crawl) — спрос мигрировал в аналог: заходить фич-гэпом против аналога.\n\n"
        "## ALIVE — окна, где спрос жив при гнилом инкумбенте\n\n"
        "| Гео | Приложение | Оценок | Vel/30д | Посл. отзыв | R60 |\n|---|---|---|---|---|---|\n")
    lines = [head_alive] + [row(r) + "\n" for r in
                            sorted(alive, key=lambda r: -(r[4] or 0))]
    lines.append(f"\n## WEAK — тёплые (посмотреть руками, Trends)\n\n"
                 "| Гео | Приложение | Оценок | Vel/30д | Посл. отзыв | R60 |\n|---|---|---|---|---|---|\n")
    lines += [row(r) + "\n" for r in sorted(weak, key=lambda r: -(r[3]))]
    lines.append(f"\n## DEAD — {len(dead)} ловушек (спрос, вероятно, умер; проверять соседей на миграцию)\n\n")
    lines += [f"- {r[2]} · {r[7][:36]} · {r[3]:,} оценок · посл. отзыв {(r[5] or '—')[:10]}\n"
              for r in sorted(dead, key=lambda r: -r[3])[:60]]
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("".join(lines))
    print(f"ALIVE {len(alive)} · WEAK {len(weak)} · DEAD {len(dead)} → {args.out}")
    con.close()


if __name__ == "__main__":
    main()
