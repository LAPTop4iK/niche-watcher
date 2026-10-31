#!/usr/bin/env python3
"""
Play Scan — зомби-скан Google Play (второй по важности источник после App Store).

Нужен пакет:  pip3 install --user google-play-scraper
(бесплатный скрапер без ключей; отдаёт дату обновления, установки, рейтинги, отзывы)

Использование:
    python3 play_scan.py                                  # все категории из seed
    python3 play_scan.py --categories construction --countries us,de
    python3 play_scan.py --limit 15 --min-rating-count 300
"""

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner as sc

try:
    from google_play_scraper import search, app as gp_app
except ImportError:
    sys.exit("Нужен google-play-scraper:  pip3 install --user google-play-scraper")

PLAY_SCHEMA = """
CREATE TABLE IF NOT EXISTS play_apps (
    app_id     TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    developer  TEXT,
    installs   TEXT,
    price      TEXT,
    genre      TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS play_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id     TEXT NOT NULL REFERENCES play_apps(app_id),
    country    TEXT NOT NULL,
    category   TEXT NOT NULL,
    query      TEXT NOT NULL,
    scanned_at TEXT NOT NULL,
    updated_at TEXT,
    stale_days INTEGER,
    rating     REAL,
    ratings_count INTEGER,
    installs   TEXT
);
CREATE INDEX IF NOT EXISTS idx_psnap ON play_snapshots(app_id, scanned_at);
"""


def months_since(ts):
    if not ts:
        return None
    return (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromtimestamp(ts, dt.timezone.utc)).days / 30.44


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", default=os.path.join(here, "seed_queries.json"))
    ap.add_argument("--db", default=os.path.join(here, "niches.db"))
    ap.add_argument("--categories", default=None)
    ap.add_argument("--countries", default=None, help="us,de — иначе из seed")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--min-rating-count", type=int, default=500)
    ap.add_argument("--min-stale-months", type=int, default=18)
    args = ap.parse_args()

    seed = json.load(open(args.seed, encoding="utf-8"))
    countries = [c.strip() for c in args.countries.split(",")] if args.countries else seed["countries"]
    wanted = set(args.categories.split(",")) if args.categories else None

    con = sqlite3.connect(args.db, timeout=30)
    con.executescript(PLAY_SCHEMA)

    zombies = []
    analyzed = 0
    for cat in seed["categories"]:
        cid = cat["id"]
        if wanted and cid not in wanted:
            continue
        if not cat.get("enabled", True):
            continue
        for country in countries:
            lang = {"us": "en", "gb": "en", "de": "de", "ru": "ru"}.get(country, "en")
            for q in cat["queries"]:
                try:
                    hits = search(q, lang=lang, country=country, n_hits=args.limit)
                except Exception as e:  # noqa: BLE001
                    print(f"! {cid}/{country}/{q}: {e}", file=sys.stderr)
                    time.sleep(3)
                    continue
                for h in hits:
                    app_id = h["appId"]
                    try:
                        d = gp_app(app_id, lang=lang, country=country)
                    except Exception:  # noqa: BLE001
                        continue
                    time.sleep(0.7)
                    analyzed += 1
                    stale_m = months_since(d.get("updated"))
                    now = sc.iso(sc.now_utc())
                    con.execute(
                        """INSERT INTO play_apps (app_id, title, developer, installs, price, genre, first_seen, last_seen)
                           VALUES (?,?,?,?,?,?,?,?)
                           ON CONFLICT(app_id) DO UPDATE SET last_seen=excluded.last_seen""",
                        (app_id, d.get("title"), d.get("developer"), d.get("installs"),
                         d.get("price"), d.get("genre"), now, now),
                    )
                    con.execute(
                        """INSERT INTO play_snapshots (app_id, country, category, query, scanned_at,
                           updated_at, stale_days, rating, ratings_count, installs)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (app_id, country, cid, q, now,
                         dt.datetime.fromtimestamp(d["updated"], dt.timezone.utc).strftime("%Y-%m-%d") if d.get("updated") else None,
                         int(stale_m * 30.44) if stale_m is not None else None,
                         d.get("score"), d.get("ratings"), d.get("installs")),
                    )
                    if (stale_m is not None and stale_m >= args.min_stale_months
                            and (d.get("ratings") or 0) >= args.min_rating_count):
                        zombies.append((stale_m, d.get("ratings") or 0, d.get("title"),
                                        d.get("developer"), country, d.get("installs"),
                                        d["updated"] and dt.datetime.fromtimestamp(d["updated"], dt.timezone.utc).strftime("%Y-%m-%d"),
                                        f"https://play.google.com/store/apps/details?id={app_id}"))
                time.sleep(1.2)
        con.commit()

    con.commit()
    con.close()
    zombies.sort(key=lambda z: (-(z[0] * min(z[1], 5000))))
    print(f"\n== Play: проанализировано {analyzed}, зомби {len(zombies)}")
    for z in zombies[:25]:
        print(f"| {z[0]:5.0f} мес | {z[1]:>7} оценок | {z[2][:38]} | {z[3][:20]} | {z[4]} | {z[5]} | версия {z[6]} | {z[7]}")


if __name__ == "__main__":
    main()
