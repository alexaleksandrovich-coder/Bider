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

---

# ЖИВЫЕ ФАКТЫ (спайк 13–14.10.2026, кабинет «Улов», BYN) — токен Продвижение+Аналитика+Статистика

## Подтверждено живыми 2xx

- [x] `promotion/count` 200: группы `adverts[]{type, status, count, advert_list[]{advertId}}` (НЕ `chards`); type на группе (у Улова type=9 для всех), 20 кампаний (5 пауза + 15 завершены)
- [x] `/api/advert/v2/adverts` 200: ответ **обёрнут** `{"adverts": [...]}`; кампания: `id` (не advertId!), `settings.name`, `settings.payment_type`, `settings.placements`, `timestamps.updated`, `nm_settings[].bids_kopecks.search/recommendations`, `type` в элементе ОТСУТСТВУЕТ
- [x] Валюта BYN (currencyCode 933) во всех ответах; ставки в копейках (search 21500 = 215 BYN CPM; 21–25 копеек CPC)
- [x] `/api/advert/v1/config` 200: **cpcStep=1, cpmStep=20** (копейки), minDailyLimit/minTopUup — есть в ответе
- [x] `/api/advert/v2/budget`: тело запроса — **`{"advertIds": [...]}`** (не ids, иначе 400); ответ обёрнут `{"adverts": [{advertId, currency, total}]}`, cash/netting отсутствуют, **total в ЦЕЛЫХ единицах валюты** (33 = 33 BYN, сошлось с кабинетом) → в БД ×100
- [x] `/adv/v3/fullstats`: 200; за окно без данных — **literal null** (не []); `days[].apps[]{appType, nms[]}` (вложение apps!); appType — числа (64/32/1); **деньги дробные единицы валюты** (sum: 0.21 BYN) → в БД ×100; `boosterStats` есть не у всех кампаний
- [x] Фактический лимит fullstats по заголовку X-Ratelimit-Limit: **1 req/мин** (спека говорила 3) → LIMITS поправлен
- [x] Ретро-сбор: окно 31 день (06.10–14.10) собрало 237.09 BYN трат, 95 заказов по «Улову»

## Правки кода по этим фактам (коммиты 14.10.2026)

- parsers: разворот обёрток `{"adverts": [...]}`, живые имена полей, `types` из count, null-tolerance, `apps[]`, `_money_to_kopecks` (float→×100, int→as-is), budget ×100
- client: budgets → `advertIds`; constants: fullstats (1, 60)
- runner: count → ids+types обе формы; fullstats — всегда полное окно 31 день (WB уточняет задним числом, скользящее окно теряло дни)

## Открытые вопросы (сверить с кабинетом)

- [ ] Сумма `sum_price` за 06–14.10 = 16.52 BYN при 95 заказах — семантика «суммы заказов» рекламы неясна, сверить с отчётом кабинета (в кабинете за период: траты 175.26 BYN, ДРР 10.77% за свой период)
- [ ] Масштаб `/adv/v1/balance` (сейчас 0) — подтвердить на ненулевом балансе
- [ ] Человекочитаемые метки app_type (64/32/1) и type=9 — уточнить словари
