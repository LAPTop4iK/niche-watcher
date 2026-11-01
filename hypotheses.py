#!/usr/bin/env python3
"""Descriptive hypothesis checks. No revenue outcomes are manufactured."""
import argparse
import db_store
from collections import defaultdict,Counter
from datetime import datetime,timezone,timedelta
import json
import math
from pathlib import Path
import random
import sqlite3
import evidence


def pearson(xs,ys):
    if len(xs)<3 or len(xs)!=len(ys):
        return None
    mx=sum(xs)/len(xs);my=sum(ys)/len(ys)
    numerator=sum((x-mx)*(y-my) for x,y in zip(xs,ys))
    denominator=math.sqrt(sum((x-mx)**2 for x in xs)*sum((y-my)**2 for y in ys))
    return numerator/denominator if denominator else None


def ranks(values):
    ordered=sorted(enumerate(values),key=lambda x:x[1]);result=[0.0]*len(values)
    start=0
    while start<len(ordered):
        end=start+1
        while end<len(ordered) and ordered[end][1]==ordered[start][1]:
            end+=1
        rank=(start+end-1)/2+1
        for index,_ in ordered[start:end]:
            result[index]=rank
        start=end
    return result


def cluster_interval(rows,repetitions=600):
    clusters=defaultdict(lambda:[0,0,0,0])
    for r in rows:
        k=0 if r['group']=='old' else 2
        clusters[r['track_id']][k]+=1
        clusters[r['track_id']][k+1]+=int(r['r90']>=2)
    values=list(clusters.values())
    if len(values)<10:
        return None
    rng=random.Random(20261031);deltas=[]
    for _ in range(repetitions):
        totals=[0,0,0,0]
        for _ in values:
            sample=rng.choice(values)
            for i in range(4):
                totals[i]+=sample[i]
        if totals[0] and totals[2]:
            deltas.append(100*(totals[1]/totals[0]-totals[3]/totals[2]))
    if len(deltas)<100:
        return None
    deltas.sort()
    return [deltas[int(.025*len(deltas))],deltas[min(len(deltas)-1,int(.975*len(deltas)))]]


def rating_bin(count):
    return 0 if count<1000 else 1 if count<5000 else 2 if count<20000 else 3


