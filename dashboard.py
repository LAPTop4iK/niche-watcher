#!/usr/bin/env python3
"""
Dashboard — генерирует чарты и index.html из niches.db для GitHub Pages.

Чарты:
  1. decay_heat.png  — матрица «декей × heat» по категориям (квадранты решений)
  2. top_zombies.png — топ-15 зомби по скору
  3. inflow.png      — приток новых приложений по категориям во времени (занимающиеся ниши)
  4. market_rank.png — индекс аномалии рынков из revenue_model

Запуск:  python3 dashboard.py            # дашборд в dashboard/
         python3 dashboard.py --out-dir site
"""

import argparse
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
plt.rcParams.update({"figure.facecolor": "white", "axes.grid": True, "grid.alpha": 0.3})


def load_latest_snapshots(db):
    con = sqlite3.connect(db)
    rows = con.execute(
        """SELECT s.category, s.country, s.rating_count, s.avg_rating, s.version_date,
                  a.name, a.seller, a.store_url
           FROM apps a
           JOIN snapshots s ON s.track_id = a.track_id
             AND s.scanned_at = (SELECT MAX(s2.scanned_at) FROM snapshots s2
                                 WHERE s2.track_id = a.track_id AND s2.country = s.country)"""
    ).fetchall()
    con.close()
    return rows


