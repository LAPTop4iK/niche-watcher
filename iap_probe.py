#!/usr/bin/env python3
"""
IAP Probe — ценовая разведка лидера: что и почём он продаёт (экспериментально).

Стратегия «играть на цене» требует знать прайс-лист конкурента. Страница App Store
отдаёт блок In-App Purchases (названия и цены) — тянем его HTML и парсим.

Использование:
    python3 iap_probe.py 443932954,1494386815 --country us
"""

import argparse
import re
import sys
import time
import urllib.request

sys.path.insert(0, __import__("os").path.dirname(__file__))


def probe(track_id, country="us"):
    url = f"https://apps.apple.com/{country}/app/id{track_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        html = r.read().decode("utf-8", "replace")

    out = {"iap": [], "has_iap_block": False}
    idx = html.find("In-App Purchases")
    if idx == -1:
        return out
    out["has_iap_block"] = True
    chunk = html[idx:idx + 6000]
    # пары «название … $X.XX» внутри списка IAP
    items = re.findall(
        r"<li[^>]*>.*?<span[^>]*>([^<]{2,60})</span>.*?([\$€£]\s?\d{1,3}(?:[.,]\d{2})?)</",
        chunk, re.S)
    seen = set()
    for name, price in items:
        name = re.sub(r"\s+", " ", name).strip()
        if name and (name, price) not in seen and "Sort" not in name:
            seen.add((name, price))
            out["iap"].append((name[:40], price))
        if len(out["iap"]) >= 8:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("track_ids", help="id,id")
    ap.add_argument("--country", default="us")
    args = ap.parse_args()

    for tid in [t.strip() for t in args.track_ids.split(",") if t.strip()]:
        try:
            r = probe(tid, args.country)
        except Exception as e:  # noqa: BLE001
            print(f"! {tid}: {e}", file=sys.stderr)
            continue
        name_m = "—"
        print(f"\n=== {tid} [{args.country}] ===")
        if not r["has_iap_block"]:
            print("In-App Purchases: блок не найден (нет IAP или страница изменилась)")
            continue
        if not r["iap"]:
            print("In-App Purchases: блок есть, парсер цен не сматчил (экспериментальный)")
            continue
        for name, price in r["iap"]:
            print(f"  • {name} — {price}")
        time.sleep(1)


if __name__ == "__main__":
    main()
