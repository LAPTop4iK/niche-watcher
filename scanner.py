#!/usr/bin/env python3
"""
Niche Decay Scanner — находит приложения-кандидаты в App Store:
давно не обновлявшиеся приложения с накопленными оценками — повод проверить боль.

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
import db_store
import html
import json
import os
import re
import sqlite3
import sys
import time
import scan_state
import raw_archive
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone, timedelta

API_URL = "https://itunes.apple.com/search"
REQUEST_DELAY = float(os.environ.get("SCAN_DELAY", "3.0"))  # iTunes ~20/мин; для длинных локальных наблюдений SCAN_DELAY=4
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
    parsed=datetime.fromisoformat(s.replace('Z','+00:00'))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- HTTP

def fetch_json(url, quiet=False):
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "niche-decay-scanner/1.0"}
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                body=resp.read()
                ref=raw_archive.capture(url,body,getattr(resp,'status',200),getattr(resp,'headers',{}))
                return raw_archive.attach_ref(json.loads(body.decode("utf-8", "replace")),ref)
        except raw_archive.ArchiveError:
            raise
        except Exception as e:  # noqa: BLE001 — сетевые ошибки любые
            if isinstance(e,urllib.error.HTTPError):
                raw_archive.capture(url,e.read(),e.code,e.headers)
            last_err = e
            is_429 = getattr(e, "code", None) == 429
            # 429 у iTunes требует длинной паузы (окно лимита ~минута), прочее — обычный backoff
            wait = int(os.environ.get("RATE_429_WAIT", "45")) * attempt if is_429 else 2 ** attempt
            if not quiet:
                print(f"  ! попытка {attempt}/{MAX_RETRIES} не удалась ({e}); жду {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"не удалось получить {url}: {last_err}")


def search_apps(term, country, limit, entity="software"):
    # A `lang` parameter changed neither the set nor the order on de, ru and sa
    # (1 Nov 2026). `software` hides iPad-only apps, but adding them reshuffles
    # about a third of the results, so that frame is a separate opt-in run.
    params = urllib.parse.urlencode(
        {"term": term, "entity": entity, "country": country, "limit": min(limit, 200)}
    )
    data = fetch_json(f"{API_URL}?{params}")
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise RuntimeError("Invalid iTunes Search response: results array missing")
    return data["results"]


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
    rank         INTEGER,
    app_name     TEXT,
    seller_name  TEXT,
    storefront_url TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_track_time ON snapshots(track_id, scanned_at);
"""


def open_db(path):
    db_store.ensure(path)
    con = sqlite3.connect(path, timeout=30)
    con.executescript(SCHEMA)
    try:  # миграция старых баз
        con.execute("ALTER TABLE snapshots ADD COLUMN rank INTEGER")
    except sqlite3.OperationalError:
        pass
    columns = {r[1] for r in con.execute("PRAGMA table_info(snapshots)")}
    for name, kind in {"app_name":"TEXT", "seller_name":"TEXT", "storefront_url":"TEXT", "run_id":"TEXT", "price_amount":"REAL", "currency":"TEXT", "release_date":"TEXT"}.items():
        if name not in columns:
            con.execute(f"ALTER TABLE snapshots ADD COLUMN {name} {kind}")
    con.executescript(scan_state.SCHEMA)
    con.execute("CREATE INDEX IF NOT EXISTS idx_snap_store_time ON snapshots(track_id,country,scanned_at,id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_snap_run ON snapshots(run_id)")
    con.commit()
    return con


def upsert_app(con, app):
    con.execute(
        """INSERT INTO apps (track_id, name, seller, bundle_id, store_url, price, genre,
                             first_country, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(track_id) DO UPDATE SET
             last_seen = MAX(apps.last_seen, excluded.last_seen), name = excluded.name,
             seller=excluded.seller, bundle_id=excluded.bundle_id, store_url=excluded.store_url,
             price=excluded.price, genre=excluded.genre""",
        (
            app["track_id"], app["name"], app["seller"], app["bundle_id"],
            app["store_url"], app["price"], app["genre"],
            app["first_country"], app["first_seen"], app["last_seen"],
        ),
    )


