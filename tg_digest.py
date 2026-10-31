#!/usr/bin/env python3
"""
TG Digest — еженедельная сводка динамики в Telegram (запускается по субботам).

Содержимое:
  - размер базы и прогонов
  - тренд зомби (сравнение последних прогонов)
  - топ-5 по velocity (кто быстрее всего набирает оценки при гнилой версии)
  - самые быстро зарастающие ниши (приток за 7 дней)
  - ссылка на дашборд

Использование:
    python3 tg_digest.py --print     # показать сообщение
    python3 tg_digest.py --send      # отправить (нужны TG_BOT_TOKEN/TG_CHAT_ID)
"""

import argparse
import html
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner as sc


def is_zombie(version_date, rc, min_months=18, min_ratings=500):
    if not version_date or rc < min_ratings:
        return False
    return sc.months_between(sc.parse_iso(version_date), sc.now_utc()) >= min_months


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(here, "niches.db"))
    ap.add_argument("--print", action="store_true")
    ap.add_argument("--send", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    n_apps = con.execute("SELECT COUNT(*) FROM apps").fetchone()[0]
    n_runs = con.execute("SELECT COUNT(DISTINCT scanned_at) FROM snapshots").fetchone()[0]

    # последние снапшоты всех приложений
    latest = con.execute(
        """SELECT s.track_id, s.country, s.category, s.rating_count, s.avg_rating,
                  s.version_date, a.name, a.store_url
           FROM snapshots s JOIN apps a ON a.track_id = s.track_id
           WHERE s.scanned_at = (SELECT MAX(s2.scanned_at) FROM snapshots s2 WHERE s2.track_id = s.track_id)"""
    ).fetchall()

    zombies = [r for r in latest if is_zombie(r[5], r[3])]
    new_7d = con.execute(
        "SELECT COUNT(*) FROM apps WHERE first_seen >= datetime('now','-7 days')"
    ).fetchone()[0]

    # velocity: треки с 2+ снапшотами >=6 дней между первым и последним
    vel = []
    rows = con.execute(
        """SELECT track_id, MIN(scanned_at), MAX(scanned_at),
                  (SELECT rating_count FROM snapshots s2 WHERE s2.track_id = s.track_id
                    ORDER BY scanned_at DESC LIMIT 1) AS rc_now
           FROM snapshots s GROUP BY track_id
           HAVING julianday(MAX(scanned_at)) - julianday(MIN(scanned_at)) >= 6"""
    ).fetchall()
    for tid, first, last, rc_now in rows:
        rc_first = con.execute(
            "SELECT rating_count FROM snapshots WHERE track_id = ? AND scanned_at = ?",
            (tid, first),
        ).fetchone()[0]
        days = max(1, (sc.parse_iso(last) - sc.parse_iso(first)).days)
        if rc_now and rc_first is not None and rc_now > rc_first:
            vel.append(((rc_now - rc_first) / days * 30.0, tid, rc_now))
    con.close()

    meta = {r[0]: r for r in latest}
    top_vel = sorted(vel, reverse=True)[:5]

    e = html.escape
    lines = [
        f"📈 <b>Niche Watcher — недельная динамика</b>",
        f"База: {n_apps:,} приложений · прогонов: {n_runs} · зомби: <b>{len(zombies)}</b> · "
        f"новых за 7 дней: {new_7d}",
    ]
    if top_vel:
        lines.append("\n🚀 <b>Топ velocity</b> (прирост оценок/30д у гнилых версий):")
        for v, tid, rc in top_vel:
            m = meta.get(tid)
            if not m:
                continue
            lines.append(f"• <a href=\"{m[7]}\">{e(m[6])}</a> {sc.flag_emoji(m[1])} — "
                         f"<b>{v:+.0f}</b>/30д ({rc:,} оценок)")
    lines.append("\n📊 <a href=\"" + sc.telegram_dashboard_url() + "\">дашборд с чартами</a>")
    msg = "\n".join(lines)

    if args.print or not args.send:
        print(msg)
    if args.send:
        sc.send_telegram(msg)


if __name__ == "__main__":
    main()
