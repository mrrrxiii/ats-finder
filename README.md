# Canada Java ATS Finder

An evidence-first job discovery service for Java roles that are either in the
Ottawa area or explicitly remote-eligible from Canada. It monitors official
public ATS boards, stores complete snapshots in SQLite, and keeps verified
publication time separate from first observation time.

The application supports Greenhouse, Lever, Ashby, SmartRecruiters, and
Workable. Every other imported ATS remains visible as `UNSUPPORTED`; it is not
silently dropped or presented as covered. See [ATS support](docs/ATS_SUPPORT.md)
for provider-specific evidence and completeness rules.

## What counts as a recent result

A job enters the main results only when all three conditions hold:

1. `Java` occurs as a standalone, case-insensitive word in the title or full
   description. `JavaScript`, JVM-only, and Spring-only text do not match.
2. The role is in Ottawa, Kanata, or Stittsville, or the public posting or
   structured ATS data explicitly establishes remote eligibility from Canada.
   Ambiguous remote roles become `REVIEW_LOCATION` and are excluded.
3. One of these independent evidence paths qualifies:

   - `VERIFIED_RECENT`: a documented ATS publication or republication timestamp
     is between `now - 24 hours` and `now` in UTC.
   - `NEWLY_DETECTED`: the stable job identifier was absent from the preceding
     successful complete snapshot of the same board, that preceding snapshot
     is at most 24 hours older, and detection occurred within the current
     rolling 24-hour display window.

An initial scan establishes a `BASELINE`; it does not claim that undated jobs
are new. An undated addition after a snapshot gap over 24 hours becomes
`NEWLY_DETECTED_TIME_UNCERTAIN` and is excluded. Failed or incomplete scans do
not replace the comparison baseline and cannot remove jobs.

`updated_at`, HTTP `Last-Modified`, search indexing time, and an ATS creation
time are never substituted for publication time. An independently verified old
publication timestamp does not fall back to snapshot newness. Potential reposts
are linked and excluded unless the new record independently carries a verified
recent publication timestamp.

## Requirements

- Python 3.12 or newer
- Network access to Hugging Face for the source import and to the supported ATS
  public endpoints for scanning
- SQLite (provided by Python)

## Exact setup commands

From this repository:

```bash
cd /home/mrrrxiii/ats-finder
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[test]'
```

Select one persistent database path for the CLI, worker, and dashboard:

```bash
export ATS_FINDER_DB=/home/mrrrxiii/ats-finder/data/ats_finder.sqlite3
.venv/bin/ats-finder init-db
```

Import the verified dataset revision used during development:

```bash
.venv/bin/ats-finder import-sources \
  --revision ff47d017ce3e0018e164eed1c4f820d07ec9fb40
```

The importer downloads `ats_career_page_urls.csv`, records the resolved commit
and SHA-256 digest, canonicalizes URLs, and deduplicates by provider plus stable
board identity. Re-running the same import is idempotent.

Run a small due batch to establish initial baselines:

```bash
.venv/bin/ats-finder-worker --once --batch-size 8
```

Run the recurring scanner in a dedicated terminal or service:

```bash
export ATS_FINDER_DB=/home/mrrrxiii/ats-finder/data/ats_finder.sqlite3
.venv/bin/ats-finder-worker --log-level INFO
```

Run the dashboard in another terminal:

```bash
cd /home/mrrrxiii/ats-finder
export ATS_FINDER_DB=/home/mrrrxiii/ats-finder/data/ats_finder.sqlite3
.venv/bin/streamlit run streamlit_app.py
```

Use one recurring worker process per database. The worker already scans boards
concurrently within that process. SQLite uniqueness constraints prevent
duplicate source/job identities, and per-source async locks prevent overlapping
scans in the process.

## CLI

The optional global `--database` argument must precede the subcommand:

```bash
.venv/bin/ats-finder --database /tmp/demo.sqlite3 init-db
.venv/bin/ats-finder --database /tmp/demo.sqlite3 add-source \
  https://jobs.lever.co/leverdemo --platform Lever --company 'Lever Demo'
.venv/bin/ats-finder --database /tmp/demo.sqlite3 scan-source 1
.venv/bin/ats-finder --database /tmp/demo.sqlite3 scan-due --limit 8
.venv/bin/ats-finder --database /tmp/demo.sqlite3 recent
.venv/bin/ats-finder --database /tmp/demo.sqlite3 recent --csv
.venv/bin/ats-finder --database /tmp/demo.sqlite3 recent --unified-sort
.venv/bin/ats-finder --database /tmp/demo.sqlite3 support-report
.venv/bin/ats-finder --database /tmp/demo.sqlite3 set-status 1 APPLIED
```

Application statuses are `NOT_APPLIED`, `SAVED`, `APPLIED`, `INTERVIEWING`,
`OFFER`, `REJECTED`, `WITHDRAWN`, and `CLOSED`. Every status transition is
audited.

## Dashboard

