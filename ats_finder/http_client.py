from __future__ import annotations

import asyncio
import email.utils
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from ats_finder.config import Settings


_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class HostRateLimited(httpx.HTTPError):
    """The host asked us to pause; this request was not retried."""

    def __init__(self, host: str, retry_after: float) -> None:
        self.host = host
        self.retry_after = retry_after
        super().__init__(
            f"host {host} is rate limited; retry after {retry_after:.1f}s"
        )


def _retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return max(0.0, (parsed.astimezone(timezone.utc) - reference).total_seconds())


class RespectfulHTTPClient:
    """HTTPX client with bounded retries and a minimum interval per host."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._sleep = sleeper
        self._monotonic = monotonic
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
            transport=transport,
        )
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}
        self._blocked_until: dict[str, float] = {}
        self._map_lock = asyncio.Lock()

    async def __aenter__(self) -> "RespectfulHTTPClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _lock_for(self, host: str) -> asyncio.Lock:
        async with self._map_lock:
            return self._host_locks.setdefault(host, asyncio.Lock())

    async def _send_once(
        self,
        url: str,
        *,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        min_interval: float | None,
    ) -> httpx.Response:
        host = (urlsplit(url).hostname or "").casefold()
        if not host:
            raise ValueError(f"Request URL has no host: {url!r}")
        lock = await self._lock_for(host)
        interval = max(
            self.settings.per_host_interval_seconds,
            min_interval or 0.0,
        )
        # Reserve the next start slot for this host under the lock, then release
        # it before sleeping or calling the network. Holding the lock for the
        # whole round trip would serialize requests to a host end to end and cap
        # throughput at 1 / (latency + interval) instead of 1 / interval.
        async with lock:
            now = self._monotonic()
            blocked_until = self._blocked_until.get(host, float("-inf"))
            if now < blocked_until:
                raise HostRateLimited(host, blocked_until - now)
            scheduled = max(
                now,
                self._last_request.get(host, float("-inf")) + interval,
            )
            self._last_request[host] = scheduled
        delay = scheduled - now
        if delay > 0:
            await self._sleep(delay)
        return await self._client.get(url, params=params, headers=headers)

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        min_interval: float | None = None,
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                response = await self._send_once(
                    url,
                    params=params,
                    headers=headers,
                    min_interval=min_interval,
                )
                if response.status_code not in _RETRYABLE_STATUS:
                    response.raise_for_status()
                    return response
                if response.status_code == 429:
                    retry_after = _retry_after_seconds(
                        response.headers.get("Retry-After")
                    )
                    cooldown = max(
                        retry_after or 0.0,
                        self.settings.rate_limit_cooldown_seconds,
                    )
                    host = (urlsplit(url).hostname or "").casefold()
                    lock = await self._lock_for(host)
                    async with lock:
                        self._blocked_until[host] = max(
                            self._blocked_until.get(host, float("-inf")),
                            self._monotonic() + cooldown,
                        )
                    raise HostRateLimited(host, cooldown)
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                delay = (
                    retry_after
                    if retry_after is not None
                    else self.settings.backoff_base_seconds * (2**attempt)
                )
                if attempt == self.settings.max_retries:
                    response.raise_for_status()
                if delay:
                    await self._sleep(delay)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt == self.settings.max_retries:
                    raise
                delay = self.settings.backoff_base_seconds * (2**attempt)
                if delay:
                    await self._sleep(delay)
        if last_error is not None:
            raise last_error
        raise RuntimeError("HTTP retry loop ended without a response")

