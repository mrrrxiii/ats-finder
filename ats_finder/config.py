from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    value = default if raw is None else int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    value = default if raw is None else float(raw)
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings shared by the scanner, worker, and dashboard."""

    database_path: Path = Path("data/ats_finder.sqlite3")
    scan_interval_seconds: int = 3600
    stale_after_intervals: float = 2.0
    concurrency: int = 8
    request_timeout_seconds: float = 20.0
    max_retries: int = 3
    backoff_base_seconds: float = 0.5
    per_host_interval_seconds: float = 0.25
    max_pages: int = 1000
    lever_page_size: int = 100
    user_agent: str = "CanadaJavaATSFinder/1.0 (+respectful public job-board monitor)"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_path=Path(os.getenv("ATS_FINDER_DB", "data/ats_finder.sqlite3")),
            scan_interval_seconds=_env_int("ATS_FINDER_SCAN_INTERVAL_SECONDS", 3600, 60),
            stale_after_intervals=_env_float("ATS_FINDER_STALE_AFTER_INTERVALS", 2.0, 1.0),
            concurrency=_env_int("ATS_FINDER_CONCURRENCY", 8, 1),
            request_timeout_seconds=_env_float("ATS_FINDER_HTTP_TIMEOUT_SECONDS", 20.0, 1.0),
            max_retries=_env_int("ATS_FINDER_MAX_RETRIES", 3, 1),
            backoff_base_seconds=_env_float("ATS_FINDER_BACKOFF_BASE_SECONDS", 0.5, 0.0),
            per_host_interval_seconds=_env_float("ATS_FINDER_PER_HOST_INTERVAL_SECONDS", 0.25, 0.0),
            max_pages=_env_int("ATS_FINDER_MAX_PAGES", 1000, 1),
            lever_page_size=_env_int("ATS_FINDER_LEVER_PAGE_SIZE", 100, 1),
            user_agent=os.getenv(
                "ATS_FINDER_USER_AGENT",
                "CanadaJavaATSFinder/1.0 (+respectful public job-board monitor)",
            ),
        )

    @property
    def stale_after_seconds(self) -> float:
        return self.scan_interval_seconds * self.stale_after_intervals


DEFAULT_DATASET_URL = (
    "https://huggingface.co/datasets/latmay/ats-career-page-urls/resolve/main/"
    "ats_career_page_urls.csv"
)

