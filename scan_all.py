"""Разовый сбор всех вариантов (с пересадками) на даты поездки. Результат: data/scan.json"""
import json, time
import watch

cfg = {"currency": "RUB", "language": "ru", "max_stops": None}
jobs = []
for d in range(14, 19):
    jobs.append(("LED", "EVN", f"2026-12-{d}"))
for d in range(19, 24):
    jobs.append(("EVN", "LED", f"2026-12-{d}"))
for a in ("SVO", "DME", "VKO"):
    for d in range(14, 19):
        jobs.append((a, "EVN", f"2026-12-{d}"))
    for d in range(20, 24):
        jobs.append(("EVN", a, f"2026-12-{d}"))
res = {}
for j in jobs:
    k = "|".join(j)
    for _ in range(3):
        try:
            res[k] = watch.fetch_offers(cfg, *j)
            break
        except Exception as e:
            res[k] = "ERR " + str(e)[:100]
            time.sleep(4)
    time.sleep(2)
    print(k, len(res[k]) if isinstance(res[k], list) else res[k], flush=True)
watch.DATA.mkdir(exist_ok=True)
(watch.DATA / "scan.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
# run 1
