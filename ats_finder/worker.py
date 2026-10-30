from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from contextlib import ExitStack, suppress
from datetime import datetime, timezone

from ats_finder.config import Settings
from ats_finder.db import Database
from ats_finder.monitor import monitoring_session
from ats_finder.scanner import BoardScanner


LOGGER = logging.getLogger("ats_finder.worker")


async def run_worker(
    settings: Settings,
    *,
    once: bool = False,
    until_complete: bool = False,
    batch_size: int | None = None,
    stop_event: asyncio.Event | None = None,
) -> int:
    if once and until_complete:
        raise ValueError("once and until_complete are mutually exclusive")
    stop = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(event, stop.set)
        except NotImplementedError:  # Windows event loops may not support this.
            pass

    completed_scans = 0
    effective_batch = batch_size or max(settings.concurrency * 4, settings.concurrency)
    LOGGER.info(
        "worker starting database=%s concurrency=%s batch=%s",
        settings.database_path,
        settings.concurrency,
        effective_batch,
    )
    with Database(settings.database_path) as database, ExitStack() as stack:
        stack.enter_context(monitoring_session(settings))
        async with BoardScanner(database, settings) as scanner:
            while not stop.is_set():
                scan_task = asyncio.create_task(
                    scanner.scan_due(limit=effective_batch)
                )
                stop_task = asyncio.create_task(stop.wait())
                done, _ = await asyncio.wait(
                    {scan_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_task in done:
                    LOGGER.info("stop requested; cancelling in-flight scans")
                    scan_task.cancel()
                    try:
                        attempts = await scan_task
                    except asyncio.CancelledError:
                        attempts = []
                    except ExceptionGroup as group:
                        for error in group.exceptions:
                            if not isinstance(error, asyncio.CancelledError):
                                LOGGER.error(
                                    "unexpected scanner failure: %s: %s",
                                    type(error).__name__,
                                    error,
                                )
                        attempts = []
                else:
                    stop_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await stop_task
                    try:
                        attempts = scan_task.result()
                    except ExceptionGroup as group:
                        unexpected: list[Exception] = []
                        for error in group.exceptions:
                            if not isinstance(error, asyncio.CancelledError):
                                LOGGER.error(
                                    "unexpected scanner failure: %s: %s",
                                    type(error).__name__,
                                    error,
                                )
                                if isinstance(error, Exception):
                                    unexpected.append(error)
                        if unexpected and (once or until_complete):
                            raise ExceptionGroup(
                                "scanner batch failed before all due sources completed",
                                unexpected,
                            ) from group
                        attempts = []

                for attempt in attempts:
                    completed_scans += 1
                    LOGGER.info(
                        "source=%s provider=%s status=%s records=%s requests=%s "
                        "presence=%s elapsed=%.2fs",
                        attempt.source.id,
                        attempt.source.provider,
                        attempt.outcome.status.value,
                        attempt.outcome.record_count,
                        attempt.request_count,
                        attempt.source.canada_presence.value,
                        attempt.elapsed_seconds,
                    )
                    for warning in attempt.warnings:
                        LOGGER.warning("source=%s %s", attempt.source.id, warning)
                if once:
                    return completed_scans
                if stop.is_set():
                    break
                if attempts:
                    await asyncio.sleep(0)
                    continue
                if until_complete:
                    LOGGER.info(
                        "scan pass complete: %d scan(s) processed; "
                        "no due sources remain",
                        completed_scans,
                    )
                    return completed_scans

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
                LOGGER.info("no due sources; sleeping up to %.1fs", delay)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
    return completed_scans


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the persistent ATS board scanner")
    parser.add_argument("--once", action="store_true", help="scan one due batch and exit")
    parser.add_argument(
        "--until-complete",
        action="store_true",
        help="scan every currently due source, then exit",
    )
    parser.add_argument("--batch-size", type=int, help="maximum sources selected per batch")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.batch_size is not None and args.batch_size <= 0:
        raise SystemExit("--batch-size must be positive")
    if args.once and args.until_complete:
        raise SystemExit("--once and --until-complete are mutually exclusive")
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    count = asyncio.run(
        run_worker(
            Settings.from_env(),
            once=args.once,
            until_complete=args.until_complete,
            batch_size=args.batch_size,
        )
    )
    if args.once or args.until_complete:
        print(f"Completed {count} scan(s).")


if __name__ == "__main__":
    main()

