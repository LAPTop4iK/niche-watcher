#!/usr/bin/env python3
"""Weekly activity digest. Use --print for a preview; --send sends explicitly."""
import argparse
import db_store
from pathlib import Path
import html
import os
import sqlite3
from collections import Counter
import evidence
import scanner as sc


def build_message(con):
    con.row_factory=sqlite3.Row
    rows=evidence.latest(con)
    cached=evidence.demand_map(con)
    candidates=[]
    for r in rows:
        if evidence.is_candidate(r):
            r.update(evidence.assess(con,r,cached.get((r['track_id'],r['country']))))
            candidates.append(r)
    counts=Counter(r['verdict'] for r in candidates)
    geos=len({r['country'] for r in rows})
    unique=con.execute('SELECT COUNT(*) FROM apps').fetchone()[0]
    lines=['🔭 <b>Niche Watcher — очередь проверки</b>',
           f'База: {unique:,} приложений · стран с данными: {geos}/24',
           'Срез базы: '+str(con.execute('SELECT MAX(scanned_at) FROM snapshots').fetchone()[0] or 'нет наблюдений'),
           f'Старые версии ≥500 оценок: {len(candidates)} записей приложение × страна',
           '\n'+' · '.join(f'{evidence.LABELS[s]}: {counts[s]}' for s in evidence.LABELS),
           '\nАктивность ≠ платежи. Ни один статус сам по себе не означает «делать».']
    growing=sorted((r for r in candidates if r['velocity'] is not None and r['velocity']>0),
                   key=lambda r:-r['velocity'])[:5]
    if growing:
        lines.append('\n<b>Прирост оценок старых версий (история ≥7 дней)</b>')
        for r in growing:
            url=f"https://apps.apple.com/{r['country']}/app/id{r['track_id']}"
            lines.append(f"• <a href=\"{url}\">{html.escape(r['name'][:120])}</a> {sc.flag_emoji(r['country'])}: "
                         f"{r['velocity']:+.0f}/30д за {r['days']:.1f} дн.")
    else:
        lines.append('\nНадёжного положительного прироста пока нет. Проверь срок наблюдений в дашборде.')
    ranked=sorted((r for r in candidates if r['verdict']=='ALIVE'),
        key=lambda r:(-int((r.get('price') or 0)>0 and (r.get('bad_recent_count') or 0)>0),
                      -int((r.get('bad_recent_count') or 0)>0),-(r['r90'] or 0)))
    active=[];seen=set()
    for row in ranked:
        if row['track_id'] not in seen:
            active.append(row);seen.add(row['track_id'])
        if len(active)==3:
            break
    if active:
        lines.append('\n<b>Для ручного исследования</b> (не рейтинг заработка)')
        for r in active:
            url=f"https://apps.apple.com/{r['country']}/app/id{r['track_id']}"
            lines.append(f"• <a href=\"{url}\">{html.escape(r['name'][:120])}</a> {sc.flag_emoji(r['country'])}: "
                         f"R90 ≥ {r['r90'] or 0}; свежие 1–2★: {r['bad_recent_count'] if r['bad_recent_count'] is not None else '—'}")
    lines.append(f'\n📊 <a href="{sc.telegram_dashboard_url()}">Доказательства и следующий шаг</a>')
    return '\n'.join(lines)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--db',default=os.path.join(os.path.dirname(__file__),'niches.db'))
    ap.add_argument('--print',action='store_true')
    ap.add_argument('--send',action='store_true')
    args=ap.parse_args()
    db_store.ensure(args.db)
    with sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro',uri=True) as con:
        msg=build_message(con)
    if args.print or not args.send:
        print(msg)
    if args.send:
        import notifications
        from datetime import datetime,timezone
        with sqlite3.connect(args.db) as con:
            notifications.enqueue(con,'weekly:'+datetime.now(timezone.utc).strftime('%G-%V'),msg)
            if not os.environ.get('TG_BOT_TOKEN') or not os.environ.get('TG_CHAT_ID'):
                raise SystemExit('Telegram credentials missing; message stays pending')
            _,failed=notifications.flush(con,sc.send_telegram)
            if failed:
                raise SystemExit('Telegram not acknowledged; message stays pending')


if __name__=='__main__':
    main()
