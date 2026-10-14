"""Throwaway spike (read-only): probe of the official WB advert-api.

Одноразовый пробник: гоняет read-методы advert-api по 1-2 токенам и печатает
markdown со статусом, заголовками X-Ratelimit и телом каждого вызова.
Каждое исключение ловится и фиксируется как факт — скрипт не падает.

Запуск:
    WB_TOKEN_BY1=... WB_TOKEN_RU1=... python scripts/spike_probe.py > .scratch/facts_raw.md

Токены берутся ТОЛЬКО из env и никогда не печатаются (Authorization не выводится).
Только stdlib (urllib) — зависимости не требуются.
Только чтение: GET-ы и POST-эндпоинты чтения (budget, bids/min). Никаких записей в WB.
SSRF-гигиена: https + фиксированный хост + белый список путей, query через urlencode,
динамические id только int, DNS резолвится и проверяется на приватные/loopback-адреса,
редиректы запрещены.
"""
import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

HOST = "https://advert-api.wildberries.ru"
EXPECTED_HOST = "advert-api.wildberries.ru"
TIMEOUT = 30
PAUSE = 1.0  # пауза между вызовами, сек
ACTIVE_STATUSES = (9, 11)

# Единственные пути, куда разрешено ходить (без query; query — через urlencode).
ALLOWED_PATHS = frozenset({
    "/adv/v1/promotion/count",
    "/api/advert/v1/config",
    "/adv/v1/balance",
    "/api/advert/v2/adverts",
    "/adv/v3/fullstats",
    "/api/advert/v2/budget",
    "/api/advert/v1/bids/min",
    "/api/advert/v0/bids/recommendations",
})

