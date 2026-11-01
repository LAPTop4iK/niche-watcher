#!/usr/bin/env python3
"""Explicit cohort scenarios. No country-level recommendation from invented affinity.

paid = one-time unlock AFTER a free installation; upfront paid downloads are
already purchases and require a different acquisition denominator. Sub revenue
is a finite cohort projection, not MRR or a payback period.
"""
import argparse
import json
import os
import math

HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    with open(os.path.join(HERE, 'markets.json'), encoding='utf-8') as f:
        data = json.load(f)
    return data['markets'], dict(data['_defaults_research'])


def validate(mk,d,installs=1,price=0,ads_share=.5):
    def finite(value):
        return isinstance(value,(int,float)) and math.isfinite(value)
    if not finite(installs) or installs<0 or not finite(price) or price<0:
        raise ValueError('Installs and price must be finite and nonnegative')
    for name,value in [('store_commission',mk['store_commission']),('refund_rate',d.get('refund_rate',0)),
        ('monthly_churn_utility',d['monthly_churn_utility']),('ads_share',ads_share),
        ('one_time_purchase_convert',d.get('one_time_purchase_convert',.02)),
        ('freemium_sub_convert_30d',d['freemium_sub_convert_30d']),
        ('freemium_sub_convert_top',d['freemium_sub_convert_top']),
        ('d1_retention',d['d1_retention']),('d7_retention',d['d7_retention']),('d30_retention',d['d30_retention'])]:
        if not finite(value) or not 0<=value<=1:
            raise ValueError(name+' must be in [0,1]')
    if not 0<=d['d30_retention']<=d['d7_retention']<=d['d1_retention']<=1:
        raise ValueError('Scenario assumes monotone D1/D7/D30 retention')
    horizon=d.get('horizon_months',12)
    if not isinstance(horizon,int) or not 1<=horizon<=120:
        raise ValueError('Horizon must be an integer in [1,120]')
    for name,value in [('eCPM',mk['ecpm_interstitial_ios']),('ad_impressions_per_dau',d['ad_impressions_per_dau'])]:
        if not finite(value) or value<0:
            raise ValueError(name+' must be finite and nonnegative')


def active_days(d):
    """Expected active days in D0..D30, linearly interpolated retention."""
    knots = [(0, 1), (1, d['d1_retention']), (7, d['d7_retention']), (30, d['d30_retention'])]
    total = 1.0
    for (start, left), (end, right) in zip(knots, knots[1:]):
        total += sum(left + (right-left) * (day-start)/(end-start) for day in range(start+1, end+1))
    return total


def geo_conv(mk, d, kind, conv_top=False):
    # Legacy 1..5 affinity ratings are uncalibrated opinions, not multipliers.
    if kind == 'paid':
        return d.get('one_time_purchase_convert', 0.02)  # illustrative assumption
    return d['freemium_sub_convert_top'] if conv_top else d['freemium_sub_convert_30d']


def rpd_ads(mk, d):
    return mk['ecpm_interstitial_ios']/1000 * d['ad_impressions_per_dau'] * active_days(d)


def rpd_paid(mk, d, price, conv_top=False):
    return geo_conv(mk, d, 'paid') * price * (1-mk['store_commission']) * (1-d.get('refund_rate', 0))


def ltv_sub(mk, d, price, conv_top=False):
    cycles = sum((1-d['monthly_churn_utility'])**i for i in range(d.get('horizon_months', 12)))
    return geo_conv(mk, d, 'sub', conv_top) * price * cycles * (1-mk['store_commission']) * (1-d.get('refund_rate', 0))