def build(con,now=None):
    now=now or datetime.now(timezone.utc)
    latest=evidence.latest(con);cached=evidence.demand_map(con)
    cohort={};study_id=None;study_cache={};analysis_asof=now
    if 'study_members' in evidence.tables(con):
        study=con.execute('SELECT study_id FROM study_members ORDER BY selected_at DESC LIMIT 1').fetchone()
        if study:
            study_id=study[0]
            cohort={(r['track_id'],r['country']):dict(r) for r in con.execute('SELECT * FROM study_members WHERE study_id=?',(study_id,))}
            dates=[evidence.parse(r['selected_at']) for r in cohort.values()]
            if 'study_reviews' in evidence.tables(con):
                study_cache={(tid,geo):json.loads(payload) for tid,geo,payload in con.execute(
                    'SELECT track_id,country,evidence_json FROM study_reviews WHERE study_id=?',(study_id,))}
                dates.extend(evidence.parse(r['checked_at']) for r in study_cache.values())
            analysis_asof=max(dates) if dates else now

    samples=[];missing=Counter()
    for current in latest:
        if cohort:
            member=cohort.get((current['track_id'],current['country']))
            if not member:
                continue
            r=dict(current,version_date=member['version_date'],release_date=member['release_date'],rating_count=member['rating_count'],feature_asof=member['selected_at'])
        else:
            r=current
        if (r['rating_count'] or 0)<500 or not r.get('version_date'):
            continue
        reference=evidence.parse(r.get('feature_asof') or now.isoformat())
        age=(reference-evidence.parse(r['version_date'])).total_seconds()/86400/30.44
        group='old' if age>=18 else 'fresh' if 0<=age<12 else None
        if group is None:
            continue
        assessment=evidence.assess(con,r,(study_cache if cohort else cached).get((r['track_id'],r['country'])),analysis_asof)
        if assessment['fetch_status']!='OK' or assessment['r90'] is None:
            missing[group]+=1;continue
        samples.append(dict(r,group=group,age=age,r90=assessment['r90']))
    summary={}
    for group in ['old','fresh']:
        selected=[r for r in samples if r['group']==group]
        active=sum(r['r90']>=2 for r in selected)
        summary[group]=dict(n=len(selected),unique_apps=len({r['track_id'] for r in selected}),
                            active=active,rate=active/len(selected) if selected else None,
                            unknown=missing[group])
    bounds=None
    if all(summary[group]['n']+summary[group]['unknown'] for group in ('old','fresh')):
        limits={group:(r['active']/(r['n']+r['unknown']),(r['active']+r['unknown'])/(r['n']+r['unknown'])) for group,r in summary.items()}
        bounds=[100*(limits['old'][0]-limits['fresh'][1]),100*(limits['old'][1]-limits['fresh'][0])]
    strata=defaultdict(lambda:defaultdict(list))
    for r in samples:
        try:
            app_age=(evidence.parse(r.get('feature_asof') or now.isoformat())-evidence.parse(r['release_date'])).days/365.25
            vintage=0 if app_age<2 else 1 if app_age<5 else 2
        except (KeyError,ValueError,TypeError,AttributeError):
            vintage='unknown'
        strata[(r['country'],rating_bin(r['rating_count']),vintage)][r['group']].append(r['r90']>=2)
    numerator=weight=matched=matched_old=matched_fresh=0
    for groups in strata.values():
        if not groups['old'] or not groups['fresh']:
            continue
        w=min(len(groups['old']),len(groups['fresh']))
        numerator+=w*(sum(groups['old'])/len(groups['old'])-sum(groups['fresh'])/len(groups['fresh']))
        weight+=w;matched+=1;matched_old+=len(groups['old']);matched_fresh+=len(groups['fresh'])
    cell_age=defaultdict(lambda:[0,0,0])
    cell_rows=evidence.latest(con,by_category=True) if not study_id else [dict(r) for r in con.execute('''WITH ranked AS (
        SELECT track_id,country,category,rating_count,version_date,ROW_NUMBER() OVER
        (PARTITION BY track_id,country,category ORDER BY scanned_at DESC,id DESC) rn
        FROM snapshots WHERE run_id=?) SELECT * FROM ranked WHERE rn=1''',(study_id,))]
    for r in cell_rows:
        if r['category'].startswith('chart:') or r['category']=='similar-graph' or (r['rating_count'] or 0)<10:
            continue
        if not r.get('version_date'):
            continue
        age=(analysis_asof-evidence.parse(r['version_date'])).days/30.44
        cell=cell_age[(r['category'],r['country'])];cell[0]+=1
        cell[1]+=int(age>=18 and r['rating_count']>=500)
        cell[2]+=int(0<=age<12)
    cells=[v for v in cell_age.values() if v[0]>=8]
    runs=evidence.coverage(con)
    new=[]
    complete=[r for r in runs if r['status']=='COMPLETE' and r['source']=='itunes-search' and (not study_id or r['run_id']==study_id)]
    if complete:
        start=complete[0]['started_at'];release_reference=evidence.parse(complete[0]['finished_at']);cutoff=release_reference-timedelta(days=7)
        discovery_rows=latest if not study_id else [dict(r) for r in con.execute('''WITH ranked AS (
            SELECT track_id,country,release_date,ROW_NUMBER() OVER
            (PARTITION BY track_id,country ORDER BY scanned_at DESC,id DESC) rn
            FROM snapshots WHERE run_id=?) SELECT r.*,a.first_seen FROM ranked r JOIN apps a USING(track_id) WHERE rn=1''',(study_id,))]
        new=[r for r in discovery_rows if r['first_seen']>=start]
        known=[r for r in new if r.get('release_date')]
        launched=sum(cutoff<=evidence.parse(r['release_date'])<=release_reference for r in known)
        new_by_app={r['track_id']:r for r in known}
        discovery=dict(reference_at=release_reference.isoformat(),release_window_days=7,new_storefront_rows=len(new),dated_release_rows=len(known),released_within_7d=launched,new_unique_apps=len({r['track_id'] for r in new}),
                       dated_unique_apps=len(new_by_app),released_unique_apps_7d=sum(cutoff<=evidence.parse(r['release_date'])<=release_reference for r in new_by_app.values()),
                       note='Строки приложение×страна; первый поиск не является датой запуска.')
    else:
        discovery=dict(note='Нужен завершённый прогон с датами первого релиза.')
    sensitivity=[]
    for threshold in [1,2,5,10]:
        sensitivity.append(dict(reviews_90d_threshold=threshold,
            old=sum(r['group']=='old' and r['r90']>=threshold for r in samples),
            fresh=sum(r['group']=='fresh' and r['r90']>=threshold for r in samples)))
    return dict(method=evidence.METHOD,computed_at=now.isoformat(),analysis_asof=analysis_asof.isoformat(),study_id=study_id,
        missing_outcome_bounds_pp=bounds,matched_storefront_weight=weight,matched_old_n=matched_old,matched_fresh_n=matched_fresh,activity_comparison=summary,
        old_minus_fresh_pp=(100*(summary['old']['rate']-summary['fresh']['rate'])
            if summary['old']['rate'] is not None and summary['fresh']['rate'] is not None else None),
        raw_cluster_resampling_interval_pp=cluster_interval(samples),matched_strata=matched,
        matched_old_minus_fresh_pp=100*numerator/weight if weight else None,
        age_share_correlation=dict(cells=len(cells),pearson=pearson([v[1]/v[0] for v in cells],[v[2]/v[0] for v in cells])),
        lifetime_ratings_vs_recent_reviews_spearman=pearson(ranks([r['rating_count'] for r in samples]),ranks([r['r90'] for r in samples])),
        sensitivity=sensitivity,discovery_vs_launch=discovery,
        payment_prediction=dict(status='UNTESTABLE',reason='Нет собственных исходов запуска, чистой прибыли и сопоставимых затрат; отзывы не заменяют платежи.'),
        limitations=['Когорта фиксируется по завершённому поиску; контроль — 20 свежих приложений на страну по стабильному хешу ID, без учёта отзывов. Если study_id отсутствует, сравнение предварительное и использует адаптивную выборку.',
            'Признаки и raw RSS фиксируются для когорты; последующие обновления demand не переписывают этот исторический эксперимент. Захват RSS допускается в окне поиска и до суток после него.',
            'Отсутствующий/пустой RSS не записывается как нулевой спрос.',
            'RSS ограничен 50 записями, поэтому счётчики справа цензурированы.',
            'Повторные страны одного приложения зависимы; интервал пересэмплирует приложения целиком.',
            'Пересэмплирование не исправляет смещение отбора и не создаёт причинного доказательства.',
            'Окна стран и локальные размеры рынков различаются.',
            'Сопоставление не выравнивает категории, доступ к дистрибуции, рекламные бюджеты и качество продукта.'])


