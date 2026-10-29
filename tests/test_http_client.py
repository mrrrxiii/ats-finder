from __future__ import annotations

import asyncio

import httpx
import pytest

from ats_finder.config import Settings
from ats_finder.http_client import HostRateLimited, RespectfulHTTPClient


def test_same_host_requests_overlap_in_flight() -> None:
    """The per-host interval must pace start times, not serialize round trips."""

    gate = asyncio.Event()
    active = 0
    max_active = 0

    class GatedTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                await gate.wait()
            finally:
                active -= 1
            return httpx.Response(200, json={"ok": True}, request=request)

    async def release_after_delay() -> None:
        await asyncio.sleep(0.05)
        gate.set()

    async def run() -> None:
        settings = Settings(per_host_interval_seconds=0.0)
        async with RespectfulHTTPClient(settings, transport=GatedTransport()) as client:
            releaser = asyncio.create_task(release_after_delay())
            try:
                await asyncio.gather(
                    *(client.get("https://api.example.com/jobs") for _ in range(3))
                )
            finally:
                releaser.cancel()

    asyncio.run(run())
    assert max_active == 3


def test_same_host_start_times_are_spaced_by_interval() -> None:
    clock = {"now": 0.0}
    sleeps: list[float] = []

    def monotonic() -> float:
        return clock["now"]

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)
        clock["now"] += delay

    async def run() -> None:
        settings = Settings(per_host_interval_seconds=0.25)
        async with RespectfulHTTPClient(
            settings,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
            sleeper=sleeper,
            monotonic=monotonic,
        ) as client:
            await asyncio.gather(
                *(client.get("https://api.example.com/jobs") for _ in range(4))
            )

    asyncio.run(run())
    # Slots are reserved at 0, 0.25, 0.50 and 0.75 seconds.
    assert len(sleeps) == 3
    assert all(delay == 0.25 for delay in sleeps)
    assert clock["now"] == 0.75


def test_429_fails_fast_and_sets_a_shared_host_cooldown() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            headers={"Retry-After": "2"},
            request=request,
        )

    async def run() -> None:
        settings = Settings(
            per_host_interval_seconds=0.0,
            max_retries=3,
            rate_limit_cooldown_seconds=2.0,
        )
        async with RespectfulHTTPClient(
            settings,
            transport=httpx.MockTransport(respond),
        ) as client:
            with pytest.raises(HostRateLimited) as first:
                await client.get("https://api.example.com/jobs")
            assert first.value.retry_after == 2.0
            with pytest.raises(HostRateLimited):
                await client.get("https://api.example.com/jobs")

    asyncio.run(run())
    # The first 429 sets the host cooldown and is not retried. The second
    # request fails fast without sending another HTTP request.
    assert calls == 1


def test_per_request_min_interval_is_honored() -> None:
    clock = {"now": 0.0}
    sleeps: list[float] = []

    def monotonic() -> float:
        return clock["now"]

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)
        clock["now"] += delay

    async def run() -> None:
        settings = Settings(per_host_interval_seconds=0.0)
        async with RespectfulHTTPClient(
            settings,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={}, request=request)
            ),
            sleeper=sleeper,
            monotonic=monotonic,
        ) as client:
            await client.get(
                "https://apply.workable.com/api/v1/widget/accounts/acme",
                min_interval=1.0,
            )
            await client.get(
                "https://apply.workable.com/api/v1/widget/accounts/acme",
                min_interval=1.0,
            )

    asyncio.run(run())
    assert sleeps == [1.0]
    assert clock["now"] == 1.0