def insert_snapshot(con, app):
    con.execute(
        """INSERT INTO snapshots (track_id, country, category, query, scanned_at,
                                  version, version_date, rating_count, avg_rating, rank, app_name, seller_name, storefront_url, run_id, price_amount, currency, release_date)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            app["track_id"], app["country"], app["category"], app["query"],
            app["scanned_at"], app["version"], app["version_date"],
            app["rating_count"], app["avg_rating"], app.get("rank"),
            app.get("name"), app.get("seller"), app.get("store_url"),
            app.get("run_id"), app.get("price"), app.get("currency"), app.get("release_date"),
        ),
    )


def previous_snapshot(con, track_id, country, min_age_days=7):
    """Последний снапшот ТОГО ЖЕ сторфронта старше min_age_days — база для velocity.
    rating_count в API пер-страновый: смешение стран даёт мусорную velocity."""
    if con is None:  # dry-run: velocity недоступна
        return None
    cutoff = iso(now_utc() - timedelta(days=min_age_days))
    row = con.execute(
        """SELECT scanned_at, rating_count FROM snapshots
           WHERE track_id = ? AND country = ? AND scanned_at <= ?
           ORDER BY scanned_at DESC LIMIT 1""",
        (track_id, country, cutoff),
    ).fetchone()
    return row  # (scanned_at, rating_count) | None


def was_flagged_before(con, track_id, min_stale_months, min_ratings, before_iso=None, country=None):
    """Считался ли трек зомби в ПРОШЛЫХ прогонах (до before_iso — иначе текущий
    прогон флагает сам себя и алерты никогда не срабатывают)."""
    if con is None:
        return False
    conditions=['track_id=?','rating_count>=?',"(julianday(scanned_at)-julianday(version_date))/30.44>=?"]
    params=[track_id,min_ratings,min_stale_months]
    if before_iso:
        conditions.append('scanned_at<?');params.append(before_iso)
    if country:
        conditions.append('country=?');params.append(country)
    return con.execute('SELECT 1 FROM snapshots WHERE '+' AND '.join(conditions)+' LIMIT 1',params).fetchone() is not None


# ---------------------------------------------------------------- анализ

def months_between(a, b):
    """Age in 30.44-day months; compare before rounding for display."""
    return (b - a).total_seconds() / 86400 / 30.44


def analyze(con, app, min_stale_months, min_ratings):
    """Считает наблюдаемые признаки; вероятность прибыли не оценивается."""
    out = dict(app)
    stale_months = None
    if app["version_date"]:
        try:
            stale_months = months_between(parse_iso(app["version_date"]), now_utc())
        except ValueError:
            pass
    out["stale_months"] = stale_months

    # velocity: прирост оценок за 30 дней относительно снапшота >= 7 дней назад
    velocity = None
    if con is not None:
        from evidence import rating_velocity
        velocity, _ = rating_velocity(con, app["track_id"], app["country"])
    out["velocity_30d"] = velocity

    is_zombie = (
        stale_months is not None
        and stale_months >= min_stale_months
        and app["rating_count"] >= min_ratings
    )
    out["is_zombie"] = is_zombie

    # Historical dissatisfaction hint; fresh complaints and payments need separate checks.
    out["is_quality_gap"] = bool(
        app["rating_count"] >= 1000
        and app["avg_rating"] and app["avg_rating"] < 4.0
        and stale_months is not None and 0 <= stale_months < 12
    )


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
                body=json.loads(resp.read().decode("utf-8"))
                ok = ok and resp.status == 200 and body.get("ok") is True
        except Exception as e:  # noqa: BLE001
            print(f"  ! telegram alert failed: {type(e).__name__}", file=sys.stderr)
            ok = False
        time.sleep(1)
    return ok


def download_price_label(row):
    price=row.get('price')
    if price is None:
        return 'цена загрузки неизвестна'
    if price==0:
        return 'бесплатная загрузка'
    return f"загрузка {price:g} {row.get('currency') or '(валюта неизвестна)'}"


def build_alert_message(new, total_zombies, print_mode=False, as_of=None):
    """One bounded scan summary. Examples are not a ranking of profitable niches."""
    from zoneinfo import ZoneInfo
    e = (lambda s: s) if print_mode else html.escape
    stamp=as_of or now_utc()
    if isinstance(stamp,str):stamp=parse_iso(stamp)
    display_time=stamp.astimezone(ZoneInfo('Europe/Warsaw')).strftime('%d.%m.%Y %H:%M')
    def render(limit):
        shown=new[:limit]
        lines=["🔭 <b>Niche Watcher — новые наблюдения</b>",
               f"Старые версии, впервые подходящие под фильтр: <b>{len(new)}</b> app×страна.",
               f"Всего старых версий в этом скане: {total_zombies} app×страна.",
               f"Сбор завершён: {display_time} (Варшава).",
               f"\nПоказано {len(shown)} из {len(new)} — примеры для ручной проверки, не рейтинг доходности."]
        for a in shown:
            name=e(a['name'][:100])
            url=f"https://apps.apple.com/{a['country']}/app/id{a['track_id']}"
            label=name if print_mode else f'<a href="{html.escape(url,quote=True)}">{name}</a>'
            rating=f"{a['avg_rating']:.1f}★" if a.get('avg_rating') else '—'
            lines.append(f"\n• <b>{label}</b> {flag_emoji(a['country'])}\n"
                         f"  {a['rating_count']:,} оценок · {rating} · версия не обновлялась {a['stale_months']:.0f} мес\n"
                         f"  {e(download_price_label(a))}")
        if len(new)>len(shown):
            lines.append(f"\nЕщё {len(new)-len(shown)} новых записей — в дашборде.")
        lines.append("\nВозраст версии не доказывает живую боль или доход. Свежую активность, жалобы и платежи нужно проверить отдельно.")
        lines.append(f'\n<a href="{telegram_dashboard_url()}">Открыть дашборд</a>')
        return '\n'.join(lines)
    for limit in range(min(6,len(new)),-1,-1):
        message=render(limit)
        # Keep even heavily escaped Unicode names within one transport message.
        if len(message)<=3800 and len(message.encode('utf-16-le'))//2<=3800:
            return message
    raise ValueError('Telegram summary header exceeds its message budget')


# ---------------------------------------------------------------- отчёт

# Generic tasks to check against the current OS. Specialized calculators and
# identifiers are not equivalent to an OS utility merely because of one word.
NATIVE_RISK_KEYWORDS = {
    'calculator','simple calculator','flashlight','torch','compass','level',
    'bubble level','magnifier','document scanner','qr scanner','measure',
    'калькулятор','фонарик','уровень','компас','сканер документов',
    '計算機','계산기','电筒',
}

_STOP_TOK = set("""for with and the your app my pro free plus hd lite best simple easy all one new
timer time""".split())


def _relevant(name,query):
    from relevance import relevant
    return relevant(name,query)


def is_native_risk(name, query):
    text=" ".join(query.casefold().split())
    return text in NATIVE_RISK_KEYWORDS


def fetch_recent_reviews(track_id, country="us", con=None):
    """Legacy display adapter; use the same exact dated sample as the demand layer."""
    try:
        from demand_check import fetch_demand,prepare,store_result
        result=fetch_demand(track_id,country)
        if con is not None:
            prepare(con);store_result(con,track_id,country,result)
        return (result[0],result[1],None) if result[-1]=='OK' else None
    except Exception:  # noqa: BLE001 — отзывные данные не критичны для прогона
        return None


def fmt_row(a):
    stale = f"{a['stale_months']:.0f} мес" if a["stale_months"] is not None else "?"
    vel = f"+{a['velocity_30d']:.0f}" if a["velocity_30d"] is not None else "n/a"
    rating = f"{a['avg_rating']:.1f}" if a["avg_rating"] else "-"
    port = a.get("seller_apps")
    port_s = f"{a['seller'][:22]}{'' if port is None else f' ({port})'}"
    rev = a.get("recent_reviews")
    rev_s = f"{rev[0]} (R60 ≥ {rev[1]}, выборка RSS)" if rev else "-"
    warn = " ⚠️портфель" if (port or 0) > 8 else ""
    native = " ? ОС" if a.get("native_risk") else ""
    return (
        f"| [{a['name'][:34]}]({a['store_url']}) "
        f"| {port_s}{warn}{native} | {a['country']} | {a['rating_count']:>6} | {rating} "
        f"| {a['version_date'][:10] if a['version_date'] else '?'} | {stale} | {vel} | {rev_s} |"
    )


REPORT_HEAD = """# Niche Decay Scan — {date}

Зомби: версия старше {stale} мес И оценок >= {ratings}.
Velocity — прирост оценок за 30 дней (нужны 2+ прогона с интервалом >= 7 дней).
(прод N) — сколько приложений этого продавца встречено в прогоне: >8 = портфельная сеть ⚠️.
посл. отзыв (N) — дата последнего отзыва и сколько отзывов за 60 дней: свежие = есть активность, готовность платить не установлена.
Порядок по накопленным оценкам — для исследования, не по вероятности успеха.
Новый в БД ≠ новый конкурент. Старая версия ≠ сломанный продукт.

| Приложение | Разработчик | Гео | Оценок | Рейтинг | Версия от | Без обновл. | Vel/30д | Посл. отзыв |
|---|---|---|---|---|---|---|---|---|
"""


def write_report(path, zombies, watchlist, args, heat_rows=None, risers=None, quality_gaps=None):
    lines = [REPORT_HEAD.format(date=iso(now_utc())[:10], stale=args.min_stale_months, ratings=args.min_rating_count)]
    if zombies:
        for a in zombies:
            lines.append(fmt_row(a))
    else:
        lines.append("_Зомби по порогам не найдено._")
    lines.append(f"\n## Watchlist (версия 9–{args.min_stale_months} мес, наблюдаю)\n")
    lines.append("| Приложение | Разработчик | Гео | Оценок | Рейтинг | Версия от | Без обновл. | Vel/30д | Посл. отзыв |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
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
                "\n## Состав выдачи по категориям\n\n"
                "fresh = версия младше 12 мес; new = впервые найденные в БД. Это не число новых запусков.\n"
                "Состав не доказывает свободную нишу: нужны релевантность выдачи, спрос, платежи и канал привлечения.\n\n"
                "| Категория | Гео | Видно | Fresh% | New | Зомби% | Топ-продавец |\n|---|---|---|---|---|---|---|\n"
                + "\n".join(heat_rows) + "\n"
            )
    if risers:
        with open(path, "a", encoding="utf-8") as f:
            f.write(
                "\n## Впервые найдены в API-выдаче (это не новые запуски)\n\n"
                "| Оценок | Приложение | Гео | Запрос | Версия от |\n|---|---|---|---|---|\n"
                + "\n".join(
                    f"| {a['rating_count']:,} | [{a['name'][:36]}]({a['store_url']}) | {a['country']} "
                    f"| {a['query']} | {a['version_date'][:10] if a['version_date'] else '?'} |"
                    for a in risers) + "\n"
            )
    if quality_gaps:
        with open(path, "a", encoding="utf-8") as f:
            f.write(
                "\n## Низкий исторический рейтинг: проверить свежие жалобы\n\n"
                "| Оценок | Рейтинг | Приложение | Гео | Версия от |\n|---|---|---|---|---|\n"
                + "\n".join(
                    f"| {a['rating_count']:,} | {a['avg_rating']:.1f} | [{a['name'][:36]}]({a['store_url']}) "
                    f"| {a['country']} | {a['version_date'][:10] if a['version_date'] else '?'} |"
                    for a in quality_gaps) + "\n"
            )
    with open(path, "a", encoding="utf-8") as f:
        f.write(
            "\n## Легенда рисков\n\n"
            "- ⚠️портфель — продавец с 9+ приложениями в прогоне: число встреченных карточек; вероятность реакции автора не измерена.\n"
            "- ? ОС — лексическое пересечение с ключевиками iOS (калькулятор, фонарик, уровень,\n"
            "  сканер...). Не авто-отказ: обязателен вопрос «поглотила ли ОС боль?» Если боль\n"
            "  решена нативно (фонарик) — скип; если нет (бетонный расчёт) — нужна отстрой\n"
            "  (Apple Pencil, Math Notes-гэпы, качество) или другой заход.\n"
        )


# ---------------------------------------------------------------- main

def load_seed(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def normalize(raw, country, category, query, rank=None):
    date_str = raw.get("currentVersionReleaseDate") or ""
    return {
        "track_id": raw.get("trackId"),
        "name": raw.get("trackName") or "?",
        "seller": raw.get("sellerName") or raw.get("artistName") or "?",
        "bundle_id": raw.get("bundleId") or "",
        "store_url": raw.get("trackViewUrl") or "",
        "price": raw.get("price"),
        "genre": raw.get("primaryGenreName") or "",
        "currency": raw.get("currency"),
        "release_date": raw.get("releaseDate"),
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
    ap.add_argument("--slot", type=int, default=None, help="недельный слот 1-4 для tier-2 (week_slot из markets.json)")
    ap.add_argument("--next-slot", action="store_true", help="напечатать слот tier-2, просканированный раньше остальных, и выйти")
    ap.add_argument("--categories", default=None, help="construction,auto — фильтр категорий")
    ap.add_argument("--limit", type=int, default=50, help="результатов на запрос (max 200)")
    ap.add_argument("--entity", default="software", choices=["software","software,iPadSoftware"], help="другая рамка хранится, но не заменяет основную выдачу")
    ap.add_argument("--min-rating-count", type=int, default=DEFAULT_MIN_RATINGS)
    ap.add_argument("--min-stale-months", type=int, default=DEFAULT_MIN_STALE_MONTHS)
    ap.add_argument("--dry-run", action="store_true", help="не писать в БД")
    ap.add_argument("--no-alerts", action="store_true")
    ap.add_argument("--print-alert", action="store_true", help="показать TG-сообщение вместо отправки")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--all-markets", action="store_true")
    ap.add_argument("--run-id", help="возобновить прогон с тем же идентификатором")
    ap.add_argument("--skip-reviews", action="store_true", help="RSS проверяет отдельный demand-слой")
    args = ap.parse_args()

    seed = load_seed(args.seed)
    with open(args.markets, encoding="utf-8") as f:
        markets = json.load(f)["markets"]
    if args.next_slot:
        con = open_db(args.db)
        print(next_slot(con, markets))
        con.close()
        return
    countries = (
        [c.strip() for c in args.countries.split(",")] if args.countries
        else [c for c, v in markets.items()
              if v.get("tier", 2) == args.tier
              and (args.slot is None or args.tier != 2 or v.get("week_slot") == args.slot)]
    )
    if args.all_markets:
        countries = list(markets)
    if not countries or any(c not in markets for c in countries):
        ap.error("Нужны страны из markets.json")
    if not 1 <= args.limit <= 200:
        ap.error("limit должен быть 1..200")
    wanted = set(args.categories.split(",")) if args.categories else None
    known_categories={c["id"] for c in seed["categories"]}
    if wanted and not wanted <= known_categories:
        ap.error("Неизвестная категория")

    lock = scan_state.writer_lock(args.db) if not args.dry_run else __import__('contextlib').nullcontext()
    with lock:
        return run_scan(args,seed,markets,countries,wanted)


def planned_jobs(seed,markets,countries,wanted=None):
    jobs=[(geo,c["id"],q) for c in seed["categories"] if c.get("enabled",True) and (not wanted or c["id"] in wanted)
          for geo in countries for q in (c["queries"].get(markets[geo].get("lang","en")) or c["queries"]["en"])]
    return list(dict.fromkeys(jobs))


def next_slot(con,markets):
    """Tier-2 group whose search results are oldest; never-scanned groups first, ties by number.
    A scheduled run starts hours late, so its weekday cannot name the group."""
    slots={}
    for geo,m in markets.items():
        if m.get("tier",2)==2 and m.get("week_slot"):
            slots.setdefault(m["week_slot"],[]).append(geo)
    def last_scan(geos):
        marks=",".join("?"*len(geos))
        return con.execute(f"""SELECT MAX(q.finished_at) FROM scan_queries q JOIN scan_runs r USING(run_id)
            WHERE r.source='itunes-search' AND q.status='OK' AND q.country IN ({marks})""",geos).fetchone()[0] or ""
    return min(sorted(slots),key=lambda slot:last_scan(slots[slot]))


def job_frames(seed,markets,jobs):
    """Query language per job: a query with no local list falls back to English."""
    queries={c["id"]:c["queries"] for c in seed["categories"]}
    for geo,cat,query in jobs:
        market=markets[geo].get("lang","en")
        yield geo,cat,query,(market if query in (queries[cat].get(market) or []) else "en"),market


def run_scan(args,seed,markets,countries,wanted):
    con = None if args.dry_run else open_db(args.db)
    jobs=planned_jobs(seed,markets,countries,wanted)
    entity=getattr(args,"entity","software")
    settings={"limit":args.limit,"storage_min_ratings":0}
    if entity!="software":
        settings["entity"]=entity
    run_id=scan_state.start_run(con,jobs,settings,args.run_id) if con else None
    if con:
        scan_state.record_frames(con,job_frames(seed,markets,jobs))
    done={(r[0],r[1],r[2]) for r in con.execute("SELECT country,category,query FROM scan_queries WHERE run_id=? AND status='OK'",(run_id,))} if con else set()
    print(f"RUN {run_id}: {len(jobs)} queries, already complete {len(done)}",flush=True)
    run_start_iso = con.execute("SELECT started_at FROM scan_runs WHERE run_id=?",(run_id,)).fetchone()[0] if con else iso(now_utc())
    analyzed, seen = [], set()
    # приложения, уже бывшие в прошлых прогонах — база для inflow (heat)
    existing_ids = (
        {r[0] for r in con.execute("SELECT track_id FROM apps")} if con else set()
    )

    remaining=[job for job in jobs if job not in done]
    search=lambda query,geo: search_apps(query,geo,args.limit,entity)
    for (country,cid,q),results,error in scan_state.query_results(con,run_id,remaining,search,REQUEST_DELAY):
        if error:
            print(f"! {country}/{cid}/{q}: {error}",file=sys.stderr,flush=True)
            if con:
                scan_state.end_query(con,run_id,country,cid,q,'ERROR',error=error)
            continue
        stored_count=0
        for pos,raw in enumerate(results,1):
            tid=raw.get('trackId');key=(tid,country,cid,q)
            if not tid or key in seen:
                continue
            seen.add(key)
            app=normalize(raw,country,cid,q,rank=pos)
            app['run_id']=run_id
            app['native_risk']=is_native_risk(app['name'],q)
            if con:
                upsert_app(con,app);insert_snapshot(con,app)
            stored_count+=1
            analyzed.append(analyze(con,app,args.min_stale_months,args.min_rating_count))
        if con:
            scan_state.end_query(con,run_id,country,cid,q,'OK',len(results),stored_count)
            done.add((country,cid,q))
            if len(done)%25==0:
                print(f"PROGRESS {len(done)}/{len(jobs)} · {country}/{cid}",flush=True)

    if con:
        con.commit()

    if con:
        # Resume reports include the whole run, not only the part after a restart.
        from evidence import latest
        data=con.execute("SELECT s.*,a.bundle_id,a.genre,a.first_country,a.first_seen,a.last_seen FROM snapshots s JOIN apps a USING(track_id) WHERE s.run_id=?",(run_id,))
        fields=[d[0] for d in data.description]
        all_rows=[dict(zip(fields,r)) for r in data.fetchall()]
        analyzed=[]
        for r in all_rows:
            r.update(name=r['app_name'],seller=r['seller_name'],store_url=r['storefront_url'],price=r['price_amount'])
            r['native_risk']=is_native_risk(r['name'],r['query'])
            analyzed.append(analyze(con,r,args.min_stale_months,args.min_rating_count))

    # Keep every query/category observation in the DB, but count each app only
    # once per storefront in alerts and each category/storefront in heat.
    unique = {}
    for app in analyzed:
        unique.setdefault((app["track_id"], app["country"], app["category"]), app)
    analyzed = list(unique.values())

    # портфельный аудит: сколько приложений каждого продавца встречено в прогоне
    seller_counts = Counter(seller for seller, tid in {(a["seller"], a["track_id"]) for a in analyzed})
    for a in analyzed:
        a["seller_apps"] = seller_counts[a["seller"]]

    zombie_map = {}
    for a in analyzed:
        if a["is_zombie"]:
            zombie_map.setdefault((a["track_id"], a["country"]), a)
    zombies = sorted(zombie_map.values(), key=lambda x: -x["rating_count"])

    # heat по категориям: рост конкуренции (свежие приложения) + приток новых за прогон
    heat = {}
    for a in analyzed:
        key = (a["category"], a["country"])
        h = heat.setdefault(key, {"total": 0, "fresh": 0, "new": 0, "zombie": 0, "sellers": Counter()})
        h["total"] += 1
        if a["stale_months"] is not None and 0 <= a["stale_months"] < 12:
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
    for a in ([] if args.skip_reviews else zombies[:12]):
        a["recent_reviews"] = fetch_recent_reviews(a["track_id"], a["country"],con)
        time.sleep(0.6)

    watchlist = sorted(
        [a for a in analyzed
         if not a["is_zombie"] and a["stale_months"] is not None
         and 9 <= a["stale_months"] < args.min_stale_months and a["rating_count"] >= max(100, args.min_rating_count // 5)],
        key=lambda x: -x["rating_count"],
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

    run_status=None
    if con:
        run_status,ok,expected=scan_state.finish_run(con,run_id)
        print(f"RUN {run_id}: {run_status} {ok}/{expected}",flush=True)
    telegram_failed=False
    if con and not args.no_alerts:
        import notifications
        new=[a for a in zombies if not was_flagged_before(con,a['track_id'],args.min_stale_months,args.min_rating_count,run_start_iso,country=a['country'])]
        finished=con.execute('SELECT finished_at FROM scan_runs WHERE run_id=?',(run_id,)).fetchone()[0]
        if new:
            build=lambda items:build_alert_message(items,len(zombies),as_of=finished)
            if args.print_alert:
                print('TG PREVIEW:',build(new))
            else:
                queued=notifications.enqueue_candidates(con,new,build)
                print(f'Telegram summary: {queued} new app/storefront items; {len(new)-queued} already queued or delivered',flush=True)
        else:
            message=f'🔭 Niche Watcher: сбор {run_status} {ok}/{expected}; кандидатов {len(zombies)}. Активность и платежи проверяются отдельно. <a href="{telegram_dashboard_url()}">Дашборд</a>'
            if args.print_alert:
                print('TG PREVIEW:',message)
            else:
                notifications.enqueue(con,f'heartbeat:{run_id}',message)
        if not args.print_alert and os.environ.get('TG_BOT_TOKEN') and os.environ.get('TG_CHAT_ID'):
            delivered,failed=notifications.flush(con,send_telegram)
            print(f'Telegram acknowledged {delivered}; pending failures {failed}',flush=True)
            telegram_failed=bool(failed)

    if con:
        con.close()
    if run_status=='PARTIAL':
        raise SystemExit("Scan incomplete; query errors are recorded and can be retried with --run-id")
    if telegram_failed and (os.environ.get("TG_BOT_TOKEN") or os.environ.get("TG_CHAT_ID")):
        raise SystemExit("Telegram delivery failed; scan data has been saved")


if __name__ == "__main__":
    main()
