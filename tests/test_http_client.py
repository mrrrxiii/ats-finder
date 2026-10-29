from __future__ import annotations

import asyncio

import httpx

from ats_finder.config import Settings
from ats_finder.http_client import RespectfulHTTPClient


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
