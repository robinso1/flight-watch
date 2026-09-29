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

ROOT = Path(__file__).parent
MSK = ZoneInfo("Europe/Moscow")
DATA = ROOT / "data"
DRY = False  # в selftest сообщения только печатаются


def now():
    return datetime.now(MSK)


def money(v):
    return f"{int(v):,}".replace(",", " ") + " руб."


def tg_send(text):
    print("--- TG ---\n" + text + "\n----------")
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if DRY or not token or not chat:
        if not DRY:
            print("TG_TOKEN или TG_CHAT не заданы, сообщение не отправлено")
        return False
    body = urllib.parse.urlencode(
        {"chat_id": chat, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=20
        ).read()
        return True
    except Exception as e:  # токен в тексте ошибки не печатаем
        print("Ошибка отправки в Telegram:", type(e).__name__)
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
    c = cfg.get("combo")
    if not c:
        return None
    best = None
    for k_out, o in cheapest.items():
        r_out, d_out = k_out.split("|")
        if r_out != c["out"]:
            continue
        for k_back, b in cheapest.items():
            r_back, d_back = k_back.split("|")
            if r_back != c["back"]:
                continue
            gap = (date.fromisoformat(d_back) - date.fromisoformat(d_out)).days
            if gap < c.get("min_stay_days", 0):
                continue
            total = o["price"] + b["price"]
            if best is None or total < best["total"]:
                best = {"total": total, "out_day": d_out, "back_day": d_back, "out": o, "back": b}
    return best


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
            alerts = []
            if k["min"] is None:
                k["min"] = best["price"]
            elif best["price"] < k["min"] * drop:
                alerts.append(f"Новый минимум по {r['name']}: было {money(k['min'])}, стало {money(best['price'])}")
            k["min"] = min(k["min"], best["price"])
            thr = r.get("threshold_rub")
            if thr and best["price"] <= thr and (k["alerted"] is None or best["price"] < k["alerted"] * drop):
                alerts.append(f"Ниже вашего порога {money(thr)} по {r['name']}")
                k["alerted"] = best["price"]
            if k["last"] != best["price"]:
                append_csv([stamp, r["name"], day, best["price"], best["airlines"], best["dep"], best["stops"]])
                k["last"] = best["price"]
            if alerts and not first_run:
                tg_send("\n".join(alerts) + "\n" + line(day, best) + "\n" + gf_link(cfg, r["origin"], r["dest"], day))

    # общий сбой источника
    if total_req and len(errors) == total_req:
        meta["fail_streak"] = meta.get("fail_streak", 0) + 1
        if meta["fail_streak"] == 3:
            tg_send("Google Flights не отвечает уже 3 проверки подряд. Пример ошибки: " + errors[0])
    else:
        if meta.get("fail_streak", 0) >= 3:
            tg_send("Google Flights снова отвечает.")
        meta["fail_streak"] = 0

    # источник отвечает, но рейсов нет
    if total_req and not errors and not cheapest:
        meta["empty_streak"] = meta.get("empty_streak", 0) + 1
        if meta["empty_streak"] == 6:
            tg_send("Три часа подряд Google Flights не показывает ни одного рейса на эти даты. Возможно, продажа еще не открыта или перевозчики в выдаче не отображаются. Проверьте вручную.")
    elif cheapest:
        meta["empty_streak"] = 0

    # лучшая пара туда-обратно
    combo = best_combo(cfg, cheapest)
    if combo:
        cm = meta.get("combo_min")
        c_thr = cfg["combo"].get("threshold_rub")
        msg = []
        if cm is not None and combo["total"] < cm * drop:
            msg.append(f"Новая лучшая пара туда-обратно: было {money(cm)}, стало {money(combo['total'])}")
        if c_thr and combo["total"] <= c_thr and (meta.get("combo_alerted") is None or combo["total"] < meta["combo_alerted"] * drop):
            msg.append(f"Пара туда-обратно ниже порога {money(c_thr)}")
            meta["combo_alerted"] = combo["total"]
        meta["combo_min"] = combo["total"] if cm is None else min(cm, combo["total"])
        if msg and not first_run:
            tg_send("\n".join(msg) + f"\nИтого {money(combo['total'])}\nТуда: " + line(combo["out_day"], combo["out"]) + "\nОбратно: " + line(combo["back_day"], combo["back"]))

    # первое сообщение и суточная сводка
    today = now().strftime("%Y-%m-%d")
    want_digest = first_run or (now().hour >= cfg.get("digest_hour_msk", 9) and meta.get("digest_date") != today)
    if want_digest and (cheapest or unpriced_seen):
        head = "Мониторинг запущен. Сейчас в выдаче:" if first_run else "Сводка за сутки:"
        parts = [head]
        for key in sorted(cheapest):
            rname, day = key.split("|")
            parts.append(f"{rname} " + line(day, cheapest[key]))
        if combo:
            parts.append(f"Лучшая пара: {money(combo['total'])} (туда {combo['out_day'][8:]}.{combo['out_day'][5:7]}, обратно {combo['back_day'][8:]}.{combo['back_day'][5:7]})")
        parts.append("Перевозчики в выдаче Google: " + (", ".join(sorted(airlines_seen)) or "нет"))
        if unpriced_seen:
            names = sorted({o["airlines"] + " " + o["dep"][-5:] for v in unpriced_seen.values() for o in v})
            parts.append("Рейсы без цены в Google (купить можно только на сайте перевозчика): " + "; ".join(names))
        tg_send("\n".join(parts))
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
    cfg["routes"][0]["threshold_rub"] = 9000
    cfg["combo"]["threshold_rub"] = 15000
    for r in cfg["routes"]:
        r["dates"] = [(now().date() + timedelta(days=d)).isoformat() for d in (60, 61)]
    globals()["fetch_offers"] = lambda cfg, o, d, day: FAKE.pop(0)(o, d, day)
    mk = lambda p, a="Россия": [{"price": p, "airlines": a, "dep": "15.12 18:55", "stops": 0}]
    FAKE[:] = [lambda o, d, day, p=p: mk(p) for p in (12000, 11500, 13000, 12500)]
    print("== проход 1: должно прийти только стартовое сообщение ==")
    run(cfg)
    FAKE[:] = [lambda o, d, day, p=p: mk(p, "FlyOne Armenia") for p in (8000, 11400, 9000, 9500)]
    print("== проход 2: минимум и порог по EVN-LED, пара ниже порога ==")
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
        ok = tg_send("Проверка связи: бот мониторинга билетов работает.")
        sys.exit(0 if ok else 1)
    run(json.loads((ROOT / "config.json").read_text(encoding="utf-8")))


if __name__ == "__main__":
    main()
