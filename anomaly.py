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

import watch  # общие функции: tg_send, money, now, gf_link

ROOT = Path(__file__).parent
# Страны, куда россиянам, по моим данным, не нужна виза заранее. НЕ ПРОВЕРЕНО на 2026 год:
# правила меняются, перед покупкой сверяйтесь с МИД и посольством. Список можно править в config.json.
VISA_FREE = ["RU", "BY", "KZ", "KG", "TJ", "UZ", "AM", "AZ", "GE", "TR", "RS", "ME", "BA", "AL",
             "AE", "QA", "IL", "MA", "TH", "VN", "MY", "CU", "MN", "TN"]
DEFAULTS = {
    "origins": ["LED", "MOW"],
    "priority": ["LED-KGD", "LED-EVN"],  # главные маршруты: порог мягче
    "priority_ratio": 0.7,
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
        if gk in priority:
            limit = c["priority_ratio"]
        elif vs == "visa":
            limit = c["fire_ratio"]  # виза нужна: сообщаем только об очень горящих
        else:
            limit = c["ratio"]
        cap = c["max_price_rub"] if gk not in priority else max(c["max_price_rub"], 1)
        if norm and best["value"] <= norm * limit and best["value"] <= cap:
            key = f"{gk}|{best['depart_date']}"
            prev = alerted.get(key)
            if prev is None or best["value"] < prev * (1 - c["realert_drop"]):
                found.append((best["value"] / norm, gk, best, norm, src, key, vs))

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
    visa_text = {"free": "виза не нужна (по списку в настройках, проверьте правила)", "visa": "виза, возможно, нужна", "unknown": "виза: неизвестно"}
    sent = 0
    for ratio, gk, o, norm, src, key, vs in found[: c["max_alerts"]]:
        origin, dest = gk.split("-")
        title = "ОЧЕНЬ ГОРЯЩАЯ ЦЕНА" if ratio <= c["fire_ratio"] else "АНОМАЛЬНО НИЗКАЯ ЦЕНА"
        if gk in priority:
            title = "ГЛАВНЫЙ МАРШРУТ, " + title
        d = o["depart_date"]
        stops = "прямой" if not o.get("number_of_changes") else f"пересадок: {o['number_of_changes']}"
        name = (cities.get(dest) or {}).get("n") or dest
        oname = "Москвы" if origin == "MOW" else origin
        text = (
            f"{title}: {origin} - {name} ({dest}), вылет из {oname}\n"
            f"{watch.money(o['value'])} при норме {watch.money(norm)} (это {int(ratio * 100)}%, норма по данным: {src})\n"
            f"Вылет {d[8:]}.{d[5:7]}, {stops}, {visa_text[vs]}\n"
            f"Найдено {str(o.get('found_at', ''))[:16].replace('T', ' ')}\n"
            f"Авиасейлс: https://www.aviasales.ru/search/{origin}{d[8:10]}{d[5:7]}{dest}1\n"
            f"Google Flights: {watch.gf_link({'currency': 'RUB', 'language': 'ru'}, origin, dest, d)}\n"
            "Цена из кэша Авиасейлса, перед покупкой проверьте её по ссылке."
        )
        if notify:
            notify(text)
        alerted[key] = o["value"]
        sent += 1

    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    print(f"Предложений: {len(offers)}, после фильтра: {len(good)}, направлений: {len(groups)}, аномалий: {len(found)}, отправлено: {sent}")
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


if __name__ == "__main__":
    main()
