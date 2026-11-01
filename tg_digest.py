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
    quality=quality_metrics.build(con,now,rows,queue)
    candidates=[r for r in queue if r['lane']=='stale']
    recent=[r for r in queue if r['lane']=='recent_pain']
    counts=Counter(r['verdict'] for r in candidates);q=quality['queries'];c=quality['review_coverage'];p=quality['recent_pain_timing'];paths=quality['alive_paths']
    geos=len({r['country'] for r in rows if any(not cat.startswith('chart:') and cat!='similar-graph' for cat in r['categories'])})
    last=quality['last_search_query_at']
    last_label=evidence.parse(last).astimezone(ZoneInfo('Europe/Warsaw')).strftime('%d.%m.%Y %H:%M') if last else 'нет журнала запросов'
    def examples(items,limit):
        out=[];seen=set()
        for row in items:
            if row['track_id'] not in seen:
                out.append(row);seen.add(row['track_id'])
            if len(out)==limit:break
        return out
    old=examples([r for r in candidates if r['verdict']=='ALIVE' and r.get('title_match') and (r.get('bad_recent_count') or 0)>0 and r.get('snapshot_age_days') is not None and 0<=r['snapshot_age_days']<=14],2)
    new=examples(sorted(recent,key=lambda r:(-int(bool(r.get('title_match'))),-int((r.get('bad_current_version_count') or 0)>0),-int((r.get('bad_after_version_count') or 0)>0))),2)
    growing=[r for r in candidates if r['velocity'] is not None and r['velocity']>0]
    def render(limit):
        lines=['🔭 <b>Niche Watcher — сводка наблюдений</b>',
               f'Поиск App Store · стран с наблюдениями: {geos}. Карточка = приложение × страна; запросы считаются отдельно. До фильтра названий.',
               f'Последнее наблюдение: {last_label} (Варшава); даты стран различаются.',
               f'\n<b>Качество данных</b>\nПригодные запросы за 14 дней: {q["fresh_ok"]}/{q["expected"]}. Достигли лимита выдачи: {q["at_limit"]}/{q["limit_known"]}; рынок может быть шире.',
               f'Пригодный RSS: {c["rss_usable"]}/{c["eligible"]} записей с ≥500 оценок. У {c["rss_recent_capped"]} все 50 отзывов попали в 90 дней: счётчики усечены.',
               f'\n<b>Версия ≥18 мес, ≥500 оценок</b>: {len(candidates)}',
               f'Активность: {counts["ALIVE"]} · слабый сигнал: {counts["WEAK"]} · нет свежего сигнала: {counts["NO_RECENT_EVIDENCE"]} · неизвестно: {counts["UNKNOWN"]}.',
               f'Активность: ≥2 записи RSS/90д ({paths.get("rss",0)}), прирост оценок сверх шума ({paths.get("velocity",0)}), оба пути ({paths.get("rss+velocity",0)}); это не оплаты, периоды сравнивать внутри пути.',
               f'\n<b>Версия &lt;18 мес + свежие 1–2★</b>: {len(recent)}',
               f'Есть жалобы по времени после обновления: {p["after_release"]}; только до него: {p["only_before_release"]}. Это не доказательство сохранения или исправления бага.',
               f'RSS собран ещё до обновления у {p["rss_before_release"]} из них: нужна повторная проверка. Версия отзыва известна в {c["rss_with_review_versions"]} срезах.']
        if lost:lines.append(f'Выпали из очереди из-за устаревшего RSS: {lost}. Это потеря покрытия, а не исчезновение жалоб.')
        if old or new:lines.append('\n<b>Примеры для проверки — не рейтинг заработка</b>\nR30 — записи RSS за 30 дней, нижняя граница.')
        for r in (old[:limit]+new[:limit]):
            url=f"https://apps.apple.com/{r['country']}/app/id{r['track_id']}"
            count=r['bad_after_version_count'] if r.get('bad_after_version_count') is not None else '?'
            current=r['bad_current_version_count'] if r.get('bad_current_version_count') is not None else '?'
            lines.append(f"• <a href=\"{url}\">{html.escape(r['name'][:70])}</a> {sc.flag_emoji(r['country'])} · {html.escape(sc.download_price_label(r))}\n"
                         f"  R30 ≥ {r['r30'] if r.get('r30') is not None else '?'}; 1–2★ в выборке 90д: {r['bad_recent_count']}; после даты версии: {count}; версия совпала: {current}.")
        if growing:
            lines.append(f'\nНадёжный положительный прирост оценок найден у {len(growing)} записей; это не установки или оплаты.')
            for r in sorted(growing,key=lambda r:-r['velocity'])[:1]:
                lines.append(f"• {html.escape(r['name'][:70])} {sc.flag_emoji(r['country'])}: {r['velocity']:+.0f}/30д, окно {r['days']:.1f} дн.")
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
            notifications.enqueue(con,'weekly:'+datetime.now(timezone.utc).strftime('%G-%V'),msg)
            if not os.environ.get('TG_BOT_TOKEN') or not os.environ.get('TG_CHAT_ID'):
                raise SystemExit('Telegram credentials missing; message stays pending')
            _,failed=notifications.flush(con,sc.send_telegram)
            if failed:
                raise SystemExit('Telegram not acknowledged; message stays pending')


if __name__=='__main__':
    main()
