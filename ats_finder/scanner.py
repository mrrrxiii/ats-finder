from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Callable

import httpx

from ats_finder.config import Settings
from ats_finder.db import Database
from ats_finder.engine import ScanEngine
from ats_finder.http_client import RespectfulHTTPClient
from ats_finder.models import (
    ATSSource,
    ScanOutcome,
    ScanStatus,
    SourceSupportStatus,
)
from ats_finder.providers import ADAPTERS
from ats_finder.providers.base import FetchResult, ProviderError


@dataclass(frozen=True, slots=True)
class ScanAttempt:
    source: ATSSource
    outcome: ScanOutcome
    request_count: int
    elapsed_seconds: float
    warnings: tuple[str, ...] = ()


class BoardScanner:
    """Fetch boards and hand only verified-complete snapshots to ``ScanEngine``."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        engine: ScanEngine | None = None,
        client: RespectfulHTTPClient | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.engine = engine or ScanEngine(database)
        self.client = client or RespectfulHTTPClient(settings)
        self._owns_client = client is None
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._source_locks: dict[int, asyncio.Lock] = {}
        self._semaphore = asyncio.Semaphore(settings.concurrency)

    async def __aenter__(self) -> "BoardScanner":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def _next_scan(self, completed_at: datetime, failures: int = 0) -> datetime:
        # Request-level retries already happened. Repeated board failures back off
        # across cycles, capped at 32 target intervals so the source is retried.
        multiplier = 1 if failures <= 0 else min(2 ** min(failures, 5), 32)
        return completed_at + timedelta(
            seconds=self.settings.scan_interval_seconds * multiplier
        )

    async def scan_source(self, source_id: int) -> ScanAttempt:
        lock = self._source_locks.setdefault(source_id, asyncio.Lock())
        async with lock, self._semaphore:
            source = self.database.get_source(source_id)
            if source is None:
                raise KeyError(f"unknown source id {source_id}")
            if source.support_status is not SourceSupportStatus.SUPPORTED:
                raise ValueError(f"source {source_id} is not supported")
            adapter = ADAPTERS.get(source.provider.casefold())
            if adapter is None:
                raise ValueError(
                    f"source {source_id} is marked supported but has no {source.provider!r} adapter"
                )

            started_at = self._now().astimezone(timezone.utc)
            timer = monotonic()
            request_count = 0
            warnings: tuple[str, ...] = ()
            try:
                result: FetchResult = await adapter.fetch(
                    source, self.client, self.settings
                )
                request_count = result.request_count
                warnings = result.warnings
                completed_at = self._now().astimezone(timezone.utc)
                if not result.complete:
                    outcome = self.engine.record_incomplete_scan(
                        source.id,
                        "Provider adapter did not certify complete pagination",
                        started_at=started_at,
                        completed_at=completed_at,
                        observed_record_count=len(result.jobs),
                        next_scan_at=self._next_scan(
                            completed_at, source.consecutive_failures + 1
                        ),
                    )
                else:
                    outcome = self.engine.process_complete_scan(
                        source.id,
                        result.jobs,
                        started_at=started_at,
                        completed_at=completed_at,
                        next_scan_at=self._next_scan(completed_at),
                    )
            except ProviderError as exc:
                completed_at = self._now().astimezone(timezone.utc)
                outcome = self.engine.record_incomplete_scan(
                    source.id,
                    f"{type(exc).__name__}: {exc}",
                    started_at=started_at,
                    completed_at=completed_at,
                    next_scan_at=self._next_scan(
                        completed_at, source.consecutive_failures + 1
                    ),
                )
            except (httpx.HTTPError, OSError, TimeoutError) as exc:
                completed_at = self._now().astimezone(timezone.utc)
                outcome = self.engine.record_failed_scan(
                    source.id,
                    f"{type(exc).__name__}: {exc}",
                    started_at=started_at,
                    completed_at=completed_at,
                    next_scan_at=self._next_scan(
                        completed_at, source.consecutive_failures + 1
                    ),
                )
            except Exception as exc:
                # Persist the attempt for auditability, then re-raise programming
                # failures so a worker/operator cannot mistake them for source decay.
                completed_at = self._now().astimezone(timezone.utc)
                self.engine.record_failed_scan(
                    source.id,
                    f"Unexpected {type(exc).__name__}: {exc}",
                    started_at=started_at,
                    completed_at=completed_at,
                    next_scan_at=self._next_scan(
                        completed_at, source.consecutive_failures + 1
                    ),
                )
                raise

            persisted_source = self.database.get_source(source_id)
            if persisted_source is None:  # pragma: no cover - source is FK-protected.
                raise RuntimeError(f"source {source_id} disappeared during its scan")
            return ScanAttempt(
                source=persisted_source,
                outcome=outcome,
                request_count=request_count,
                elapsed_seconds=monotonic() - timer,
                warnings=warnings,
            )

    def fair_due_sources(
        self,
        *,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> list[ATSSource]:
        current = (now or self._now()).astimezone(timezone.utc)
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        sources = [
            source
            for source in self.database.list_sources(
                support_status=SourceSupportStatus.SUPPORTED
            )
            if source.next_scan_at is None or source.next_scan_at <= current
        ]

        interval = float(self.settings.scan_interval_seconds)

        def effective_score(source: ATSSource) -> tuple[float, float, int]:
            due_from = (
                source.next_scan_at
                or source.last_attempted_scan_at
                or source.imported_at
                or source.created_at
            )
            overdue_intervals = max(0.0, (current - due_from).total_seconds() / interval)
            # Aging is unbounded: a low-priority board eventually outranks a
            # repeatedly due high-priority board, preventing permanent starvation.
            return (
                source.priority + overdue_intervals,
                -due_from.timestamp(),
                -source.id,
            )

        sources.sort(key=effective_score, reverse=True)
        return sources if limit is None else sources[:limit]

    async def scan_due(self, *, limit: int | None = None) -> list[ScanAttempt]:
        due = self.fair_due_sources(limit=limit)
        if not due:
            return []
        results = await asyncio.gather(
            *(self.scan_source(source.id) for source in due),
            return_exceptions=True,
        )
        attempts: list[ScanAttempt] = []
        unexpected: list[BaseException] = []
        for result in results:
            if isinstance(result, BaseException):
                unexpected.append(result)
            else:
                attempts.append(result)
        if unexpected:
            raise ExceptionGroup("one or more source scans failed unexpectedly", unexpected)
        return attempts

