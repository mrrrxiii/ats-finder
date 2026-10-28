from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from ats_finder.config import Settings
from ats_finder.db import Database
from ats_finder.engine import ScanEngine
from ats_finder.importer import download_dataset, import_payload, load_local_dataset
from ats_finder.models import ApplicationStatus, SourceInput, SourceSupportStatus
from ats_finder.scanner import BoardScanner
from ats_finder.sources import source_candidate


def _json_default(value: object) -> object:
    if isinstance(value, (datetime, Path)):
        return str(value)
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"cannot encode {type(value).__name__}")


def _print_json(value: object) -> None:
    print(json.dumps(value, default=_json_default, indent=2, ensure_ascii=False))


def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    if args.database is not None:
        settings = replace(settings, database_path=Path(args.database))
    return settings


async def _import_command(args: argparse.Namespace, settings: Settings) -> None:
    payload = (
        load_local_dataset(Path(args.file))
        if args.file
        else await download_dataset(settings, revision=args.revision)
    )
    with Database(settings.database_path) as database:
        report = import_payload(database, payload)
    _print_json(report)


async def _scan_source(args: argparse.Namespace, settings: Settings) -> None:
    with Database(settings.database_path) as database:
        async with BoardScanner(database, settings) as scanner:
            result = await scanner.scan_source(args.source_id)
    _print_json(result)


async def _scan_due(args: argparse.Namespace, settings: Settings) -> None:
    with Database(settings.database_path) as database:
        async with BoardScanner(database, settings) as scanner:
            results = await scanner.scan_due(limit=args.limit)
    _print_json(results)


def _add_source(args: argparse.Namespace, settings: Settings) -> None:
    candidate = source_candidate(args.url, args.platform)
    company = args.company or candidate.company
    with Database(settings.database_path) as database:
        source = database.upsert_source(
            SourceInput(
                provider=candidate.provider,
                company=company,
                board_identifier=candidate.board_identifier,
                career_page_url=candidate.canonical_url,
                support_status=SourceSupportStatus(candidate.support_status.upper()),
                support_reason=candidate.support_reason,
                source_url="manual",
                imported_at=datetime.now(timezone.utc),
            )
        )
    _print_json(source)


def _recent(args: argparse.Namespace, settings: Settings) -> None:
    with Database(settings.database_path) as database:
        rows = ScanEngine(database).recent_jobs(unified_sort=args.unified_sort)
    if args.csv:
        fieldnames = [
            "company",
            "title",
            "location",
            "workplace_type",
            "discovery_type",
            "published_at",
            "first_seen_at",
            "detection_at",
            "previous_board_scan_at",
            "provider",
            "application_url",
        ]
        writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            data = asdict(row)
            writer.writerow({key: data[key] for key in fieldnames})
    else:
        _print_json(rows)


def _support_report(settings: Settings) -> None:
    with Database(settings.database_path) as database:
        sources = database.list_sources()
    report: dict[str, dict[str, int]] = {}
    for source in sources:
        provider = report.setdefault(
            source.provider,
            {
                "supported": 0,
                "unsupported": 0,
                "unknown": 0,
                "failed": 0,
                "incomplete": 0,
            },
        )
        provider[source.support_status.value.casefold()] += 1
        if source.last_scan_result.value == "FAILED":
            provider["failed"] += 1
        elif source.last_scan_result.value == "INCOMPLETE":
            provider["incomplete"] += 1
    _print_json(report)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Canada Java ATS discovery engine")
    parser.add_argument("--database", help="SQLite path (defaults to ATS_FINDER_DB)")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init-db", help="initialize the SQLite schema")

    importer = commands.add_parser("import-sources", help="import and deduplicate ATS boards")
    source = importer.add_mutually_exclusive_group()
    source.add_argument("--file", help="local dataset CSV instead of downloading")
    importer.add_argument("--revision", default="main", help="Hugging Face revision or commit")

    add = commands.add_parser("add-source", help="add one canonical ATS career URL")
    add.add_argument("url")
    add.add_argument("--platform", required=True)
    add.add_argument("--company")

    scan = commands.add_parser("scan-source", help="scan one source by database id")
    scan.add_argument("source_id", type=int)

    due = commands.add_parser("scan-due", help="scan a fair batch of due sources")
    due.add_argument("--limit", type=int)

    recent = commands.add_parser("recent", help="show confirmed jobs in the rolling window")
    recent.add_argument("--unified-sort", action="store_true")
    recent.add_argument("--csv", action="store_true")

    status = commands.add_parser("set-status", help="update local application tracking")
    status.add_argument("job_id", type=int)
    status.add_argument("status", choices=[item.value for item in ApplicationStatus])

    commands.add_parser("support-report", help="separate support and scan outcomes")
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    settings = _settings(args)
    if args.command == "init-db":
        with Database(settings.database_path) as database:
            print(f"Initialized schema version {database.schema_version} at {settings.database_path}")
    elif args.command == "import-sources":
        asyncio.run(_import_command(args, settings))
    elif args.command == "add-source":
        _add_source(args, settings)
    elif args.command == "scan-source":
        asyncio.run(_scan_source(args, settings))
    elif args.command == "scan-due":
        if args.limit is not None and args.limit <= 0:
            parser.error("--limit must be positive")
        asyncio.run(_scan_due(args, settings))
    elif args.command == "recent":
        _recent(args, settings)
    elif args.command == "set-status":
        with Database(settings.database_path) as database:
            job = ScanEngine(database).set_application_status(
                args.job_id, ApplicationStatus(args.status)
            )
        _print_json(job)
    elif args.command == "support-report":
        _support_report(settings)
    else:  # pragma: no cover - argparse enforces a known subcommand.
        parser.error(f"unknown command {args.command}")


if __name__ == "__main__":
    main()

