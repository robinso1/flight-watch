#!/usr/bin/env python3
"""Мониторинг цен на авиабилеты через Google Flights (fast-flights) с алертами в Telegram.

Запуск: python watch.py            обычный проход
        python watch.py --test-tg  проверить отправку в Telegram
        python watch.py --selftest офлайн-проверка логики на выдуманных ценах
Секреты берутся из переменных окружения TG_TOKEN и TG_CHAT.
"""
import argparse
import csv
import json
import os
import random
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import msg

ROOT = Path(__file__).parent
MSK = ZoneInfo("Europe/Moscow")
DATA = ROOT / "data"
DRY = False  # в selftest сообщения только печатаются


def now():
    return datetime.now(MSK)


def money(v):
    return f"{int(v):,}".replace(",", " ") + " руб."


def tg_send(text, html=False):
    """Отправка в Telegram. html=True - текст с разметкой (жирный, ссылки); при отказе шлём без разметки."""
    print("--- TG ---\n" + (msg.strip_tags(text) if html else text) + "\n----------")
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if DRY or not token or not chat:
        if not DRY:
            print("TG_TOKEN или TG_CHAT не заданы, сообщение не отправлено")
        return False

    def post(body):
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", urllib.parse.urlencode(body).encode(), timeout=20).read()

    base = {"chat_id": chat, "disable_web_page_preview": "true"}
    try:
        post({**base, "text": text, **({"parse_mode": "HTML"} if html else {})})
        return True
    except Exception as e:  # токен в тексте ошибки не печатаем
        print("Ошибка отправки в Telegram:", type(e).__name__)
    if html:  # запасной путь: лучше сообщение без красоты, чем потерянный алерт
        try:
            post({**base, "text": msg.strip_tags(text)})
            return True
        except Exception as e:
            print("Ошибка запасной отправки:", type(e).__name__)
    return False


def gf_link(cfg, origin, dest, day):
    from fast_flights import FlightQuery, create_query

    q = create_query(
        flights=[FlightQuery(date=day, from_airport=origin, to_airport=dest)],
        trip="one-way",
        currency=cfg["currency"],
        language=cfg["language"],
    )
    return q.url()


def parse_google(html):
    """Устойчивый разбор выдачи Google Flights. Рейсы без цены не роняют весь разбор."""
    import json as _json

    from fast_flights.parser import _parse_time
    from selectolax.lexbor import LexborHTMLParser

    script = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if script is None:
        raise RuntimeError("в ответе Google нет блока с рейсами (капча или страница согласия)")
    data = script.text().split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        return []
    payload = _json.loads(data)
    items = (payload[3] or [None])[0] or []
    out = []
    for k in items:
        try:
            flight = k[0]
            try:
                price = k[1][0][1]
            except (TypeError, IndexError):
                price = None
            segs = flight[2] or []
            first = segs[0]
            t = _parse_time(first[8])
            d = first[20] or [0, 0, 0]
            out.append(
                {
                    "price": int(price) if price else None,
                    "airlines": ", ".join(flight[1] or []),
                    "dep": f"{d[2]:02d}.{d[1]:02d} {t[0]:02d}:{t[1]:02d}",
                    "stops": len(segs) - 1,
                }
            )
        except Exception:
            continue
    return out


def fetch_offers(cfg, origin, dest, day):
    """Список предложений [{price|None, airlines, dep, stops}] за один день в одну сторону."""
    from fast_flights import FlightQuery, create_query, fetch_flights_html

    q = create_query(
        flights=[FlightQuery(date=day, from_airport=origin, to_airport=dest)],
        trip="one-way",
        currency=cfg["currency"],
        language=cfg["language"],
        max_stops=cfg.get("max_stops"),
    )
    offers = parse_google(fetch_flights_html(q))
    ms = cfg.get("max_stops")
    return [o for o in offers if ms is None or o["stops"] <= ms]


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def append_csv(row):
    path = DATA / "prices.csv"
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["ts", "route", "date", "price_rub", "airlines", "departure", "stops"])
        w.writerow(row)


