# Спайк-2 (фаза 2): формы записи pause/start/daily-limits + orders — факты

Дата: 2026-10-14. Спека: верифицированное зеркало `https://raw.githubusercontent.com/eslazarev/wildberries-sdk/main/clients/go/promotion/api/openapi.yaml` (снимок 14.10.2026, локально `.scratch/facts_raw_openapi_promotion.yaml`, 10995 строк; не коммитится). Строки указаны для ЭТОГО снимка — зеркало обновляется, номера могут дрейфовать. Для orders вторая спека того же зеркала: `clients/go/reports/api/openapi.yaml` (снимок 14.10.2026, `.scratch/facts_raw_openapi_reports.yaml`, 6220 строк) — зеркала «statistics»-клиента нет, `/api/v1/supplier/orders` живёт в `reports` (тот же хост statistics-api, servers: prod `https://statistics-api.wildberries.ru` стр.256, sandbox стр.259).
Скрипты: `scripts/spike2_orders.py` (локальные токены из env), `scripts/spike2_prod.py` (в контейнере, токен из БД). Сырой вывод локального прогона: `.scratch/facts_raw2_orders.md` (не коммитится). Write-вызовы к бою НЕ делались (только чтение); write-формы ниже — из официальной спеки.

## Формы записи (из спеки, файл/строка)

Все методы: security `HeaderApiKey` = заголовок `Authorization: <токен>` БЕЗ Bearer (как фаза-1).

### GET /adv/v0/pause — promotion/api/openapi.yaml стр.1154 (operationId getV0Pause стр.1170)

- [x] Форма: **GET**, query-параметр `id` (required, **integer**, example 1234) — единственный параметр, тела НЕТ
- [x] Семантика: ставит на паузу кампанию в статусе `9` (активна); `x-readonly-method: false` (стр.1257)
- [x] Лимиты (таблица в description, стр.~1160): Персональный 5/с (интервал 200мс, всплеск 5); Сервисный 5/с; Базовый с секретом 5/с; **Базовый 5/ч (интервал 12 мин, всплеск 1)**
- [x] Ответы: `200` — БЕЗ тела (описание «Успешно»); `400` IncorrectId / AdvertNotFound → Model400Response `{"error": "AdvertChangeStatus: Not Found: advert not found"}` (примеры стр.5659–5668); `401`/`403` problem+json (формат как в фазе-1); **`422` text/plain `"Статус кампании не изменен"`** (пример StatusNoChangeAdv стр.5681) — ожидаемый исход повторной паузы, в контроллере трактовать как no-op, не как аварию; `429` problem+json

### GET /adv/v0/start — стр.1038 (operationId getV0Start стр.1055)

- [x] Форма: **GET**, query `id` (required, integer), тела НЕТ
- [x] Семантика: запускает кампании в статусах `4` (готова к запуску) и `11` (пауза); спека прямо велит перед запуском проверить бюджет кампании (иначе LowBudget)
- [x] Лимиты: те же, что pause (5/с; базовый 5/ч)
- [x] Ответы: как pause + `400 LowBudget` → `{"error": "AdvertChangeStatus: Low Budget: not enough budget"}` (стр.5663–5666); `422` «Статус кампании не изменен»
- [x] Для справки: `/adv/v0/stop` (стр.1267) завершает кампании 9/11 НЕОБРАТИМО — контроллером не используем

### GET/PUT /api/advert/v0/daily-limits — стр.5386

- [x] GET (getV0DailyLimits стр.5398, `x-readonly-method: true`): query `advertIds` (**required, string**, до 100 id через запятую, example `"12346567,987654321"`); тела НЕТ
- [x] GET 200 → `V0GetDailyLimitsResponse` (стр.6210): **обёртка** `{"adverts": [V0DailyLimitAdvert]}`
- [x] `V0DailyLimitAdvert` (стр.6237), ВСЕ 8 полей required: `advertId` int64; `enabled` bool (лимит включён); `dailyLimit` int64 — **разменные единицы, 0,01 валюты (копейки)**; `spentToday` int64 (копейки, потрачено сегодня); `currency` string ISO 4217; `carryOverEnabled` bool (перенос остатка на след. день); `valid` bool (хватает ли лимита под текущие ставки); `requiredLimit` int64 (копейки, рекомендуемый минимум при текущих ставках)
- [x] PUT (putV0DailyLimits стр.5491, `x-readonly-method: false`): **requestBody required**, `V0PutDailyLimitsRequest` (стр.6307) — **НЕ массив, а объект**: `advertIds` — массив int64, minItems 1 / maxItems 100 (required); `enabled` bool (required); `dailyLimit` int64, minimum 1000, «обязателен при enabled=true», реальный минимум = `minDailyLimit` из GET /api/advert/v1/config; `carryOverEnabled` bool («обязателен при enabled=true»). Лимит ОДИН на все id запроса (не per-advert массив {advertId, limit}); выключение = `{"advertIds":[...], "enabled": false}` без dailyLimit
- [x] PUT 200 → `V0PutDailyLimitsResponse` (стр.6386): `{"adverts": [{advertId int64, belowMinLimit bool, requiredLimit int64}]}` (все поля required)
- [x] PUT 400: `AdvertIdsCountInvalid` → `"advertIds must contain from 1 to 100 items"` (стр.5604–5608); `DailyLimitTooLow` → `"dailyLimit must be at least 100000"` (стр.5599; origin `camp-api-public-cache` — пример; живой минимум брать из config)
- [x] Лимиты запросов GET и PUT (таблица в description): **Персональный 5/мин (интервал 12с, всплеск 5); Сервисный 5/мин** — Базовых в таблице НЕТ
- [x] Токен: `x-token-types: [personal, service]` (GET стр.5477–5479, PUT стр.5586–5588) — **только Personal/Service, базовый не подходит** (у нас Personal — ок)
- [x] Связка с config: `minDailyLimit` в `V2GetConfigResponse` (стр.6645) — «минимальный размер дневного лимита вне зависимости от ставок», копейки (пример стр.3035–3049: UZS, 100000)

