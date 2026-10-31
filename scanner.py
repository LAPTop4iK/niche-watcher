#!/usr/bin/env python3
"""
Niche Decay Scanner — находит «зомби»-приложения в App Store:
аудитория и рейтинг есть, обновлений давно нет => окно для входа.

Только stdlib Python 3. Источник данных: iTunes Search API (бесплатно, без ключа).

Примеры:
    python3 scanner.py                                  # полный прогон по seed_queries.json
    python3 scanner.py --categories construction,auto   # только выбранные категории
    python3 scanner.py --countries us,de --limit 25     # узкий прогон
    python3 scanner.py --dry-run                        # без записи в БД (только консоль)

Алерты в Telegram: экспортируй TG_BOT_TOKEN и TG_CHAT_ID.
Снапшоты пишутся в SQLite (по умолчанию niches.db) — velocity считается
между прогонами, поэтому прогоняй регулярно (GitHub Actions cron делает это сам).
"""

import argparse
import html
import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone, timedelta

API_URL = "https://itunes.apple.com/search"
REQUEST_DELAY = 3.0          # сек между запросами; iTunes неофициально ~20/мин, 3с = безопасно
MAX_RETRIES = 5
HTTP_TIMEOUT = 20

# Пороги "зомби" по умолчанию (можно переопределить флагами)
DEFAULT_MIN_RATINGS = 500
DEFAULT_MIN_STALE_MONTHS = 18


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- HTTP

