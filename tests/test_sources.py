from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import httpx

from ats_finder.config import Settings
from ats_finder.db import Database
from ats_finder.http_client import RespectfulHTTPClient
from ats_finder.importer import DatasetPayload, download_dataset, import_payload
from ats_finder.sources import canonicalize_url, parse_dataset_csv, source_candidate

from conftest import FIXED_NOW


def test_dataset_csv_import_deduplicates_boards_and_reports_bad_rows() -> None:
    csv_content = """canonical_url,ats_platform
http://boards.greenhouse.io/acme/,Greenhouse
https://boards.greenhouse.io/acme,Greenhouse
https://jobs.eu.lever.co/example,Lever
https://example.wd5.myworkdayjobs.com/en-US/Careers,Workday
,Lever
"""

    candidates, errors = parse_dataset_csv(
        csv_content,
        dataset_revision="fixture-revision",
    )

    assert len(candidates) == 3
    by_provider = {candidate.provider: candidate for candidate in candidates}
    assert by_provider["greenhouse"].board_identifier == "acme"
    assert by_provider["greenhouse"].canonical_url == (
        "https://boards.greenhouse.io/acme"
    )
    assert by_provider["greenhouse"].support_status == "supported"
    assert by_provider["lever"].board_identifier == "eu:example"
    assert by_provider["lever"].support_status == "supported"
    assert by_provider["workday"].board_identifier.startswith("url:")
    assert by_provider["workday"].support_status == "unsupported"
    assert by_provider["workday"].support_reason == (
        "No verified adapter is implemented for this ATS provider"
    )
    assert {candidate.dataset_name for candidate in candidates} == {
        "latmay/ats-career-page-urls"
    }
    assert {candidate.dataset_revision for candidate in candidates} == {
        "fixture-revision"
    }
    assert errors == ["row 6: missing canonical_url or ats_platform"]


def test_url_canonicalization_is_deterministic() -> None:
    assert canonicalize_url(
        "http://BOARDS.GREENHOUSE.IO:443/acme/?z=2&a=3&a=1#jobs"
    ) == "https://boards.greenhouse.io/acme?a=1&a=3&z=2"


def test_additional_verified_provider_urls_extract_stable_board_identifiers() -> None:
    workable = source_candidate(
        "https://apply.workable.com/acme-canada/?lng=en",
        "Workable",
    )

    assert workable.provider == "workable"
    assert workable.board_identifier == "acme-canada"
    assert workable.support_status == "supported"

    smartrecruiters = source_candidate(
        "https://jobs.smartrecruiters.com/ExampleCo",
        "SmartRecruiters",
    )

    assert smartrecruiters.provider == "smartrecruiters"
    assert smartrecruiters.board_identifier == "ExampleCo"
    assert smartrecruiters.support_status == "supported"


def test_import_payload_persists_deduplicated_sources_and_provenance(
    tmp_path: Path,
) -> None:
    content = """canonical_url,ats_platform
https://boards.greenhouse.io/acme,Greenhouse
https://boards.greenhouse.io/acme/,Greenhouse
https://jobs.lever.co/example,Lever
https://example.wd5.myworkdayjobs.com/en-US/Careers,Workday
,Lever
"""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    payload = DatasetPayload(
        text=content,
        request_url="https://huggingface.co/requested-fixture.csv",
        resolved_url="https://huggingface.co/resolved-fixture.csv",
        revision="fixture-commit-sha",
        sha256=digest,
    )
    database = Database(tmp_path / "import.sqlite3")
    try:
        first = import_payload(database, payload, imported_at=FIXED_NOW)
        second = import_payload(database, payload, imported_at=FIXED_NOW)

        assert first == second
        assert first.unique_sources == 3
        assert first.supported_sources == 2
        assert first.unsupported_sources == 1
        assert first.malformed_rows == 1
        assert first.errors == ("row 6: missing canonical_url or ats_platform",)
        assert first.source_revision == "fixture-commit-sha"
        assert first.source_sha256 == digest

        sources = database.list_sources()
        assert len(sources) == 3
        by_provider = {source.provider: source for source in sources}
        assert set(by_provider) == {"greenhouse", "lever", "workday"}
        assert by_provider["greenhouse"].board_identifier == "acme"
        assert by_provider["lever"].board_identifier == "global:example"
        assert by_provider["workday"].board_identifier.startswith("url:")
        for source in sources:
            assert source.source_url == payload.resolved_url
            assert source.dataset_name == "latmay/ats-career-page-urls"
            assert source.dataset_revision == payload.revision
            assert source.imported_at == FIXED_NOW
            assert source.dataset_record_id is not None
            assert len(source.dataset_record_id) == 64
    finally:
        database.close()


