from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from ats_finder.config import Settings
from ats_finder.db import Database
from ats_finder.http_client import RespectfulHTTPClient
from ats_finder.models import SourceInput, SourceSupportStatus
from ats_finder.sources import parse_dataset_csv


DATASET_NAME = "latmay/ats-career-page-urls"
DATASET_FILE = "ats_career_page_urls.csv"
_REVISION_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


@dataclass(frozen=True, slots=True)
class DatasetPayload:
    text: str
    request_url: str
    resolved_url: str
    revision: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ImportReport:
    unique_sources: int
    supported_sources: int
    unsupported_sources: int
    malformed_rows: int
    source_revision: str
    source_sha256: str
    errors: tuple[str, ...]


def dataset_url(revision: str = "main") -> str:
    if not revision or not _REVISION_RE.fullmatch(revision):
        raise ValueError("dataset revision contains unsupported characters")
    encoded = quote(revision, safe="/._-")
    return (
        f"https://huggingface.co/datasets/{DATASET_NAME}/resolve/"
        f"{encoded}/{DATASET_FILE}"
    )


def _resolved_revision(url: str, fallback: str) -> str:
    path = urlsplit(url).path
    match = re.search(r"/resolve/([^/]+)/", path)
    return match.group(1) if match else fallback


async def download_dataset(
    settings: Settings,
    *,
    revision: str = "main",
    client: RespectfulHTTPClient | None = None,
) -> DatasetPayload:
    request_url = dataset_url(revision)
    owns_client = client is None
    active_client = client or RespectfulHTTPClient(settings)
    try:
        response = await active_client.get(request_url, headers={"Accept": "text/csv"})
    finally:
        if owns_client:
            await active_client.aclose()
    content = response.content
    if not content:
        raise ValueError("Hugging Face dataset response was empty")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Hugging Face dataset is not UTF-8 CSV") from exc
    resolved_url = str(response.url)
    return DatasetPayload(
        text=text,
        request_url=request_url,
        resolved_url=resolved_url,
        revision=(
            response.headers.get("x-repo-commit")
            or _resolved_revision(resolved_url, revision)
        ),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def load_local_dataset(path: Path) -> DatasetPayload:
    content = path.read_bytes()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path} is not UTF-8 CSV") from exc
    digest = hashlib.sha256(content).hexdigest()
    return DatasetPayload(
        text=text,
        request_url=str(path.resolve()),
        resolved_url=str(path.resolve()),
        revision=f"local-sha256:{digest}",
        sha256=digest,
    )


def import_payload(
    database: Database,
    payload: DatasetPayload,
    *,
    imported_at: datetime | None = None,
) -> ImportReport:
    observed_at = (imported_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    candidates, errors = parse_dataset_csv(
        payload.text,
        dataset_name=DATASET_NAME,
        dataset_revision=payload.revision,
    )
    supported = 0
    unsupported = 0
    source_inputs: list[SourceInput] = []
    for candidate in candidates:
        status = SourceSupportStatus(candidate.support_status.upper())
        supported += status is SourceSupportStatus.SUPPORTED
        unsupported += status is SourceSupportStatus.UNSUPPORTED
        source_inputs.append(
            SourceInput(
                provider=candidate.provider,
                company=candidate.company,
                board_identifier=candidate.board_identifier,
                career_page_url=candidate.canonical_url,
                support_status=status,
                support_reason=candidate.support_reason,
                source_url=payload.resolved_url,
                dataset_name=DATASET_NAME,
                dataset_revision=payload.revision,
                dataset_record_id=hashlib.sha256(
                    candidate.canonical_url.encode("utf-8")
                ).hexdigest(),
                imported_at=observed_at,
            )
        )
    database.bulk_upsert_sources(source_inputs, now=observed_at)
    return ImportReport(
        unique_sources=len(candidates),
        supported_sources=supported,
        unsupported_sources=unsupported,
        malformed_rows=len(errors),
        source_revision=payload.revision,
        source_sha256=payload.sha256,
        errors=tuple(errors),
    )