### GET /adv/v1/balance — стр.1620 (operationId getV1Balance стр.1641)

- [x] Форма: GET **без параметров** (валюты в query НЕТ; валюта приходит в ответе); `x-readonly-method: true`
- [x] Лимиты: Персональный/Сервисный/Базовый с секретом 1/с (интервал 1с, всплеск 5); Базовый 2/ч (интервал 30 мин)
- [x] 200 → `getV1Balance_200_response` (стр.9309): `balance` int — «счёт» в **базовых единицах валюты (ЦЕЛЫЕ, не копейки)**; `net` int — «баланс» (взаиморасчёт, пополнить нельзя); `currency` string ISO 4217; `bonus` int; `cashbacks[] {sum, percent, expiration_date}`
- [x] Масштаб подтверждает фазу-1: balance/net/bonus — целые единицы → в БД ×100
- [x] Токен: x-token-types НЕТ (подходит любой тип), категория «Продвижение»

### GET /api/v1/supplier/orders — reports/api/openapi.yaml стр.71 (operationId getV1SupplierOrders), хост statistics-api

- [x] Форма: GET; `dateFrom` (required, string, RFC3339: дата или дата-время, секунды или миллисекунды, часовой пояс Москва UTC+3); `flag` (optional, integer, default 0). `x-readonly-method: true`, `x-category: statistics`
- [x] `flag=0` → строки с `lastChangeDate >= dateFrom`, **0..~80000 строк**; пагинация: следующий запрос с `dateFrom = lastChangeDate` последней строки предыдущего; `[]` — всё выгружено
- [x] Данные хранятся **90 дней** с момента оформления; обновляются **раз в 30 минут**; 1 строка = 1 заказ; заказы без подтверждённой оплаты (рассрочка/отложенный платёж) могут отсутствовать
- [x] Лимиты: **Персональный 1/мин (интервал 1 мин, всплеск 10)**; Сервисный 1/мин (всплеск 10); Базовый с секретом 1/мин; Базовый 1/3ч
- [x] 200 → массив `OrdersItem` (стр.4095). Поля и типы из schema: `date` string (дата заказа, без offset = МСК), `lastChangeDate` string (инкремент-курсор), `warehouseName`/`warehouseType` (enum «Склад WB»/«Склад продавца»), `countryName`, `oblastOkrugName`, `regionName`, `supplierArticle`, `nmId` integer, `barcode`, `category`, `subject`, `brand`, `techSize`, `incomeID` integer, `isSupply` bool, `isRealization` bool, `totalPrice` **number** («цена без скидок», единицы валюты, НЕ копейки), `discountPercent` integer (скидка продавца %), `spp` **number** (скидка WB %), `finishedPrice` **number** («цена с учётом всех скидок, кроме суммы по WB Кошельку»), `priceWithDisc` **number**, `isCancel` bool, `cancelDate` string (**«0001-01-01T00:00:00» у неотменённых**), `sticker` string, `gNumber` string (ID корзины: заказы одной транзакции), `srid` string (уникальный ID заказа; = `rid` в API Маркетплейс)
- [x] **`storno` в OrdersItem НЕТ** — отмена определяется только `isCancel`/`cancelDate` (в плане упоминался storno — в актуальной спеке его нет)
- [x] Прочие коды: `402` payment required (нет подписи WB solution); `400` DateFromFieldRequired/DateFromValueNotValidated; `403` как фаза-1 (в примере origin `ag-marketplace`, живой факт ниже — `ag-statistics`)

