#!/usr/bin/env python3
"""
Calibrate — калибровка денежной модели фактом твоего первого релиза.

После запуска приложения подставь фактические числа (App Store Connect / Play Console),
скрипт пересчитает твои реальные коэффициенты и пропишет их в markets.json
в секцию _calibration — после этого revenue_model показывает ТВОЮ экономику,
а не бенчмаркную.

Использование:
    python3 calibrate.py --installs 1200 --ratings-gained 38 --paying 41 --revenue 87.40
    python3 calibrate.py --installs 1200 --ratings-gained 38 --paying 41 --revenue 87.40 --apply
"""

import argparse
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--installs", type=int, required=True, help="установок за период")
    ap.add_argument("--ratings-gained", type=int, required=True, help="прирост оценок за период")
    ap.add_argument("--paying", type=int, help="платящих/подписок за период")
    ap.add_argument("--revenue", type=float, help="выручка за период, USD (после комиссии)")
    ap.add_argument("--apply", action="store_true", help="записать в markets.json")
    args = ap.parse_args()

    review_ratio = args.installs / max(1, args.ratings_gained)
    conv = args.paying / max(1, args.installs) if args.paying else None
    arpu = args.revenue / max(1, args.installs) if args.revenue else None

    print(f"Твои фактические коэффициенты (за период):")
    print(f"  reviews→downloads: 1 оценка на {review_ratio:.0f} установок "
          f"(индустрия: 20–100, дефолт 40)")
    if conv is not None:
        print(f"  install→paying: {conv*100:.2f}% (RevenueCat медиана freemium: 1.7%, топ 4.2%)")
    if arpu is not None:
        print(f"  ARPU: {arpu:.3f}$ на установку")

    if args.apply and conv is not None:
        path = os.path.join(HERE, "markets.json")
        m = json.load(open(path, encoding="utf-8"))
        m["_calibration"] = {
            "review_ratio": round(review_ratio, 1),
            "install_to_paying": round(conv, 5),
            "arpu_usd": round(arpu, 4),
            "note": "факт первого релиза — приоритетнее _defaults_research",
        }
        json.dump(m, open(path, "w"), ensure_ascii=False, indent=2)
        print("✓ записано в markets.json._calibration — revenue_model теперь читает твои цифры"
              if "_calibration" in m else "ok")
    else:
        print("\nПерезапусти с --apply, чтобы зафиксировать в markets.json.")


if __name__ == "__main__":
    main()
