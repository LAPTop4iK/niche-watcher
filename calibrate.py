#!/usr/bin/env python3
"""
Calibrate — калибровка денежной модели фактом твоего первого релиза.

После запуска приложения подставь фактические числа (App Store Connect / Play Console),
скрипт сохранит наблюдаемые коэффициенты за период в _calibration.
Это не когортный LTV: автоматически подменять им модель нельзя.

Использование:
    python3 calibrate.py --installs 1200 --ratings-gained 38 --paying 41 --revenue 87.40
    python3 calibrate.py --installs 1200 --ratings-gained 38 --paying 41 --revenue 87.40 --apply
"""

import argparse
import json
import os
import math
import tempfile
from datetime import datetime,timezone
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--installs", type=int, required=True, help="установок за период")
    ap.add_argument("--ratings-gained", type=int, required=True, help="прирост оценок за период")
    ap.add_argument("--paying", type=int, help="число платящих; с --matched-cohort только из указанной когорты установок")
    ap.add_argument("--revenue", type=float, help="выручка за период, USD (после комиссии)")
    ap.add_argument('--matched-cohort',action='store_true',help='платящие и выручка относятся ровно к указанным установкам')
    ap.add_argument('--app-id')
    ap.add_argument('--geo')
    ap.add_argument('--model',choices=['ads','unlock','subscription','funnel'])
    ap.add_argument('--period-start')
    ap.add_argument('--period-end')
    ap.add_argument("--apply", action="store_true", help="записать в markets.json")
    args = ap.parse_args()

    if args.revenue is not None and not math.isfinite(args.revenue):
        ap.error("Выручка должна быть конечным числом")
    if args.installs <= 0 or args.ratings_gained < 0 or (args.paying is not None and (args.paying<0 or (args.matched_cohort and args.paying>args.installs))) or (args.revenue is not None and args.revenue < 0):
        ap.error("проверь установки, оценки, платящих и выручку")
    if args.apply and (args.paying is None or args.revenue is None):
        ap.error("для --apply нужны --paying и --revenue (допустимы нули)")
    review_ratio = args.installs / args.ratings_gained if args.ratings_gained else None
    period_ratio = args.paying / args.installs if args.paying is not None else None
    conv = period_ratio if args.matched_cohort else None
    receipts_per_install = args.revenue / args.installs if args.revenue is not None else None

    print(f"Твои фактические коэффициенты (за период):")
    print(f"  Установок на оценку за период: {review_ratio if review_ratio is not None else 'не определено'}; не универсальный коэффициент для конкурентов")
    if conv is not None:
        print(f"  install→paying: {conv*100:.2f}% (подтверждённая пользователем совпадающая когорта)")
    elif period_ratio is not None:
        print(f"  Отношение платящих к установкам за период: {period_ratio:.3f}; это не конверсия без совпадения когорты")
    if receipts_per_install is not None:
        print(f"  Поступления / установки: {receipts_per_install:.3f}$; " + ('реализованный RPI этой когорты за период' if args.matched_cohort else 'отношение периодных итогов, не ARPU и не LTV'))

    if args.apply:
        path = os.path.join(HERE, "markets.json")
        m = json.loads(Path(path).read_text(encoding='utf-8'))
        m["_calibration"] = {
            "review_ratio": round(review_ratio, 1) if review_ratio is not None else None,
            "install_to_paying": round(conv, 5) if conv is not None else None,
            "period_payers_to_installs_ratio":round(period_ratio,5),
            "scope":"matched-install-cohort" if args.matched_cohort else "period-totals",
            "observed_cohort_rpi_usd":round(receipts_per_install,4) if args.matched_cohort else None,
            "period_receipts_per_install_usd":round(receipts_per_install,4),
            "note": "наблюдение за период; не когортный LTV и не универсальная конверсия",
            "app_id":args.app_id,"geo":args.geo,"model":args.model,
            "period_start":args.period_start,"period_end":args.period_end,
            "recorded_at":datetime.now(timezone.utc).isoformat(),
            "installs":args.installs,"paying":args.paying,"net_revenue_usd":args.revenue,
        }
        m.setdefault('_calibration_history',[]).append(m['_calibration'])
        with tempfile.NamedTemporaryFile('w',dir=HERE,delete=False,encoding='utf-8') as f:
            json.dump(m,f,ensure_ascii=False,indent=2);f.write('\n');temporary=f.name
        os.replace(temporary,path)
        print("✓ наблюдение сохранено в markets.json._calibration; автоматической экстраполяции нет"
              if "_calibration" in m else "ok")
    else:
        print("\nПерезапусти с --apply, чтобы зафиксировать в markets.json.")


if __name__ == "__main__":
    main()