def best_combo(cfg, cheapest):
    """Лучшая связка туда-обратно среди маршрутов из списков outs и backs.
    К цене каждого плеча добавляется надбавка маршрута (дорога и еда в пути)."""
    c = cfg.get("combo")
    if not c:
        return None
    extras = {r["name"]: r.get("extra", 0) for r in cfg["routes"]}
    outs = c.get("outs") or [c["out"]]
    backs = c.get("backs") or [c["back"]]
    best = None
    for k_out, o in cheapest.items():
        r_out, d_out = k_out.split("|")
        if r_out not in outs:
            continue
        for k_back, b in cheapest.items():
            r_back, d_back = k_back.split("|")
            if r_back not in backs:
                continue
            gap = (date.fromisoformat(d_back) - date.fromisoformat(d_out)).days
            if gap < c.get("min_stay_days", 0):
                continue
            extra = extras.get(r_out, 0) + extras.get(r_back, 0)
            total = o["price"] + b["price"] + extra
            if best is None or total < best["total"]:
                best = {"total": total, "extra": extra, "out_day": d_out, "back_day": d_back, "out": o, "back": b,
                        "out_route": r_out, "back_route": r_back}
    return best


def hot_level(price, hot, fire):
    """0 - обычная цена, 1 - горящая, 2 - очень горящая."""
    if fire and price <= fire:
        return 2
    if hot and price <= hot:
        return 1
    return 0


LEVEL_NAME = {1: "ГОРЯЩИЙ БИЛЕТ", 2: "ОЧЕНЬ ГОРЯЩИЙ БИЛЕТ"}
AIRLINE_SITES = {
    "flyone": "https://www.flyone.eu",
    "россия": "https://www.rossiya-airlines.com",
    "rossiya": "https://www.rossiya-airlines.com",
    "aeroflot": "https://www.aeroflot.ru",
    "аэрофлот": "https://www.aeroflot.ru",
    "победа": "https://www.pobeda.aero",
    "pobeda": "https://www.pobeda.aero",
}


def links(cfg, origin, dest, day, airlines=""):
    """Ссылки на покупку: Google Flights, Авиасейлс и сайт перевозчика."""
    out = [
        "Google Flights: " + gf_link(cfg, origin, dest, day),
        f"Авиасейлс: https://www.aviasales.ru/search/{origin}{day[8:10]}{day[5:7]}{dest}1",
    ]
    seen = set()
    for name, url in AIRLINE_SITES.items():
        if name in airlines.lower() and url not in seen:
            seen.add(url)
            out.append(f"Сайт перевозчика: {url}")
    return "\n".join(out)


def link_pairs(cfg, origin, dest, day, airlines=""):
    """Ссылки для проверки билета: Авиасейлс, Google Flights и сайт перевозчика, если он известен."""
    pairs = [
        ("Авиасейлс", f"https://www.aviasales.ru/search/{origin}{day[8:10]}{day[5:7]}{dest}1"),
        ("Google Flights", gf_link(cfg, origin, dest, day)),
    ]
    seen = set()
    for name, url in AIRLINE_SITES.items():
        if name in airlines.lower() and url not in seen:
            seen.add(url)
            pairs.append(("Сайт перевозчика", url))
    return pairs


def cost_lines(cfg, r, eff):
    """Строки про полную стоимость на человека и на всех, если задана надбавка или число людей."""
    pax = cfg.get("pax", 1)
    extra = r.get("extra", 0)
    lines = []
    if extra:
        lines.append(f"{r.get('extra_label', 'Дорога и еда')}: +{msg.money(extra)}")
    if extra or pax > 1:
        lines.append(f"💳 Итого: {msg.money(eff)} на человека" + (f", {msg.money(eff * pax)} на двоих" if pax == 2 else f", {msg.money(eff * pax)} на {pax}" if pax > 1 else ""))
    return lines


def leg(day, o, origin, dest):
    return {"origin": origin, "dest": dest, "date": day, "price": o["price"], "carrier": o["airlines"],
            "dep_time": o["dep"][-5:], "stops": o["stops"]}


def line(day, o):
    st = "прямой" if o["stops"] == 0 else f"пересадок: {o['stops']}"
    return f"{day[8:]}.{day[5:7]} {money(o['price'])}, {o['airlines']}, вылет {o['dep'][-5:]}, {st}"


