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
    ) -> httpx.Response:
        host = (urlsplit(url).hostname or "").casefold()
        if not host:
            raise ValueError(f"Request URL has no host: {url!r}")
        lock = await self._lock_for(host)
        async with lock:
            elapsed = self._monotonic() - self._last_request.get(host, float("-inf"))
            delay = self.settings.per_host_interval_seconds - elapsed
            if delay > 0:
                await self._sleep(delay)
            try:
                return await self._client.get(url, params=params, headers=headers)
            finally:
                self._last_request[host] = self._monotonic()

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                response = await self._send_once(url, params=params, headers=headers)
                if response.status_code not in _RETRYABLE_STATUS:
                    response.raise_for_status()
                    return response
                if attempt == self.settings.max_retries:
                    response.raise_for_status()
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                delay = (
                    retry_after
                    if retry_after is not None
                    else self.settings.backoff_base_seconds * (2**attempt)
                )
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

