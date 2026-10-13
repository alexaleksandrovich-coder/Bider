# WB advert-api: факты живого прогона (спайк)

Дата прогона: 2026-10-13. Источник: `scripts/spike_probe.py`, сырые выводы `.scratch/facts_raw_tok1.md` (AVTOPOSTER2), `_tok2.md` (AVTOPOSTER), `_tok3.md` (markirovka WB_LIVE_KEY) — не коммитятся.
Токены: 3 шт. из локальных проектов; все Personal JWT (acc=3), срок до 04–07.04.2027, payload валиден. Это НЕ токены всех 4 кабинетов — это имевшиеся под рукой.

## Результат по токенам (все три идентично)

- [x] Токен работает (count 200) → НЕТ доступа к advert-api: **403 на всех 4 методах волны 1** (count, config, balance, adverts) на всех трёх токенах
- [x] Причина 403 подтверждена телом ошибки: `"detail": "scope is not allowed for this resource", "origin": "ag-advert"` = у токенов нет категории «Продвижение» (выпущены для Контента/Маркетплейса/маркировки)
- [ ] Валюта / currencyCode (из /api/advert/v1/config) — не подтверждено (нужен токен с «Продвижением»)
- [ ] Шаги ставок cpcStep / cpmStep — не подтверждено
- [ ] minDailyLimit / minTopUp — не подтверждено
- [ ] Реальные типы РК (из count/adverts) — не подтверждено
- [ ] payment_type РК (cpm/cpc) — не подтверждено
- [ ] avg_position присутствует в fullstats.boosterStats — не подтверждено
- [ ] bids/min: формат тела — не подтверждено (до POST не дошло)
- [ ] recommendations: параметры — не подтверждено (до GET не дошло)

## Факты, подтверждённые даже через 403

- [x] Хост `advert-api.wildberries.ru` живой, отдаёт JSON; латентность 700–1000 мс
- [x] Формат ошибок (совпадает со спекой): `{"status", "statusText", "title", "detail", "requestId", "origin", "timestamp"}`, origin=`ag-advert`
- [x] Заголовки `X-Ratelimit-*` на 403 НЕ возвращаются (remaining/limit/retry = None) — перепроверить на 2xx
- [x] Скрипт-пробник работает: py_compile ok, dry-run ok (skip без токенов, exit 0), исключения не роняют прогон, токены не печатаются

## Выводы для constants.py

- HOST = `https://advert-api.wildberries.ru` — подтверждён
- LIMITS — пока из спеки (снимок 26.09.2026), сверить на 2xx после токенов: count/adverts 5/с, budget 20/мин, bids_min 20/мин, recommendations 5/мин, fullstats 3/мин, balance 1/с, normquery 5/мин
- cpcStep / cpmStep, валюта и масштаб денег, minDailyLimit / minTopUp — БЛОКИРОВАНО: ждать токены с категорией «Продвижение»
- Прочие факты: обработка 403 «scope not allowed» в сборщике — писать в collection_log и НЕ ретраить (конфиг-ошибка, не транзиентная)

## Блокер

Нужны Personal-токены с категориями «Продвижение» + «Аналитика» + «Статистика» (уровень «Чтение и запись») из 4 кабинетов. После получения — повторить прогон (≤10 мин) и дозаполнить этот файл.