def run(cfg):
    DATA.mkdir(exist_ok=True)
    state = load_json(DATA / "state.json", {"keys": {}, "meta": {}})
    keys, meta = state["keys"], state["meta"]
    first_run = not keys and not meta.get("started")
    drop = 1 - cfg.get("drop_percent", 3) / 100
    stamp = now().strftime("%Y-%m-%d %H:%M")

    cheapest, errors, empties, total_req, airlines_seen = {}, [], 0, 0, set()
    unpriced_seen = {}
    for r in cfg["routes"]:
        for day in r["dates"]:
            if date.fromisoformat(day) < now().date():
                continue
            total_req += 1
            key = f"{r['name']}|{day}"
            try:
                offers = fetch_offers(cfg, r["origin"], r["dest"], day)
            except Exception as e:
                import traceback
                tb = traceback.extract_tb(e.__traceback__)[-1]
                errors.append(f"{key}: {type(e).__name__} {str(e)[:100]} ({tb.name}:{tb.lineno})")
                offers = None
            if not DRY:
                time.sleep(random.uniform(3, 8))
            if offers is None:
                continue
            if not offers:
                empties += 1
                continue
            for o in offers:
                airlines_seen.update(a.strip() for a in o["airlines"].split(","))
            unpriced = [o for o in offers if not o["price"]]
            offers = [o for o in offers if o["price"]]
            if unpriced:
                unpriced_seen[key] = unpriced
            if not offers:
                empties += 1
                continue
            best = min(offers, key=lambda o: o["price"])
            cheapest[key] = best
            k = keys.setdefault(key, {"min": None, "last": None, "alerted": None})
            alerts, hot_alerts = [], []
            extra = r.get("extra", 0)
            eff = best["price"] + extra  # цена с дорогой и едой, на одного
            prev_min = k["min"]
            if k["min"] is None:
                k["min"] = eff
            elif eff < k["min"] * drop:
                alerts.append("drop")
            k["min"] = min(k["min"], eff)
            lvl = hot_level(eff, cfg.get("hot_rub"), cfg.get("fire_rub"))
            if lvl and (lvl > k.get("lvl", 0) or k["alerted"] is None or eff < k["alerted"] * drop):
                hot_alerts.append(lvl)
                k["alerted"] = eff
                k["lvl"] = lvl
            if k["last"] != best["price"]:
                append_csv([stamp, r["name"], day, best["price"], best["airlines"], best["dep"], best["stops"]])
                k["last"] = best["price"]
            if hot_alerts or (alerts and not first_run):
                kind = ("great" if hot_alerts[0] == 2 else "low") if hot_alerts else "drop"
                tg_send(
                    msg.deal(kind, r["origin"], r["dest"], day, best["price"], prev=prev_min if alerts else None,
                             stops=best["stops"], dep_time=best["dep"][-5:], carrier=best["airlines"],
                             cost_lines=cost_lines(cfg, r, eff),
                             links=link_pairs(cfg, r["origin"], r["dest"], day, best["airlines"])),
                    html=True,
                )

    # общий сбой источника
    if total_req and len(errors) == total_req:
        meta["fail_streak"] = meta.get("fail_streak", 0) + 1
        if meta["fail_streak"] == 3:
            tg_send(msg.notice("⚠️", "Google Flights не отвечает", "Уже 3 проверки подряд. Пример ошибки: " + errors[0]), html=True)
    else:
        if meta.get("fail_streak", 0) >= 3:
            tg_send(msg.notice("✅", "Google Flights снова отвечает"), html=True)
        meta["fail_streak"] = 0

    # источник отвечает, но рейсов нет
    if total_req and not errors and not cheapest:
        meta["empty_streak"] = meta.get("empty_streak", 0) + 1
        if meta["empty_streak"] == 6:
            tg_send(msg.notice("⚠️", "Нет рейсов в выдаче", "Три часа подряд Google Flights не показывает ни одного рейса на эти даты. Возможно, продажа ещё не открыта или перевозчики в выдаче не отображаются. Проверьте вручную."), html=True)
    elif cheapest:
        meta["empty_streak"] = 0

    # лучшая пара туда-обратно
    combo = best_combo(cfg, cheapest)
    if combo:
        cm = meta.get("combo_min")
        new_min = cm is not None and combo["total"] < cm * drop
        hot_c = None
        clvl = hot_level(combo["total"], cfg["combo"].get("hot_rub"), cfg["combo"].get("fire_rub"))
        if clvl and (clvl > meta.get("combo_lvl", 0) or meta.get("combo_alerted") is None or combo["total"] < meta["combo_alerted"] * drop):
            hot_c = clvl
            meta["combo_alerted"] = combo["total"]
            meta["combo_lvl"] = clvl
        prev_combo = cm
        meta["combo_min"] = combo["total"] if cm is None else min(cm, combo["total"])
        if hot_c or (new_min and not first_run):
            r_out = next(r for r in cfg["routes"] if r["name"] == combo["out_route"])
            r_back = next(r for r in cfg["routes"] if r["name"] == combo["back_route"])
            kind = ("great" if hot_c == 2 else "low") if hot_c else "drop"
            note = None
            pax = cfg.get("pax", 1)
            note = (f"Дорога и еда в пути заложены: {money(combo['extra'])}\n" if combo["extra"] else "") + \
                   f"💳 Итого {money(combo['total'])} на человека" + (f", {money(combo['total'] * pax)} на {pax}" if pax > 1 else "")
            tg_send(
                msg.roundtrip(
                    kind,
                    leg(combo["out_day"], combo["out"], r_out["origin"], r_out["dest"]),
                    leg(combo["back_day"], combo["back"], r_back["origin"], r_back["dest"]),
                    combo["total"],
                    links_out=link_pairs(cfg, r_out["origin"], r_out["dest"], combo["out_day"], combo["out"]["airlines"]),
                    links_back=link_pairs(cfg, r_back["origin"], r_back["dest"], combo["back_day"], combo["back"]["airlines"]),
                    prev=prev_combo if new_min else None,
                    note=note,
                ),
                html=True,
            )

    # первое сообщение и суточная сводка
    today = now().strftime("%Y-%m-%d")
    want_digest = first_run or (now().hour >= cfg.get("digest_hour_msk", 9) and meta.get("digest_date") != today)
    if want_digest and not (cheapest or unpriced_seen) and not first_run:
        # тишина не должна путаться со сбоем: раз в сутки сообщаем, что бот жив
        tg_send(msg.notice("📊", "Сводка", "Бот работает. Google Flights сейчас не показывает рейсов на выбранные даты, проверки идут по расписанию."), html=True)
        meta["digest_date"] = today
    elif want_digest and (cheapest or unpriced_seen):
        head = "🟢 <b>МОНИТОРИНГ ЗАПУЩЕН</b>" if first_run else "📊 <b>СВОДКА</b>"
        routes = []
        for r in cfg["routes"]:
            if not r.get("digest", True):
                continue
            opts = []
            for key, o in cheapest.items():
                rname, day = key.split("|")
                if rname == r["name"]:
                    opts.append({"date": day, "price": o["price"], "dep_time": o["dep"][-5:], "carrier": o["airlines"], "stops": o["stops"]})
            routes.append({"origin": r["origin"], "dest": r["dest"], "options": opts})
        unpriced_names = sorted({o["airlines"] + " " + o["dep"][-5:] for v in unpriced_seen.values() for o in v})
        tg_send(
            msg.digest(head, routes, pair=combo, carriers=sorted(airlines_seen), unpriced=unpriced_names),
            html=True,
        )
        meta["digest_date"] = today
    meta["started"] = True

    # раз в неделю трогаем файл, чтобы GitHub не отключил расписание в неактивном репозитории
    if now().weekday() == 0:
        (DATA / "keepalive.txt").write_text(today + "\n", encoding="utf-8")

    save_json(DATA / "state.json", state)
    print(f"Запросов: {total_req}, с ошибками: {len(errors)}, пустых: {empties}, с ценой: {len(cheapest)}")
    for e in errors[:3]:
        print("ERR", e)


