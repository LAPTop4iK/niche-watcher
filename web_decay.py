#!/usr/bin/env python3
"""Wayback observations: archive coverage and the latest HTTP-200 capture.

Neither measures the last product change, search ranking, demand or revenue.
Monthly collapsing keeps the first capture in a month, so the latest capture
must be requested independently with limit=-1 (CDX server documentation).
"""

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone


def cdx(url,latest=False):
    params={
        "url": url, "output": "json", "fl": "timestamp,statuscode",
        "filter": "statuscode:200",
    }
    params.update({"limit":-1,"fastLatest":"true"} if latest else {"collapse":"timestamp:6"})
    q=urllib.parse.urlencode(params)
    req = urllib.request.Request(f"https://web.archive.org/cdx/search/cdx?{q}",
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

    print("| URL | Месяцев со снимками | Последний снимок | Возраст снимка | Ограничение |")
    print("|---|---|---|---|---|")
    for url in urls:
        try:
            rows = cdx(url)
            latest=cdx(url,latest=True) if rows else []
        except Exception as e:  # noqa: BLE001
            print(f"| {url} | ? | ? | ? | ошибка CDX: {e} |")
            continue
        if not rows:
            print(f"| {url} | 0 | — | — | нет снимков (не индикатор) |")
            continue
        snaps = len(rows)
        if not latest:
            print(f"| {url} | {len(rows)} | ? | ? | последний снимок не получен |")
            continue
        last = max(r[0] for r in latest)
        last_dt = datetime.strptime(last[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
        months = (datetime.now(timezone.utc) - last_dt).days / 30.44
        verdict="Архив не измеряет обновления или текущий спрос"
        print(f"| {url} | {snaps} | {last_dt:%Y-%m-%d} | {months:.0f} мес | {verdict} |")
        time.sleep(3)  # CDX вежливость


if __name__ == "__main__":
    main()
