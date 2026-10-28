from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from datetime import datetime, timezone

from ats_finder.config import Settings
from ats_finder.db import Database
from ats_finder.scanner import BoardScanner


LOGGER = logging.getLogger("ats_finder.worker")


async def run_worker(
    settings: Settings,
    *,
    once: bool = False,
    batch_size: int | None = None,
) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(event, stop.set)
        except NotImplementedError:  # Windows event loops may not support this.
            pass

    completed_scans = 0
    effective_batch = batch_size or max(settings.concurrency * 4, settings.concurrency)
    with Database(settings.database_path) as database:
        async with BoardScanner(database, settings) as scanner:
            while not stop.is_set():
                try:
                    attempts = await scanner.scan_due(limit=effective_batch)
                except ExceptionGroup as group:
                    for error in group.exceptions:
                        LOGGER.error(
                            "unexpected scanner failure: %s: %s",
                            type(error).__name__,
                            error,
                        )
                    attempts = []
                for attempt in attempts:
                    completed_scans += 1
                    LOGGER.info(
                        "source=%s provider=%s status=%s records=%s requests=%s elapsed=%.2fs",
                        attempt.source.id,
                        attempt.source.provider,
                        attempt.outcome.status.value,
                        attempt.outcome.record_count,
                        attempt.request_count,
                        attempt.elapsed_seconds,
                    )
                    for warning in attempt.warnings:
                        LOGGER.warning("source=%s %s", attempt.source.id, warning)
                if once:
                    return completed_scans
                if attempts:
                    await asyncio.sleep(0)
                    continue

                sources = scanner.database.list_sources()
                future_due = [
                    source.next_scan_at
                    for source in sources
                    if source.next_scan_at is not None
                ]
                now = datetime.now(timezone.utc)
                delay = 60.0
                if future_due:
                    delay = max(0.25, min(60.0, (min(future_due) - now).total_seconds()))
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
    return completed_scans


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the persistent ATS board scanner")
    parser.add_argument("--once", action="store_true", help="scan one due batch and exit")
    parser.add_argument("--batch-size", type=int, help="maximum sources selected per batch")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.batch_size is not None and args.batch_size <= 0:
        raise SystemExit("--batch-size must be positive")
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    count = asyncio.run(
        run_worker(
            Settings.from_env(),
            once=args.once,
            batch_size=args.batch_size,
        )
    )
    if args.once:
        print(f"Completed {count} scan(s).")


if __name__ == "__main__":
    main()

