#!/usr/bin/env python3
"""Поиск аномально низких цен из Петербурга по всем направлениям (Travelpayouts Data API).

Запуск: python anomaly.py            обычный проход
        python anomaly.py --selftest офлайн-проверка на выдуманных данных
Нужны переменные окружения TRAVELPAYOUTS_TOKEN, TG_TOKEN, TG_CHAT.
"""
import gzip
import json
import os
import statistics
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import msg
import watch  # общие функции: tg_send, now, gf_link

ROOT = Path(__file__).parent
# Страны, куда россиянам, по моим данным, не нужна виза заранее. НЕ ПРОВЕРЕНО на 2026 год:
# правила меняются, перед покупкой сверяйтесь с МИД и посольством. Список можно править в config.json.
VISA_FREE = ["RU", "BY", "KZ", "KG", "TJ", "UZ", "AM", "AZ", "GE", "TR", "RS", "ME", "BA", "AL",
             "AE", "QA", "IL", "MA", "TH", "VN", "MY", "CU", "MN", "TN"]
DEFAULTS = {
    "origins": ["LED", "MOW"],
    "priority": ["LED-KGD", "LED-EVN"],  # главные маршруты: порог мягче
    "priority_ratio": 0.5,
    "hot_discount": 0.6,     # "горячие" Авиасейлс: скидка от медианы календаря цен не меньше 60%
    "hot_fire_discount": 0.7,
    "hot_min_points": 8,     # сколько дней в календаре нужно, чтобы медиана считалась надежной
    "hot_max_price": 40000,
    "niche": {"dests": [], "ratio": 0.2, "discount": 0.8},  # малопопулярные направления: только очень глубокая скидка
    "strict_origins": {},    # {"MOW": {"ratio": 0.25, "discount": 0.85}}: из этих городов только очень сильные аномалии
    "low_interest": {"dests": [], "max_price": 1500},  # неинтересные внутренние города: только если совсем дёшево
    "dest_windows": {},      # {"EVN": ["2026-12-15", "2026-12-18"]}: вылеты в это направление вне окна не показываем
    "abs_rub": {},           # {"LED-KGD": {"send": 2000, "fire": 1500}}: абсолютные пороги вместо процентов
    "visa_free": VISA_FREE,
    "origin": "LED",
    "one_way": True,
    "max_changes": 1,        # не больше одной пересадки
    "ratio": 0.5,            # аномалия: цена не выше 50% от нормы направления
    "fire_ratio": 0.35,      # очень горящая: не выше 35% от нормы
    "max_price_rub": 30000,  # отсекаем дорогие направления, где 50% все равно дорого
    "min_days": 7,           # сколько дней истории нужно для устойчивой нормы
    "min_snapshot": 6,       # сколько предложений нужно для нормы по текущей выдаче
    "history_days": 45,
    "max_alerts": 10,
    "realert_drop": 0.05,    # повторный алерт по тому же билету, если цена упала еще на 5%
}


def api_get(url):
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip, deflate"})
    raw = urllib.request.urlopen(req, timeout=60).read()
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass
    return json.loads(raw.decode("utf-8"))


def fetch_latest(token, c, origin, destination=None):
    params = {
            "currency": "rub",
            "origin": origin,
            "period_type": "year",
            "one_way": "true" if c["one_way"] else "false",
            "sorting": "price",
            "limit": 1000,
            "show_to_affiliates": "false",  # все цены, а не только с партнерским маркером
            "token": token,
    }
    if destination:
        params["destination"] = destination
    d = api_get("http://api.travelpayouts.com/v2/prices/latest?" + urllib.parse.urlencode(params))
    if not d.get("success"):
        raise RuntimeError("API вернул ошибку: " + str(d.get("error")))
    out = d["data"] or []
    for o in out:
        o["_origin"] = origin
    return out