def test_dataset_download_records_resolved_commit_header() -> None:
    content = b"canonical_url,ats_platform\nhttps://boards.greenhouse.io/acme,Greenhouse\n"

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/ats_career_page_urls.csv")
        return httpx.Response(
            200,
            content=content,
            headers={"x-repo-commit": "ff47d017ce3e0018e164eed1c4f820d07ec9fb40"},
            request=request,
        )

    async def run() -> None:
        settings = Settings(per_host_interval_seconds=0)
        async with RespectfulHTTPClient(
            settings, transport=httpx.MockTransport(respond)
        ) as client:
            payload = await download_dataset(settings, client=client)
        assert payload.revision == "ff47d017ce3e0018e164eed1c4f820d07ec9fb40"
        assert payload.sha256 == hashlib.sha256(content).hexdigest()

    asyncio.run(run())


def test_operator_exclusion_survives_a_dataset_reimport(tmp_path: Path) -> None:
    content = """canonical_url,ats_platform
https://boards.greenhouse.io/acme,Greenhouse
"""
    payload = DatasetPayload(
        text=content,
        request_url="https://huggingface.co/requested-fixture.csv",
        resolved_url="https://huggingface.co/resolved-fixture.csv",
        revision="fixture-commit-sha",
        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )
    database = Database(tmp_path / "excluded.sqlite3")
    try:
        exclusion = database.add_source_exclusion(
            "greenhouse",
            "acme",
            "not suitable",
            now=FIXED_NOW,
        )
        assert exclusion["reason"] == "not suitable"

        report = import_payload(database, payload, imported_at=FIXED_NOW)
        assert report.unique_sources == 1
        assert report.supported_sources == 1
        assert report.excluded_sources == 1

        assert database.get_source_by_board("greenhouse", "acme") is None
        assert database.list_sources() == []
        assert database.is_source_excluded("greenhouse", "acme")

        assert (
            database.remove_source_exclusion("greenhouse", "acme")
            is True
        )
        second = import_payload(database, payload, imported_at=FIXED_NOW)
        assert second.excluded_sources == 0
        source = database.get_source_by_board("greenhouse", "acme")
        assert source is not None
        assert [
            item.id for item in database.list_sources(supported_only=True)
        ] == [source.id]
    finally:
        database.close()


def test_import_payload_stores_smartrecruiters_sources(tmp_path: Path) -> None:
    content = """canonical_url,ats_platform
https://boards.greenhouse.io/acme,Greenhouse
https://jobs.smartrecruiters.com/AcmeCanada,SmartRecruiters
"""
    payload = DatasetPayload(
        text=content,
        request_url="https://huggingface.co/requested-fixture.csv",
        resolved_url="https://huggingface.co/resolved-fixture.csv",
        revision="fixture-commit-sha",
        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )
    database = Database(tmp_path / "smartrecruiters.sqlite3")
    try:
        report = import_payload(database, payload, imported_at=FIXED_NOW)

        assert report.unique_sources == 2
        assert report.supported_sources == 2
        assert report.removed_sources == 0
        assert report.excluded_sources == 0
        assert {source.provider for source in database.list_sources()} == {
            "greenhouse",
            "smartrecruiters",
        }
        with database.read() as connection:
            stored = connection.execute(
                "SELECT COUNT(*) FROM ats_sources "
                "WHERE provider = 'smartrecruiters'"
            ).fetchone()[0]
        assert stored == 1
    finally:
        database.close()

