"""Throwaway spike (read-only): WB statistics-api supplier orders probe.

Одноразовый пробник заказов: GET /api/v1/supplier/orders?dateFrom=<RFC3339, МСК>&flag=0
на statistics-api.wildberries.ru по 1-3 токенам из env; печатает markdown
со статусом, заголовками X-Ratelimit, телом (с обрезкой) и разбором формата
строки заказа (имена полей, масштаб денег, таймзона, srid, флаги отмены).
Каждое исключение ловится и фиксируется как факт — скрипт не падает.

Запуск:
    WB_ORDERS_TOKEN_1=... [WB_ORDERS_TOKEN_2=... WB_ORDERS_TOKEN_3=...] \
        python scripts/spike2_orders.py [days] > .scratch/facts2_raw_orders.md

days — глубина окна dateFrom (по умолчанию 2, максимум 90).
Токены берутся ТОЛЬКО из env и никогда не печатаются (Authorization не выводится).
Только stdlib (urllib) — зависимости не требуются.
Только чтение (GET). Никаких записей в WB.
SSRF-гигиена: https + белый список хостов + белый список путей, query через
urlencode, динамические параметры только int/константы, DNS резолвится и
проверяется на приватные/loopback-адреса, редиректы запрещены.
"""
import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 30
PAUSE = 2.0  # пауза между вызовами, сек

# Белые списки: хост -> разрешённые пути (без query; query — через urlencode).
ALLOWED_HOSTS = {
    "statistics-api.wildberries.ru": frozenset({"/api/v1/supplier/orders"}),
    "advert-api.wildberries.ru": frozenset({"/api/advert/v0/daily-limits"}),
}