- **Latest Java Openings** separates verified publication evidence from
  snapshot detection evidence, supports the required evidence-specific sort,
  exports CSV, displays job details, links only to official application URLs,
  and updates application status.
- **Scan Coverage** separates supported, unsupported, failed, incomplete,
  never-scanned, and stale sources. It reports observed scan frequency and job
  evidence totals.
- **Job History** shows first/last observation, removal/reappearance/repost
  state, every scan observation, immutable content versions, and application
  status history, with CSV export.

## Scheduler and failure behavior

The target interval is one hour by default, not a guarantee that every imported
board will finish hourly. Actual throughput depends on source count, provider
latency, rate limits, retries, and configured concurrency.

- Requests are serialized per host with a configurable minimum interval.
- HTTP 408, 425, 429, 500, 502, 503, and 504 responses and transport timeouts use
  bounded exponential retry; `Retry-After` is honored.
- Provider pagination must prove completeness. Parse or pagination uncertainty
  records an `INCOMPLETE` scan.
- A complete board snapshot, its observations, job events, versions, and source
  checkpoint commit atomically. Each completed board is therefore an
  incremental checkpoint.
- On restart, the worker reads persisted `next_scan_at` and last-attempt state.
- Repeated board failures back off up to 32 scan intervals.
- Fair ordering combines configured priority with unbounded overdue age, so a
  low-priority due board eventually outranks repeatedly scanned boards.
- SQLite WAL mode, immediate write transactions, stable composite identities,
  and unique constraints protect persisted state.

## Configuration

| Environment variable | Default | Meaning |
|---|---:|---|
| `ATS_FINDER_DB` | `data/ats_finder.sqlite3` | Shared SQLite path |
| `ATS_FINDER_SCAN_INTERVAL_SECONDS` | `3600` | Target source interval; minimum 60 |
| `ATS_FINDER_STALE_AFTER_INTERVALS` | `2.0` | Snapshot becomes stale after this many target intervals |
| `ATS_FINDER_CONCURRENCY` | `8` | Concurrent board scans |
| `ATS_FINDER_HTTP_TIMEOUT_SECONDS` | `20.0` | HTTPX operation timeout |
| `ATS_FINDER_MAX_RETRIES` | `3` | Retries after the initial request |
| `ATS_FINDER_BACKOFF_BASE_SECONDS` | `0.5` | Exponential retry base |
| `ATS_FINDER_PER_HOST_INTERVAL_SECONDS` | `0.25` | Minimum delay between requests to one host |
| `ATS_FINDER_MAX_PAGES` | `1000` | Pagination safety ceiling |
| `ATS_FINDER_LEVER_PAGE_SIZE` | `100` | Lever page size |
| `ATS_FINDER_USER_AGENT` | `CanadaJavaATSFinder/1.0 (+respectful public job-board monitor)` | HTTP user agent |

Example conservative settings:

```bash
export ATS_FINDER_CONCURRENCY=4
export ATS_FINDER_PER_HOST_INTERVAL_SECONDS=0.5
export ATS_FINDER_HTTP_TIMEOUT_SECONDS=15
export ATS_FINDER_MAX_RETRIES=2
```

## Architecture

```text
Hugging Face source CSV
        |
        v
canonical source registry ---- unsupported-source reporting
        |
        v
official provider adapters -> complete normalized board snapshot
        |                         |
        | failure/incomplete      | certified complete
        v                         v
scan audit only          atomic SQLite comparison + checkpoint
                                  |
                  +---------------+----------------+
                  v                                v
          publication evidence             snapshot evidence
                  +---------------+----------------+
                                  v
                    Java + confirmed location filter
                                  |
                                  v
                         CLI / Streamlit dashboard
```

Provider adapters only fetch and normalize. `ScanEngine` owns comparison and
classification. `Database` owns transactional persistence and immutable audit
streams. `BoardScanner` converts HTTP/provider outcomes into success, failure,
or incomplete scan records. `worker.py` supplies recurring fair scheduling.

The schema is documented in [docs/SCHEMA.md](docs/SCHEMA.md); executable DDL and
migration logic live in `ats_finder/db.py`.

## Tests

Run deterministic tests without network access:

```bash
.venv/bin/python -m pytest -q
```

Run only the bounded live provider checks:

```bash
.venv/bin/python -m pytest -q -m live --run-live
```

Run everything, including live checks:

```bash
.venv/bin/python -m pytest -q --run-live
```

Live tests have a 60-second outer bound per provider and use lower retry/page
limits. They are opt-in because public endpoints and network conditions change.
The exact boards and last verified result are recorded in
[docs/ATS_SUPPORT.md](docs/ATS_SUPPORT.md).

## Dataset attribution

Source-board discovery uses
[Latmay's ATS Career Page URLs](https://huggingface.co/datasets/latmay/ats-career-page-urls),
licensed CC BY 4.0. The source is a discovery seed, not proof that a board is
live or that its contents were scanned. Import provenance, resolved revision,
and content hash are persisted for auditability.

This project does not submit applications or bypass authentication, access
controls, CAPTCHAs, or provider rate limits.