def selftest():
    """Проверка логики без сети: две выдуманные выдачи подряд."""
    global DATA, DRY
    DRY = True
    DATA = Path(tempfile.mkdtemp())
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    cfg["hot_rub"], cfg["fire_rub"] = 10000, 7000
    cfg["combo"]["hot_rub"], cfg["combo"]["fire_rub"] = 15000, 11000
    for r in cfg["routes"]:
        r["dates"] = [(now().date() + timedelta(days=d)).isoformat() for d in (60, 61)]
    prices = {"LED-EVN": 7000, "EVN-LED": 8500, "SVO-EVN": 3500, "EVN-SVO": 4200}
    mode = {"k": 1}

    def fake(cfg, o, d, day):
        base = prices.get(f"{o}-{d}", 12000) * mode["k"]
        return [{"price": base, "airlines": "FlyOne Armenia", "dep": "15.12 18:55", "stops": 0}]

    globals()["fetch_offers"] = fake
    print("== проход 1: только стартовое сообщение ==")
    run(cfg)
    mode["k"] = 0.9
    print("== проход 2: цены упали, связка с Москвой должна дать сообщение ==")
    run(cfg)
    print("state:", (DATA / "state.json").read_text()[:300])
    print("csv строк:", len((DATA / "prices.csv").read_text().splitlines()) - 1)


FAKE = []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-tg", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.test_tg:
        ok = tg_send(msg.notice("✅", "Проверка связи", "Бот мониторинга билетов работает."), html=True)
        sys.exit(0 if ok else 1)
    run(json.loads((ROOT / "config.json").read_text(encoding="utf-8")))


if __name__ == "__main__":
    main()