def city_names(data_dir):
    """Названия городов: качаем cities.json раз в неделю."""
    path = data_dir / "cities.json"
    try:
        fresh = (date.today() - date.fromtimestamp(path.stat().st_mtime)).days < 7
    except OSError:
        fresh = False
    if not fresh:
        try:
            raw = api_get("http://api.travelpayouts.com/data/cities.json")
            names = {x["code"]: {"n": (x.get("name_translations") or {}).get("ru") or x.get("name"), "c": x.get("country_code")} for x in raw}
            path.write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print("Не удалось обновить cities.json:", type(e).__name__)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def norm_for(dest, price_list, hist, today, c):
    """Норма цены направления: медиана дневных минимумов, а пока истории мало - медиана текущей выдачи."""
    days = hist.get(dest, {})
    past = [v for k, v in days.items() if k != today]
    if len(past) >= c["min_days"]:
        return statistics.median(past), "история"
    if len(price_list) >= c["min_snapshot"]:
        return statistics.median(price_list), "выдача"
    return None, None


def visa_status(dest, cities, c):
    info = cities.get(dest)
    if not info:
        return "unknown"
    return "free" if info.get("c") in c["visa_free"] else "visa"


def in_window(c, dest, d):
    w = c.get("dest_windows", {}).get(dest)
    return not w or (w[0] <= d <= w[1])


def run(c, token, data_dir, offers, notify):
    today = watch.now().strftime("%Y-%m-%d")
    state_path = data_dir / "anomaly_state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        state = {"hist": {}, "alerted": {}}
    hist, alerted = state["hist"], state["alerted"]
    cities = city_names(data_dir) if notify else {}
    priority = set(c["priority"])

    seen, good = set(), []
    for o in offers:
        sig = (o["_origin"], o["destination"], o["depart_date"], o.get("value"))
        if sig in seen:
            continue
        seen.add(sig)
        if not in_window(c, o["destination"], o["depart_date"]):
            continue
        if o.get("actual", True) and (o.get("number_of_changes") or 0) <= c["max_changes"] and o.get("value"):
            good.append(o)
    groups = {}
    for o in good:
        groups.setdefault(f"{o['_origin']}-{o['destination']}", []).append(o)

    found = []
    for gk, lst in groups.items():
        origin, dest = gk.split("-")
        prices = [o["value"] for o in lst]
        norm, src = norm_for(gk, prices, hist, today, c)
        best = min(lst, key=lambda o: o["value"])
        vs = visa_status(dest, cities, c)
        li = c.get("low_interest", {})
        if dest in li.get("dests", []) and best["value"] > li.get("overrides", {}).get(dest, li["max_price"]):
            continue
        if gk in priority:
            limit = c["priority_ratio"]
        elif vs == "visa":
            limit = c["fire_ratio"]  # виза нужна: сообщаем только об очень горящих
        else:
            limit = c["ratio"]
        st = c.get("strict_origins", {}).get(origin)
        if st:
            limit = min(limit, st["ratio"])
        nc = c.get("niche", {})
        if dest in nc.get("dests", []) and gk not in priority:
            limit = min(limit, nc["ratio"])
        cap = c["max_price_rub"] if gk not in priority else max(c["max_price_rub"], 1)
        ab = c.get("abs_rub", {}).get(gk)
        if ab:
            hit = best["value"] <= ab["send"]
            norm = norm or ab.get("norm")
        else:
            hit = bool(norm) and best["value"] <= norm * limit and best["value"] <= cap
        if hit:
            key = f"{gk}|{best['depart_date']}"
            prev = alerted.get(key)
            if prev is None or best["value"] < prev * (1 - c["realert_drop"]):
                found.append((best["value"] / (norm or best["value"]), gk, best, norm, src, key, vs))

    for gk, lst in groups.items():
        m = min(o["value"] for o in lst)
        h = hist.setdefault(gk, {})
        h[today] = min(h.get(today, m), m)
    cutoff = (date.fromisoformat(today) - timedelta(days=c["history_days"])).isoformat()
    for gk in list(hist):
        hist[gk] = {k: v for k, v in hist[gk].items() if k >= cutoff}
        if not hist[gk]:
            del hist[gk]
    for k in list(alerted):
        if k.split("|")[1] < today:
            del alerted[k]

    # приоритетные маршруты идут первыми, дальше по глубине скидки
    found.sort(key=lambda x: (x[1] not in priority, x[0]))
    sent = 0
    for ratio, gk, o, norm, src, key, vs in found[: c["max_alerts"]]:
        origin, dest = gk.split("-")
        d = o["depart_date"]
        text = msg.deal(
            "fire" if (o["value"] <= c["abs_rub"][gk]["fire"] if gk in c.get("abs_rub", {}) else ratio <= c["fire_ratio"]) else "low",
            origin, dest, d, o["value"],
            norm=norm,
            stops=o.get("number_of_changes") or 0,
            visa=vs if vs in ("free", "visa") else None,
            links=[
                ("Авиасейлс", f"https://www.aviasales.ru/search/{origin}{d[8:10]}{d[5:7]}{dest}1"),
                ("Google Flights", watch.gf_link({"currency": "RUB", "language": "ru"}, origin, dest, d)),
            ],
            found_at=o.get("found_at"),
            cities=cities,
            priority=gk in priority,
            cache_note=True,
        )
        if notify:
            notify(text, html=True)
        alerted[key] = o["value"]
        sent += 1

    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    print(f"Предложений: {len(offers)}, после фильтра: {len(good)}, направлений: {len(groups)}, аномалий: {len(found)}, отправлено: {sent}")
    return found


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    return (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2) if n else None


