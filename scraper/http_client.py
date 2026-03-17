from __future__ import annotations

import asyncio

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = structlog.get_logger()

USER_AGENT = "Mozilla/5.0 (compatible; FrontierDentalBot/1.0)"

DEFAULT_TIMEOUT = 30.0


class HttpClient:
    """Async HTTP client with retry and rate-limiting."""

    def __init__(self, delay: float = 1.0, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._delay = delay
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
        reraise=True,
    )
    async def get_page(self, url: str) -> tuple[int, str]:
        """Fetch a URL and return (status_code, html_text).

        Retries up to 3 times with exponential backoff (1s, 2s, 4s)
        on HTTP errors and timeouts.
        """
        log.debug("http_get", url=url)
        response = await self._client.get(url)
        response.raise_for_status()
        await asyncio.sleep(self._delay)
        return response.status_code, response.text

    async def get_page_no_raise(self, url: str) -> tuple[int, str]:
        """Fetch a URL, returning the status code without raising on 4xx/5xx.

        Still retries on connection-level errors.
        """
        log.debug("http_get_no_raise", url=url)
        try:
            response = await self._client.get(url)
            await asyncio.sleep(self._delay)
            return response.status_code, response.text
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            log.warning("http_error", url=url, error=str(exc))
            raise
