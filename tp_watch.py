#!/usr/bin/env python3
"""Мониторинг связок Питер/Москва - Ереван, Кутаиси, Гюмри с пересадками (Travelpayouts, кэш Авиасейлса).

Запуск: python tp_watch.py            обычный проход
        python tp_watch.py --selftest офлайн-проверка на выдуманных ценах
Даты туда берутся из anomaly.dest_windows.EVN, обратно и пороги из секции tp_watch в config.json.
Пороги hot/fire сравниваются с ценой билета на одного, без надбавки на дорогу.

Алерты только реально выгодные:
  билет в бюджете (hot/fire), повторно только если подешевел еще на new_min_drop;
  лучшая пара туда-обратно с дорогой и едой не дороже pair_hot на человека.
"""
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import msg
import watch

ROOT = Path(__file__).parent
EVN = "EVN"
CARRIERS = {"PC": "Pegasus", "DP": "Победа", "3F": "FlyOne Armenia", "S7": "S7", "5N": "Smartavia",
            "A4": "Азимут", "SU": "Аэрофлот", "FV": "Россия", "U6": "Уральские авиалинии", "TK": "Turkish Airlines",
            "J2": "AZAL", "N4": "Nordwind", "UT": "ЮТэйр", "RM": "Aircompany Armenia", "XC": "Corendon",
            "ZF": "Azur Air", "WZ": "Red Wings", "6R": "Алроса", "HY": "Uzbekistan Airways", "KC": "Air Astana"}
API = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"


def day_range(a, b):
    d, out = date.fromisoformat(a), []
    while d <= date.fromisoformat(b):
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def legs(cfg):
    """[(ключ, откуда, куда, [даты], настройки маршрута)]: туда и обратно для каждого маршрута tp_watch.routes."""
    t = cfg["tp_watch"]
    w = cfg["anomaly"]["dest_windows"][EVN]
    outs, backs = day_range(*w), t["back_dates"]
    res = []
    for r in t["routes"]:
        a, b = r["from"], r["to"]
        res.append((f"{a}-{b}", a, b, outs, r))
        res.append((f"{b}-{a}", b, a, backs, r))
    return res


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


def best_by_day(rows, dates, max_tr):
    best = {}
    for x in rows:
        day = (x.get("departure_at") or "")[:10]
        if day in dates and x.get("price") and (x.get("transfers") or 0) <= max_tr:
            if day not in best or x["price"] < best[day]["price"]:
                best[day] = x
    return best


def hm(minutes):
    return f"{minutes // 60} ч {minutes % 60:02d} мин" if minutes else None


def migrate(state):
    if state.get("v") == 2:
        return state
    keys = {k: {"min": v["price"], "last": v["price"], "days": {}} for k, v in state.items() if isinstance(v, dict) and v.get("price")}
    return {"v": 2, "keys": keys, "meta": {}}


