#!/usr/bin/env python3
"""Weekly activity digest. Use --print for a preview; --send sends explicitly."""
import argparse
import db_store
from pathlib import Path
import html
import os
import re
import sqlite3
from collections import Counter
import evidence
import scanner as sc

APP_LINK=re.compile(r'apps\.apple\.com/[a-z]{2}/app/id(\d+)')


def week_key(now):
    return 'weekly:'+now.strftime('%G-%V')


def previous_weekly_apps(con,now):
    """Apps linked in the last delivered weekly message up to this ISO week."""
    if 'notification_outbox' not in evidence.tables(con):
        return set()
    row=con.execute("""SELECT payload FROM notification_outbox WHERE event_key LIKE 'weekly:%'
        AND event_key<=? AND status='SENT' ORDER BY event_key DESC LIMIT 1""",(week_key(now),)).fetchone()
    return {int(tid) for tid in APP_LINK.findall(row[0])} if row else set()


def build_message(con,now=None):
    """One search-source summary; missing observations never become zero demand."""
    from zoneinfo import ZoneInfo
    import quality_metrics
    now=now or sc.now_utc()
    if not con.in_transaction:con.execute('BEGIN')
    rows=evidence.latest(con)
    queue,coverage=evidence.research_queue(con,now)
    lost=coverage['search']['lost_to_stale_rss']
    queue=[r for r in queue if 'search' in r['sources']]
    quality=quality_metrics.build(con,now,rows,queue,coverage)
    candidates=[r for r in queue if r['lane']=='stale']
    recent=[r for r in queue if r['lane']=='recent_pain']
    counts=Counter(r['verdict'] for r in candidates);q=quality['queries'];c=quality['review_coverage'];p=quality['recent_pain_timing'];paths=quality['alive_paths'];f=quality['queue_funnel']
    geos=len({r['country'] for r in rows if any(not cat.startswith('chart:') and cat!='similar-graph' for cat in r['categories'])})
    last=quality['last_search_query_at']
    last_label=evidence.parse(last).astimezone(ZoneInfo('Europe/Warsaw')).strftime('%d.%m.%Y %H:%M') if last else 'нет журнала запросов'
    shown=previous_weekly_apps(con,now)
    def examples(items,limit):
        """Queue order, but an app with RSS records in 30 days that the previous
        weekly message did not link comes first. Repeats, then R30 = 0 (no record
        in 30 days, the weaker example), only fill a shortfall."""
        out=[];seen=set()
        for row in items:
            if row['track_id'] not in seen:
                out.append(row);seen.add(row['track_id'])
        return sorted(out,key=lambda r:(r.get('r30')==0,r['track_id'] in shown))[:limit]
    old=examples([r for r in candidates if r['verdict']=='ALIVE' and (r.get('bad_recent_count') or 0)>0 and r.get('snapshot_age_days') is not None and 0<=r['snapshot_age_days']<=14],2)
    new=examples(sorted(recent,key=lambda r:(-(r.get('pain_excess') or 0),-int((r.get('bad_current_version_count') or 0)>0),r.get('best_rank') or 1000)),2)
    growing=[r for r in candidates if r['velocity'] is not None and r['velocity']>0]
    # Relative to the app's size: by absolute velocity the line showed the largest
    # apps (week 46: My Love de, 330k ratings). On Nov 6-15 2026 the top pick by
    # velocity/rating_count kept growing next week 10 times of 10, median +3.6%/30d
    # against +1.7% for the absolute pick; a minimum increment only lowered that.
    fastest=max(growing,key=lambda r:r['velocity']/r['rating_count'],default=None)
    t=quality['rss_throttling']
    stops=f" ({', '.join(f'{d[8:10]}.{d[5:7]}' for d in t['stop_days'])}); в эти дни отзывы проверены не все" if t['stop_days'] else ''
    def render(limit):
        lines=['🔭 <b>Niche Watcher — сводка наблюдений</b>',
               f'Поиск App Store · стран с наблюдениями: {geos}. Карточка = приложение × страна; запросы считаются отдельно. Старые версии — до фильтра названий.',
               f'Последнее наблюдение: {last_label} (Варшава); даты стран различаются.',
               f'\n<b>Качество данных</b>\nПригодные запросы за 14 дней: {q["fresh_ok"]}/{q["expected"]}. Выдача, вероятно, усечена (≥35 из 50): {q["likely_truncated"]}/{q["limit_known"]}; рынок шире.',
               f'Пригодный RSS: {c["rss_usable"]}/{c["eligible"]} записей с ≥500 оценок. У {c["rss_recent_capped"]} все полученные отзывы попали в 90 дней: счётчики усечены.',
               f'Остановки RSS из-за пустых ответов (троттлинг): {t["days"]} из {t["window_days"]} дней{stops}.',
               f'\n<b>Версия ≥18 мес, ≥500 оценок</b>: {len(candidates)}',
               f'Активность: {counts["ALIVE"]} · слабый сигнал: {counts["WEAK"]} · нет свежего сигнала: {counts["NO_RECENT_EVIDENCE"]} · неизвестно: {counts["UNKNOWN"]}.',
               f'Активность: ≥2 записи RSS/90д ({paths.get("rss",0)}), прирост оценок сверх шума ({paths.get("velocity",0)}), оба пути ({paths.get("rss+velocity",0)}); это не оплаты, периоды сравнивать внутри пути.',
               f'Воронка: поиск {f["search"]["records"]} → активность {f["alive"]["records"]} → топ-10 {f["top10"]["records"]} → запрос в названии {f["title_match"]["records"]} ({f["title_match"]["apps"]} приложений).',
               f'\n<b>Версия &lt;18 мес + 1–2★ выше базы категории, запрос в названии</b>: {len(recent)}',
               # The timing lines describe every searched recent row with a complaint, not only the queue.
               f'Из {p["total"]} поисковых записей со свежими 1–2★: жалобы после обновления у {p["after_release"]}, только до него у {p["only_before_release"]}. Это не доказательство сохранения или исправления бага.',
               f'RSS собран ещё до обновления у {p["rss_before_release"]} из этих {p["total"]}: нужна повторная проверка. Версия отзыва известна в {c["rss_with_review_versions"]} срезах.']
        if lost:lines.append(f'Выпали из очереди из-за устаревшего RSS: {lost}. Это потеря покрытия, а не исчезновение жалоб.')
        if old or new:lines.append('\n<b>Примеры для проверки — не рейтинг заработка</b>\nR30 — записи RSS за 30 дней, нижняя граница.')
        for r in (old[:limit]+new[:limit]):
            url=f"https://apps.apple.com/{r['country']}/app/id{r['track_id']}"
            count=r['bad_after_version_count'] if r.get('bad_after_version_count') is not None else '?'
            current=r['bad_current_version_count'] if r.get('bad_current_version_count') is not None else '?'
            lines.append(f"• <a href=\"{url}\">{html.escape(r['name'][:70])}</a> {sc.flag_emoji(r['country'])} · {html.escape(sc.download_price_label(r))}\n"
                         f"  Позиция: {r.get('best_rank') or '?'}; R30 ≥ {r['r30'] if r.get('r30') is not None else '?'}; 1–2★ в выборке 90д: {r['bad_recent_count']}; после даты версии: {count}; версия совпала: {current}.")
        if fastest:
            r=fastest
            lines.append(f'\nНадёжный положительный прирост оценок найден у {len(growing)} записей; это не установки или оплаты. Быстрее всех относительно числа оценок:')
            lines.append(f"• {html.escape(r['name'][:70])} {sc.flag_emoji(r['country'])}: {r['velocity']:+.0f}/30д при {r['rating_count']} оценках ({100*r['velocity']/r['rating_count']:+.1f}%), окно {r['days']:.1f} дн.")
        else:lines.append('\nПрироста оценок сверх шума счётчика пока нет. При истории меньше 7 дней он не измеряется, это не нулевой рост.')
        lines.append('\nОплаты и выручка конкурентов неизвестны. Следующий шаг: прочитать жалобы → проверить продукт и альтернативы → проверить предложение оплатой.')
        lines.append(f'\n<a href="{sc.telegram_dashboard_url()}">Открыть дашборд</a>')
        return '\n'.join(lines)
    for limit in (2,1,0):
        message=render(limit)
        if len(message)<=3800 and len(message.encode('utf-16-le'))//2<=3800:return message
    raise ValueError('Summary exceeds the single-message budget')


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
            notifications.enqueue(con,week_key(datetime.now(timezone.utc)),msg)
            if not os.environ.get('TG_BOT_TOKEN') or not os.environ.get('TG_CHAT_ID'):
                raise SystemExit('Telegram credentials missing; message stays pending')
            _,failed=notifications.flush(con,sc.send_telegram)
            if failed:
                raise SystemExit('Telegram not acknowledged; message stays pending')


if __name__=='__main__':
    main()
