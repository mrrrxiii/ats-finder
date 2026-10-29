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
    warnings: tuple[str, ...] = field(default_factory=tuple)
    provider_metadata: dict[str, Any] = field(default_factory=dict)


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

