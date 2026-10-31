#!/usr/bin/env python3
"""
Revenue Model — юнит-экономика монетизации нишевого приложения по рынкам.

Формулы (все параметры берутся из markets.json, там же ссылки на источники):

  ADS:      RPD   = ecpm/1000 × impressions_per_dau × avg_active_days
            avg_active_days = d1 + (d1-d30)/29 × ... — упрощённо: d1 + d7 + d30 (активные дни на юзера)
  PAID:     RPD   = hardpaywall_conv × price × (1 - commission)
  SUB:      LTV   = trial_conv × trial_to_paid × price × (1/churn) × (1 - commission)
  HYBRID:   сумма долей.

  Break-even CPI = RPD (ads/paid) или LTV (sub) — выше этого платить за установку нельзя.

Использование:
    python3 revenue_model.py --geo us,jp,de --installs 10000 --model compare
    python3 revenue_model.py --geo jp --model paid --price 6.99 --installs 5000
    python3 revenue_model.py --geo us --model sub --price 4.99 --conv-top        # топ-квартиль конверсии
"""

import argparse
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    with open(os.path.join(HERE, "markets.json"), encoding="utf-8") as f:
        data = json.load(f)
    return data["markets"], data["_defaults_research"]


def active_days(d):
    """Активные дни на установку: retention-кривая d1/d7/d30 как суррогат площади под кривой."""
    return d["d1_retention"] + d["d7_retention"] + d["d30_retention"]


def geo_conv(mk, d, kind, conv_top=False):
    """Конверсия, скорректированная платёжной культурой рынка (affinity 3 = базовая линия)."""
    base = d["freemium_sub_convert_top"] if conv_top else d["freemium_sub_convert_30d"]
    if kind == "paid":
        return d["hardpaywall_convert"] * (mk["paid_affinity"] / 3.0)
    return base * (mk["sub_affinity"] / 3.0)


def rpd_ads(mk, d):
    return mk["ecpm_interstitial_ios"] / 1000 * d["ad_impressions_per_dau"] * active_days(d)


def rpd_paid(mk, d, price, conv_top=False):
    return geo_conv(mk, d, "paid") * price * (1 - mk["store_commission"])


def ltv_sub(mk, d, price, conv_top=False):
    months = 1.0 / d["monthly_churn_utility"]
    return geo_conv(mk, d, "sub", conv_top) * price * months * (1 - mk["store_commission"])


def model_revenue(geo, mk, d, model, installs, price, conv=None, ads_share=0.5):
    """Выручка за installs по выбранной модели, USD (до вычета затрат на трафик)."""
    if model == "ads":
        return installs * rpd_ads(mk, d)
    if model == "paid":
        return installs * rpd_paid(mk, d, price)
    if model == "sub":
        return installs * ltv_sub(mk, d, price)
    if model == "hybrid":
        # 50% юзеров воронка рекламы, 50% — пейволл с подпиской (классический утилитарный гибрид)
        return installs * (ads_share * rpd_ads(mk, d) + (1 - ads_share) * ltv_sub(mk, d, price))
    raise ValueError(model)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--geo", default="us", help="us,jp,de — из markets.json; 'all' для рейтинга")
    ap.add_argument("--model", default="compare", choices=["compare", "ads", "paid", "sub", "hybrid", "rank"])
    ap.add_argument("--installs", type=int, default=10000)
    ap.add_argument("--price", type=float, default=4.99, help="цена (разовая или в месяц)")
    ap.add_argument("--conv-top", action="store_true", help="использовать топ-квартиль конверсий вместо медианы")
    args = ap.parse_args()

    markets, d = load()

    if args.model == "rank":
        # Рейтинг всех рынков по каждой модели + индекс аномалии (выручка/CPI = рычаг органики)
        print(f"Выручка на {args.installs} установок, USD. Аномалия = выручка лучшей модели / CPI (рычаг органики)\n")
        rows = []
        for geo, mk in markets.items():
            revs = {m: model_revenue(geo, mk, d, m, args.installs, args.price) for m in ["ads", "paid", "sub", "hybrid"]}
            best_m = max(revs, key=revs.get)
            anomaly = revs[best_m] / mk["cpi_usd"]
            rows.append((geo, revs["ads"], revs["paid"], revs["sub"], best_m, anomaly, mk["tier"]))
        print(f"{'гео':4} | {'ads':>6} | {'paid':>7} | {'sub':>8} | {'best':6} | {'аномалия':>8} | tier")
        print("-" * 62)
        for r in sorted(rows, key=lambda x: -x[5]):
            flag = "  ←" if r[5] > 5000 else ""
            print(f"{r[0]:4} | {r[1]:6.0f} | {r[2]:7.0f} | {r[3]:8.0f} | {r[4]:6} | {r[5]:8.0f} | {r[6]}{flag}")
        print("\n← высокая аномалия = рынок даёт много денег на доллар трафика: лучший кандидат для органического запуска")
        return

    for geo in [g.strip() for g in args.geo.split(",")]:
        mk = markets[geo]
        print(f"\n===== {geo.upper()}  (lang={mk['lang']}, eCPM int={mk['ecpm_interstitial_ios']}$, "
              f"paid={mk['paid_affinity']}/5, sub={mk['sub_affinity']}/5, CPI={mk['cpi_usd']}$) =====")

        if args.model == "compare":
            rows = []
            for m in ["ads", "paid", "sub", "hybrid"]:
                rev = model_revenue(geo, mk, d, m, args.installs, args.price)
                # break-even CPI: ads/paid окупаются с первой установки, sub — весь LTV
                be = {"ads": rpd_ads(mk, d), "paid": rpd_paid(mk, d, args.price),
                      "sub": ltv_sub(mk, d, args.price, conv_top=args.conv_top),
                      "hybrid": model_revenue(geo, mk, d, "hybrid", 1, args.price)}[m]
                rows.append((m, rev, rev / args.installs, be))
            print(f"{'модель':8} | {'выручка на '+str(args.installs)+' уст.':>14} | {'RPD/LTV $':>9} | {'max CPI $':>9}")
            print("-" * 55)
            for m, rev, rpd, be in rows:
                print(f"{m:8} | {rev:14.0f} | {rpd:9.3f} | {be:9.3f}")
            best = max(rows, key=lambda r: r[1])[0]
            print(f"→ лучшая модель на этом рынке: {best}; платный UA выгоден при CPI < break-even")
            print(f"  конверсии с учётом культуры рынка: hard-paywall={geo_conv(mk,d,'paid')*100:.1f}%, "
                  f"freemium sub 30д={geo_conv(mk,d,'sub')*100:.1f}% (топ {geo_conv(mk,d,'sub',True)*100:.1f}%)  [RevenueCat 2026 × affinity]")
        else:
            rev = model_revenue(geo, mk, d, args.model, args.installs, args.price)
            per = rev / args.installs
            print(f"модель {args.model}: выручка {rev:.0f}$ на {args.installs} установок ({per:.3f}$/установку)")
            if args.model in ("ads", "paid"):
                print(f"break-even CPI = {per:.3f}$ (рыночный CPI {mk['cpi_usd']}$ → "
                      f"{'УБЫТОЧНО' if mk['cpi_usd'] > per else 'окупается'}, маржа {(per-mk['cpi_usd']):.3f}$/установку)")
            else:
                months_payback = mk["cpi_usd"] / per if per else 999
                print(f"окупаемость CPI {mk['cpi_usd']}$ за {months_payback:.1f} мес LTV-потока")


if __name__ == "__main__":
    main()
