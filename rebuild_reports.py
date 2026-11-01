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
import validate_data

ROOT=Path(__file__).resolve().parent


def candidate_report(con,out):
    cached=evidence.demand_map(con);rows=[]
    for row in evidence.latest(con):
        if evidence.is_candidate(row):
            row.update(evidence.assess(con,row,cached.get((row['track_id'],row['country']))))
            rows.append(row)
    rows.sort(key=lambda r:(list(evidence.LABELS).index(r['verdict']),-int((r.get('bad_recent_count') or 0)>0 and (r.get('price') or 0)>0),-(r['r90'] or 0)))
    lines=['# Кандидаты для исследования',f'\nМетод {evidence.METHOD}. Дата {datetime.now(timezone.utc).isoformat(timespec="seconds")}. Всего {len(rows)} app×country. Это не список прибыльных ниш.',
           '\nСначала свежая активность и сочетание платной загрузки со свежими отзывами 1–2★; далее число отзывов. Цена не подтверждает текущие продажи. R90 — нижняя граница выборки RSS. Платежи неизвестны.',
           '\n| Приложение | Страна | Сигнал | Оценок | Цена загрузки | R90 ≥ | Свежие 1–2★ | Версия от |',
           '|---|---|---|---:|---|---:|---:|---|']
    for r in rows:
        name=r['name'].replace('|','/').replace('\n',' ')
        price='—' if r['price'] is None else f"{r['price']} {r.get('currency') or ''}" if r['price'] else 'free'
        lines.append(f"| [{name}](https://apps.apple.com/{r['country']}/app/id{r['track_id']}) | {r['country']} | {r['verdict']} | {r['rating_count']} | {price} | {r['r90'] if r['r90'] is not None else '—'} | {r['bad_recent_count'] if r['bad_recent_count'] is not None else '—'} | {r['version_date'][:10]} |")
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
                  ['dashboard.py','--db',str(snapshot)]]
        for command in commands:
            subprocess.run([sys.executable,*command],cwd=ROOT,check=True,stdout=subprocess.DEVNULL)
        with open(ROOT/'docs/telegram_preview.txt','w') as output:
            subprocess.run([sys.executable,'tg_digest.py','--db',str(snapshot),'--print'],cwd=ROOT,check=True,stdout=output)
    print('Rebuilt candidate, demand, geography, hypotheses, metrics, Telegram preview and dashboard from one snapshot.')


if __name__=='__main__':
    main()
