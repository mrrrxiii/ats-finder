# SQLite Schema

Schema version: **3**. The executable schema, indexes, triggers, v1-to-v2
backfill, and row mappings are in `ats_finder/db.py`. Version 3 adds the
`idx_jobs_repost_link`, `idx_observations_repost_link`, and
`idx_job_events_repost_link` indexes so retention can clear repost links with
indexed lookups instead of full table scans. Timestamps are timezone-aware UTC
ISO 8601 strings ending in `Z`.

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
| `last_successful_scan_at`, `last_attempted_scan_at`, `last_scan_result` | Complete-baseline and attempt state |
| `next_scan_at`, `consecutive_failures`, `priority` | Persistent scheduling state |
| `source_url`, `dataset_name`, `dataset_revision`, `dataset_record_id`, `imported_at` | Import provenance |
| `created_at`, `updated_at` | Record audit timestamps |

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
`composite_identity` and `(source_id, provider_job_id)`.

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

The full per-job result of every successful complete snapshot. Unique key:
`(scan_id, job_id)`. It stores observation time, content fingerprint, presence,
`BASELINE`/`ADDED`/`UNCHANGED`/`CHANGED`/`REMOVED`/`REAPPEARED`/
`POTENTIAL_REPOST` status, comparison scan evidence, and a copy of the
normalized job/filter/publication fields needed to reconstruct the decision.
Removed jobs receive an explicit `is_present=0` observation; failed and
incomplete scans receive none.

## `job_versions`

Immutable unique content states derived by trigger from present observations.
Unique keys are `first_observation_id` and `(job_id, content_fingerprint)`.
Each version includes the full normalized content, filter decisions,
publication evidence, official application URL, first scan, and recorded time.

## `job_events`

Immutable event stream derived by trigger from every observation. One event per
observation records event type, presence, fingerprint, discovery type,
comparison scan/gap, and optional repost link. This gives the dashboard a
stable change/removal/reappearance history without inferring it from current
rows.

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
- Scheduling, scan baseline lookup, current source state, recent evidence,
  observation history, event history, versions, application history, and repost
  links have dedicated indexes.
