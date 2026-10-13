# WB advert-api: факты живого прогона (спайк)

Дата: 2026-10-13
Источник: scripts/spike_probe.py, сырой вывод .scratch/facts_raw.md (не коммитится)

## BY (кабинет: <имя>)

- [ ] Токен работает (count 200):
- [ ] Валюта / currencyCode (из /api/advert/v1/config):
- [ ] Шаги ставок cpcStep / cpmStep:
- [ ] minDailyLimit / minTopUp:
- [ ] Реальные типы РК (из count/adverts):
- [ ] payment_type РК (cpm/cpc):
- [ ] avg_position присутствует в fullstats.boosterStats:
- [ ] Формат ошибок 4xx (adverts ids=0):
- [ ] Заголовки X-Ratelimit (какие вернулись):
- [ ] bids/min: формат тела принят/отклонён (тело ошибки):
- [ ] recommendations: параметры приняты/отклонены (тело):

## RU (кабинет: <имя>)

- [ ] Токен работает (count 200):
- [ ] Валюта / currencyCode (из /api/advert/v1/config):
- [ ] Шаги ставок cpcStep / cpmStep:
- [ ] minDailyLimit / minTopUp:
- [ ] Реальные типы РК (из count/adverts):
- [ ] payment_type РК (cpm/cpc):
- [ ] avg_position присутствует в fullstats.boosterStats:
- [ ] Формат ошибок 4xx (adverts ids=0):
- [ ] Заголовки X-Ratelimit (какие вернулись):
- [ ] bids/min: формат тела принят/отклонён (тело ошибки):
- [ ] recommendations: параметры приняты/отклонены (тело):

## Выводы для constants.py

- HOST:
- LIMITS (fullstats/budget/bids_min/recommendations/adverts/count/balance):
- cpcStep / cpmStep:
- валюта и масштаб денег (копейки/рубли):
- minDailyLimit / minTopUp:
- прочие факты для клиента/парсеров:
