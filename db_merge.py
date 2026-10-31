#!/usr/bin/env python3
"""
DB Merge — слияние двух niches.db (структурное решение бинарных git-конфликтов).

Проблема: SQLite-байны не мержатся в git; когда локальный прогон и Actions-прогон
коммитят niches.db параллельно, rebase падает и одна сторона теряется.
Решение: снапшоты аддитивны — сливаем их в одну базу (apps upsert по track_id,
snapshots вставкой «того, чего нет» по (track_id, scanned_at)).

Использование (при конфликте или после него):
    git fetch origin
    git show origin/main:niches.db > /tmp/remote.db
    python3 db_merge.py --main niches.db --other /tmp/remote.db   # other вливается в main
"""

import argparse
import os
import sqlite3
import sys


def merge(main_path, other_path):
    con = sqlite3.connect(main_path, timeout=30)
    con.execute("ATTACH DATABASE ? AS src", (other_path,))

    apps_added = con.execute(
        """INSERT OR IGNORE INTO main.apps
           (track_id, name, seller, bundle_id, store_url, price, genre,
            first_country, first_seen, last_seen)
           SELECT track_id, name, seller, bundle_id, store_url, price, genre,
                  first_country, first_seen, last_seen FROM src.apps"""
    ).rowcount
    # last_seen/max: подтянуть более свежий
    con.execute(
        """UPDATE main.apps SET last_seen = (
               SELECT MAX(v) FROM (SELECT last_seen AS v FROM main.apps WHERE track_id = main.apps.track_id
                                   UNION ALL SELECT last_seen FROM src.apps WHERE track_id = main.apps.track_id))
           WHERE track_id IN (SELECT track_id FROM src.apps)"""
    )

    snaps_added = con.execute(
        """INSERT INTO main.snapshots
           (track_id, country, category, query, scanned_at, version, version_date,
            rating_count, avg_rating, rank)
           SELECT s.track_id, s.country, s.category, s.query, s.scanned_at, s.version,
                  s.version_date, s.rating_count, s.avg_rating, s.rank
           FROM src.snapshots s
           WHERE NOT EXISTS (
               SELECT 1 FROM main.snapshots m
               WHERE m.track_id = s.track_id AND m.scanned_at = s.scanned_at)"""
    ).rowcount

    # play-таблицы, если есть в обеих
    for tbl, pk in [("play_apps", "app_id")]:
        try:
            con.execute(
                f"""INSERT OR IGNORE INTO main.{tbl} SELECT * FROM src.{tbl}"""
            )
        except sqlite3.OperationalError:
            pass
    try:
        con.execute(
            """INSERT INTO main.play_snapshots
               (app_id, country, category, query, scanned_at, updated_at, stale_days,
                rating, ratings_count, installs)
               SELECT s.app_id, s.country, s.category, s.query, s.scanned_at, s.updated_at,
                      s.stale_days, s.rating, s.ratings_count, s.installs
               FROM src.play_snapshots s
               WHERE NOT EXISTS (SELECT 1 FROM main.play_snapshots m
                                 WHERE m.app_id = s.app_id AND m.scanned_at = s.scanned_at)"""
        )
    except sqlite3.OperationalError:
        pass

    con.commit()
    con.execute("DETACH DATABASE src")
    total = con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    con.close()
    print(f"merge OK: +{apps_added} apps, +{snaps_added} snapshots; в базе теперь {total} снапшотов")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--main", default="niches.db")
    ap.add_argument("--other", required=True)
    args = ap.parse_args()
    if not os.path.exists(args.other):
        sys.exit(f"нет файла {args.other}")
    merge(args.main, args.other)
