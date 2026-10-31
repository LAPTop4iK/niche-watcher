#!/usr/bin/env python3
"""
Similar Crawl — обход графа «You Might Also Like» на страницах App Store.

Зачем: keyword-сканер находит ниши, которые ты умеешь назвать. Граф похожих
приложений находит КЛАСТЕРЫ, которые ты назвать не умеешь: если вокруг зомби
30% соседей тоже гнилые — это целая сгнившая ниша, а не одно приложение.
Так закрывается слепая зона keyword-подхода (и то, что видят платные аналоги
через «similar apps», только бесплатно).

Использование:
    python3 similar_crawl.py --seeds 443932954,1494386815 --depth 1
    python3 similar_crawl.py --from-report report.md --depth 1 --top-seeds 5
"""

import argparse
import json
import os
import re
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner as sc

PAGE_RE = re.compile(r"/app/[a-z0-9\-%]+/id(\d+)", re.I)


def page_ids(track_id, country="us"):
    """ID приложений из блока похожих на странице приложения."""
    url = f"https://apps.apple.com/{country}/app/id{track_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        html = r.read().decode("utf-8", "replace")
    ids = []
    for m in PAGE_RE.finditer(html):
        tid = m.group(1)
        if tid not in ids:
            ids.append(tid)
    return ids[1:]  # первый id — само приложение


def stale_months(version_date):
    if not version_date:
        return None
    d = datetime.strptime(version_date[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - d).days / 30.44


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default=None, help="track id,id")
    ap.add_argument("--from-report", default=None, help="взять топ-N зомби из report.md")
    ap.add_argument("--top-seeds", type=int, default=5)
    ap.add_argument("--depth", type=int, default=1, help="1 = только соседи, 2 = соседи соседей (медленно)")
    ap.add_argument("--country", default="us")
    ap.add_argument("--db", default=os.path.join(here, "niches.db"))
    ap.add_argument("--out", default=os.path.join(here, "clusters_report.md"))
    ap.add_argument("--min-stale-months", type=float, default=18)
    args = ap.parse_args()

    seeds = []
    if args.seeds:
        seeds += [s.strip() for s in args.seeds.split(",") if s.strip()]
    if args.from_report:
        txt = open(args.from_report, encoding="utf-8").read()
        found = re.findall(r"/app/[a-z0-9\-%]+/id(\d+)", txt)
        seen = set()
        for f in found:
            if f not in seen:
                seen.add(f)
                seeds.append(f)
            if len(seeds) >= args.top_seeds:
                break

    visited = set()
    con = sc.open_db(args.db)
    results = []

    frontier = list(dict.fromkeys(seeds))
    for depth in range(args.depth):
        nxt = []
        for tid in frontier:
            if tid in visited:
                continue
            visited.add(tid)
            try:
                nb = page_ids(tid, args.country)
            except Exception as e:  # noqa: BLE001
                print(f"! seed {tid}: {e}", file=sys.stderr)
                continue
            time.sleep(0.8)
            print(f"[{tid}] соседей: {len(nb)}")
            # lookup соседей батчем
            rows = []
            for i in range(0, len(nb), 100):
                chunk = nb[i:i + 100]
                data = sc.fetch_json(
                    "https://itunes.apple.com/lookup?id=" + ",".join(chunk)
                    + f"&country={args.country}&entity=software"
                )
                rows.extend(data.get("results", []))
                time.sleep(sc.REQUEST_DELAY)
            stale = []
            for r in rows:
                sm = stale_months(r.get("currentVersionReleaseDate", ""))
                if sm is None:
                    continue
                app = sc.normalize(r, args.country, "similar-graph", f"neighbors-of-{tid}")
                if (r.get("userRatingCount") or 0) >= 10:
                    sc.upsert_app(con, app)
                    sc.insert_snapshot(con, app)
                if sm >= args.min_stale_months:
                    stale.append((sm, r))
            con.commit()
            results.append({
                "seed": tid,
                "neighbors": len(rows),
                "stale": len(stale),
                "stale_pct": 100.0 * len(stale) / max(1, len(rows)),
                "median_age": statistics.median([s for s, _ in [(stale_months(r.get("currentVersionReleaseDate", "")), 0) for r in rows if stale_months(r.get("currentVersionReleaseDate", ""))]] or [0]),
                "top_stale": sorted(stale, key=lambda x: -(x[1].get("userRatingCount") or 0))[:4],
            })
            nxt += nb
        frontier = [n for n in dict.fromkeys(nxt) if n not in visited][:40]

    con.close()
    results.sort(key=lambda r: -r["stale_pct"])
    lines = [
        f"# Similar-Graph Decay — {sc.iso(sc.now_utc())[:10]}\n",
        f"Глубина {args.depth}, гео {args.country}. Порог гнилости {args.min_stale_months} мес.\n",
        "| Seed | Соседей | Гнилых | % гнилых | Медианный возраст | Топ-гнилые соседи |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        tops = "; ".join(f"{a['trackName'][:26]} ({a.get('userRatingCount') or 0}★, {s:.0f} мес)" for s, a in r["top_stale"])
        lines.append(f"| {r['seed']} | {r['neighbors']} | {r['stale']} | {r['stale_pct']:.0f}% | {r['median_age']:.0f} мес | {tops} |")
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n== отчёт: {args.out}")


if __name__ == "__main__":
    main()
