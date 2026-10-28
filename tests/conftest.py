from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ats_finder.db import Database
from ats_finder.engine import ScanEngine
from ats_finder.models import (
    ATSSource,
    NormalizedJob,
    PublicationSemantics,
    SourceInput,
    SourceSupportStatus,
    WorkplaceType,
)


UTC = timezone.utc
FIXED_NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="run bounded tests against official public ATS endpoints",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--run-live"):
        return
    skip = pytest.mark.skip(reason="pass --run-live to call public ATS endpoints")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@dataclass(frozen=True)
class EngineHarness:
    path: Path
    database: Database
    source: ATSSource
    engine: ScanEngine


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[EngineHarness]:
    path = tmp_path / "history.sqlite3"
    database = Database(path)
    source = database.upsert_source(
        SourceInput(
            provider="greenhouse",
            company="Example Company",
            board_identifier="example-company",
            career_page_url="https://boards.greenhouse.io/example-company",
            support_status=SourceSupportStatus.SUPPORTED,
        ),
        now=FIXED_NOW,
    )
    yield EngineHarness(
        path=path,
        database=database,
        source=source,
        engine=ScanEngine(database),
    )
    database.close()


@pytest.fixture
def job_factory() -> Callable[..., NormalizedJob]:
    def make_job(
        provider_job_id: str = "job-1",
        *,
        title: str = "Java Developer",
        description: str = "Build Java services.",
        location: str = "Ottawa, Ontario, Canada",
        application_url: str | None = None,
        company: str | None = None,
        workplace_type: WorkplaceType = WorkplaceType.UNKNOWN,
        canada_remote_eligibility: bool | None = None,
        published_at: datetime | None = None,
        publication_source: str | None = None,
        publication_semantics: PublicationSemantics | None = None,
        publication_is_verified: bool = False,
    ) -> NormalizedJob:
        return NormalizedJob(
            provider_job_id=provider_job_id,
            title=title,
            description=description,
            location=location,
            application_url=(
                application_url
                or f"https://boards.greenhouse.io/example-company/jobs/{provider_job_id}"
            ),
            company=company,
            workplace_type=workplace_type,
            canada_remote_eligibility=canada_remote_eligibility,
            published_at=published_at,
            publication_source=publication_source,
            publication_semantics=publication_semantics,
            publication_is_verified=publication_is_verified,
        )

    return make_job

