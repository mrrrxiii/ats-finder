from __future__ import annotations

from typing import Any

from ats_finder.config import Settings
from ats_finder.models import ATSSource, NormalizedJob, WorkplaceType
from ats_finder.providers.base import (
    FetchResult,
    PaginationError,
    ProviderParseError,
    RespectfulAsyncClient,
)
from ats_finder.providers.text import html_to_text


def _workplace_type(value: object) -> WorkplaceType:
    normalized = str(value or "").strip().casefold().replace("_", "-")
    if normalized == "remote":
        return WorkplaceType.REMOTE
    if normalized == "hybrid":
        return WorkplaceType.HYBRID
    if normalized in {"on-site", "onsite"}:
        return WorkplaceType.ONSITE
    return WorkplaceType.UNKNOWN


def _lever_description(item: dict[str, object]) -> str:
    parts: list[str] = []
    primary = item.get("descriptionPlain")
    if isinstance(primary, str) and primary.strip():
        parts.append(primary.strip())
    else:
        for field in ("openingPlain", "descriptionBodyPlain"):
            value = item.get(field)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
    lists = item.get("lists")
    if isinstance(lists, list):
        for block in lists:
            if not isinstance(block, dict):
                continue
            heading = block.get("text")
            content = block.get("content")
            rendered = html_to_text(content)
            if isinstance(heading, str) and heading.strip():
                parts.append(heading.strip())
            if rendered:
                parts.append(rendered)
    additional = item.get("additionalPlain")
    if isinstance(additional, str) and additional.strip():
        parts.append(additional.strip())
    return "\n\n".join(parts)


def parse_lever_job(item: object, *, fallback_company: str) -> NormalizedJob:
    if not isinstance(item, dict):
        raise ProviderParseError("Lever job entry is not an object")
    provider_id = item.get("id")
    title = item.get("text")
    url = item.get("applyUrl") or item.get("hostedUrl")
    if not isinstance(provider_id, str) or not provider_id:
        raise ProviderParseError("Lever job is missing its id")
    if (
        not isinstance(title, str)
        or not title.strip()
        or not isinstance(url, str)
        or not url.strip()
    ):
        raise ProviderParseError("Lever job is missing text/title or official URL")

    categories = item.get("categories")
    locations: list[str] = []
    if isinstance(categories, dict):
        all_locations = categories.get("allLocations")
        if isinstance(all_locations, list):
            locations.extend(
                value.strip()
                for value in all_locations
                if isinstance(value, str) and value.strip()
            )
        primary = categories.get("location")
        if isinstance(primary, str) and primary.strip() and primary.strip() not in locations:
            locations.insert(0, primary.strip())
    country = item.get("country")
    country_code = country.upper() if isinstance(country, str) else None
    country_label = {"CA": "Canada", "US": "United States"}.get(country_code or "")
    if country_label and all(country_label.casefold() not in value.casefold() for value in locations):
        locations.append(country_label)

    workplace = _workplace_type(item.get("workplaceType"))
    remote_eligibility: bool | None = None
    if workplace in {WorkplaceType.REMOTE, WorkplaceType.HYBRID}:
        if country_code == "CA":
            remote_eligibility = True
        elif country_code and country_code != "CA":
            remote_eligibility = False

    # Lever may currently emit createdAt, but it is not in the official Postings
    # API field contract. It is intentionally not used as publication evidence.
    return NormalizedJob(
        provider_job_id=provider_id,
        title=title.strip(),
        description=_lever_description(item),
        location="; ".join(dict.fromkeys(locations)),
        application_url=url,
        company=fallback_company,
        workplace_type=workplace,
        canada_remote_eligibility=remote_eligibility,
        published_at=None,
        publication_source=None,
        publication_semantics=None,
        publication_is_verified=False,
    )


class LeverAdapter:
    provider = "lever"

    @staticmethod
    def _site_and_domain(source: ATSSource) -> tuple[str, str]:
        identifier = source.board_identifier
        if identifier.startswith("eu:"):
            return identifier.split(":", 1)[1], "api.eu.lever.co"
        if identifier.startswith("global:"):
            identifier = identifier.split(":", 1)[1]
        return identifier, "api.lever.co"

    async def fetch(
        self,
        source: ATSSource,
        client: RespectfulAsyncClient,
        settings: Settings,
    ) -> FetchResult:
        site, domain = self._site_and_domain(source)
        if not site:
            raise ProviderParseError("Lever source has an empty site identifier")
        url = f"https://{domain}/v0/postings/{site}"
        raw_jobs: list[object] = []
        seen_ids: set[str] = set()
        request_count = 0

        for page_index in range(settings.max_pages):
            response = await client.get(
                url,
                params={
                    "mode": "json",
                    "skip": page_index * settings.lever_page_size,
                    "limit": settings.lever_page_size,
                },
                headers={"Accept": "application/json"},
            )
            request_count += 1
            try:
                page: Any = response.json()
            except ValueError as exc:
                raise ProviderParseError("Lever page is not valid JSON") from exc
            if not isinstance(page, list):
                raise ProviderParseError("Lever page is not a JSON array")
            if len(page) > settings.lever_page_size:
                raise PaginationError("Lever returned more records than the requested limit")

            for item in page:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    raise ProviderParseError("Lever page contains a job without a string id")
                job_id = item["id"]
                if job_id in seen_ids:
                    raise PaginationError(f"Lever repeated job id {job_id} across pages")
                seen_ids.add(job_id)
                raw_jobs.append(item)

            if len(page) < settings.lever_page_size:
                break
        else:
            raise PaginationError(
                f"Lever pagination exceeded the configured {settings.max_pages} page limit"
            )

        jobs = tuple(
            parse_lever_job(item, fallback_company=source.company) for item in raw_jobs
        )
        return FetchResult(
            jobs=jobs,
            request_count=request_count,
            provider_metadata={"pages": request_count},
        )

