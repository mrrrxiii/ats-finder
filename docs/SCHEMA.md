# SQLite Schema

Schema version: **7**. The executable schema, indexes, triggers, v1-to-v7
migrations, and row mappings are in `ats_finder/db.py`. Version 7 makes
`job_events` a view over `job_observations` and resolves both observation and
current-job content from `job_versions`. Existing duplicated content can be
compacted while retaining every observation and the canonical immutable
version using `ats-finder compact-history --vacuum`. Version 6 stopped storing
observation descriptions directly. Version 5 adds the
`canada_presence` source classification. A board with active jobs but no
Canadian location is marked `NONE` and rechecked after 30 days instead of the
normal interval. Version 4 adds the `source_exclusions` table, an operator
policy that keeps a board out of every future scan without deleting its
history. Version 3 added the `idx_jobs_repost_link`,
`idx_observations_repost_link`, and `idx_job_events_repost_link` indexes;
version 7 removes the last one with the physical event table. Timestamps are
timezone-aware UTC ISO 8601 strings ending in `Z`.

SQLite runs with foreign keys enabled, a 30-second busy timeout by default, WAL
mode for file databases, and immediate transactions for writes. A successful
complete scan and its entire comparison result commit in one transaction.

## `ats_sources`

One canonical provider board. Unique key: `(provider, board_identifier)`.

| Column | Purpose |
|---|---|
| `id` | Integer primary key |
| `provider`, `company`, `board_identifier`, `career_page_url` | Canonical board identity and display data |
| `support_status`, `support_reason` | `SUPPORTED`, `UNSUPPORTED`, or `UNKNOWN`, independently of scan state |
| `canada_presence` | `UNKNOWN`, `SOME`, or `NONE`; `NONE` boards are rechecked monthly |
| `last_successful_scan_at`, `last_attempted_scan_at`, `last_scan_result` | Complete-baseline and attempt state |
| `next_scan_at`, `consecutive_failures`, `priority` | Persistent scheduling state |
| `source_url`, `dataset_name`, `dataset_revision`, `dataset_record_id`, `imported_at` | Import provenance |
| `created_at`, `updated_at` | Record audit timestamps |

## `source_exclusions`

Operator policy that removes a board from every future scan without deleting
its source row or history. Unique key: `(provider, board_identifier)`.

| Column | Purpose |
|---|---|
| `id` | Integer primary key |
| `provider`, `board_identifier` | Stable board identity, matching `ats_sources` |
| `reason` | Operator-supplied reason |
| `excluded_at` | When the exclusion was recorded |

The scheduler's `supported_only` selection, the monitor's supported totals, the
coverage page, and `BoardScanner.scan_source()` all treat an excluded board as
ineligible. The importer skips excluded identities, so the table also survives
a dataset re-import without re-creating the source row.

## `scan_runs`

One immutable scan attempt. `source_id` references `ats_sources`.

| Column | Purpose |
|---|---|
| `id`, `source_id` | Scan identity and board |
| `started_at`, `ended_at` | Attempt interval |
| `completion_status` | `SUCCESS`, `FAILED`, or `INCOMPLETE` |
| `pagination_complete`, `record_count`, `error_information` | Completeness evidence and diagnostics |
| `previous_complete_scan_id`, `previous_complete_scan_at`, `snapshot_gap_seconds` | Exact comparison baseline and gap |

Only a successful scan has `pagination_complete=1` and can become the next
baseline.

## `jobs`

Current materialized state of every identifier ever observed. Unique keys:
`composite_identity` and `(source_id, provider_job_id)`. The current
`description` is resolved from the matching `job_versions` row using
`content_fingerprint`.

| Column group | Columns |
|---|---|
| Identity | `id`, `source_id`, `composite_identity`, `provider_job_id` |
| Content | `company`, `title`, `description`, `location`, `workplace_type`, `application_url`, `content_fingerprint` |
| Filters | `canada_remote_eligibility` (tri-state), `location_match`, `location_reason`, `java_keyword_match` |
| Publication evidence | `published_at`, `publication_source`, `publication_semantics`, `publication_is_verified` |
| Presence | `first_seen_at`, `last_seen_at`, `current_state`, `removed_at` |
| Detection evidence | `discovery_type`, `discovery_at`, `discovery_previous_scan_at`, `discovery_snapshot_gap_seconds` |
| Repost link | `potential_repost_of_job_id`, `repost_similarity` |
| Local tracking | `application_status` |
| Audit | `created_at`, `updated_at` |

## `job_observations`

The per-job decision for every successful complete snapshot. Unique key:
`(scan_id, job_id)`. It stores observation time, content fingerprint, presence,
`BASELINE`/`ADDED`/`UNCHANGED`/`CHANGED`/`REMOVED`/`REAPPEARED`/
`POTENTIAL_REPOST` status, comparison scan evidence, and repost link. Schema
version 7 leaves duplicated content and filter columns empty and resolves them
from the matching immutable `job_versions` row. Removed jobs receive an
explicit `is_present=0` observation; failed and incomplete scans receive none.

## `job_versions`

Immutable unique content states derived by trigger from present observations.
Unique keys are `first_observation_id` and `(job_id, content_fingerprint)`.
Each version includes the full normalized content, filter decisions,
publication evidence, official application URL, first scan, and recorded time.

## `job_events`

Read-only view exposing every observation as an immutable event. It uses the
same identifiers, event type, presence, fingerprint, discovery type,
comparison scan/gap, and optional repost link, but stores no duplicate rows.
This gives the dashboard a stable change/removal/reappearance history without
inferring it from current rows.

## `application_status_history`

Immutable local application-tracking audit. It records previous/new status,
effective/recorded times, and `JOB_CREATED`, `USER_UPDATE`, or
`MIGRATION_BASELINE` change kind. Insert/update triggers keep it synchronized
with `jobs.application_status`.

## `schema_metadata`

Key/value table containing the current `schema_version`. Database open runs
idempotent initialization and audit-table backfill before advancing the version.

## Important constraints and indexes

- Publication semantics intentionally have no `updated_at` option.
- Boolean and tri-state columns use SQLite `CHECK` constraints.
- Similarity scores are constrained to `[0, 1]`; gaps and counts cannot be
  negative.
- Foreign keys cascade source/job deletion into their dependent audit records.
- `source_exclusions` is keyed by `(provider, board_identifier)` and is not
  affected by source deletion, re-insertion, or dataset re-import.
- Scheduling, scan baseline lookup, current source state, recent evidence,
  observation history, versions, application history, and repost links have
  dedicated indexes. The recent-jobs partial index covers only active,
  Java-confirmed, location-confirmed jobs.