def fetch_json(url, quiet=False):
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "niche-decay-scanner/1.0"}
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001 — сетевые ошибки любые
            last_err = e
            is_429 = getattr(e, "code", None) == 429
            # 429 у iTunes требует длинной паузы (окно лимита ~минута), прочее — обычный backoff
            wait = int(os.environ.get("RATE_429_WAIT", "45")) * attempt if is_429 else 2 ** attempt
            if not quiet:
                print(f"  ! попытка {attempt}/{MAX_RETRIES} не удалась ({e}); жду {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"не удалось получить {url}: {last_err}")


def search_apps(term, country, limit):
    params = urllib.parse.urlencode(
        {"term": term, "entity": "software", "country": country, "limit": min(limit, 200)}
    )
    data = fetch_json(f"{API_URL}?{params}")
    return data.get("results", [])


# ---------------------------------------------------------------- БД

SCHEMA = """
CREATE TABLE IF NOT EXISTS apps (
    track_id      INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    seller        TEXT,
    bundle_id     TEXT,
    store_url     TEXT,
    price         REAL,
    genre         TEXT,
    first_country TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id     INTEGER NOT NULL REFERENCES apps(track_id),
    country      TEXT NOT NULL,
    category     TEXT NOT NULL,
    query        TEXT NOT NULL,
    scanned_at   TEXT NOT NULL,
    version      TEXT,
    version_date TEXT,
    rating_count INTEGER,
    avg_rating   REAL,
    rank         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_snap_track_time ON snapshots(track_id, scanned_at);
"""


def open_db(path):
    con = sqlite3.connect(path, timeout=30)
    con.executescript(SCHEMA)
    try:  # миграция старых баз
        con.execute("ALTER TABLE snapshots ADD COLUMN rank INTEGER")
    except sqlite3.OperationalError:
        pass
    return con


def upsert_app(con, app):
    con.execute(
        """INSERT INTO apps (track_id, name, seller, bundle_id, store_url, price, genre,
                             first_country, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(track_id) DO UPDATE SET
             last_seen = excluded.last_seen, name = excluded.name""",
        (
            app["track_id"], app["name"], app["seller"], app["bundle_id"],
            app["store_url"], app["price"], app["genre"],
            app["first_country"], app["first_seen"], app["last_seen"],
        ),
    )


def insert_snapshot(con, app):
    con.execute(
        """INSERT INTO snapshots (track_id, country, category, query, scanned_at,
                                  version, version_date, rating_count, avg_rating, rank)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            app["track_id"], app["country"], app["category"], app["query"],
            app["scanned_at"], app["version"], app["version_date"],
            app["rating_count"], app["avg_rating"], app.get("rank"),
        ),
    )


def previous_snapshot(con, track_id, min_age_days=7):
    """Последний снапшот старше min_age_days — база для velocity."""
    if con is None:  # dry-run: velocity недоступна
        return None
    cutoff = iso(now_utc() - timedelta(days=min_age_days))
    row = con.execute(
        """SELECT scanned_at, rating_count FROM snapshots
           WHERE track_id = ? AND scanned_at <= ?
           ORDER BY scanned_at DESC LIMIT 1""",
        (track_id, cutoff),
    ).fetchone()
    return row  # (scanned_at, rating_count) | None


def was_flagged_before(con, track_id, min_stale_months, min_ratings, before_iso=None):
    """Считался ли трек зомби в ПРОШЛЫХ прогонах (до before_iso — иначе текущий
    прогон флагает сам себя и алерты никогда не срабатывают)."""
    if con is None:
        return False
    cond = "AND scanned_at < ?" if before_iso else ""
    params = [track_id, min_ratings] + ([before_iso] if before_iso else [])
    row = con.execute(
        f"""SELECT MIN(version_date) FROM snapshots
            WHERE track_id = ? AND rating_count >= ? {cond}""",
        params,
    ).fetchone()
    if not row or not row[0]:
        return False
    months = months_between(parse_iso(row[0]), now_utc())
    return months >= min_stale_months


# ---------------------------------------------------------------- анализ

def months_between(a, b):
    """Полных месяцев между датами (грубо, по дням)."""
    return (b - a).days / 30.44


def analyze(con, app, min_stale_months, min_ratings):
    """Считает метрики и скор (0..100) для одного снапшота."""
    out = dict(app)
    stale_months = None
    if app["version_date"]:
        try:
            stale_months = months_between(parse_iso(app["version_date"]), now_utc())
        except ValueError:
            pass
    out["stale_months"] = stale_months

    # velocity: прирост оценок за 30 дней относительно снапшота >= 7 дней назад
    prev = previous_snapshot(con, app["track_id"])
    velocity = None
    if prev:
        old_iso, old_count = prev
        days = max(1, (now_utc() - parse_iso(old_iso)).days)
        velocity = (app["rating_count"] - old_count) / days * 30.0
    out["velocity_30d"] = velocity

    is_zombie = (
        stale_months is not None
        and stale_months >= min_stale_months
        and app["rating_count"] >= min_ratings
    )
    out["is_zombie"] = is_zombie

    # quality gap: спрос доказан (аудитория), продукт плох (низкий рейтинг), лидер жив
    out["is_quality_gap"] = bool(
        app["rating_count"] >= 1000
        and app["avg_rating"] and app["avg_rating"] < 4.0
        and stale_months is not None and stale_months < 12
    )

    # --- скоринг 0..100
    staleness = 0.0
    if stale_months is not None:
        staleness = 35.0 * min(1.0, max(0.0, stale_months) / 36.0)

    mass = 0.0
    if app["rating_count"] and app["rating_count"] > 0:
        mass = 30.0 * min(1.0, math.log10(app["rating_count"] + 1) / 5.0)  # 100k оценок = 30

    if velocity is None:
        velocity_pts = 10.0  # данных пока нет — нейтрально
    elif velocity > 0:
        velocity_pts = 25.0 * min(1.0, velocity / 50.0)  # +50 оценок/мес у зомби = максимум
    else:
        velocity_pts = 0.0

    out["score"] = round(staleness + mass + velocity_pts, 1)
    return out


# ---------------------------------------------------------------- алерты

def flag_emoji(iso_country):
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in iso_country.upper()[:2])


def telegram_dashboard_url():
    repo = os.environ.get("GITHUB_REPOSITORY", "LAPTop4iK/niche-watcher")
    return f"https://{repo.split('/')[0].lower()}.github.io/{repo.split('/')[1]}/"


def send_telegram(text):
    """Отправка с разбивкой по лимиту 4096 символов и экранированием HTML."""
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        return False
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3900:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = True
    for chunk in chunks:
        payload = json.dumps({"chat_id": chat_id, "text": chunk,
                              "parse_mode": "HTML", "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                ok = ok and resp.status == 200
        except Exception as e:  # noqa: BLE001
            print(f"  ! telegram alert failed: {e}", file=sys.stderr)
            ok = False
        time.sleep(1)
    return ok


def build_alert_message(new, total_zombies, print_mode=False):
    """Красивый digest: ссылки на сторы, флаги, velocity, свежесть, риски, ссылка на дашборд."""
    e = (lambda s: s) if print_mode else html.escape
    lines = [f"🧟 <b>Niche Watcher</b> — новые зомби: <b>{len(new)}</b> (всего в прогоне: {total_zombies})", ""]
    for i, a in enumerate(new[:12], 1):
        vel = f" · {a['velocity_30d']:+.0f}/30д" if a["velocity_30d"] is not None else ""
        rev = f" · 📩 отзыв {a['recent_reviews'][0]}" if a.get("recent_reviews") else ""
        port = " · ⚠️портфель" if (a.get("seller_apps") or 0) > 8 else ""
        native = " · ⛔натив" if a.get("native_risk") else ""
        link = f'<a href="{a["store_url"]}">{e(a["name"])}</a>' if not print_mode else a["name"]
        rating = f"{a['avg_rating']:.1f}★" if a.get("avg_rating") else "—"
        lines.append(
            f"{i}. <b>{link}</b> {flag_emoji(a['country'])}\n"
            f"   {a['rating_count']:,} оценок · {rating} · "
            f"<b>{a['stale_months']:.0f} мес</b> без обновл.{vel}{rev}{port}{native}"
        )
    if len(new) > 12:
        lines.append(f"… и ещё {len(new) - 12} — см. отчёт")
    lines.append("")
    lines.append(f"📊 <a href=\"{telegram_dashboard_url()}\">дашборд с динамикой</a> · "
                 f"отчёт: report.md в репо")
    return "\n".join(lines)


# ---------------------------------------------------------------- отчёт

# Ключевые слова, означающие конкуренцию с нативными фичами iOS (калькулятор,
# фонарик, уровень, компас, сканер — встроены в ОС). Зомби в таких нишах —
# часто СМЕРТЬ СПРОСА (платформа поглотила боль), а не возможность.
NATIVE_RISK_KEYWORDS = (
    "calculator", "flashlight", "level", "compass", "scanner",
    "measure", "magnifier", "torch",
    "калькулятор", "фонарик", "уровень", "компас", "сканер",
    "計算", "계산기", "电筒",
)


_STOP_TOK = set("""for with and the your app my pro free plus hd lite best simple easy all one new
timer time""".split())


def _relevant(name, query):
    """Ложные корреляции выдачи: гиганты утекают в длинные запросы (ADP по
    «hourly paycheck calculator»). Райзер ниши обязан делить токен с запросом."""
    tn = {w for w in re.split(r"[^a-zа-яёäöüß0-9]+", name.lower()) if len(w) > 2 and w not in _STOP_TOK}
    tq = {w for w in re.split(r"[^a-zа-яёäöüß0-9]+", query.lower()) if len(w) > 2 and w not in _STOP_TOK}
    return bool(tn & tq)


def is_native_risk(name, query):
    text = f"{name} {query}".lower()
    return any(k in text for k in NATIVE_RISK_KEYWORDS)


def fetch_recent_reviews(track_id, country="us"):
    """(дата последнего отзыва, число отзывов за 60 дней) через RSS — сигнал живого спроса."""
    try:
        url = f"https://itunes.apple.com/{country}/rss/customerreviews/id={track_id}/sortby=mostrecent/page=1/json"
        data = fetch_json(url)
        entries = (data.get("feed") or {}).get("entry") or []
        if isinstance(entries, dict):
            entries = [entries]
        dates = []
        for e in entries:
            if "im:rating" not in e:
                continue
            lbl = e.get("updated", {}).get("label", "")
            if lbl:
                dates.append(lbl[:10])
        if not dates:
            return None
        cutoff = (now_utc() - timedelta(days=60)).strftime("%Y-%m-%d")
        recent60 = sum(1 for d in dates if d >= cutoff)
        rated = [e2 for e2 in entries if "im:rating" in e2]
        bad = sum(1 for e2 in rated if int(e2["im:rating"]["label"]) <= 2)
        bad_share = round(100.0 * bad / len(rated)) if rated else None
        return dates[0], recent60, bad_share
    except Exception:  # noqa: BLE001 — отзывные данные не критичны для прогона
        return None


def fmt_row(a):
    stale = f"{a['stale_months']:.0f} мес" if a["stale_months"] is not None else "?"
    vel = f"+{a['velocity_30d']:.0f}" if a["velocity_30d"] is not None else "n/a"
    rating = f"{a['avg_rating']:.1f}" if a["avg_rating"] else "-"
    port = a.get("seller_apps")
    port_s = f"{a['seller'][:22]}{'' if port is None else f' ({port})'}"
    rev = a.get("recent_reviews")
    rev_s = (f"{rev[0]} ({rev[1]})" + (f" 💔{rev[2]}%" if rev and rev[2] is not None else "")) if rev else "-"
    warn = " ⚠️портфель" if (port or 0) > 8 else ""
    native = " ⛔натив" if a.get("native_risk") else ""
    return (
        f"| {a['score']:5.1f} | [{a['name'][:34]}]({a['store_url']}) "
        f"| {port_s}{warn}{native} | {a['country']} | {a['rating_count']:>6} | {rating} "
        f"| {a['version_date'][:10] if a['version_date'] else '?'} | {stale} | {vel} | {rev_s} |"
    )


REPORT_HEAD = """# Niche Decay Scan — {date}

Зомби: версия старше {stale} мес И оценок >= {ratings}.
Velocity — прирост оценок за 30 дней (нужны 2+ прогона с интервалом >= 7 дней).
(прод N) — сколько приложений этого продавца встречено в прогоне: >8 = портфельная сеть ⚠️.
посл. отзыв (N) — дата последнего отзыва и сколько отзывов за 60 дней: свежие = спрос жив.

| Скор | Приложение | Разработчик | Гео | Оценок | Рейтинг | Версия от | Без обновл. | Vel/30д | Посл. отзыв |
|---|---|---|---|---|---|---|---|---|---|
"""


def write_report(path, zombies, watchlist, args, heat_rows=None, risers=None, quality_gaps=None):
    lines = [REPORT_HEAD.format(date=iso(now_utc())[:10], stale=args.min_stale_months, ratings=args.min_rating_count)]
    if zombies:
        for a in zombies:
            lines.append(fmt_row(a))
    else:
        lines.append("_Зомби по порогам не найдено._")
    lines.append(f"\n## Watchlist (версия 9–{args.min_stale_months} мес, наблюдаю)\n")
    lines.append("| Скор | Приложение | Разработчик | Гео | Оценок | Рейтинг | Версия от | Без обновл. | Vel/30д | Посл. отзыв |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    if watchlist:
        for a in watchlist:
            lines.append(fmt_row(a))
    else:
        lines.append("_Пусто._")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    if heat_rows:
        with open(path, "a", encoding="utf-8") as f:
            f.write(
                "\n## Heat по категориям (рост конкуренции)\n\n"
                "fresh = версия младше 12 мес (активные конкуренты); new = новые в БД за прогон.\n"
                "Читать парой: зомби% ВЫСОКИЙ + fresh% НИЗКИЙ = свободная ниша; зомби% высокий + fresh% высокий = ниша уже поднимают.\n\n"
                "| Категория | Гео | Видно | Fresh% | New | Зомби% | Топ-продавец |\n|---|---|---|---|---|---|---|\n"
                + "\n".join(heat_rows) + "\n"
            )
    if risers:
        with open(path, "a", encoding="utf-8") as f:
            f.write(
                "\n## Risers — новые в топ-20 выдачи с аудиторией (нишу занимают сейчас)\n\n"
                "| Оценок | Приложение | Гео | Запрос | Версия от |\n|---|---|---|---|---|\n"
                + "\n".join(
                    f"| {a['rating_count']:,} | [{a['name'][:36]}]({a['store_url']}) | {a['country']} "
                    f"| {a['query']} | {a['version_date'][:10] if a['version_date'] else '?'} |"
                    for a in risers) + "\n"
            )
    if quality_gaps:
        with open(path, "a", encoding="utf-8") as f:
            f.write(
                "\n## Quality gaps — популярно, но плохо оценено (feature-gap raid цели)\n\n"
                "| Оценок | Рейтинг | Приложение | Гео | Версия от |\n|---|---|---|---|---|\n"
                + "\n".join(
                    f"| {a['rating_count']:,} | {a['avg_rating']:.1f} | [{a['name'][:36]}]({a['store_url']}) "
                    f"| {a['country']} | {a['version_date'][:10] if a['version_date'] else '?'} |"
                    for a in quality_gaps) + "\n"
            )
    with open(path, "a", encoding="utf-8") as f:
        f.write(
            "\n## Легенда рисков\n\n"
            "- ⚠️портфель — продавец с 9+ приложениями в прогоне: зомби может проснуться.\n"
            "- ⛔натив — пересечение с нативными ключевиками iOS (калькулятор, фонарик, уровень,\n"
            "  сканер...). Не авто-отказ: обязателен вопрос «поглотила ли ОС боль?» Если боль\n"
            "  решена нативно (фонарик) — скип; если нет (бетонный расчёт) — нужна отстрой\n"
            "  (Apple Pencil, Math Notes-гэпы, качество) или другой заход.\n"
        )


# ---------------------------------------------------------------- main

def load_seed(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def normalize(raw, country, category, query, rank=None):
    date_str = raw.get("currentVersionReleaseDate", "").replace("Z", "Z")
    return {
        "track_id": raw.get("trackId"),
        "name": raw.get("trackName") or "?",
        "seller": raw.get("sellerName") or raw.get("artistName") or "?",
        "bundle_id": raw.get("bundleId") or "",
        "store_url": raw.get("trackViewUrl") or "",
        "price": raw.get("price", 0) or 0,
        "genre": raw.get("primaryGenreName") or "",
        "country": country,
        "category": category,
        "query": query,
        "scanned_at": iso(now_utc()),
        "version": str(raw.get("version") or ""),
        "version_date": date_str or None,
        "rating_count": raw.get("userRatingCount") or 0,
        "avg_rating": raw.get("averageUserRating"),
        "rank": rank,
        "first_country": country,
        "first_seen": iso(now_utc()),
        "last_seen": iso(now_utc()),
    }


def main():
    ap = argparse.ArgumentParser(description="App Store zombie-niche scanner")
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--seed", default=os.path.join(here, "seed_queries.json"))
    ap.add_argument("--markets", default=os.path.join(here, "markets.json"))
    ap.add_argument("--db", default=os.path.join(here, "niches.db"))
    ap.add_argument("--report", default=os.path.join(here, "report.md"))
    ap.add_argument("--countries", default=None, help="us,de — перекрывает markets.json")
    ap.add_argument("--tier", type=int, default=1, help="1 = ежедневные рынки, 2 = еженедельные (из markets.json)")
    ap.add_argument("--slot", type=int, default=None, help="недельный слот 1-4 для tier-2 (day-of-week)")
    ap.add_argument("--categories", default=None, help="construction,auto — фильтр категорий")
    ap.add_argument("--limit", type=int, default=50, help="результатов на запрос (max 200)")
    ap.add_argument("--min-rating-count", type=int, default=DEFAULT_MIN_RATINGS)
    ap.add_argument("--min-stale-months", type=int, default=DEFAULT_MIN_STALE_MONTHS)
    ap.add_argument("--dry-run", action="store_true", help="не писать в БД")
    ap.add_argument("--no-alerts", action="store_true")
    ap.add_argument("--print-alert", action="store_true", help="показать TG-сообщение вместо отправки")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    seed = load_seed(args.seed)
    with open(args.markets, encoding="utf-8") as f:
        markets = json.load(f)["markets"]
    countries = (
        [c.strip() for c in args.countries.split(",")] if args.countries
        else [c for c, v in markets.items()
              if v.get("tier", 2) == args.tier
              and (args.slot is None or args.tier != 2 or v.get("week_slot") == args.slot)]
    )
    wanted = set(args.categories.split(",")) if args.categories else None

    con = None if args.dry_run else open_db(args.db)
    run_start_iso = iso(now_utc())  # граница «до этого прогона» для алертов
    analyzed, seen = [], set()
    # приложения, уже бывшие в прошлых прогонах — база для inflow (heat)
    existing_ids = (
        {r[0] for r in con.execute("SELECT track_id FROM apps")} if con else set()
    )

    for cat in seed["categories"]:
        cid = cat["id"]
        if wanted and cid not in wanted:
            continue
        if not cat.get("enabled", True):
            continue
        for country in countries:
            lang = markets.get(country, {}).get("lang", "en")
            queries = cat["queries"].get(lang) or cat["queries"]["en"]
            for q in queries:
                if not args.quiet:
                    print(f"[{cid}/{country}] {q}")
                try:
                    results = search_apps(q, country, args.limit)
                except RuntimeError as e:
                    print(f"  ! пропускаю запрос: {e}", file=sys.stderr)
                    continue
                for pos, raw in enumerate(results, 1):
                    tid = raw.get("trackId")
                    if not tid or tid in seen:
                        continue
                    seen.add(tid)
                    app = normalize(raw, country, cid, q, rank=pos)
                    if app["rating_count"] < 10:  # шум отсекаем сразу
                        continue
                    app["native_risk"] = is_native_risk(app["name"], q)
                    if con:
                        upsert_app(con, app)
                        insert_snapshot(con, app)
                    analyzed.append(analyze(con, app, args.min_stale_months, args.min_rating_count) if con
                                    else analyze(None, app, args.min_stale_months, args.min_rating_count))
                time.sleep(REQUEST_DELAY)

    if con:
        con.commit()

    # портфельный аудит: сколько приложений каждого продавца встречено в прогоне
    seller_counts = Counter(a["seller"] for a in analyzed)
    for a in analyzed:
        a["seller_apps"] = seller_counts[a["seller"]]

    zombies = sorted([a for a in analyzed if a["is_zombie"]], key=lambda x: -x["score"])

    # heat по категориям: рост конкуренции (свежие приложения) + приток новых за прогон
    heat = {}
    for a in analyzed:
        key = (a["category"], a["country"])
        h = heat.setdefault(key, {"total": 0, "fresh": 0, "new": 0, "zombie": 0, "sellers": Counter()})
        h["total"] += 1
        if a["stale_months"] is not None and a["stale_months"] < 12:
            h["fresh"] += 1
        if a["track_id"] not in existing_ids:
            h["new"] += 1
        if a["is_zombie"]:
            h["zombie"] += 1
        h["sellers"][a["seller"]] += 1

    # risers: новые в топ-50 выдачи с аудиторией — «нишу уже кто-то занимает»
    risers = sorted(
        [a for a in analyzed
         if a["track_id"] not in existing_ids and a["rating_count"] >= 500
         and a.get("rank") and a["rank"] <= 20 and _relevant(a["name"], a["query"])],
        key=lambda x: -x["rating_count"],
    )[:15]

    # quality gaps: популярно + плохо оценено + версия живая = feature-gap raid цель
    quality_gaps = sorted(
        [a for a in analyzed if a.get("is_quality_gap")],
        key=lambda x: -x["rating_count"],
    )[:15]

    # свежесть отзывов для топ-кандидатов (спрос жив?) — 12 запросов RSS
    for a in zombies[:12]:
        a["recent_reviews"] = fetch_recent_reviews(a["track_id"], a["country"])
        time.sleep(0.6)

    watchlist = sorted(
        [a for a in analyzed
         if not a["is_zombie"] and a["stale_months"] is not None
         and 9 <= a["stale_months"] < args.min_stale_months and a["rating_count"] >= max(100, args.min_rating_count // 5)],
        key=lambda x: -x["score"],
    )[:20]

    heat_rows_out = []
    for (cat, geo), h in sorted(heat.items(), key=lambda kv: -(kv[1]["zombie"] / max(1, kv[1]["total"]))):
        top_seller, top_cnt = (h["sellers"].most_common(1)[0] if h["sellers"] else ("—", 0))
        conc = f"{top_seller[:18]} ({100.0*top_cnt/max(1,h['total']):.0f}%)"
        heat_rows_out.append(
            f"| {cat} | {geo} | {h['total']} | {100.0*h['fresh']/max(1,h['total']):.0f}% | "
            f"{h['new']} | {100.0*h['zombie']/max(1,h['total']):.0f}% | {conc} |"
        )
    write_report(args.report, zombies, watchlist, args, heat_rows_out, risers, quality_gaps)
    if not args.quiet:
        print(f"\n== Итог: приложений проанализировано {len(analyzed)}, зомби {len(zombies)}, watchlist {len(watchlist)}")
        for a in zombies[:15]:
            print(fmt_row(a))

    # Алерты: только НОВЫЕ зомби (не флагались в прошлых прогонах)
    if con and not args.no_alerts and zombies:
        new = [a for a in zombies if not was_flagged_before(con, a["track_id"], args.min_stale_months, args.min_rating_count, run_start_iso)]
        if new:
            msg = build_alert_message(new, len(zombies))
            if args.print_alert:
                print("\n" + "=" * 50 + "\nTG ALERT (print mode):\n" + msg + "\n" + "=" * 50)
            else:
                send_telegram(msg)

    if con:
        con.close()


if __name__ == "__main__":
    main()
