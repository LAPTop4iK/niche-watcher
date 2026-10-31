#!/usr/bin/env python3
"""
Web Decay — тот же decay-арбитраж для сайтов через Wayback CDX API (бесплатно, легально).

Для каждого URL: сколько снимков в архиве, когда последний, «застой» = месяцы с
последнего изменения. Страница-инструмент, живая в топе Google, не менявшаяся
2+ года — веб-зомби (README §14).

Использование:
    python3 web_decay.py --urls "https://concrete-calculators.com,https://example.com/calc"
    python3 web_decay.py --file urls.txt
"""

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone


def cdx(url):
    q = urllib.parse.urlencode({
        "url": url, "output": "json", "fl": "timestamp,statuscode",
        "filter": "statuscode:200", "collapse": "timestamp:6",
    })
    req = urllib.request.Request(f"http://web.archive.org/cdx/search/cdx?{q}",
                                 headers={"User-Agent": "niche-watcher/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        rows = json.loads(r.read().decode("utf-8", "replace") or "[]")
    return rows[1:] if rows else []  # первая строка — заголовок


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--urls", help="url,url,url")
    ap.add_argument("--file", help="файл с URL построчно")
    args = ap.parse_args()

    urls = []
    if args.urls:
        urls += [u.strip() for u in args.urls.split(",") if u.strip()]
    if args.file:
        urls += [l.strip() for l in open(args.file, encoding="utf-8") if l.strip()]
    if not urls:
        sys.exit("укажи --urls или --file")

    print("| URL | Снимков | Последний снимок | Застой | Вердикт |")
    print("|---|---|---|---|---|")
    for url in urls:
        try:
            rows = cdx(url)
        except Exception as e:  # noqa: BLE001
            print(f"| {url} | ? | ? | ? | ошибка CDX: {e} |")
            continue
        if not rows:
            print(f"| {url} | 0 | — | — | нет снимков (не индикатор) |")
            continue
        snaps = len(rows)
        last = rows[-1][0]
        last_dt = datetime.strptime(last[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
        months = (datetime.now(timezone.utc) - last_dt).days / 30.44
        verdict = ("🟢 веб-зомби: 2+ года без изменений" if months >= 24 else
                   "🟡 6–24 мес без изменений" if months >= 6 else "🔴 обновляется недавно")
        print(f"| {url} | {snaps} | {last_dt:%Y-%m-%d} | {months:.0f} мес | {verdict} |")
        time.sleep(3)  # CDX вежливость


if __name__ == "__main__":
    main()
