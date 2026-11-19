#!/usr/bin/env python3
"""Дорога домой после концерта: самый дешевый путь Ереван -> Петербург (Travelpayouts, кэш Авиасейлса).

Запуск: python tp_watch.py            обычный проход
        python tp_watch.py --selftest офлайн-проверка на выдуманных ценах
Настройки в секции return_trip config.json.

Варианты, которые перебираются (цена на одного, все в рублях):
  прямой билет из стартового аэропорта (Ереван, Гюмри, Тбилиси, Кутаиси, Владикавказ) в Петербург или Москву;
  билет с пересадками (до max_transfers) одним заказом;
  два отдельных билета через город-хаб (Москва, Сочи, Минводы, Минск, Стамбул...) из любого старта, пересадка своими силами;
  из Москвы дальше поезд (цена из календаря Туту на день прилета, запасная оценка, если календарь не ответил).
К цене билетов добавляются: дорога до стартового аэропорта, еда в пути, лишняя еда и ночь при долгой пересадке,
дорога от аэропорта домой. Итог сравнивается по полной стоимости.

Каждый вариант получает класс: комфортно, терпимо, тяжело (правила в return_trip.comfort).
Минимум ведется отдельно по трем классам: самый дешевый, лучший терпимый, лучший комфортный.
Сообщение приходит, когда любой из них стал ниже уже присланного на alert_drop (и один раз при запуске).
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
         "VOG": "Волгоград", "UFA": "Уфа", "OVB": "Новосибирск", "MCX": "Махачкала", "GRV": "Грозный", "NAL": "Нальчик",
         "MSQ": "Минск", "IST": "Стамбул", "SAW": "Стамбул (Сабиха)", "GYD": "Баку", "TAS": "Ташкент", "ALA": "Алматы"}


CUR_NAME = {"RUB": "₽", "AMD": "драм", "GEL": "лари", "USD": "$"}


def get_rates():
    """Курс рубля к драму, лари и доллару: ЦБ РФ, запасной источник open.er-api.com."""
    import re
    import urllib.request
    try:
        raw = urllib.request.urlopen("https://www.cbr.ru/scripts/XML_daily.asp", timeout=20).read().decode("cp1251")
        out = {}
        for code in ("AMD", "GEL", "USD"):
            m = re.search(rf"<CharCode>{code}</CharCode><Nominal>(\d+)</Nominal><Name>[^<]*</Name><Value>([\d,]+)", raw)
            out[code] = float(m.group(2).replace(",", ".")) / int(m.group(1))
        return out, "ЦБ РФ"
    except Exception as e:
        print("Курс ЦБ не получен:", type(e).__name__)
    d = json.loads(urllib.request.urlopen("https://open.er-api.com/v6/latest/RUB", timeout=20).read().decode())
    return {k: 1 / d["rates"][k] for k in ("AMD", "GEL", "USD")}, "open.er-api.com"


def price_rub(p, rates):
    """p: {"amount", "cur"} -> рубли и подпись вида '8 000 драм'."""
    cur = p.get("cur", "RUB")
    rub = p["amount"] if cur == "RUB" else p["amount"] * rates[cur]
    local = "" if cur == "RUB" else f" ({p['amount']:,} {CUR_NAME[cur]})".replace(",", " ")
    return int(round(rub)), local


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


def comfort(rt, o):
    """Весь путь в часах (наземка до аэропорта + перелеты + поезд из Москвы) и класс: 2 комфорт, 1 более-менее, 0 эконом."""
    c, legs = rt["comfort"], o["legs"]
    start = legs[0]["origin"]
    if legs[-1]["arr"] is None:
        return None, 0
    h = (legs[-1]["arr"] - legs[0]["dep"]).total_seconds() / 3600 + rt["starts"][start].get("ground_h", 0)
    if legs[-1]["dest"] == "MOW":
        h += rt["train_h"]
    stops = sum(t["stops"] for t in legs) + len(legs) - 1
    if (start in c["starts"] and len(legs) == 1 and stops <= c["max_stops"] and h <= c["max_h"]
            and legs[-1]["dest"] == "LED"):
        return h, 2
    if (start not in c["ok_exclude"] and stops <= c["ok_max_stops"] and h <= c["ok_max_h"]
            and o.get("wait", 0) <= c["ok_max_wait_h"]):
        return h, 1
    return h, 0


def build(rt, legs, rail_cal):
    """legs: {(откуда, куда): [билеты]}. Возвращает все варианты с полной стоимостью, по возрастанию."""
    opts = []
    pax = rt.get("pax", 1)
    mp = lambda x: msg.money(x * pax)

    def finish(t):
        if t["dest"] == "MOW":
            p, real = train_price(rt, t["arr"], rail_cal)
            cost = p + rt["train_food"] + rt["moscow_transfer"]
            return cost, (f"🚆 Поезд Москва - Петербург {'по календарю Туту' if real else '(оценка)'} {mp(p)}, "
                          f"{rt['moscow']['label']} {mp(rt['moscow_transfer'])}, еда {mp(rt['train_food'])}: +{mp(cost)}")
        return rt["home_cost"], f"🚇 {rt['home']['label']}: +{mp(rt['home_cost'])}"

    for start, st in rt["starts"].items():
        for dst in ("LED", "MOW"):
            for t in legs.get((start, dst), []):
                fin, fin_line = finish(t)
                extra = st["cost"] + rt["food"] + fin
                opts.append({"legs": [t], "total": (t["price"] + extra) * pax, "lines": [
                    f"{st['label']}: +{mp(st['cost'])}", f"🍞 Еда в пути: +{mp(rt['food'])}", fin_line]})
    # два отдельных билета через хаб, из любого стартового аэропорта
    for start, st in rt["starts"].items():
        for hub in rt["hubs"]:
            if hub == start:
                continue
            ht = rt.get("hub_transfer_cost", {}).get(hub, 0)
            for a in legs.get((start, hub), []):
                if a["arr"] is None:
                    continue
                for dst in ("LED",) if hub == "MOW" else ("LED", "MOW"):
                    for b in legs.get((hub, dst), []):
                        wait = (b["dep"] - a["arr"]).total_seconds() / 3600
                        if not (rt["min_connection_h"] <= wait <= rt["max_connection_h"]):
                            continue
                        fin, fin_line = finish(b)
                        long_wait = rt["long_wait_cost"] if wait >= rt["long_wait_h"] else 0
                        food = rt["food"] + rt["transfer_food"] + long_wait + ht
                        extra = st["cost"] + food + fin
                        lines = [f"{st['label']}: +{mp(st['cost'])}",
                                 f"🍞 Еда в пути и пересадка {round(wait)} ч в городе {name(hub)}"
                                 + (f" (с переездом между аэропортами {mp(ht)})" if ht else "") + f": +{mp(food)}",
                                 fin_line, "⚠️ Два отдельных билета: багаж и стыковку никто не гарантирует"]
                        opts.append({"legs": [a, b], "total": (a["price"] + b["price"] + extra) * pax, "lines": lines, "wait": wait})
    for o in opts:
        o["hours"], o["comfort"] = comfort(rt, o)
    opts.sort(key=lambda o: (o["total"], o["legs"][0]["dep"]))
    return opts


def leg_line(t, pax=1):
    st = "прямой" if not t["stops"] else f"{t['stops']} перес."
    dur = f", в пути {t['mins'] // 60} ч {t['mins'] % 60:02d} мин" if t["mins"] else ""
    return (f"✈️ {name(t['origin'])} → {name(t['dest'])}, {msg.date_short(t['dep'].date().isoformat())} {t['dep']:%H:%M}: "
            f"<b>{msg.money(t['price'] * pax)}</b>{' за двоих' if pax == 2 else ''} · {msg.esc(t['carrier'] or '')}, {st}{dur} · {msg.a(t['link'], 'билет')}")


COMFORT = {2: "😌 комфортно", 1: "🙂 терпимо", 0: "😬 тяжело, зато дешево"}
CATS = (("cheap", "Самый дешевый", 0), ("ok", "Более-менее комфортный", 1), ("comfy", "Комфортный", 2))


def best_by_cat(opts):
    return {k: next((o for o in opts if o["comfort"] >= lvl), None) for k, _, lvl in CATS}


def opt_block(o, pax, title):
    lines = [f"<b>{title}: {msg.money(o['total'])} на двоих</b> ({msg.money(o['total'] // pax)} на человека)" if pax == 2
             else f"<b>{title}: {msg.money(o['total'])}</b>"]
    if o.get("hours"):
        lines.append(f"⏱ Весь путь ~{round(o['hours'])} ч · {COMFORT[o['comfort']]}")
    lines += [leg_line(t, pax) for t in o["legs"]]
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


def report(rt, opts, prev, pax, why):
    """prev: прошлые присланные минимумы по классам; why: какие классы подешевели."""
    best = opts[0]
    head = "🚨🔥🚨 <b>ДЕШЕВО ДОМОЙ - БЕРИ</b> 🚨🔥🚨" if best["total"] <= rt["target"] * pax else "🛬 <b>ДОРОГА ДОМОЙ: НОВЫЙ МИНИМУМ</b>"
    if "cheap" not in why:
        head = "🛬 <b>ДОРОГА ДОМОЙ: " + ("КОМФОРТНЫЙ" if "comfy" in why else "ТЕРПИМЫЙ") + " ВАРИАНТ ПОДЕШЕВЕЛ</b>"
    blocks = [head]
    cats = best_by_cat(opts)
    shown, titles = [], []
    for k, title, _ in reversed(CATS):
        o = cats[k]
        if o is None:
            continue
        t = title + (" 🔻" if k in why else "")
        if k in why and prev.get(k):
            t += f" (было {msg.money(prev[k])})"
        same = next((i for i, x in enumerate(shown) if x is o), None)
        if same is None:
            shown.append(o)
            titles.append(t)
        else:
            titles[same] = title + ", он же " + titles[same][0].lower() + titles[same][1:]
    blocks += [opt_block(o, pax, t) for o, t in zip(shown, titles)]
    for o in pick(opts, rt["top"]):
        if any(o is x for x in shown):
            continue
        shown.append(o)
        blocks.append(opt_block(o, pax, "Еще вариант"))
        if len(shown) >= rt["top"] + 1:
            break
    first_day = rt["first_day"]
    early = next((o for o in opts if o["legs"][0]["dep"].date().isoformat() == first_day and o["comfort"] >= 1), None)
    if early and all(early is not o for o in shown):
        blocks.append(opt_block(early, pax, f"Самый ранний терпимый ({msg.date_short(first_day)})"))
    blocks.append(f"<i>Все суммы на {pax} чел. Полная цена: билеты, дорога до аэропорта и от него, еда. Вылет {msg.date_short(first_day)}-"
                  f"{msg.date_short(rt['last_day'])}, не раньше 03:00. Данные из кэша Авиасейлса: проверьте цену перед покупкой</i>")
    return "\n\n".join(blocks)


def run(cfg, token, data_dir, fetcher=fetch, rail_fetch=None, notify=watch.tg_send):
    rt = cfg["return_trip"]
    pax = cfg.get("pax", 1)
    path = data_dir / "tp_state.json"
    state = watch.load_json(path, {})
    if state.get("v") != 5:
        state = {"v": 5, "rates": state.get("rates")}
    months = sorted({rt["first_day"][:7], rt["last_day"][:7]})
    pairs = [(s, d) for s in rt["starts"] for d in ("LED", "MOW")]
    pairs += [(s, h) for s in rt["starts"] for h in rt["hubs"] if h != s]
    pairs += [(h, d) for h in rt["hubs"] for d in ("LED", "MOW") if h != d]
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
    # реальные цены наземки в местной валюте -> рубли по курсу дня (при сбое берем прошлый курс)
    try:
        rates, rates_src = get_rates()
        state["rates"] = rates
    except Exception as e:
        rates, rates_src = state.get("rates") or rt["rates_fallback"], "прошлый курс"
        print("Курс не получен, беру прошлый:", type(e).__name__)
    print("Курс:", rates_src, {k: round(v, 4) for k, v in rates.items()})
    rt = json.loads(json.dumps(rt))
    rt["pax"] = pax
    for st in rt["starts"].values():
        st["cost"], local = price_rub(st["price"], rates)
        st["label"] = st["label"] + (local[:-1] + " с человека)" if local else "")
    rt["home_cost"], _ = price_rub(rt["home"], rates)
    rt["moscow_transfer"], _ = price_rub(rt["moscow"], rates)
    rt["hub_transfer_cost"] = {h: price_rub(p, rates)[0] for h, p in rt.get("hub_transfer", {}).items()}
    opts = build(rt, legs, rail_cal)
    print(f"Запросов: {len(pairs)}, с ошибками: {len(errors)}, вариантов: {len(opts)}")
    for o in pick(opts, 8):
        print(o["total"], o["comfort"], round(o["hours"] or 0), " + ".join(f"{t['origin']}-{t['dest']} {t['dep']:%d.%m %H:%M} {t['price']} {t['carrier']}" for t in o["legs"]))
    if errors and len(errors) == len(pairs):
        watch.save_json(path, state)
        sys.exit(1)
    if opts:
        cats = best_by_cat(opts)
        now = {k: o["total"] for k, o in cats.items() if o}
        prev = state.get("best_sent") or {}
        why = [k for k, v in now.items() if k not in prev or v < prev[k] * (1 - rt["alert_drop"])]
        print("Минимумы по классам:", now, "присланные:", prev)
        if why:
            notify(report(rt, opts, prev, pax, why), html=True)
            for k in why:
                prev[k] = now[k]
            state["best_sent"] = prev
        state["best_now"] = now
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
            ("MOW", "LED"): [row("MOW", "LED", "2026-12-20T12:00:00+03:00", 2500, 90, 0, "DP")],
            ("EVN", "MRV"): [row("EVN", "MRV", "2026-12-20T08:00:00+04:00", 4000, 90, 0, "A4")],
            ("MRV", "LED"): [row("MRV", "LED", "2026-12-20T15:00:00+03:00", 5000, 210, 0, "DP")],
            ("TBS", "LED"): [row("TBS", "LED", "2026-12-21T12:00:00+04:00", 9000, 330, 1, "J2"),
                             row("TBS", "LED", "2026-12-24T12:00:00+04:00", 3000, 330, 1, "J2")],  # после окна: отсечь
        }
        return data.get((o, dst), [])

    rail_fake = lambda a, b, day: {"2026-12-20": 1500, "2026-12-21": 2500}
    globals()["get_rates"] = lambda: ({"AMD": 0.23, "GEL": 31.9, "USD": 82.8}, "тест")
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
