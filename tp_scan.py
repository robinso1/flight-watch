import os, json, requests
tok = os.environ["TRAVELPAYOUTS_TOKEN"]
out = {}
import traceback
for o, d in (("LED","EVN"),("EVN","LED"),("MOW","EVN"),("EVN","MOW")):
    try:
        r = requests.get("https://api.travelpayouts.com/aviasales/v3/prices_for_dates", params={
        "origin": o, "destination": d, "departure_at": "2026-12", "one_way": "true", "unique": "false",
        "sorting": "price", "limit": 1000, "currency": "rub", "token": tok}, timeout=60)
    except Exception as e:
        out[f'{o}-{d}'] = 'ERR ' + str(e)[:150]; print(out[f'{o}-{d}']); continue
    try:
        j = r.json()
    except Exception as e:
        out[f'{o}-{d}'] = f'ERR http {r.status_code} {r.text[:150]}'; print(out[f'{o}-{d}']); continue
    out[f"{o}-{d}"] = [x for x in j.get("data", []) if "2026-12-14" <= x.get("departure_at","")[:10] <= "2026-12-23"] if j.get("success") else j
    print(o, d, r.status_code, len(out[f"{o}-{d}"]) if isinstance(out[f"{o}-{d}"], list) else out[f"{o}-{d}"], flush=True)
os.makedirs("data", exist_ok=True)
json.dump(out, open("data/tp.json", "w"), ensure_ascii=False, indent=1)
