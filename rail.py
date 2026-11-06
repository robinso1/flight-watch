#!/usr/bin/env python3
"""Супер аномальные цены на поезда из Петербурга и обратно (календарь цен Туту.ру).

Запуск: python rail.py            обычный проход
        python rail.py --selftest офлайн-проверка на выдуманном календаре
Источник неофициальный: внутренний API Туту (РЖД из облака GitHub не отвечает).
Цена в календаре минимальная по всем вагонам за день, обычно сидячий или плацкарт.

Алерт только для очень редких цен, оба условия сразу:
  цена не выше max_ratio от медианы календаря маршрута;
  цена не выше p10_ratio от 10-го процентиля (обычный дешевый уровень маршрута).
Повтор по тому же поезду и дате только при цене ниже на 3% (общий антидубль watch.fresh_deal).
"""
import json
import statistics
import sys
import tempfile
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import msg
import watch

ROOT = Path(__file__).parent
API = "https://vid-api.tutu.ru/api/calendar_prices"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def fetch(src, dst, day):
    body = json.dumps({"type": "train", "filters": [], "trips": [{"date": day, "from": src, "to": dst}],
                       "travelers": [{"age": None}]}).encode()
    req = urllib.request.Request(API, data=body, method="POST", headers={
        "Content-Type": "application/json", "Origin": "https://www.tutu.ru", "Referer": "https://www.tutu.ru/", "User-Agent": UA})
    d = json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))
    if "code" in d and "message" in d:
        raise RuntimeError(f"{d['code']}: {str(d['message'])[:80]}")
    out = {}
    for k, v in d.items():
        if isinstance(v, (int, float)) and v > 0:
            dd, mm, yy = k.split(".")
            out[f"{yy}-{mm}-{dd}"] = int(v)
    return out


def percentile(xs, q):
    xs = sorted(xs)
    return xs[max(0, min(len(xs) - 1, int(round(q * (len(xs) - 1)))))]


def hits_for(cal, c, today):
    """Дни с супер ценой. Норма считается без самого дня."""
    days = {d: p for d, p in cal.items() if d > today}
    if len(days) < c["min_days"]:
        return []
    out = []
    for d, p in days.items():
        rest = [v for k, v in days.items() if k != d]
        med, p10 = statistics.median(rest), percentile(rest, 0.1)
        if p <= med * c["max_ratio"] and p <= p10 * c["p10_ratio"]:
            out.append((d, p, med))
    return out


def run(cfg, data_dir, fetcher=fetch, notify=watch.tg_send):
    c = cfg["rail"]
    today = watch.now().strftime("%Y-%m-%d")
    start = (watch.now().date() + timedelta(days=1)).strftime("%d.%m.%Y")
    home, home_name = c["home"]["code"], c["home"]["name"]
    state_path = data_dir / "rail_state.json"
    state = watch.load_json(state_path, {})
    found, errors, total = [], [], 0
    for city in c["cities"]:
        for src, dst, sn, dn in ((home, city["code"], home_name, city["name"]), (city["code"], home, city["name"], home_name)):
            total += 1
            try:
                cal = fetcher(src, dst, start)
            except Exception as e:
                errors.append(f"{sn}-{dn}: {type(e).__name__} {str(e)[:80]}")
                continue
            for d, p, med in hits_for(cal, c, today):
                key = f"RAIL|{src}-{dst}|{d}"
                if watch.fresh_deal(data_dir, "rail", key, p):
                    found.append((p / med, sn, dn, src, dst, d, p, med))
    for e in errors:
        print("Ошибка Туту:", e)
    # молчание не должно путаться со сбоем: после 24 проходов подряд без ответа сообщаем один раз
    if total and len(errors) == total:
        state["fail_streak"] = state.get("fail_streak", 0) + 1
        if state["fail_streak"] == 24:
            notify(msg.notice("⚠️", "Календарь поездов Туту не отвечает", "Уже сутки. Пример ошибки: " + errors[0]), html=True)
    else:
        state["fail_streak"] = 0
    found.sort()
    if found:
        lines = ["🚆🔥🚆 <b>СУПЕР ЦЕНА НА ПОЕЗД</b> 🚆🔥🚆"]
        for ratio, sn, dn, src, dst, d, p, med in found[: c["max_alerts"]]:
            link = f"https://www.tutu.ru/poezda/rasp_d.php?nnst1={src}&nnst2={dst}&date={d[8:10]}.{d[5:7]}.{d[:4]}"
            lines.append(f"{msg.esc(sn)} → {msg.esc(dn)}, {msg.date_short(d)}: <b>{msg.money(p)}</b>\n"
                         f"Обычно ~{msg.money(med)} · скидка {round((1 - ratio) * 100)}% · {msg.a(link, 'билет')}")
        lines.append("<i>Минимальная цена за день по всем вагонам, обычно сидячий или плацкарт. Проверьте тип вагона</i>")
        notify("\n\n".join(lines), html=True)
    watch.save_json(state_path, state)
    print(f"Маршрутов: {total}, с ошибками: {len(errors)}, супер цен: {len(found)}")


def selftest():
    watch.DRY = True
    d = Path(tempfile.mkdtemp())
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    base = watch.now().date()
    k = {"low": 1100}

    def fake(src, dst, day):
        cal = {}
        for i in range(2, 90):
            dd = (base + timedelta(days=i)).isoformat()
            cal[dd] = 4000 if i % 3 else 1100  # каждый третий день дешевое сидячее, это норма, не аномалия
        if src == "2004000" and dst == "2000000":
            cal[(base + timedelta(days=20)).isoformat()] = k["low"]
        return cal

    print("== проход 1: 1 100 при обычных 1 100 - не аномалия ==")
    run(cfg, d, fake)
    print("== проход 2: 500 - супер цена, одно сообщение ==")
    k["low"] = 500
    run(cfg, d, fake)
    print("== проход 3: те же 500 - повтора нет ==")
    run(cfg, d, fake)


def main():
    if "--selftest" in sys.argv:
        return selftest()
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    try:
        run(cfg, watch.DATA)
    except Exception as e:  # поезда не должны ронять остальной бот
        print("Сбой rail:", type(e).__name__, str(e)[:200])


if __name__ == "__main__":
    main()