MONEY_KEYS = ("totalPrice", "finishedPrice", "priceWithDisc", "spp", "discountPercent", "forPay")
CANCEL_KEYS = ("isCancel", "cancelDate", "storno", "isRealization")
DATE_KEYS = ("date", "lastChangeDate")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Любой редирект — ошибка, а не переход (запрещаем смену цели)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def validate_target(url: str, expected_host: str) -> None:
    """Разрешаем только https://<expected_host> из белого списка, публичные IP."""
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected_host
        or parsed.hostname not in ALLOWED_HOSTS
        or parsed.username
    ):
        raise ValueError(f"unexpected target: {parsed.scheme}://{parsed.hostname}")
    if parsed.path not in ALLOWED_HOSTS[parsed.hostname]:
        raise ValueError(f"path not allowed: {parsed.path!r}")
    for info in socket.getaddrinfo(parsed.hostname, 443, proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(f"resolved to non-public address: {ip}")


def msk_rfc3339_days_ago(days: int) -> str:
    """Момент `days`*24ч назад по Москве (UTC+3) в RFC3339: 2026-10-12T14:00:00+03:00."""
    fmt = "%Y-%m-%dT%H:%M:%S+03:00"
    return time.strftime(fmt, time.gmtime(time.time() + 3 * 3600 - days * 86400))


def safe_json(raw: bytes):
    """Безопасный парс тела: не json -> {"text": первые 1000 символов}."""
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:  # json.loads
        return {"text": text[:1000]}


def call(host: str, method: str, path: str, token: str, query=None):
    """Один пробник: запрос + markdown-блок в stdout. Не бросает исключений.

    host/path — только из белых списков; query — dict, сериализуется urlencode.
    Возвращает распарсенное тело (или None при исключении).
    """
    url = f"https://{host}{path}"
    if query:
        url = url + "?" + urllib.parse.urlencode(query)
    validate_target(url, host)

    t0 = time.monotonic()
    status = None
    resp_headers = {}
    raw = b""
    exc_repr = None
    try:
        req = urllib.request.Request(
            url, data=None, headers={"Authorization": token}, method=method  # БЕЗ Bearer
        )
        try:
            with _OPENER.open(req, timeout=TIMEOUT) as resp:
                status = resp.status
                resp_headers = resp.headers
                raw = resp.read()
        except urllib.error.HTTPError as e:
            # 4xx/5xx (и редиректы через _NoRedirect) — тоже факт, а не падение.
            status = e.code
            resp_headers = e.headers
            raw = e.read()
    except Exception as e:  # noqa: BLE001  # спайк: фиксируем всё
        exc_repr = repr(e)
    dt_ms = int((time.monotonic() - t0) * 1000)

    shown = path + ("?" + urllib.parse.urlencode(query) if query else "")
    print(f"### {method} {shown} -> {status if status is not None else 'EXC'} ({dt_ms}ms)", flush=True)
    if exc_repr is not None:
        print(f"exception: {exc_repr}", flush=True)
    print(
        "ratelimit: "
        f"remaining={resp_headers.get('X-Ratelimit-Remaining')} "
        f"limit={resp_headers.get('X-Ratelimit-Limit')} "
        f"retry={resp_headers.get('X-Ratelimit-Retry')}",
        flush=True,
    )
    payload = safe_json(raw)
    print("```json", flush=True)
    print(json.dumps(payload, ensure_ascii=False, indent=1)[:4000], flush=True)
    print("```", flush=True)
    time.sleep(PAUSE)
    return payload


def first_of(rows, key):
    """Первое не-None значение ключа в списке строк."""
    for row in rows:
        if isinstance(row, dict) and row.get(key) is not None:
            return row.get(key)
    return None


def analyze_orders(payload) -> None:
    """Разбор формата строки заказа: поля, деньги, таймзона, srid, отмены."""
    if not isinstance(payload, list):
        print("> тело не массив строк заказа — разбор пропущен", flush=True)
        return
    rows = [r for r in payload if isinstance(r, dict)]
    print(f"> строк заказа: {len(payload)} (dict: {len(rows)})", flush=True)
    if not rows:
        return
    fields = sorted({k for r in rows for k in r})
    print(f"> имена полей ({len(fields)}): {fields}", flush=True)

    for key in MONEY_KEYS:
        value = first_of(rows, key)
        if value is not None:
            print(f"> money {key}: python type={type(value).__name__}, примеры: "
                  f"{[r.get(key) for r in rows[:5]]}", flush=True)
    for key in DATE_KEYS:
        value = first_of(rows, key)
        if value is not None:
            print(f"> date {key}: примеры: {[r.get(key) for r in rows[:3]]}", flush=True)
    srid = first_of(rows, "srid")
    if srid is not None:
        print(f"> srid: type={type(srid).__name__}, len={len(str(srid))}, пример: {srid}", flush=True)
    for key in CANCEL_KEYS:
        if key in fields:
            values = [r.get(key) for r in rows[:5]]
            truthy = sum(1 for r in rows if r.get(key))
            print(f"> cancel {key}: примеры {values}, truthy в выборке: {truthy}", flush=True)
    cancelled = [r for r in rows if r.get("isCancel")]
    if cancelled:
        print("> пример отменённой строки:", flush=True)
        print("```json", flush=True)
        print(json.dumps(cancelled[0], ensure_ascii=False, indent=1)[:2000], flush=True)
        print("```", flush=True)


def run_token(env_name: str, name: str, days: int) -> None:
    token = os.environ.get(env_name)
    if not token:
        print(f"skip {env_name}", flush=True)
        return
    print(f"## {name}", flush=True)
    date_from = msk_rfc3339_days_ago(days)
    print(f"> dateFrom={date_from} (окно {days} сут, МСК), flag=0", flush=True)
    payload = call(
        "statistics-api.wildberries.ru",
        "GET",
        "/api/v1/supplier/orders",
        token,
        query={"dateFrom": date_from, "flag": 0},
    )
    try:
        analyze_orders(payload)
    except Exception as e:  # noqa: BLE001
        print(f"exception in analyze_orders: {e!r}", flush=True)
    print(flush=True)


def main() -> None:
    days = 2
    for arg in os.sys.argv[1:]:
        try:
            days = max(1, min(90, int(arg)))
        except ValueError:
            pass
    run_token("WB_ORDERS_TOKEN_1", "token1 (AVTOPOSTER2)", days)
    run_token("WB_ORDERS_TOKEN_2", "token2 (AVTOPOSTER)", days)
    run_token("WB_ORDERS_TOKEN_3", "token3 (markirovka WB_LIVE_KEY)", days)


if __name__ == "__main__":
    main()