def hot_scan(c, token, data_dir, notify):
    """Горячие билеты Авиасейлс (специальные предложения) + своя скидка: цена против медианы календаря цен месяца."""
    state_path = data_dir / "anomaly_state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        state = {"hist": {}, "alerted": {}}
    alerted = state.setdefault("alerted", {})
    cities = city_names(data_dir) if notify else {}
    cand, sent, checked = [], 0, 0
    for origin in c["origins"]:
        try:
            q = urllib.parse.urlencode({"origin": origin, "locale": "ru", "currency": "rub", "market": "ru", "token": token})
            d = api_get("https://api.travelpayouts.com/aviasales/v3/get_special_offers?" + q)
            for o in d.get("data") or []:
                if o.get("price") and o.get("destination") and o.get("departure_at"):
                    cand.append((origin, o))
        except Exception as e:
            print("Ошибка горячих билетов", origin, type(e).__name__, str(e)[:80])
    found = []
    for origin, o in cand:
        dest, price, date_ = o["destination"], o["price"], o["departure_at"][:10]
        if not in_window(c, dest, date_):
            continue
        li = c.get("low_interest", {})
        if dest in li.get("dests", []) and price > li.get("overrides", {}).get(dest, li["max_price"]):
            continue
        try:
            q = urllib.parse.urlencode({"currency": "rub", "origin": origin, "destination": dest,
                                        "show_to_affiliates": "false", "month": date_[:7] + "-01", "token": token})
            m = api_get("https://api.travelpayouts.com/v2/prices/month-matrix?" + q).get("data") or []
        except Exception as e:
            print("Ошибка календаря", origin, dest, type(e).__name__, str(e)[:80])
            continue
        rows = [x for x in m if x.get("value") and x.get("actual", True)]
        checked += 1
        if len(rows) < c["hot_min_points"]:
            continue
        norm = median([x["value"] for x in rows])
        disc = 1 - price / norm
        vs = visa_status(dest, cities, c)
        need = c["hot_fire_discount"] if vs == "visa" else c["hot_discount"]
        if origin in c.get("strict_origins", {}):
            need = max(need, c["strict_origins"][origin]["discount"])
        nc = c.get("niche", {})
        if dest in nc.get("dests", []):
            need = max(need, nc["discount"])
        key = f"{origin}-{dest}|{date_}"
        prev = alerted.get(key)
        if disc >= need and price <= c["hot_max_price"] and (prev is None or price < prev * (1 - c["realert_drop"])):
            same = [x for x in rows if x.get("depart_date") == date_]
            best = min(same, key=lambda x: x["value"]) if same else {}
            found.append((disc, origin, dest, date_, price, norm, vs, best, key, o))
    found.sort(key=lambda x: -x[0])
    for disc, origin, dest, date_, price, norm, vs, best, key, o in found[: c["max_alerts"]]:
        text = msg.deal(
            "fire" if disc >= c["hot_fire_discount"] else "low", origin, dest, date_, price,
            norm=norm, stops=best.get("number_of_changes"),
            dep_time=o["departure_at"][11:16] if len(o.get("departure_at", "")) >= 16 else None,
            carrier=o.get("airline_title"),
            visa=vs if vs in ("free", "visa") else None,
            links=[
                ("Авиасейлс", f"https://www.aviasales.ru/search/{origin}{date_[8:10]}{date_[5:7]}{dest}1"),
                ("Google Flights", watch.gf_link({"currency": "RUB", "language": "ru"}, origin, dest, date_)),
            ],
            found_at=best.get("found_at"), cities=cities, cache_note=True,
        )
        if notify:
            notify(text, html=True)
        alerted[key] = price
        sent += 1
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    print(f"Горячие билеты: предложений {len(cand)}, проверено календарей {checked}, подходит {len(found)}, отправлено {sent}")
    return found


