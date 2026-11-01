#!/usr/bin/env python3
"""Rebuild reports from one coherent SQLite snapshot. No network or messages."""
import argparse
import db_store
from collections import Counter
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import evidence
import demand_check
import dashboard
import quality_metrics
import validate_data

ROOT=Path(__file__).resolve().parent


def candidate_report(con,out):
    rows,coverage=evidence.research_queue(con)
    sample=coverage['all'];funnel=quality_metrics.queue_funnel(rows)
    lines=['# Кандидаты для исследования',f'\nМетод {evidence.METHOD}. Дата {datetime.now(timezone.utc).isoformat(timespec="seconds")}. Всего {len(rows)} app×country. Это не список прибыльных ниш.',
           '\nДва направления: версии ≥18 мес и версии <18 мес, где на текущем выпуске ≥3 разных отзыва 1–2★, их доля выше базы категории×страны и повторяется тема. Это ещё не подтверждённая боль: отзывы нужно прочитать. Порядок: сигнал, лучшая позиция в поиске, превышение базы. Это не рейтинг заработка. R90 — нижняя граница выборки RSS. Платежи неизвестны.',
           f"\nВоронка старых версий: поиск {funnel['search']['records']} → активность {funnel['alive']['records']} → топ-10 {funnel['top10']['records']} → запрос в названии {funnel['title_match']['records']} ({funnel['title_match']['apps']} приложений).",
           f"\nВерсии <18 мес: пригодный RSS {sample['rss_usable']}/{sample['eligible']} app×страна; есть 1–2★ у {sample['any_complaint']}, прошли условие {sample['complaint_records']}; выпали из-за устаревшего RSS {sample['lost_to_stale_rss']}. Это охват проверок, а не частота проблем на рынке.",
           '\n| Приложение | Страна | Направление | Сигнал | Позиция | Запрос | Оценок | Цена загрузки | R90 ≥ | Свежие 1–2★ | Версия от |',
           '|---|---|---|---|---:|---|---:|---|---:|---:|---|']
    for r in rows:
        name=r['name'].replace('|','/').replace('\n',' ')
        price='—' if r['price'] is None else f"{r['price']} {r.get('currency') or ''}" if r['price'] else 'free'
        query=(r.get('best_query') or '—').replace('|','/')
        lines.append(f"| [{name}](https://apps.apple.com/{r['country']}/app/id{r['track_id']}) | {r['country']} | {evidence.LANES[r['lane']]} | {r['verdict']} | {r.get('best_rank') or '—'} | {query} | {r['rating_count']} | {price} | {r['r90'] if r['r90'] is not None else '—'} | {r['bad_recent_count'] if r['bad_recent_count'] is not None else '—'} | {r['version_date'][:10]} |")
    out.write_text('\n'.join(lines)+'\n')


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--db',default=str(ROOT/'niches.db'));ap.add_argument('--require-complete',action='store_true');ap.add_argument('--require-run');args=ap.parse_args()
    db_store.ensure(args.db)
    # All generated artifacts refer to the same snapshot, even if a producer starts.
    with tempfile.TemporaryDirectory(prefix='niche-report-') as folder:
        snapshot=Path(folder)/'snapshot.db'
        with sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro',uri=True) as source:
            destination=sqlite3.connect(snapshot);source.backup(destination);destination.close()
        with sqlite3.connect(snapshot) as con:
            checks,errors=validate_data.validate(con,args.require_complete,args.require_run)
            if errors:
                raise SystemExit('; '.join(errors))
            demand_check.write_cached_report(con,str(ROOT/'demand_report.md'))
            candidate_report(con,ROOT/'report.md')
        commands=[['scoreboard.py','--db',str(snapshot),'--top','100'],
                  ['geo_gap.py','--db',str(snapshot)],['divergence.py','--db',str(snapshot)],
                  ['hypotheses.py','--db',str(snapshot)],
                  ['audit_metrics.py','--db',str(snapshot),'--out',str(ROOT/'docs/current_metrics.json')],
                  ['quality_metrics.py','--db',str(snapshot),'--out',str(ROOT/'docs/data_quality.json')],
                  ['dashboard.py','--db',str(snapshot)]]
        for command in commands:
            subprocess.run([sys.executable,*command],cwd=ROOT,check=True,stdout=subprocess.DEVNULL)
        with open(ROOT/'docs/telegram_preview.txt','w') as output:
            subprocess.run([sys.executable,'tg_digest.py','--db',str(snapshot),'--print'],cwd=ROOT,check=True,stdout=output)
    print('Rebuilt candidate, demand, geography, hypotheses, metrics, Telegram preview and dashboard from one snapshot.')


if __name__=='__main__':
    main()