def model_revenue(geo, mk, d, model, installs, price, conv=None, ads_share=0.5, conv_top=False):
    d = dict(d)
    if conv is not None:
        if not 0 <= conv <= 1:
            raise ValueError('conversion must be in [0,1]')
        d.update(one_time_purchase_convert=conv, freemium_sub_convert_30d=conv, freemium_sub_convert_top=conv)
    validate(mk,d,installs,price,ads_share)
    values = {'ads': rpd_ads(mk,d), 'paid': rpd_paid(mk,d,price), 'sub': ltv_sub(mk,d,price,conv_top)}
    values['hybrid'] = ads_share*values['ads'] + (1-ads_share)*values['sub']
    return installs*values[model]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--geo', default='us')
    ap.add_argument('--model', default='compare', choices=['compare','ads','paid','sub','hybrid','rank'])
    ap.add_argument('--installs', type=int, default=10000)
    ap.add_argument('--price', type=float, default=4.99)
    ap.add_argument('--conv-top', action='store_true')
    ap.add_argument('--observed',action='store_true',help='показать записанный факт за период без экстраполяции')
    ap.add_argument('--horizon',type=int,default=12)
    ap.add_argument('--churn',type=float,help='месячный отток, доля 0..1')
    ap.add_argument('--refund',type=float,help='доля возвратов 0..1')
    ap.add_argument('--acquisition-cost',type=float,help='стоимость привлечения одной установки; 0 для органики')
    ap.add_argument('--conversion', type=float, help='доля платящих (0..1), явный сценарий')
    ap.add_argument('--variable-cost', type=float, default=0, help='затраты на API/сервис на установку, USD')
    args = ap.parse_args()
    if args.installs <= 0 or args.price < 0 or args.variable_cost < 0 or (args.conversion is not None and not 0 <= args.conversion <= 1):
        ap.error('нужны установки >0, неотрицательные цена/затраты и конверсия 0..1')
    markets,d = load()
    if args.observed:
        with open(os.path.join(HERE,'markets.json'),encoding='utf-8') as f:
            fact=json.load(f).get('_calibration')
        print(json.dumps(fact,ensure_ascii=False,indent=2) if fact else 'Наблюдаемых данных ещё нет')
        return
    d['horizon_months']=args.horizon
    if args.churn is not None:
        d['monthly_churn_utility']=args.churn
    if args.refund is not None:
        d['refund_rate']=args.refund
    if any(not math.isfinite(v) for v in (args.price,args.variable_cost)) or (args.acquisition_cost is not None and (not math.isfinite(args.acquisition_cost) or args.acquisition_cost<0)):
        ap.error('Нужны конечные неотрицательные затраты и цена')
    print('СЦЕНАРИЙ, не прогноз. Paid = разовая покупка внутри бесплатного приложения.\n'
          f'Ads: D0–D30; Sub: когорта за {args.horizon} мес при постоянном churn. Горизонты различаются.\n'
          'Affinity не используется; выбор модели требует данных конкретной ниши.\n'
          'Калибровка _calibration — наблюдение за период, не LTV; автоматически не экстраполируется.\n')
    geos = markets if args.model == 'rank' or args.geo == 'all' else args.geo.split(',')
    for geo in geos:
        if geo not in markets:
            ap.error('Неизвестная страна '+geo)
        mk=markets[geo]
        try:
            validate(mk,d,args.installs,args.price)
        except ValueError as error:
            ap.error(str(error))
        models=['ads','paid','sub','hybrid'] if args.model in ('compare','rank') else [args.model]
        for model in models:
            rev=model_revenue(geo,mk,d,model,args.installs,args.price,conv=args.conversion,conv_top=args.conv_top)
            per=rev/args.installs
            acquisition=mk['cpi_usd'] if args.acquisition_cost is None else args.acquisition_cost
            contribution=per-args.variable_cost-acquisition
            print(f'{geo} {model}: поступления ${rev:,.0f}; ${per:.3f}/установку; '
                  f'после заданных переменных затрат и CPI ${contribution:+.3f}/уст.')
    print('\nЭто не прибыль: не учтены труд, налоги и неуказанные расходы. '
          'Срок окупаемости без временного ряда платежей неизвестен. '
          'Hybrid: взаимоисключающие доли рекламной и подписочной воронок 50/50 — только допущение.')


if __name__ == '__main__':
    main()