def markdown(data):
    def number(value):
        return 'недостаточно данных' if value is None else f'{value:.3f}'
    old=data['activity_comparison']['old'];fresh=data['activity_comparison']['fresh']
    lines=['# Проверка гипотез и корреляций',
        '\nЭто описательная проверка доступных наблюдений. Прибыль и причинный эффект не измерены.\n',
        'Когорта: '+str(data.get('study_id') or 'предварительная, не зафиксирована')+'; исходы на '+str(data.get('analysis_asof')),
        '| Группа | App×страна с пригодным RSS | Уникальных приложений | ≥2 отзыва / 90 дней | Доля | Нет пригодных данных |',
        '|---|---:|---:|---:|---:|---:|']
    for name,r in [('Версия ≥18 месяцев',old),('Версия <12 месяцев',fresh)]:
        lines.append(f"| {name} | {r['n']} | {r['unique_apps']} | {r['active']} | {number(r['rate'])} | {r['unknown']} |")
    lines += [f"\nСырая разность долей старых и свежих, п.п.: {number(data['old_minus_fresh_pp'])}.",
        f"Разность после сопоставления по стране, группам массы оценок и группам возраста приложения: {number(data['matched_old_minus_fresh_pp'])} п.п.; страт {data['matched_strata']}, вес {data['matched_storefront_weight']}; в сопоставленных стратах {data['matched_old_n']} старых / {data['matched_fresh_n']} свежих строк.",
        f"Границы сырой разности при крайних предположениях об отсутствующих исходах: {data['missing_outcome_bounds_pp']} п.п. Это границы чувствительности, не доверительный интервал.",
        f"Интервал для сырой разности, пересэмплирование по приложениям: {data['raw_cluster_resampling_interval_pp']}. Это не доверительный интервал для всего рынка.",
        f"\nКорреляция долей старых и свежих версий: {number(data['age_share_correlation']['pearson'])}; ячеек {data['age_share_correlation']['cells']}. Общий знаменатель и непересекающиеся возраста создают структурную зависимость.",
        f"Корреляция Спирмена накопленных оценок с недавними отзывами: {number(data['lifetime_ratings_vs_recent_reviews_spearman'])}. Ограничение RSS и неодинаковые рынки ограничивают интерпретацию.",
        '\nЧувствительность количества активных к порогу отзывов: '+json.dumps(data['sensitivity'],ensure_ascii=False),
        '\nПервое обнаружение и запуск: '+json.dumps(data['discovery_vs_launch'],ensure_ascii=False),
        '\n**Предсказание прибыли: не проверяется.** '+data['payment_prediction']['reason'],
        '\nОграничения:\n']+['- '+x for x in data['limitations']]
    return '\n'.join(lines)+'\n'


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--db',default='niches.db');ap.add_argument('--out',default='docs/hypotheses.json');ap.add_argument('--report',default='docs/hypotheses.md');args=ap.parse_args()
    db_store.ensure(args.db)
    with sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro',uri=True) as con:
        con.execute('BEGIN');data=build(con)
    Path(args.out).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    Path(args.report).write_text(markdown(data))
    print('Hypothesis checks saved:',args.report)


if __name__=='__main__':
    main()