def process(cfg, data, state, today, data_dir=None):
    """data: {ключ_маршрута: [строки API]}. Меняет state, возвращает тексты алертов.
    Шлем только реально выгодное: билет в бюджете и пару туда-обратно в бюджете. Просто падения цены не шлем."""
    t = cfg["tp_watch"]
    pax = cfg.get("pax", 1)
    on_two = lambda v: f"{msg.money(v * pax)} " + ("на двоих" if pax == 2 else f"на {pax}")
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    alerts, outs, backs = [], [], []
    for key, o, d, dates, cc in legs(cfg):
        if key not in data:
            continue
        for day, x in sorted(best_by_day(data[key], dates, t["max_transfers"]).items()):
            p = x["price"]
            k = state["keys"].setdefault(f"{key}|{day}", {"min": None, "last": None, "days": {}})
            days = k.setdefault("days", {})
            prev_min = k["min"]
            link = ("https://www.aviasales.ru" + x["link"]) if x.get("link") else f"https://www.aviasales.ru/search/{o}{day[8:10]}{day[5:7]}{d}1"
            carrier = CARRIERS.get(x.get("airline"), x.get("airline"))
            eff = p + cc.get("extra", 0)
            leg_info = {"origin": o, "dest": d, "date": day, "price": p, "eff": eff, "carrier": carrier,
                        "dep_time": (x.get("departure_at") or "")[11:16], "stops": x.get("transfers"), "link": link}
            (outs if o == cc["from"] else backs).append(leg_info)

            kind = None
            lvl = 2 if p <= cc["fire"] else 1 if p <= cc["hot"] else 0
            if lvl and (lvl > k.get("lvl", 0) or not k.get("alerted") or p < k["alerted"] * (1 - t["new_min_drop"])):
                kind = "fire" if lvl == 2 else "low"
                k["alerted"], k["lvl"] = p, lvl
            k["min"] = p if prev_min is None else min(prev_min, p)
            k["last"] = p
            days[today] = min(days.get(today, p), p)
            for old in [x for x in days if x < yesterday]:
                del days[old]
            if kind and data_dir and not watch.fresh_deal(data_dir, "tp", watch.deal_key(o, d, day), p):
                kind = None
            if not kind:
                continue
            lines = []
            if hm(x.get("duration") or x.get("duration_to")):
                lines.append(f"⏱ В пути: {hm(x.get('duration') or x.get('duration_to'))}")
            if cc.get("extra"):
                lines.append(f"{cc.get('extra_label', 'Дорога и еда')}: +{msg.money(cc['extra'])}")
            lines.append(f"💳 Итого: {msg.money(eff)} на человека, {on_two(eff)}")
            lines.append(f"Порог: {msg.money(cc['hot'])} за билет")
            text = msg.deal(kind, o, d, day, p,
                            prev=prev_min if prev_min and prev_min > p else None,
                            stops=x.get("transfers"), dep_time=leg_info["dep_time"] or None,
                            carrier=carrier, cost_lines=lines, links=[("Авиасейлс", link)], cache_note=True)
            alerts.append((p, text))
    alerts.sort(key=lambda a: a[0])
    texts = [a[1] for a in alerts[: t["max_alerts"]]]

    # лучшая пара туда-обратно (любые маршруты, с наземкой): только если укладывается в бюджет пары
    pairs = sorted(((o_["eff"] + b_["eff"], o_, b_) for o_ in outs for b_ in backs), key=lambda x: x[0])
    if pairs:
        total, o_, b_ = pairs[0]
        meta = state.setdefault("meta", {})
        prev = meta.get("pair_alerted")
        if total <= t["pair_hot"] and (prev is None or total < prev * (1 - t["new_min_drop"])) and \
                (not data_dir or watch.fresh_deal(data_dir, "tp", "PAIR|" + watch.deal_key(o_["origin"], b_["dest"], o_["date"], b_["date"]), total)):
            meta["pair_alerted"] = total
            kind = "fire" if total <= t["pair_fire"] else "low"
            note = (f"💳 Итого {msg.money(total)} на человека, {on_two(total)}, с дорогой и едой\n"
                    f"Порог пары: {msg.money(t['pair_hot'])} на человека")
            texts.insert(0, msg.roundtrip(kind, o_, b_, total,
                                          links_out=[("Авиасейлс", o_["link"])], links_back=[("Авиасейлс", b_["link"])],
                                          prev=prev, note=note))
    return texts


def run(cfg, token, data_dir, fetcher=fetch, notify=watch.tg_send):
    path = data_dir / "tp_state.json"
    state = migrate(watch.load_json(path, {}))
    today = watch.now().strftime("%Y-%m-%d")
    data, errors = {}, []
    lg = legs(cfg)
    for key, o, d, dates, _ in lg:
        try:
            data[key] = fetcher(token, o, d, sorted({x[:7] for x in dates}))
        except Exception as e:
            errors.append(f"{key}: {type(e).__name__} {str(e)[:120]}")
    for e in errors:
        print("Ошибка API:", e)
    texts = process(cfg, data, state, today, data_dir)
    for text in texts:
        notify(text, html=True)
    watch.save_json(path, state)
    for key, _, _, dates, _ in lg:
        for day in dates:
            k = state["keys"].get(f"{key}|{day}")
            if k and k.get("last"):
                print(f"{key} {day}: {k['last']} (мин {k['min']})")
    print(f"Маршрутов: {len(lg)}, с ошибками: {len(errors)}, алертов: {len(texts)}")
    if errors and len(errors) == len(lg):
        sys.exit(1)


def selftest():
    watch.DRY = True
    d = Path(tempfile.mkdtemp())
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    prices = {"LED-EVN": 16000, "EVN-LED": 18000, "MOW-EVN": 12000, "EVN-MOW": 14000, "LED-KUT": 9000, "KUT-LED": 9000, "LED-LWN": 13000, "LWN-LED": 13000}
    k = {"v": 1.0}

    def fake(token, o, dest, months):
        key = f"{o}-{dest}"
        dates = next(x[3] for x in legs(cfg) if x[0] == key)
        rows = [{"departure_at": f"{day}T15:35:00+03:00", "price": int(prices[key] * k["v"]) + i, "transfers": 1,
                 "airline": "PC", "duration_to": 595, "link": f"/search/{o}{day[8:10]}{day[5:7]}{dest}1"}
                for i, day in enumerate(dates)]
        rows.append({"departure_at": f"{dates[0]}T10:00:00+03:00", "price": 1000, "transfers": 2, "airline": "XX"})
        return rows

    print("== проход 1: цены выше порогов, алертов нет (2 пересадки за 1 000 отсекаются) ==")
    run(cfg, "x", d, fake)
    print("== проход 2: -12%, Москва 10 560 ниже порога 11 500, Питер и обратно - новый минимум ==")
    k["v"] = 0.88
    run(cfg, "x", d, fake)
    print("== проход 3: те же цены, алертов нет ==")
    run(cfg, "x", d, fake)


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
