import os, json, requests
tok = os.environ["TRAVELPAYOUTS_TOKEN"]
out = {}
for o, d in (("LED","EVN"),("EVN","LED"),("MOW","EVN"),("EVN","MOW")):
    r = requests.get("https://api.travelpayouts.com/aviasales/v3/prices_for_dates", params={
        "origin": o, "destination": d, "departure_at": "2026-12", "one_way": "true", "unique": "false",
        "sorting": "price", "limit": 1000, "currency": "rub", "token": tok}, timeout=60)
    j = r.json()
    out[f"{o}-{d}"] = [x for x in j.get("data", []) if "2026-12-14" <= x["departure_at"][:10] <= "2026-12-23"] if j.get("success") else j
    print(o, d, r.status_code, len(out[f"{o}-{d}"]) if isinstance(out[f"{o}-{d}"], list) else out[f"{o}-{d}"], flush=True)
os.makedirs("data", exist_ok=True)
json.dump(out, open("data/tp.json", "w"), ensure_ascii=False, indent=1)
