#!/usr/bin/env python3
"""Explicit cohort scenarios. No country-level recommendation from invented affinity.

paid = one-time unlock AFTER a free installation; upfront paid downloads are
already purchases and require a different acquisition denominator. Sub revenue
is a finite cohort projection, not MRR or a payback period. The output states
what reality would have to look like: required searches and CPT, not a forecast.
"""
import argparse
import json
import os
import math
import random

HERE = os.path.dirname(os.path.abspath(__file__))
STANDARD_FEE=.30
EU_STANDARD_FEE=.26
RENEWAL_FEE=.15  # After a subscriber's first year.
# SplitMetrics 2018, low confidence: tap-through by search position.
TTR_BY_RANK={1:.284,2:.062,3:.027,5:.012,9:.010}
# P25, median, P75. RevenueCat 2026 and AppTweak 2025, Utilities in the US.
PRIORS=dict(install_to_paid=(.010,.024,.051),ltv_payer=(9.0,19.0,37.0),cpt=(.80,1.23,2.75),
            ttr=(.085,.106,.13),tap_to_install=(.598,.632,.70))
Z75=.6745


def load():
    with open(os.path.join(HERE, 'markets.json'), encoding='utf-8') as f:
        data = json.load(f)
    return data['markets'], dict(data['_defaults_research'])


