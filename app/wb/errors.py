"""Errors raised by the WB advert-api client."""


class WbApiError(Exception):
    """Failure of a WB API call (after the client's built-in single 429 retry).

    Attributes:
        status: HTTP status code, or -1 when the request failed at the network
            level (no response at all, e.g. DNS/connect/timeout error).
        detail: Human-readable description: the API's JSON "detail" field, the
            raw response body, or the network exception repr.
        retry_after: Seconds WB asked to wait before the next attempt (from a
            429's X-Ratelimit-Retry header), when known; None otherwise.
    """

    def __init__(self, status: int, detail: str, retry_after: float | None = None) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.retry_after = retry_after

    def __str__(self) -> str:
        text = f"WB API error {self.status}: {self.detail}"
        if self.retry_after is not None:
            text += f" (retry after {self.retry_after:g}s)"
        return text
