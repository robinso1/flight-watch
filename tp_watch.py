#!/usr/bin/env python3
"""Дорога домой после концерта: самый дешевый путь Ереван -> Петербург (Travelpayouts, кэш Авиасейлса).

Запуск: python tp_watch.py            обычный проход
        python tp_watch.py --selftest офлайн-проверка на выдуманных ценах
Настройки в секции return_trip config.json.

Варианты, которые перебираются (цена на одного, все в рублях):
  прямой билет из стартового аэропорта (Ереван, Гюмри, Тбилиси, Кутаиси, Владикавказ) в Петербург или Москву;
  два отдельных билета через город-хаб (Сочи, Минводы, Казань...), пересадка своими силами;
  из Москвы дальше поезд (цена из календаря Туту на день прилета, запасная оценка, если календарь не ответил).
К цене билетов добавляются: дорога до стартового аэропорта, еда в пути, лишняя еда и ночь при долгой пересадке,
дорога от аэропорта домой. Итог сравнивается по полной стоимости.

Сообщение приходит, только когда лучший итог стал ниже уже присланного на alert_drop (и один раз при запуске).
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import msg
import watch

ROOT = Path(__file__).parent
CARRIERS = {"PC": "Pegasus", "DP": "Победа", "3F": "FlyOne Armenia", "S7": "S7", "5N": "Smartavia",
            "A4": "Азимут", "SU": "Аэрофлот", "FV": "Россия", "U6": "Уральские авиалинии", "TK": "Turkish Airlines",
            "J2": "AZAL", "N4": "Nordwind", "UT": "ЮТэйр", "RM": "Aircompany Armenia", "XC": "Corendon",
            "ZF": "Azur Air", "WZ": "Red Wings", "6R": "Алроса", "HY": "Uzbekistan Airways", "KC": "Air Astana",
            "Y7": "NordStar", "EO": "Pegas Fly", "IO": "ИрАэро", "R3": "Якутия", "QH": "Air Kyrgyzstan",
            "GH": "Глобус", "7R": "РусЛайн", "YC": "Ямал", "SZ": "Somon Air", "A9": "Georgian Airways"}
API = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
NAMES = {"EVN": "Ереван", "LWN": "Гюмри", "TBS": "Тбилиси", "KUT": "Кутаиси", "OGZ": "Владикавказ", "LED": "Петербург",
         "MOW": "Москва", "AER": "Сочи", "MRV": "Минводы", "KZN": "Казань", "KUF": "Самара", "SVX": "Екатеринбург",
         "VOG": "Волгоград", "UFA": "Уфа", "OVB": "Новосибирск", "MCX": "Махачкала", "GRV": "Грозный", "NAL": "Нальчик"}


def name(code):
    return NAMES.get(code) or msg.city_of(code) or code


def fetch(token, origin, dest, months):
    import requests

    rows = []
    for m in months:
        r = requests.get(API, params={"origin": origin, "destination": dest, "departure_at": m, "one_way": "true",
                                      "unique": "false", "sorting": "price", "limit": 1000, "currency": "rub",
                                      "token": token}, timeout=60)
        j = r.json()
        if not j.get("success"):
            raise RuntimeError(f"HTTP {r.status_code}: {str(j.get('error'))[:100]}")
        rows += j.get("data") or []
    return rows


def parse(x):
    """Билет из ответа API: время вылета и прилета (с поясом), цена, перевозчик, ссылка."""
    try:
        dep = datetime.fromisoformat(x["departure_at"])
    except (KeyError, ValueError, TypeError):
        return None
    mins = x.get("duration") or x.get("duration_to") or 0
    arr = dep + timedelta(minutes=mins) if mins else None
    o, d = x.get("origin"), x.get("destination")
    link = ("https://www.aviasales.ru" + x["link"]) if x.get("link") else \
        f"https://www.aviasales.ru/search/{o}{dep:%d%m}{d}1"
    return {"origin": o, "dest": d, "dep": dep, "arr": arr, "mins": mins, "price": x["price"],
            "stops": x.get("transfers") or 0, "carrier": CARRIERS.get(x.get("airline"), x.get("airline")), "link": link}


def tickets(rows, rt, earliest=None, origin=None, dest=None, next_day=False):
    """next_day: для второго билета через хаб разрешен вылет и на день позже последнего дня окна."""
    out = []
    last = datetime.fromisoformat(rt["last_day"] + "T23:59:59+04:00") + timedelta(days=1 if next_day else 0)
    for x in rows:
        if not x.get("price") or (x.get("transfers") or 0) > rt["max_transfers"]:
            continue
        x = dict(x, origin=x.get("origin") or origin, destination=x.get("destination") or dest)
        t = parse(x)
        if not t or t["dep"] > last or (earliest and t["dep"] < earliest):
            continue
        out.append(t)
    return out


def train_price(rt, arr, rail_cal):
    """Поезд Москва -> Петербург после прилета: минимум календаря Туту на день прилета или следующий."""
    if arr is None:
        return rt["train_fallback"], False
    msk = arr.astimezone(timezone(timedelta(hours=3)))
    day = msk.date() if msk.hour < 21 else msk.date() + timedelta(days=1)
    prices = [rail_cal.get(d.isoformat()) for d in (day, day + timedelta(days=1))]
    prices = [p for p in prices if p]
    if not prices:
        return rt["train_fallback"], False
    return min(prices), True


def build(rt, legs, rail_cal):
    """legs: {(откуда, куда): [билеты]}. Возвращает все варианты с полной стоимостью, по возрастанию."""
    opts = []

    def finish(t):
        if t["dest"] == "MOW":
            p, real = train_price(rt, t["arr"], rail_cal)
            cost = p + rt["train_food"] + rt["moscow_transfer"]
            return cost, (f"🚆 Поезд Москва - Петербург {'по календарю Туту' if real else '(оценка)'} {msg.money(p)}, "
                          f"еда и дорога до вокзала: +{msg.money(cost)}")
        return rt["home_cost"], f"🚇 Из Пулково домой: +{msg.money(rt['home_cost'])}"

    for start, st in rt["starts"].items():
        for dst in ("LED", "MOW"):
            for t in legs.get((start, dst), []):
                fin, fin_line = finish(t)
                extra = st["cost"] + rt["food"] + fin
                opts.append({"legs": [t], "total": t["price"] + extra, "lines": [
                    f"{st['label']}: +{msg.money(st['cost'])}", f"🍞 Еда в пути: +{msg.money(rt['food'])}", fin_line]})
    # два отдельных билета через хаб, только из Еревана
    st = rt["starts"]["EVN"]
    for hub in rt["hubs"]:
        for a in legs.get(("EVN", hub), []):
            if a["arr"] is None:
                continue
            for dst in ("LED", "MOW"):
                for b in legs.get((hub, dst), []):
                    wait = (b["dep"] - a["arr"]).total_seconds() / 3600
                    if not (rt["min_connection_h"] <= wait <= rt["max_connection_h"]):
                        continue
                    fin, fin_line = finish(b)
                    long_wait = rt["long_wait_cost"] if wait >= rt["long_wait_h"] else 0
                    food = rt["food"] + rt["transfer_food"] + long_wait
                    extra = st["cost"] + food + fin
                    lines = [f"{st['label']}: +{msg.money(st['cost'])}",
                             f"🍞 Еда в пути и пересадка {round(wait)} ч в городе {name(hub)}: +{msg.money(food)}",
                             fin_line, "⚠️ Два отдельных билета: багаж и стыковку никто не гарантирует"]
                    opts.append({"legs": [a, b], "total": a["price"] + b["price"] + extra, "lines": lines})
    opts.sort(key=lambda o: (o["total"], o["legs"][0]["dep"]))
    return opts


def leg_line(t):
    st = "прямой" if not t["stops"] else f"{t['stops']} перес."
    dur = f", в пути {t['mins'] // 60} ч {t['mins'] % 60:02d} мин" if t["mins"] else ""
    return (f"✈️ {name(t['origin'])} → {name(t['dest'])}, {msg.date_short(t['dep'].date().isoformat())} {t['dep']:%H:%M}: "
            f"<b>{msg.money(t['price'])}</b> · {msg.esc(t['carrier'] or '')}, {st}{dur} · {msg.a(t['link'], 'билет')}")


def opt_block(o, pax, title):
    lines = [f"<b>{title}: {msg.money(o['total'])}</b> на человека, {msg.money(o['total'] * pax)} на двоих"]
    lines += [leg_line(t) for t in o["legs"]]
    lines += [msg.esc(x) for x in o["lines"]]
    return "\n".join(lines)


def pick(opts, top):
    """Лучшие варианты без повторов маршрута."""
    seen, out = set(), []
    for o in opts:
        sig = tuple((t["origin"], t["dest"]) for t in o["legs"])
        if sig in seen:
            continue
        seen.add(sig)
        out.append(o)
        if len(out) == top:
            break
    return out


def report(rt, opts, prev, pax):
    best = opts[0]
    head = "🚨🔥🚨 <b>ДЕШЕВО ДОМОЙ - БЕРИ</b> 🚨🔥🚨" if best["total"] <= rt["target"] else "🛬 <b>ДОРОГА ДОМОЙ: НОВЫЙ МИНИМУМ</b>"
    blocks = [head]
    if prev:
        blocks.append(f"Было {msg.money(prev)}, стало {msg.money(best['total'])} на человека (-{msg.money(prev - best['total'])})")
    top = pick(opts, rt["top"])
    for i, o in enumerate(top):
        blocks.append(opt_block(o, pax, "Лучший" if i == 0 else f"Вариант {i + 1}"))
    first_day = rt["first_day"]
    early = next((o for o in opts if o["legs"][0]["dep"].date().isoformat() == first_day), None)
    if early and all(early is not o for o in top):
        blocks.append(opt_block(early, pax, f"Самый ранний ({msg.date_short(first_day)})"))
    blocks.append(f"<i>Полная цена: билеты, дорога до аэропорта и от него, еда. Вылет не раньше {msg.date_short(first_day)} 03:00. "
                  f"Данные из кэша Авиасейлса: проверьте цену перед покупкой</i>")
    return "\n\n".join(blocks)


def run(cfg, token, data_dir, fetcher=fetch, rail_fetch=None, notify=watch.tg_send):
    rt = cfg["return_trip"]
    pax = cfg.get("pax", 1)
    path = data_dir / "tp_state.json"
    state = watch.load_json(path, {})
    if state.get("v") != 3:
        state = {"v": 3}
    months = sorted({rt["first_day"][:7], rt["last_day"][:7]})
    pairs = [(s, d) for s in rt["starts"] for d in ("LED", "MOW")]
    pairs += [("EVN", h) for h in rt["hubs"]] + [(h, d) for h in rt["hubs"] for d in ("LED", "MOW")]
    legs, errors = {}, []
    for o, d in pairs:
        earliest = datetime.fromisoformat(rt["starts"][o]["earliest"]) if o in rt["starts"] else None
        try:
            legs[(o, d)] = tickets(fetcher(token, o, d, months), rt, earliest, o, d, next_day=o in rt["hubs"])
        except Exception as e:
            errors.append(f"{o}-{d}: {type(e).__name__} {str(e)[:100]}")
    for e in errors:
        print("Ошибка API:", e)
    rail_cal = {}
    try:
        if rail_fetch is None:
            import rail
            rail_fetch = rail.fetch
        d0 = datetime.fromisoformat(rt["first_day"])
        rail_cal = rail_fetch("2000000", "2004000", f"{d0:%d.%m.%Y}")
    except Exception as e:
        print("Календарь поездов не ответил:", type(e).__name__, str(e)[:80])
    opts = build(rt, legs, rail_cal)
    print(f"Запросов: {len(pairs)}, с ошибками: {len(errors)}, вариантов: {len(opts)}")
    for o in pick(opts, 8):
        print(o["total"], " + ".join(f"{t['origin']}-{t['dest']} {t['dep']:%d.%m %H:%M} {t['price']} {t['carrier']}" for t in o["legs"]))
    if errors and len(errors) == len(pairs):
        watch.save_json(path, state)
        sys.exit(1)
    if opts:
        best = opts[0]["total"]
        prev = state.get("best_sent")
        if prev is None or best < prev * (1 - rt["alert_drop"]):
            notify(report(rt, opts, prev, pax), html=True)
            state["best_sent"] = best
        state["best_now"] = best
        state["checked"] = watch.now().strftime("%Y-%m-%d %H:%M")
    watch.save_json(path, state)


def selftest():
    watch.DRY = True
    d = Path(tempfile.mkdtemp())
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    k = {"v": 1.0}

    def row(o, dst, when, price, mins, tr=0, al="PC"):
        return {"origin": o, "destination": dst, "departure_at": when, "price": int(price * k["v"]), "duration": mins,
                "transfers": tr, "airline": al}

    def fake(token, o, dst, months):
        data = {
            ("EVN", "LED"): [row("EVN", "LED", "2026-12-20T04:15:00+04:00", 18000, 600, 1),
                             row("EVN", "LED", "2026-12-20T01:00:00+04:00", 5000, 300)],  # до 03:00 после концерта: отсечь
            ("EVN", "MOW"): [row("EVN", "MOW", "2026-12-20T03:50:00+04:00", 11000, 210, 0, "UT")],
            ("EVN", "MRV"): [row("EVN", "MRV", "2026-12-20T08:00:00+04:00", 4000, 90, 0, "A4")],
            ("MRV", "LED"): [row("MRV", "LED", "2026-12-20T15:00:00+03:00", 5000, 210, 0, "DP")],
            ("TBS", "LED"): [row("TBS", "LED", "2026-12-21T12:00:00+04:00", 9000, 330, 1, "J2"),
                             row("TBS", "LED", "2026-12-24T12:00:00+04:00", 3000, 330, 1, "J2")],  # после окна: отсечь
        }
        return data.get((o, dst), [])

    rail_fake = lambda a, b, day: {"2026-12-20": 1500, "2026-12-21": 2500}
    print("== проход 1: стартовое сообщение, лучший через Минводы, билет за 5 000 в 01:00 отсечен ==")
    run(cfg, "x", d, fake, rail_fake)
    print("== проход 2: цены те же, тишина ==")
    run(cfg, "x", d, fake, rail_fake)
    print("== проход 3: все на 10% дешевле, сообщение о новом минимуме ==")
    k["v"] = 0.9
    run(cfg, "x", d, fake, rail_fake)


def main():
    if "--selftest" in sys.argv:
        return selftest()
    token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not token:
        print("TRAVELPAYOUTS_TOKEN не задан")
        sys.exit(1)
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    run(cfg, token, watch.DATA)


if __name__ == "__main__":
    main()
