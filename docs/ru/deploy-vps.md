# Деплой Bider на VPS (Ubuntu + Docker + Caddy)

Путь от чистого VPS до работающего Bider за HTTPS-поддоменом. Паттерн — тот же, что у Repraiser: Docker Compose, секреты в `.env` на хосте, TLS заканчивает Caddy снаружи, схема БД применяется сама при старте контейнера. Домен `robotay.ru` на Cloudflare с wildcard-записью `*.robotay.ru → IP сервера` — отдельная DNS-правка для нового поддомена не нужна.

## 1. Код и секреты

```bash
git clone <URL-репозитория> bider && cd bider   # или git pull в существующем каталоге
cp .env.prod.example .env && chmod 600 .env
```

Заполните в `.env` два обязательных секрета:

```bash
openssl rand -hex 16   # → POSTGRES_PASSWORD
openssl rand -hex 12   # → BIDER_ADMIN_PASSWORD (логин/пароль администратора дашборда)
```

`BIDER_FERNET_KEY` (шифрование WB-токенов в БД):

```bash
docker compose run --rm --no-deps app python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

(если контейнер ещё не собран — любой локальный python с `cryptography`, см. комментарий в `.env.prod.example`).

## 2. Запуск

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose logs -f app   # дождитесь строки про Uvicorn running, затем Ctrl+C
```

Миграции применяются при старте контейнера (entrypoint ретраит `alembic upgrade head`, пока Postgres поднимается). Приложение слушает **только 127.0.0.1:8000** — наружу его выпускает reverse proxy.

В prod-оверлее включён `BIDER_RUN_COLLECTOR=true` — фоновые read-only циклы сбора стартуют вместе с приложением.

> **Если рядом живёт общий Caddy в docker (как на robotay.ru):** контейнеру приложения нужно попасть в сеть прокси. Создайте `docker-compose.override.yml` рядом с compose-файлами (в репо не коммитится — имя сети сервероспецифично):
>
> ```yaml
> services:
>   app:
>     networks:
>       default: {}
>       <сеть-caddy>:
>         aliases: [bider]
> networks:
>   <сеть-caddy>:
>     external: true
> ```
>
> и **всегда указывайте все три файла явно** — при явных `-f` compose не подхватывает override автоматически:
>
> ```bash
> docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.override.yml up -d --build
> ```
>
> Тогда Caddy-блок выглядит так (контейнер доступен по алиасу `bider`):
>
> ```
> bider.robotay.ru {
>     import service_tls
>     reverse_proxy bider:8000
> }
> ```

## 3. Поддомен и TLS (Caddy)

```bash
sudo tee -a /etc/caddy/Caddyfile <<'EOF'

bider.robotay.ru {
    reverse_proxy 127.0.0.1:8000
}
EOF
sudo systemctl reload caddy
```

Caddy сам получит сертификат Let's Encrypt для `bider.robotay.ru` (DNS-wildcard уже указывает на сервер). Аналогично nginx+certbot — на ваш вкус; главное, чтобы приложение не светилось напрямую.

## 4. Первый вход и подключение кабинетов

1. Откройте `https://bider.robotay.ru`, войдите `BIDER_ADMIN_USERNAME` / `BIDER_ADMIN_PASSWORD` из `.env`.
2. **Кабинеты** → добавьте каждый кабинет WB: имя, регистратура (BY/RU), Personal-токен с категориями **«Продвижение» + «Аналитика» + «Статистика»** (уровень «Чтение и запись»). Токен сохраняется зашифрованным (Fernet) и больше нигде не показывается.
3. Через ≤10 минут на обзоре появится свежесть данных; страница Health покажет статус всех циклов сбора.

## 5. Обновление без потери данных

```bash
cd bider && git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

База — в именованном томе `bider_pgdata`, пересоздание контейнера её не трогает. Миграции накатываются при старте (вперёд-only; откат схемы не поддерживается — откатывайте код на соседний каталог с копией тома).

## 6. Бэкап

```bash
# снапшот БД (консистентный, приложение можно не останавливать):
docker compose -f docker-compose.yml exec postgres pg_dump -U bider bider | gzip > backups/bider-$(date +%Y%m%d-%H%M).sql.gz
```

Добавьте в cron ежедневный запуск + копируйте архив с сервера (rsync на домашнюю машину). Без `BIDER_FERNET_KEY` расшифровать токены из восстановленной БД нельзя — бэкапьте `.env` отдельно и держите в секрете.

## Что где лежит

| Том / файл | Что там |
|---|---|
| volume `bider_pgdata` | вся БД: кабинеты (токены зашифрованы), РК, ставки, статистика, журналы |
| `.env` на хосте | все секреты (Postgres, Fernet, админ); в репо не попадает |
| `/etc/caddy/Caddyfile` | блок `bider.robotay.ru → 127.0.0.1:8000` |