WAVE1 = [
    ("GET", "/adv/v1/promotion/count", None),
    ("GET", "/api/advert/v1/config", None),
    ("GET", "/adv/v1/balance", None),
    ("GET", "/api/advert/v2/adverts", {"ids": "0"}),  # ожидаемо 4xx: фиксируем формат ошибки
]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Любой редирект — ошибка, а не переход (запрещаем смену цели)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def validate_target(url: str) -> None:
    """Разрешаем только https://advert-api.wildberries.ru с публичными IP."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != EXPECTED_HOST or parsed.username:
        raise ValueError(f"unexpected target: {parsed.scheme}://{parsed.hostname}")
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


def msk_today() -> str:
    """Сегодня по Москве (UTC+3), YYYY-MM-DD."""
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 3 * 3600))


def msk_days_ago(days: int) -> str:
    """Дата `days` дней назад по Москве (UTC+3), YYYY-MM-DD."""
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 3 * 3600 - days * 86400))


def safe_json(raw: bytes):
    """Безопасный парс тела: не json -> {"text": первые 1000 символов}."""
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:  # json.loads
        return {"text": text[:1000]}


def call(method: str, path: str, token: str, body=None, query=None):
    """Один пробник: запрос + markdown-блок в stdout. Не бросает исключений.

    path — только из ALLOWED_PATHS; query — dict, сериализуется urlencode.
    Возвращает распарсенное тело (или None при исключении).
    """
    if path not in ALLOWED_PATHS:
        raise ValueError(f"path not allowed: {path!r}")
    url = HOST + path
    if query:
        url = url + "?" + urllib.parse.urlencode(query)
    validate_target(url)

    t0 = time.monotonic()
    status = None
    resp_headers = {}
    raw = b""
    exc_repr = None
    try:
        data = None
        req_headers = {"Authorization": token}  # БЕЗ Bearer
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
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


def active_ids_from_count(count_payload):
    """advertId активных РК (status 9/11) из ответа count + карта id -> type.

    Структура: {"adverts": [{"type": int, "status": int,
                             "advert_list": [{"advertId": int, ...}]}]}
    """
    ids = []
    id_types = {}
    if not isinstance(count_payload, dict):
        return ids, id_types
    for group in count_payload.get("adverts") or []:
        if not isinstance(group, dict):
            continue
        status = group.get("status")
        gtype = group.get("type")
        for item in group.get("advert_list") or []:
            if not isinstance(item, dict):
                continue
            aid = item.get("advertId", item.get("advert_id"))
            if isinstance(aid, int) and status in ACTIVE_STATUSES:
                ids.append(aid)
                id_types[aid] = gtype
    return ids, id_types


def find_first_key(node, names):
    """Первое найденное значение по любому из имён ключа (рекурсивно)."""
    if isinstance(node, dict):
        for name in names:
            if name in node and node[name] is not None:
                return node[name]
        for value in node.values():
            found = find_first_key(value, names)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = find_first_key(item, names)
            if found is not None:
                return found
    return None


def collect_nm_ids(node, out, limit=100):
    """Собрать nm_id из структуры РК (рекурсивно, ключи nm/nmId/nm_id)."""
    if len(out) >= limit:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key.lower() in ("nm", "nmid", "nm_id") and isinstance(value, int):
                out.append(value)
                if len(out) >= limit:
                    return
            else:
                collect_nm_ids(value, out, limit)
                if len(out) >= limit:
                    return
    elif isinstance(node, list):
        for item in node:
            collect_nm_ids(item, out, limit)
            if len(out) >= limit:
                return


def wave2(token: str, count_payload) -> None:
    """Follow-ups по данным из count: adverts / budget / fullstats / bids/min / recommendations."""
    ids, id_types = active_ids_from_count(count_payload)
    ids = [int(i) for i in ids]  # в URL идут только целые
    if not ids:
        print("> нет активных РК (status 9/11 в count) — волна 2 пропущена", flush=True)
        return
    batch = ids[:50]
    print(f"> активных РК (status 9/11): {len(ids)}; берём первые {len(batch)}: {batch}", flush=True)

    adverts_payload = call("GET", "/api/advert/v2/adverts", token, query={"ids": ",".join(map(str, batch))})

    first_campaign = adverts_payload[0] if isinstance(adverts_payload, list) and adverts_payload else None
    payment_type = find_first_key(first_campaign, ("paymentType", "payment_type"))
    camp_type = id_types.get(ids[0])
    if camp_type is None:
        camp_type = find_first_key(first_campaign, ("type",))
    print(f"> первая РК id={ids[0]}: type={camp_type!r}, payment_type={payment_type!r}", flush=True)

    call("POST", "/api/advert/v2/budget", token, body={"ids": batch})

    call(
        "GET",
        "/adv/v3/fullstats",
        token,
        query={"ids": str(ids[0]), "beginDate": msk_days_ago(7), "endDate": msk_today()},
    )

    nm_ids: list[int] = []
    collect_nm_ids(first_campaign, nm_ids, limit=100)
    if nm_ids:
        print(f"> nm_ids из adverts ({len(nm_ids)} шт): {nm_ids[:10]}...", flush=True)
        # ФОРМАТ ТЕЛА НЕ ПОДТВЕРЖДЁН: при 4xx код+тело — целевой исход.
        call("POST", "/api/advert/v1/bids/min", token, body={"nm_ids": nm_ids[:100]})
    else:
        print("> nm_id не найдены в adverts — bids/min пропущен", flush=True)

    if camp_type is not None and payment_type is not None:
        # ПАРАМЕТРЫ НЕ ПОДТВЕРЖДЕНЫ: при 4xx код+тело фиксируем как факт.
        call(
            "GET",
            "/api/advert/v0/bids/recommendations",
            token,
            query={"type": camp_type, "payment_type": payment_type},
        )
    else:
        print("> type/payment_type первой РК не определены — recommendations пропущен", flush=True)


def run_cabinet(env_name: str, name: str) -> None:
    token = os.environ.get(env_name)
    if not token:
        print(f"skip {env_name}", flush=True)
        return
    print(f"## {name}", flush=True)
    count_payload = None
    for method, path, query in WAVE1:
        payload = call(method, path, token, query=query)
        if path == "/adv/v1/promotion/count":
            count_payload = payload
    try:
        wave2(token, count_payload)
    except Exception as e:  # noqa: BLE001
        print(f"exception in wave2: {e!r}", flush=True)
    print(flush=True)


def main() -> None:
    run_cabinet("WB_TOKEN_BY1", "BY")
    run_cabinet("WB_TOKEN_RU1", "RU")


if __name__ == "__main__":
    main()