def stale_months(version_date):
    if not version_date:
        return None
    d = datetime.strptime(version_date[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - d).days / 30.44


def chart_decay_heat(rows, path):
    agg = defaultdict(lambda: [0, 0, 0])  # zombie, fresh, total
    for cat, country, rc, ar, vd, name, seller, url in rows:
        if rc < 10:
            continue
        sm = stale_months(vd)
        key = f"{cat}|{country}"
        agg[key][2] += 1
        if sm is not None and sm >= 18:
            agg[key][0] += 1
        if sm is not None and sm < 12:
            agg[key][1] += 1
    pts = [(100 * v[0] / v[2], 100 * v[1] / v[2], k, v[2]) for k, v in agg.items() if v[2] >= 8]
    fig, ax = plt.subplots(figsize=(11, 7))
    ax.axhline(40, color="grey", lw=0.8); ax.axvline(15, color="grey", lw=0.8)
    colors = []
    for z, f, _, _ in pts:
        colors.append("tab:green" if z >= 15 and f < 40 else "tab:orange" if z >= 15 else "tab:red" if f >= 40 else "tab:blue")
    xs, ys, sizes = [p[0] for p in pts], [p[1] for p in pts], [min(400, 20 + p[3]) for p in pts]
    ax.scatter(xs, ys, s=sizes, c=colors, alpha=0.65, edgecolors="black", linewidths=0.5)
    for z, f, k, _ in pts:
        if z >= 12 or f >= 45:
            ax.annotate(k, (z, f), fontsize=7, alpha=0.8)
    ax.set_xlabel("Зомби% (версия старше 18 мес)")
    ax.set_ylabel("Fresh% (версия младше 12 мес)")
    ax.set_title("Матрица: декей × heat (зелёный = свободная ниша, красный = рынок занят)")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return pts


def chart_top_zombies(rows, path):
    zs = []
    for cat, country, rc, ar, vd, name, seller, url in rows:
        sm = stale_months(vd)
        if sm is None or sm < 18 or rc < 500:
            continue
        score = min(1, sm / 36) * 35 + min(1, __import__("math").log10(rc + 1) / 5) * 30 + 10
        zs.append((score, name[:34], country, rc, sm))
    zs.sort(reverse=True)
    zs = zs[:15][::-1]
    fig, ax = plt.subplots(figsize=(10, 7))
    ax.barh([f"{n} [{g}]" for _, n, g, _, _ in zs], [s for s, *_ in zs], color="tab:red", alpha=0.7)
    for i, (_, n, g, rc, sm) in enumerate(zs):
        ax.text(2, i, f"{rc:,}★ · {sm:.0f} мес", va="center", fontsize=8)
    ax.set_xlabel("Скор зомби (без velocity — первый прогон)")
    ax.set_title("Топ-15 зомби: огромная аудитория при мёртвой версии")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def chart_inflow(db, path):
    con = sqlite3.connect(db)
    rows = con.execute(
        """SELECT substr(s.scanned_at, 1, 10) AS day, s.category, COUNT(*) AS new_apps
           FROM apps a JOIN snapshots s ON a.track_id = s.track_id
           WHERE substr(a.first_seen, 1, 10) = substr(s.scanned_at, 1, 10)
           GROUP BY day, s.category ORDER BY day"""
    ).fetchall()
    con.close()
    by_cat = defaultdict(dict)
    for day, cat, n in rows:
        by_cat[cat][day] = n
    top = sorted(by_cat, key=lambda c: -sum(by_cat[c].values()))[:6]
    fig, ax = plt.subplots(figsize=(11, 6))
    for cat in top:
        days = sorted(by_cat[cat])
        ax.plot(days, [by_cat[cat][d] for d in days], marker="o", label=cat)
    ax.legend(fontsize=8)
    ax.set_ylabel("Новых приложений, впервые увиденных за прогон")
    ax.set_title("Приток в ниши по прогонам: растущая линия = нишу занимают")
    plt.xticks(rotation=30, fontsize=7)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def chart_market_rank(path):
    import revenue_model as rm
    markets, d = rm.load()
    rows = []
    for geo, mk in markets.items():
        revs = {m: rm.model_revenue(geo, mk, d, m, 10000, 4.99) for m in ["ads", "paid", "sub"]}
        best = max(revs, key=revs.get)
        rows.append((geo, revs[best] / mk["cpi_usd"], best))
    rows.sort(key=lambda r: -r[1])
    fig, ax = plt.subplots(figsize=(10, 6))
    cmap = {"ads": "tab:blue", "paid": "tab:orange", "sub": "tab:green"}
    ax.bar([r[0] for r in rows], [r[1] for r in rows], color=[cmap[r[2]] for r in rows])
    ax.set_ylabel("Индекс аномалии: выручка/CPI")
    ax.set_title("Рынки: рычаг органики (цвет = лучшая модель: green=sub, orange=paid, blue=ads)")
    plt.xticks(rotation=45, fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


HTML = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Niche Watcher</title>
<style>
body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:1100px;margin:0 auto;padding:16px;background:#0e1116;color:#e6e6e6}
h1{font-size:1.5rem} h2{font-size:1.1rem;margin-top:2rem;color:#9ecbff}
img{max-width:100%;border-radius:8px;background:#fff;padding:6px}
table{border-collapse:collapse;width:100%;font-size:.85rem}
td,th{border:1px solid #333;padding:4px 8px} th{background:#1a2230}
a{color:#58a6ff} .meta{color:#8b949e;font-size:.85rem}
</style></head><body>
<h1>🔭 Niche Watcher — наблюдатель за нишами</h1>
<p class="meta">Обновлено __DATE__ · источник: __N__ приложений в базе · __RUNS__ прогонов</p>
<h2>Матрица ниш: декей × heat</h2><img src="charts/decay_heat.png">
<h2>Топ зомби (аудитория есть, поддержка мертва)</h2><img src="charts/top_zombies.png">
<h2>Приток в ниши (кто занимает свободное)</h2><img src="charts/inflow.png">
<h2>Рынки: аномалии заработка</h2><img src="charts/market_rank.png">
<h2>Топ-10 кандидатов</h2>
<table><tr><th>Скор</th><th>Приложение</th><th>Гео</th><th>Оценок</th><th>Версия от</th><th>Продавец</th></tr>
__ROWS__</table>
<p class="meta">Полные отчёты: <a href="https://github.com/__REPO__/blob/main/report.md">report.md</a> ·
<a href="https://github.com/__REPO__/blob/main/charts_report.md">charts_report.md</a> ·
<a href="https://github.com/__REPO__/blob/main/clusters_report.md">clusters_report.md</a></p>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(HERE, "niches.db"))
    ap.add_argument("--out-dir", default=os.path.join(HERE, "dashboard"))
    ap.add_argument("--repo", default="LAPTop4iK/niche-watcher")
    args = ap.parse_args()

    os.makedirs(os.path.join(args.out_dir, "charts"), exist_ok=True)
    rows = load_latest_snapshots(args.db)

    chart_decay_heat(rows, os.path.join(args.out_dir, "charts", "decay_heat.png"))
    chart_top_zombies(rows, os.path.join(args.out_dir, "charts", "top_zombies.png"))
    chart_inflow(args.db, os.path.join(args.out_dir, "charts", "inflow.png"))
    chart_market_rank(os.path.join(args.out_dir, "charts", "market_rank.png"))

    zs = []
    for cat, country, rc, ar, vd, name, seller, url in rows:
        sm = stale_months(vd)
        if sm is not None and sm >= 18 and rc >= 500:
            tid = url.split("id")[-1] if url else ""
            zs.append((min(1, sm / 36) * 35 + min(1, __import__("math").log10(rc + 1) / 5) * 30 + 10,
                       name, country, rc, vd[:10], seller, tid))
    zs.sort(reverse=True)
    trs = "".join(
        f"<tr><td>{s:.0f}</td><td><a href='https://apps.apple.com/app/id{tid}'>{n}</a></td>"
        f"<td>{g}</td><td>{rc:,}</td><td>{vd}</td><td>{se}</td></tr>"
        for s, n, g, rc, vd, se, tid in zs[:10]
    )
    con = sqlite3.connect(args.db)
    n_apps = con.execute("SELECT COUNT(*) FROM apps").fetchone()[0]
    runs = con.execute("SELECT COUNT(DISTINCT scanned_at) FROM snapshots").fetchone()[0]
    con.close()
    html = (
        HTML.replace("__DATE__", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
        .replace("__N__", f"{n_apps:,}")
        .replace("__RUNS__", str(runs))
        .replace("__ROWS__", trs)
        .replace("__REPO__", args.repo)
    )
    with open(os.path.join(args.out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print(f"dashboard → {args.out_dir} (charts: 4, apps in db: {n_apps}, runs: {runs})")


if __name__ == "__main__":
    main()