def selftest():
    watch.DRY = True
    d = Path(tempfile.mkdtemp())
    c = dict(DEFAULTS, min_days=3)
    mk = lambda o, dest, price, dep="2026-12-20", ch=0: {
        "_origin": o, "destination": dest, "value": price, "depart_date": dep, "number_of_changes": ch,
        "found_at": "2026-09-29T15:00:00+03:00", "actual": True,
    }
    snap = [mk("LED", "KGD", p) for p in (9000, 9500, 10000, 10500, 11000, 12000)] + [mk("LED", "KGD", 6500, "2026-11-10")]
    snap += [mk("LED", "IST", p) for p in (20000, 21000, 22000, 23000, 24000, 25000)] + [mk("LED", "IST", 9000, "2026-11-11")]
    snap += [mk("MOW", "BKK", p) for p in (30000, 31000, 32000, 33000, 34000, 35000)] + [mk("MOW", "BKK", 9000, "2026-11-12")]
    print("== KGD: 6500 при норме ~10 750 (60%), главный маршрут (порог 70%) - алерт первым. IST 9000 - 40%, обычная аномалия. BKK из MOW - 27% ==")
    run(c, "x", d, snap, watch.tg_send)
    print("== горячие: MIR 9939 при медиане календаря ~36 000 (скидка 72%) ==")
    global api_get
    real = api_get
    def fake(url):
        if "get_special_offers" in url:
            return {"data": [{"destination": "MIR", "price": 9939, "departure_at": "2026-10-20T09:30:00+03:00", "airline_title": "Pyramids Airlines"},
                             {"destination": "UFA", "price": 3534, "departure_at": "2026-10-03T00:45:00+03:00", "airline_title": "Nordwind"}]}
        base = 36000 if "MIR" in url else 4500
        rows = [{"depart_date": f"2026-10-{i:02d}", "value": base + i * 300, "actual": True, "number_of_changes": 0, "found_at": "2026-09-30T16:02:13Z"} for i in range(1, 20)]
        rows.append({"depart_date": "2026-10-20", "value": 9939 if "MIR" in url else 3534, "actual": True, "number_of_changes": 0, "found_at": "2026-09-30T16:02:13Z"})
        return {"data": rows}
    api_get = fake
    try:
        hot_scan(dict(c, origins=["LED"]), "x", d, watch.tg_send)
    finally:
        api_get = real


def main():
    if "--selftest" in sys.argv:
        return selftest()
    token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not token:
        print("TRAVELPAYOUTS_TOKEN не задан")
        sys.exit(1)
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    c = dict(DEFAULTS, **cfg.get("anomaly", {}))
    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    offers, errors = [], []
    jobs = [(o, None) for o in c["origins"]] + [tuple(p.split("-")) for p in c["priority"]]
    for origin, dest in jobs:
        try:
            offers += fetch_latest(token, c, origin, dest)
        except Exception as e:
            errors.append(f"{origin}-{dest or 'все'}: {type(e).__name__} {str(e)[:100]}")
    for e in errors:
        print("Ошибка API:", e)
    if len(errors) == len(jobs):
        sys.exit(1)
    run(c, token, data_dir, offers, watch.tg_send)
    try:
        hot_scan(c, token, data_dir, watch.tg_send)
    except Exception as e:
        print("Горячие билеты: сбой", type(e).__name__, str(e)[:100])


if __name__ == "__main__":
    main()
