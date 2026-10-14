"""WB API constants: hosts, rate-limit classes, path->class mapper.

HOST is the advert-api; STATISTICS_HOST is the statistics-api (supplier
orders) — a different server with its own limits, but the limit classes and
the single klass() mapper are shared here (the two path namespaces don't
clash). LIMITS come from the WB spec (snapshot 26.09.2026). .scratch/facts.md
marks them as NOT yet confirmed against live X-Ratelimit-* headers: the spike
got 403 "scope is not allowed for this resource" on every method (the
available tokens lack the "Advancement" category). Re-verify and edit here —
and only here — once real tokens arrive.
"""

HOST = "https://advert-api.wildberries.ru"
STATISTICS_HOST = "https://statistics-api.wildberries.ru"

# (requests, period_sec) per limit class.
LIMITS = {
    # Live fact (spike-2 prod 14.10.2026, «Улов»): after one request
    # X-Ratelimit-Remaining=9, Limit=10 — 1/min refill with a burst of 10.
    "orders": (1, 60),
    # Live fact (spike 13.10.2026, BYN cabinet): X-Ratelimit-Limit=1/min — stricter than the spec's 3/min.
    "fullstats": (1, 60),
    "budget": (20, 60),
    "bids_min": (20, 60),
    "recommendations": (5, 60),
    "adverts": (5, 1),
    "count": (5, 1),
    "balance": (1, 1),
    "normquery": (5, 60),
}  # from the WB spec (snapshot 26.09.2026); verify against live headers once tokens are available — see .scratch/facts.md

# Exact path prefixes mapped to a limit class (first match wins; no prefix is
# a prefix of another, so order is only cosmetic).
_PATH_CLASS: tuple[tuple[str, str], ...] = (
    # statistics-api host (orders); the namespace does not clash with advert-api
    ("/api/v1/supplier/orders", "orders"),
    ("/adv/v3/fullstats", "fullstats"),
    ("/api/advert/v2/budget", "budget"),
    ("/api/advert/v1/bids/min", "bids_min"),
    ("/api/advert/v0/bids/recommendations", "recommendations"),
    ("/api/advert/v2/adverts", "adverts"),
    ("/adv/v1/promotion/count", "count"),
    ("/adv/v1/balance", "balance"),
    ("/adv/v0/normquery", "normquery"),
    ("/api/advert/v1/normquery", "normquery"),
    # The task text lists only "/adv/v0/normquery" and "/api/advert/v1/normquery"
    # for the normquery class, but client.normquery_stats() calls
    # /adv/v1/normquery/stats, which neither prefix covers — it would raise
    # ValueError on every stats request. Added so every client path maps.
    ("/adv/v1/normquery", "normquery"),
)


def klass(path: str) -> str:
    """Map an API path to its rate-limit class; unknown paths raise ValueError."""
    for prefix, name in _PATH_CLASS:
        if path.startswith(prefix):
            return name
    raise ValueError(f"unknown WB API path (no limit class): {path!r}")
