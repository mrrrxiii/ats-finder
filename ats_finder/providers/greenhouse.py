from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ats_finder.config import Settings
from ats_finder.filters import location_may_be_relevant
from ats_finder.models import (
    ATSSource,
    NormalizedJob,
    PublicationSemantics,
    WorkplaceType,
)
from ats_finder.providers.base import FetchResult, ProviderParseError, RespectfulAsyncClient
from ats_finder.providers.text import html_to_text


_JAVA = re.compile(r"(?<![A-Za-z0-9_])java(?![A-Za-z0-9_])", re.IGNORECASE)
_TARGET_LOCATION = re.compile(
    r"\b(?:ottawa|kanata|stittsville|canada|ontario|remote|worldwide)\b",
    re.IGNORECASE,
)


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderParseError("Greenhouse first_published is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("Greenhouse first_published timestamp has no timezone")
    return parsed


def _workplace_type(title: str, description: str, location: str) -> WorkplaceType:
    text = f"{title}\n{location}\n{description}".casefold()
    if re.search(r"\bhybrid\b", text):
        return WorkplaceType.HYBRID
    if re.search(r"\bremote\b|work from home", text):
        return WorkplaceType.REMOTE
    if location:
        return WorkplaceType.ONSITE
    return WorkplaceType.UNKNOWN


def parse_greenhouse_job(item: object, *, fallback_company: str) -> NormalizedJob:
    if not isinstance(item, dict):
        raise ProviderParseError("Greenhouse job entry is not an object")
    provider_id = item.get("id")
    title = item.get("title")
    url = item.get("absolute_url")
    if (
        provider_id is None
        or not isinstance(title, str)
        or not title.strip()
        or not isinstance(url, str)
        or not url.strip()
    ):
        raise ProviderParseError("Greenhouse job is missing id, title, or absolute_url")

    raw_location = item.get("location")
    location = ""
    if isinstance(raw_location, dict) and isinstance(raw_location.get("name"), str):
        location = raw_location["name"].strip()
    office_locations: list[str] = []
    offices = item.get("offices")
    if isinstance(offices, list):
        for office in offices:
            if isinstance(office, dict):
                for field in ("location", "name"):
                    value = office.get(field)
                    if isinstance(value, str) and value.strip() and value.strip() not in office_locations:
                        office_locations.append(value.strip())
    if office_locations:
        joined = "; ".join(office_locations)
        if joined.casefold() not in location.casefold():
            location = f"{location}; {joined}".strip("; ")

    description = html_to_text(item.get("content"))
    published_at = _parse_datetime(item.get("first_published"))
    company = item.get("company_name")
    if not isinstance(company, str) or not company.strip():
        company = fallback_company

    return NormalizedJob(
        provider_job_id=str(provider_id),
        title=title.strip(),
        description=description,
        location=location,
        application_url=url,
        company=company.strip(),
        workplace_type=_workplace_type(title, description, location),
        published_at=published_at,
        publication_source=(
            "Greenhouse Job Board API job detail: first_published" if published_at else None
        ),
        publication_semantics=(
            PublicationSemantics.ORIGINAL_PUBLICATION
            if published_at
            else None
        ),
        publication_is_verified=published_at is not None,
    )


class GreenhouseAdapter:
    provider = "greenhouse"

    @staticmethod
    def _board_token(source: ATSSource) -> str:
        token = source.board_identifier
        if token.startswith("global:"):
            token = token.split(":", 1)[1]
        if not token:
            raise ProviderParseError("Greenhouse source has an empty board token")
        return token

    async def fetch(
        self,
        source: ATSSource,
        client: RespectfulAsyncClient,
        settings: Settings,
    ) -> FetchResult:
        token = self._board_token(source)
        base = f"https://boards-api.greenhouse.io/v1/boards/{token}"
        response = await client.get(f"{base}/jobs")
        request_count = 1
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise ProviderParseError("Greenhouse jobs response is not valid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ProviderParseError("Greenhouse jobs response has no jobs array")
        raw_jobs: list[object] = payload["jobs"]
        meta = payload.get("meta")
        if not isinstance(meta, dict) or not isinstance(meta.get("total"), int):
            raise ProviderParseError("Greenhouse response has no integer meta.total completeness marker")
        if meta["total"] != len(raw_jobs):
            raise ProviderParseError(
                f"Greenhouse returned {len(raw_jobs)} jobs but meta.total is {meta['total']}"
            )

        summary_jobs: list[NormalizedJob] = []
        summary_ids: set[str] = set()
        prospect_count = 0
        for raw in raw_jobs:
            if isinstance(raw, dict) and raw.get("internal_job_id", object()) is None:
                prospect_count += 1
                continue
            summary_job = parse_greenhouse_job(
                raw, fallback_company=source.company
            )
            if summary_job.provider_job_id in summary_ids:
                raise ProviderParseError(
                    "Greenhouse returned duplicate job id "
                    f"{summary_job.provider_job_id}"
                )
            summary_ids.add(summary_job.provider_job_id)
            summary_jobs.append(summary_job)

        provider_metadata = {
            "reported_total": meta["total"],
            "prospect_posts_excluded": prospect_count,
        }
        if not summary_jobs:
            return FetchResult(
                jobs=(),
                request_count=request_count,
                provider_metadata=provider_metadata,
            )
        if not any(
            location_may_be_relevant(job.location) for job in summary_jobs
        ):
            return FetchResult(
                jobs=(),
                request_count=request_count,
                location_filtered=True,
                observed_record_count=len(summary_jobs),
                provider_metadata=provider_metadata,
            )

        response = await client.get(
            f"{base}/jobs",
            params={"content": "true"},
        )
        request_count += 1
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderParseError(
                "Greenhouse jobs response is not valid JSON"
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ProviderParseError("Greenhouse jobs response has no jobs array")
        raw_jobs = payload["jobs"]
        meta = payload.get("meta")
        if not isinstance(meta, dict) or not isinstance(meta.get("total"), int):
            raise ProviderParseError(
                "Greenhouse response has no integer meta.total completeness marker"
            )
        if meta["total"] != len(raw_jobs):
            raise ProviderParseError(
                f"Greenhouse returned {len(raw_jobs)} jobs but meta.total is {meta['total']}"
            )

        jobs: list[NormalizedJob] = []
        warnings: list[str] = []
        seen_ids: set[str] = set()
        prospect_count = 0
        for raw in raw_jobs:
            # Greenhouse documents null internal_job_id as a prospect post rather
            # than a position. Keep it in the completeness count, but not as a job.
            if isinstance(raw, dict) and raw.get("internal_job_id", object()) is None:
                prospect_count += 1
                continue
            job = parse_greenhouse_job(raw, fallback_company=source.company)
            if job.provider_job_id in seen_ids:
                raise ProviderParseError(
                    f"Greenhouse returned duplicate job id {job.provider_job_id}"
                )
            seen_ids.add(job.provider_job_id)

            # The list endpoint documents updated_at, not publication time. Query the
            # documented detail endpoint only for relevant-looking candidates so that
            # first_published can be used without turning each board scan into N+1 calls.
            if (
                job.published_at is None
                and _JAVA.search(f"{job.title}\n{job.description}")
                and _TARGET_LOCATION.search(job.location)
            ):
                try:
                    detail_response = await client.get(
                        f"{base}/jobs/{job.provider_job_id}"
                    )
                    request_count += 1
                    detail = detail_response.json()
                    detailed_job = parse_greenhouse_job(
                        detail, fallback_company=source.company
                    )
                    if detailed_job.provider_job_id != job.provider_job_id:
                        raise ProviderParseError(
                            "Greenhouse detail response id does not match requested job"
                        )
                    job = detailed_job
                except Exception as exc:  # Optional evidence must not erase a complete list.
                    warnings.append(
                        f"Publication evidence unavailable for job {job.provider_job_id}: "
                        f"{type(exc).__name__}: {exc}"
                    )
            jobs.append(job)

        return FetchResult(
            jobs=tuple(jobs),
            request_count=request_count,
            warnings=tuple(warnings),
            provider_metadata={
                "reported_total": meta["total"],
                "prospect_posts_excluded": prospect_count,
                "location_preflight_requests": 1,
            },
        )