## Заказы (живые пробы) — 14.10.2026, 17:58 UTC, локально, scripts/spike2_orders.py (окно 2 сут, flag=0, dateFrom RFC3339 +03:00)

- [x] token1 AVTOPOSTER2 (len 413, sha256[:12] 376f56231b8a): **403** `{"detail": "scope is not allowed for this resource", "origin": "ag-statistics"}`, 732ms
- [x] token2 AVTOPOSTER (len 413, sha256[:12] 51ec94c7255f): **403** то же, origin `ag-statistics`, 700ms
- [x] token3 markirovka WB_LIVE_KEY (len 394, sha256[:12] 7c241c947974): **403** то же, origin `ag-statistics`, 734ms
- [x] Это три РАЗНЫХ токена (хеши), и у всех НЕТ категории «Статистика» — тот же паттерн, что в фазе-1 для advert-api (origin меняется на ag-statistics)
- [x] `X-Ratelimit-*` на 403 не возвращаются (remaining/limit/retry = None) — подтвердилось и здесь
- [ ] Формат строки заказа live (реальные типы int/float полей денег, сырые значения date/lastChangeDate, формат srid, отменённые строки) — **ЗАБЛОКИРОВАНО**: нет локального токена с «Статистикой»; подтвердится прогоном `spike2_prod.py` на сервере (токен «Улова» имеет Статистику — facts.md)

## daily-limits (живой)

- [ ] GET /api/advert/v0/daily-limits на бою — не выполнялся локально (из трёх локальных токенов ни у одного нет «Продвижения»); готов `scripts/spike2_prod.py` (py_compile ok, dry-run ok: импорты app.crypto/app.db/app.models проходят, падает только на коннекте к БД, которой локально нет). **Нужен деплой и запуск `docker exec bider-app-1 python scripts/spike2_prod.py`** — снимет: существование/форму ответа daily-limits на бою, X-Ratelimit заголовки, формат строки orders на 200. advertIds берутся из нашей таблицы campaigns (до 100, сначала активные 9/11)
- [ ] PUT daily-limits на бою — СПЕЦИАЛЬНО НЕ ПРОБУЕМСЯ (write; форма взята из спеки, выше)

## Выводы для кода фазы 2

- [x] `pause(advert_id)`/`start(advert_id)`: GET `/adv/v0/pause|start?id=<int>`; pause — только из статуса 9, start — из 4/11; 200 без тела; 422 text/plain «Статус кампании не изменен» = no-op; 400 LowBudget у start → проверить budget до старта. Лимиты 5/с — с запасом хватает циклу 60с
- [x] `daily_limits_get(ids)`/`daily_limits_put(ids, enabled, limit_kopecks=None, carry_over=None)`: GET query `advertIds` (csv ≤100); PUT — объект `{"advertIds": [...], "enabled": bool[, "dailyLimit": int, "carryOverEnabled": bool]}`; лимит 5/мин (интервал 12с в лимитере); деньги — копейки (совместимо с `daily_limit_kopecks`/`price_kopecks` схемы); ответ GET несёт `spentToday` — можно контролировать CPM-перерасход нативно
- [x] orders: деньги в ЕДИНИЦАХ валюты (number, бывают дробные) → в БД ×100 (та же `_money_to_kopecks`, что fullstats); `totalPrice` = цена БЕЗ скидок; для выручки брать `finishedPrice`; отмена = `isCancel` (+`cancelDate`-заглушка «0001-01-01T00:00:00»); курсор инкремента = `lastChangeDate`; пагинация по ~80000 строк обязательна в collector; окно истории 90 дней
- [x] ОБНОВЛЕНИЕ РАЗ В 30 МИН (спека): цикл 60с безопасен (не чаще лимита 1/мин), но новые заказы появляются с лагом до 30 мин — для тепловой карты часов приемлемо, для realtime нет
- [x] `balance`: целые единицы валюты → ×100; 1/с

## Открытые вопросы

- [ ] Live-формат строки orders (типы полей денег int/float, таймзонные суффиксы в date/lastChangeDate, реальные srid) — серверный прогон spike2_prod.py
- [ ] Фактический лимит orders по X-Ratelimit-Limit на 200 (спека: 1/мин, всплеск 10) — как fullstats, может отличаться
- [ ] daily-limits на бою: существует ли метод на нашем аккаунте (BYN), форма/заголовки — серверный прогон
- [ ] Требуется ли для pause/start/daily-limits токен «Чтение и запись» (у «Улова» уже чтение+запись; спека не различает уровни — x-token-types задаёт только тип токена)
- [ ] Смысл `valid`/`requiredLimit` при установке лимита ниже рекомендуемого (belowMinLimit) — допустим ли в live, не блокирует ли WB кампанию
- [ ] 422 «Статус кампании не изменен»: подтверждить на бою, что повторные pause/start дают именно 422 (а не 200) — обработать как no-op
