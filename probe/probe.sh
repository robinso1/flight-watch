#!/bin/bash
# Проверка доступности источников цен ЖД: Питер (2004000) - Москва (2000000), 20.12.2026
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
echo "IP: $(curl -s -m 10 https://ipinfo.io/country)"
probe() { # name, curl args...
  local name=$1; shift
  local out; out=$(curl -s -m 25 -A "$UA" -w '\nHTTP %{http_code} %{time_total}s' "$@" 2>&1)
  echo "== $name: $(echo "$out" | tail -1)"; echo "$out" | head -c 400 | tr '\n' ' '; echo
}
probe "tutu travelpayouts" "https://www.tutu.ru/poezda/api/travelpayouts/v2/?term=2004000&term2=2000000"
probe "pass.rzd.ru RID" "https://pass.rzd.ru/timetable/public/ru?layer_id=5827&dir=0&tfl=3&checkSeats=1&code0=2004000&code1=2000000&dt0=20.12.2026"
probe "ticket.rzd.ru apib2b" -X POST -H 'Content-Type: application/json' -H 'Origin: https://ticket.rzd.ru' "https://ticket.rzd.ru/apib2b/p/Railway/V1/Search/TrainPricing?service_provider=B2B_RZD" \
  -d '{"Origin":"2004000","Destination":"2000000","DepartureDate":"2026-12-20T00:00:00","TimeFrom":0,"TimeTo":24,"CarGrouping":"DontGroup","GetByLocalTime":true,"SpecialPlacesDemand":"StandardPlacesAndForDisabledPersons","CarIssuingType":"All","GetTrainsFromSchedule":true}'
probe "ticket.rzd.ru main" "https://ticket.rzd.ru/"
probe "tutu.ru main" "https://www.tutu.ru/poezda/"
echo "== tutu calendar_prices"
curl -s -m 25 -A "$UA" -X POST -H 'Content-Type: application/json' -H 'Origin: https://www.tutu.ru' -H 'Referer: https://www.tutu.ru/' \
  -w '\nHTTP %{http_code}\n' https://vid-api.tutu.ru/api/calendar_prices \
  -d '{"type":"train","filters":[],"trips":[{"date":"20.12.2026","from":"2004000","to":"2000000"}],"travelers":[{"age":null}]}' | head -c 300; echo
