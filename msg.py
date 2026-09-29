"""Единый шаблон Telegram-сообщений (концепция Minimal Travel).

Все уведомления обоих ботов собираются здесь. Формат: Telegram HTML
(жирный, курсив, кликабельные ссылки). Пустые поля строк не создают:
если данных нет, сообщение просто становится короче.
"""
import html
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
NB = " "  # неразрывный пробел: цена не рвётся на две строки
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
SHORT = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]

# страна аэропорта для флажка, когда справочник городов недоступен
AIRPORT_CC = {"LED": "RU", "MOW": "RU", "KGD": "RU", "SVO": "RU", "DME": "RU", "VKO": "RU", "EVN": "AM"}
CITY_RU = {"LED": "Санкт-Петербург", "MOW": "Москва", "KGD": "Калининград", "EVN": "Ереван"}

STATUS = {
    "fire": "🔥 <b>АНОМАЛЬНО НИЗКАЯ ЦЕНА</b>",
    "low": "✈️ <b>НИЗКАЯ ЦЕНА</b>",
    "great": "🔥 <b>ОТЛИЧНАЯ ЦЕНА</b>",
    "drop": "📉 <b>ЦЕНА УПАЛА</b>",
}


def esc(s):
    return html.escape(str(s), quote=False)


def money(v):
    return f"{int(round(v)):,}".replace(",", NB) + NB + "₽"


def date_long(iso):
    y, m, d = iso[:10].split("-")
    return f"{int(d)} {MONTHS[int(m) - 1]} {y}"


def date_short(iso):
    _, m, d = iso[:10].split("-")
    return f"{int(d)} {SHORT[int(m) - 1]}"


def flag(cc):
    if not cc or len(cc) != 2 or not cc.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in cc.upper())


def cc_of(code, cities=None):
    if cities and cities.get(code, {}) and cities[code].get("c"):
        return cities[code]["c"]
    return AIRPORT_CC.get(code)


def city_of(code, cities=None):
    if cities and cities.get(code, {}) and cities[code].get("n"):
        return cities[code]["n"]
    return CITY_RU.get(code)


def a(url, label):
    return f'<a href="{html.escape(url, quote=True)}">{esc(label)}</a>'


def route_line(origin, dest, cities=None, arrow="→"):
    fo, fd = flag(cc_of(origin, cities)), flag(cc_of(dest, cities))
    left = f"{fo} {origin}".strip()
    right = f"{dest} {fd}".strip()
    return f"<b>{left} {arrow} {right}</b>"


def stops_text(stops):
    if stops is None:
        return None
    return "Прямой рейс" if stops == 0 else f"Пересадок: {stops}"


def links_block(pairs, title="🔎 <b>Проверить билет</b>"):
    """pairs: [(подпись, url)]. Пустые ссылки пропускаются."""
    rows = [a(u, t) for t, u in pairs if u]
    return [title] + rows if rows else []