def validate(mk,d,installs=1,price=0,ads_share=.5):
    def finite(value):
        return isinstance(value,(int,float)) and math.isfinite(value)
    if not finite(installs) or installs<0 or (price is not None and (not finite(price) or price<0)):
        raise ValueError('Installs and price must be finite and nonnegative')
    churn=d.get('monthly_churn_utility')
    for name,value in [('store_commission',mk['store_commission']),('vat_rate',mk.get('vat_rate',0)),('refund_rate',d.get('refund_rate',0)),
        ('monthly_churn_utility',0 if churn is None else churn),('ads_share',ads_share),
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


def retention(d,day):
    """Convex curve through D1/D7/D30: a power law between knots, extended past D30."""
    knots=[(1,d['d1_retention']),(7,d['d7_retention']),(30,d['d30_retention'])]
    if day==0:
        return 1.0
    for (start,left),(end,right) in zip(knots,knots[1:]):
        if day<=end or end==30:
            if left<=0 or right<=0:
                return left+(right-left)*min(day-start,end-start)/(end-start)
            return left*(day/start)**(math.log(right/left)/math.log(end/start))
    raise ValueError('Retention knots are incomplete')


def active_days(d,days=30):
    """Expected active days in D0..Dn."""
    return sum(retention(d,day) for day in range(days+1))


def fee(mk,program,months_elapsed=0):
    """Apple's share. The Small Business Program needs an application and ends at $1M."""
    if program=='small-business':
        return mk['store_commission']
    if program!='standard':
        raise ValueError('Unknown commission program')
    if months_elapsed>=12:
        return RENEWAL_FEE
    return EU_STANDARD_FEE if mk.get('eu') else STANDARD_FEE


def net_price(price,tax,commission,refund=0):
    """A store price includes VAT, and the commission applies after it is removed."""
    return price/(1+tax)*(1-commission)*(1-refund)


def survival(plan,cycle):
    return 1.0 if cycle==0 else plan['first_renewal']*plan['later_renewal']**(cycle-1)


def plan_ltv(mk,d,plan,price=None,program='small-business'):
    """Net revenue per payer of one plan within the horizon."""
    horizon=d.get('horizon_months',12);per_year=plan['cycles_per_year']
    churn=d.get('monthly_churn_utility');total=0
    for cycle in range(math.ceil(horizon*per_year/12)):
        months=cycle*12/per_year
        alive=(1-churn)**months if churn is not None else survival(plan,cycle)
        total+=alive*net_price(plan['price'] if price is None else price,mk.get('vat_rate',0),fee(mk,program,months),d.get('refund_rate',0))
    return total


def ltv_payer(mk,d,program='small-business'):
    """L = Σ_plan w × Σ_{t<H} survival(t) × p, shares normalized."""
    plans=d['plans'].values();weight=sum(p['share'] for p in plans)
    return sum(p['share']/weight*plan_ltv(mk,d,p,program=program) for p in plans)


def cac(cpt,tap_to_install,install_to_paid):
    return cpt/(tap_to_install*install_to_paid)


def break_even_cpt(ltv,tap_to_install,install_to_paid):
    return ltv*tap_to_install*install_to_paid


def searches_required(target,ttr,page_to_install,install_to_paid,ltv):
    return target/(ttr*page_to_install*install_to_paid*ltv)


def payers_max(market_impressions,share,ttr,tap_to_install,install_to_paid):
    return market_impressions*share*ttr*tap_to_install*install_to_paid


def draw(rng,quartiles):
    low,median,high=quartiles
    return math.exp(rng.gauss(math.log(median),(math.log(high)-math.log(low))/(2*Z75)))


def monte_carlo(model,priors=PRIORS,runs=10000,seed=1):
    """Independent lognormals through the quartiles; independence is an assumption."""
    rng=random.Random(seed)
    values=sorted(model(**{name:draw(rng,q) for name,q in priors.items()}) for _ in range(runs))
    return dict(p10=values[runs//10],p50=values[runs//2],p90=values[runs*9//10],
                positive_share=sum(v>0 for v in values)/runs)


def sensitivity(model,priors=PRIORS):
    """Swing of the result between a parameter's quartiles: what to measure first."""
    base={name:q[1] for name,q in priors.items()}
    swings={name:abs(model(**dict(base,**{name:q[2]}))-model(**dict(base,**{name:q[0]}))) for name,q in priors.items()}
    return sorted(swings.items(),key=lambda item:-item[1])


def paid_contribution(install_to_paid,ltv_payer,cpt,tap_to_install,**_):
    """Contribution per tap of paid search."""
    return ltv_payer*tap_to_install*install_to_paid-cpt


def geo_conv(mk, d, kind, conv_top=False):
    # Legacy 1..5 affinity ratings are uncalibrated opinions, not multipliers.
    if kind == 'paid':
        return d.get('one_time_purchase_convert', 0.02)  # illustrative assumption
    return d['freemium_sub_convert_top'] if conv_top else d['freemium_sub_convert_30d']


def horizon_days(d):
    return round(d.get('horizon_months',12)*30.44)


def rpd_ads(mk, d, days=None):
    return mk['ecpm_interstitial_ios']/1000 * d['ad_impressions_per_dau'] * active_days(d,days or horizon_days(d))


def rpd_paid(mk, d, price, conv_top=False, program='small-business'):
    return geo_conv(mk, d, 'paid') * net_price(price,mk.get('vat_rate',0),fee(mk,program),d.get('refund_rate',0))


def ltv_sub(mk, d, price=None, conv_top=False, program='small-business'):
    """Per install. A given price means one monthly plan; otherwise the plan mix."""
    payer=ltv_payer(mk,d,program) if price is None else plan_ltv(mk,d,d['plans']['monthly'],price,program)
    return geo_conv(mk, d, 'sub', conv_top) * payer


def model_revenue(geo, mk, d, model, installs, price, conv=None, ads_share=0.5, conv_top=False, program='small-business'):
    d = dict(d)
    if conv is not None:
        if not 0 <= conv <= 1:
            raise ValueError('conversion must be in [0,1]')
        d.update(one_time_purchase_convert=conv, freemium_sub_convert_30d=conv, freemium_sub_convert_top=conv)
    validate(mk,d,installs,price,ads_share)
    values = {'ads': rpd_ads(mk,d), 'sub': ltv_sub(mk,d,price,conv_top,program)}
    values['paid'] = rpd_paid(mk,d,4.99 if price is None else price,program=program)
    # Both shares of a hybrid now run over the same horizon.
    values['hybrid'] = ads_share*values['ads'] + (1-ads_share)*values['sub']
    return installs*values[model]


def market_priors(mk,d,program):
    """The published payer value is American; this storefront keeps less of a price."""
    us_like=dict(mk,vat_rate=0,eu=False)
    ratio=ltv_payer(mk,d,program)/ltv_payer(us_like,d,program)
    return dict(PRIORS,ltv_payer=tuple(q*ratio for q in PRIORS['ltv_payer']))


def print_requirements(mk,d,program,target,rank,tap_to_install,install_to_paid):
    ltv=ltv_payer(mk,d,program);ttr=TTR_BY_RANK[rank];priors=market_priors(mk,d,program)
    print(f'\nLTV платящего за {d.get("horizon_months",12)} мес, смесь планов: ${ltv:.2f}. RevenueCat Utilities: $9–37, медиана $19.')
    print(f'Платный поиск: при CPT ${PRIORS["cpt"][1]} CAC ${cac(PRIORS["cpt"][1],tap_to_install,install_to_paid):.0f}; безубыточный CPT ${break_even_cpt(ltv,tap_to_install,install_to_paid):.2f}.')
    print(f'Органика: для ${target:,.0f}/мес при позиции {rank} нужно {searches_required(target,ttr,tap_to_install,install_to_paid,ltv):,.0f} поисков в месяц.')
    result=monte_carlo(paid_contribution,priors)
    print(f'Вклад на тап платного поиска: P10 ${result["p10"]:+.2f}, P50 ${result["p50"]:+.2f}, P90 ${result["p90"]:+.2f}; положительный в {result["positive_share"]:.0%} прогонов.')
    print('Что измерить первым: '+', '.join(f'{name} (размах ${swing:.2f})' for name,swing in sensitivity(paid_contribution,priors)[:3])+'.')
    print('CPT и конверсии — априорные значения Utilities, США. Параметры независимы по допущению; бенчмарки взяты по клиентам поставщиков и расходятся вдвое.')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--geo', default='us')
    ap.add_argument('--model', default='compare', choices=['compare','ads','paid','sub','hybrid','rank'])
    ap.add_argument('--installs', type=int, default=10000)
    ap.add_argument('--price', type=float, help='цена месячного плана; по умолчанию смесь планов из markets.json')
    ap.add_argument('--conv-top', action='store_true')
    ap.add_argument('--observed',action='store_true',help='показать записанный факт за период без экстраполяции')
    ap.add_argument('--horizon',type=int,default=12)
    ap.add_argument('--churn',type=float,help='явный постоянный месячный отток вместо кривых планов, доля 0..1')
    ap.add_argument('--refund',type=float,help='доля возвратов 0..1')
    ap.add_argument('--program',default='small-business',choices=['small-business','standard'],help='программа комиссии; Small Business требует заявки и действует до $1M')
    ap.add_argument('--cpt',type=float,help='цена тапа; 0 для сценария органики')
    ap.add_argument('--tap-to-install',type=float,default=PRIORS['tap_to_install'][1])
    ap.add_argument('--install-to-paid',type=float,default=PRIORS['install_to_paid'][1])
    ap.add_argument('--conversion', type=float, help='доля платящих (0..1), явный сценарий')
    ap.add_argument('--cost-per-active-day', type=float, default=0, help='затраты на API/сервис на активного пользователя в день, USD')
    ap.add_argument('--requirements',action='store_true',help='CAC, безубыточный CPT, нужные поиски и Монте-Карло')
    ap.add_argument('--rank',type=int,default=1,choices=sorted(TTR_BY_RANK))
    ap.add_argument('--target',type=float,default=1000,help='цель, $ в месяц')
    args = ap.parse_args()
    price=args.price
    if args.installs <= 0 or (price is not None and price < 0) or args.cost_per_active_day < 0 or (args.conversion is not None and not 0 <= args.conversion <= 1):
        ap.error('нужны установки >0, неотрицательные цена/затраты и конверсия 0..1')
    if not 0<args.tap_to_install<=1 or not 0<args.install_to_paid<=1:
        ap.error('конверсии должны быть в (0,1]')
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
    if any(not math.isfinite(v) for v in (0 if price is None else price,args.cost_per_active_day)) or (args.cpt is not None and (not math.isfinite(args.cpt) or args.cpt<0)):
        ap.error('Нужны конечные неотрицательные затраты и цена')
    print('СЦЕНАРИЙ, не прогноз. Paid = разовая покупка внутри бесплатного приложения.\n'
          f'Горизонт всех моделей {args.horizon} мес. Цена включает НДС витрины, комиссия берётся после его вычета.\n'
          f'Программа комиссии: {args.program}. Sub: смесь планов с шагом на первом продлении.\n'
          'Affinity не используется; выбор модели требует данных конкретной ниши.\n'
          'Калибровка _calibration — наблюдение за период, не LTV; автоматически не экстраполируется.\n')
    geos = markets if args.model == 'rank' or args.geo == 'all' else args.geo.split(',')
    for geo in geos:
        if geo not in markets:
            ap.error('Неизвестная страна '+geo)
        mk=markets[geo]
        try:
            validate(mk,d,args.installs,price)
        except ValueError as error:
            ap.error(str(error))
        models=['ads','paid','sub','hybrid'] if args.model in ('compare','rank') else [args.model]
        for model in models:
            rev=model_revenue(geo,mk,d,model,args.installs,price,conv=args.conversion,conv_top=args.conv_top,program=args.program)
            per=rev/args.installs
            serving=args.cost_per_active_day*active_days(d,horizon_days(d))
            contribution=per-serving-args.cpt/args.tap_to_install if args.cpt is not None else None
            print(f'{geo} {model}: поступления ${rev:,.0f}; ${per:.3f}/установку; '
                  +(f'после обслуживания и цены установки ${contribution:+.3f}/уст.' if contribution is not None else
                    'вклад не рассчитан: задайте --cpt, включая 0 для сценария органики.'))
        if args.requirements:
            print_requirements(mk,d,args.program,args.target,args.rank,args.tap_to_install,args.install_to_paid)
    print('\nЭто не прибыль: не учтены труд, налоги с дохода и неуказанные расходы. '
          'Срок окупаемости без временного ряда платежей неизвестен. '
          'Hybrid: взаимоисключающие доли рекламной и подписочной воронок 50/50 — только допущение.')


if __name__ == '__main__':
    main()
