from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from ats_finder.config import Settings
from ats_finder.models import ATSSource, NormalizedJob


class ProviderError(RuntimeError):
    """A provider response cannot be treated as a complete board snapshot."""


class ProviderResponseError(ProviderError):
    pass


class ProviderParseError(ProviderError):
    pass


class PaginationError(ProviderError):
    pass


@dataclass(frozen=True, slots=True)
class FetchResult:
    jobs: tuple[NormalizedJob, ...]
    request_count: int
    complete: bool = True
    location_filtered: bool = False
    observed_record_count: int | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    provider_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.location_filtered and not self.complete:
            raise ValueError("a location-filtered result must be complete")
        if self.location_filtered and self.jobs:
            raise ValueError("a location-filtered result cannot contain jobs")
        if self.location_filtered and self.observed_record_count is None:
            raise ValueError("a location-filtered result requires an observed record count")
        if self.observed_record_count is not None and self.observed_record_count < 0:
            raise ValueError("observed_record_count must be non-negative")


class ProviderAdapter(Protocol):
    provider: str

    async def fetch(
        self,
        source: ATSSource,
        client: "RespectfulAsyncClient",
        settings: Settings,
    ) -> FetchResult: ...


class RespectfulAsyncClient(Protocol):
    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        min_interval: float | None = None,
    ) -> httpx.Response: ...

