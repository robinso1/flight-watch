"""Мониторинг дешёвых вариантов Питер/Москва - Ереван с пересадками (источник тот же, что у Авиасейлса).
Хранит минимум на каждую дату в data/tp_state.json, шлёт в Telegram при падении цены на 10% и больше."""
import json, os, sys
import requests
import watch
from msg import esc, money

DROP = 0.10
WATCH = {"LED-EVN": ("LED", "EVN", ["2026-12-14", "2026-12-15", "2026-12-16", "2026-12-17", "2026-12-18"]),
         "EVN-LED": ("EVN", "LED", ["2026-12-20", "2026-12-21", "2026-12-22", "2026-12-23"]),
         "MOW-EVN": ("MOW", "EVN", ["2026-12-14", "2026-12-15", "2026-12-16", "2026-12-17", "2026-12-18"]),
         "EVN-MOW": ("EVN", "MOW", ["2026-12-20", "2026-12-21", "2026-12-22", "2026-12-23"])}
NAMES = {"LED": "Питер", "EVN": "Ереван", "MOW": "Москва"}
tok = os.environ["TRAVELPAYOUTS_TOKEN"]
path = watch.DATA / "tp_state.json"
state = watch.load_json(path, {})
first = not state
lines, alerts = [], []
for key, (o, d, dates) in WATCH.items():
    r = requests.get("https://api.travelpayouts.com/aviasales/v3/prices_for_dates", params={
        "origin": o, "destination": d, "departure_at": "2026-12", "one_way": "true", "unique": "false",
        "sorting": "price", "limit": 1000, "currency": "rub", "token": tok}, timeout=60).json()
    best = {}
    for x in r.get("data", []):
        day = x.get("departure_at", "")[:10]
        if day in dates and (day not in best or x["price"] < best[day]["price"]):
            best[day] = x
    for day in dates:
        x = best.get(day)
        if not x:
            continue
        k = f"{key}|{day}"
        old = state.get(k, {}).get("price")
        stops = "прямой" if x.get("transfers") == 0 else f"{x.get('transfers')} перес."
        line = f"{NAMES[o]}-{NAMES[d]} {day[8:]}.12: <b>{money(x['price'])}</b> ({esc(x.get('airline',''))}, {stops}, вылет {x['departure_at'][11:16]})"
        lines.append(line)
        if old and x["price"] <= old * (1 - DROP):
            alerts.append(f"{line}\nбыло {money(old)}, упало на {round((1 - x['price'] / old) * 100)}%")
        state[k] = {"price": x["price"], "airline": x.get("airline"), "transfers": x.get("transfers"), "ts": x.get("departure_at")}
watch.save_json(path, state)
if alerts:
    watch.tg_send("📉 <b>ЦЕНА УПАЛА</b>\n\n" + "\n\n".join(alerts), html=True)
elif first:
    watch.tg_send("✈️ <b>Мониторинг связок запущен</b>\nТекущие минимумы (с пересадками, цена на человека):\n\n" + "\n".join(lines), html=True)
print("\n".join(lines))