def found_text(found_at):
    """'Обнаружено: сегодня, 00:39' из ISO-времени; при ошибке разбора возвращает None."""
    if not found_at:
        return None
    try:
        dt = datetime.fromisoformat(str(found_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(MSK)
        today = datetime.now(MSK).date()
        delta = (today - dt.date()).days
        day = "сегодня" if delta == 0 else "вчера" if delta == 1 else date_short(dt.date().isoformat())
        return f"Обнаружено: {day}, {dt:%H:%M}"
    except ValueError:
        return None


def _join(blocks):
    """Склеивает смысловые блоки одной пустой строкой, пропуская пустые."""
    return "\n\n".join("\n".join(b) for b in blocks if b)


def _footer(parts):
    parts = [p for p in parts if p]
    return [f"<i>{esc(' · '.join(parts))}</i>"] if parts else []


VISA = {"free": "🛂 Виза: не нужна", "visa": "🛂 Виза: возможно, нужна"}


def deal(kind, origin, dest, date_iso, price, *, norm=None, prev=None, stops=None, dep_time=None,
         carrier=None, visa=None, links=(), found_at=None, cities=None, priority=False, cache_note=False):
    """Один билет в одну сторону: аномалия, низкая цена, падение цены."""
    head = [STATUS[kind], ""]
    names = [city_of(origin, cities), city_of(dest, cities)]
    route = [route_line(origin, dest, cities)]
    if all(names):
        route.append(f"<i>{esc(names[0])} → {esc(names[1])}</i>")
    if priority:
        route.append("<i>⭐ приоритетное направление</i>")

    main = [f"📅 {date_long(date_iso)}", f"💰 <b>{money(price)}</b>"]
    for line in (stops_text(stops) and f"✈️ {stops_text(stops)}", dep_time and f"🕐 Вылет: {esc(dep_time)}",
                 carrier and f"🛫 Перевозчик: {esc(carrier)}", VISA.get(visa)):
        if line:
            main.append(line)

    cmp_block = []
    if norm and norm > price:
        pct = round(price / norm * 100)
        cmp_block = [f"Обычная цена ~{money(norm)}", f"Экономия ~{money(norm - price)} · {pct}% от нормы"]
    elif prev and prev > price:
        cmp_block = [f"Раньше от {money(prev)}", f"Экономия {money(prev - price)}"]

    note = None
    if cache_note:
        note = "Данные из кэша: проверьте цену и визу перед покупкой" if visa == "free" else "Данные из кэша: проверьте цену перед покупкой"
    footer = [f"<i>{esc(p)}</i>" for p in (found_text(found_at), note) if p]
    return _join([head[:1], route, main, cmp_block, links_block(links), footer])


def roundtrip(kind, out, back, total, *, links_out=(), links_back=(), cities=None, prev=None):
    """Туда-обратно. out/back: dict(origin, dest, date, price, carrier, dep_time, stops)."""
    same = out["date"] == back["date"]
    cc = lambda c: cc_of(c, cities)
    title = f"{flag(cc(out['origin']))} {out['origin']} ⇄ {out['dest']} {flag(cc(out['dest']))}".strip()
    dates = date_long(out["date"]) if same else f"{date_short(out['date'])} → {date_short(back['date'])} {back['date'][:4]}"
    main = [f"📅 {dates}", f"💰 <b>{money(total)}</b> туда-обратно"]
    if prev and prev > total:
        main.append(f"Раньше от {money(prev)}")

    def leg(label, x):
        name = city_of(x["dest"], cities) or x["dest"]
        first = f"🛫 {esc(x['dep_time'])} → {esc(name)}" if x.get("dep_time") else f"🛫 → {esc(name)}"
        second = f"💰 {money(x['price'])}"
        third = " · ".join(p for p in (x.get("carrier") and f"✈️ {esc(x['carrier'])}", stops_text(x.get("stops"))) if p)
        lines = [f"<b>{label}</b>", first, second]
        if third:
            lines.append(third)
        if not same:
            lines.insert(1, f"📅 {date_short(x['date'])}")
        return lines

    both_direct = out.get("stops") == 0 and back.get("stops") == 0
    summary = ["──────────────", "🎟️ Прямой рейс в обе стороны"] if both_direct else []
    rows = []
    if links_out or links_back:
        rows = ["🔎 <b>Проверить билет</b>"]
        for label, pairs in (("Туда", links_out), ("Обратно", links_back)):
            ls = [a(u, t) for t, u in pairs if u]
            if ls:
                rows.append(f"{label}: " + " · ".join(ls))
    return _join([[STATUS[kind]], [f"<b>{title}</b>"], main, leg("Туда", out), leg("Обратно", back), summary, rows])


def digest(head, routes, pair=None, carriers=None, unpriced=None, cities=None, pair_links=None):
    """Сводка: у каждого направления лучший вариант первым, остальные ниже компактно.

    routes: [dict(origin, dest, options=[dict(date, price, dep_time, carrier, stops)])]
    """
    blocks = [[head]]
    for r in routes:
        opts = sorted(r["options"], key=lambda o: o["price"])
        if not opts:
            continue
        best, rest = opts[0], opts[1:]
        lines = [route_line(r["origin"], r["dest"], cities)]

        def one(o):
            bits = [date_short(o["date"]), f"<b>{money(o['price'])}</b>" if o is best else money(o["price"])]
            if o.get("dep_time"):
                bits.append(esc(o["dep_time"]))
            if o.get("carrier"):
                bits.append(esc(o["carrier"]))
            if o.get("stops") not in (None, 0):
                bits.append(f"пересадок {o['stops']}")
            return " · ".join(bits)

        lines.append("💰 " + one(best))
        lines += [one(o) for o in rest[:3]]
        if len(rest) > 3:
            lines.append(f"<i>и ещё {len(rest) - 3}</i>")
        blocks.append(lines)
    if pair:
        blocks.append(["💡 <b>Лучшая пара</b>",
                       f"{money(pair['total'])} · туда {date_short(pair['out_day'])}, обратно {date_short(pair['back_day'])}"])
    if unpriced:
        blocks.append(["<i>Рейсы без цены в Google (цена только на сайте перевозчика): " + esc("; ".join(unpriced)) + "</i>"])
    if carriers:
        blocks.append([f"<i>Перевозчики в выдаче: {esc(', '.join(carriers))}</i>"])
    return _join(blocks)


def notice(icon, title, text=None):
    lines = [f"{icon} <b>{esc(title)}</b>"]
    if text:
        lines.append(esc(text))
    return "\n".join(lines)


def strip_tags(s):
    """Текст без разметки: для консоли и запасной отправки, если Telegram отвергнет HTML."""
    return html.unescape(re.sub(r"</?[a-z][^>]*>", "", s))
