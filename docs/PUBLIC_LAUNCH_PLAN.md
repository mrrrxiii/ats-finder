# Public Launch Plan

Status: proposed

Prepared: 2026-10-30

## 1. Product goal

Publish a read-only job information platform that:

- Discovers Java roles relevant to a worldwide audience, prioritizing roles
  explicitly remote and retaining normalized location information for other
  roles.
- Stores enough job information to search and browse current opportunities.
- Links every result to the employer's official job-description page.
- Lets job seekers filter jobs without creating an account.

The public product is not an application tracker.

Working product scope:

> A worldwide search engine for Java jobs, with strong support for explicitly
> remote roles and enough location metadata to filter by city, country, or
> hiring region when the employer provides it.

The current Canada-specific public name should not be retained unless the
public scope remains Canada-only.

## 2. Public scope

Included:

- One public Jobs page.
- One job-detail page per job.
- Company, title, location, description, and date filtering.
- Work-mode, hiring-region, and job-relevance filters.
- Pagination and shareable filter URLs.
- Data-freshness information.
- A canonical "View job" link to the original ATS posting.

Excluded:

- User accounts or authentication.
- Application statuses or personal application history.
- Job notes, reminders, or saved searches.
- CSV or file downloads.
- Application submission.
- Public source-scanning diagnostics.
- User-facing audit-history and immutable-version tables.

The scanning engine, provider adapters, completeness checks, evidence
classification, and filtering rules remain internal services.

## 3. Target architecture

```text
Public ATS APIs
      |
      v
Supervised worker
      |
      v
SQLite database
  - source scheduling
  - current jobs
  - immutable job versions
  - compact operational events
  - FTS5 search index
      |
      v
FastAPI + server-rendered HTML/HTMX
      |
      v
HTTPS reverse proxy with caching and rate limiting
```

Retain the existing scanning engine and provider adapters. Replace the public
Streamlit presentation with a server-rendered search application.

The deployable system has exactly two application processes:

1. One supervised worker.
2. One supervised public web process.

Both use the same persistent SQLite volume on a single host.

### Public job fields

The search model needs normalized, nullable fields:

- `country_code`
- `region`
- `city`
- `work_mode`
- `remote_scope`
- `employment_type`
- `listing_date`
- `field_sources`

`field_sources` records whether each optional value came from provider
structure, was inferred from posting text, or is unknown.

## 4. Data retention

Initial policy decision:

```text
ATS_FINDER_JOB_RETENTION_MONTHS=6
```

Six months is the launch baseline. It must remain configurable so the operating
period can change without code changes.

Define the public listing date as:

```text
listing_date =
    verified published_at
    when available
    otherwise discovery_at
```

Retain a job when either `published_at` or `discovery_at` falls inside the
retention period. Delete a job when both timestamps are older than the cutoff.

Rules:

- Never use `last_seen_at` for retention.
- Use only verified publication timestamps for the publication path.
- Use the real first-observation time for undated jobs.
- Delete jobs and dependent versions/events in bounded batches.
- Retain source definitions and scheduling state.
- Retain scan attempts for 90 days, then prune them in bounded batches.
- Do not expose retention internals in the public UI.

The UI must label the date as either:

```text
Published <date>
First detected <date>
```

## 5. Public search filters

### Required filters

| Filter | Semantics |
|---|---|
| Company | Case-insensitive contains |
| Job title | Case-insensitive contains |
| Location | Case-insensitive contains |
| Description | Whitespace-separated terms combined with AND |
| Whole word | Applies to all text filters |
| Date | Uses `listing_date`; supports preset ranges and custom start/end |
| Include unspecified | Controls whether jobs missing optional structured data remain visible |

Whole-word behavior:

```text
Java
```

Matches `Java`, `Java-based`, and `Java.`. It does not match `JavaScript`.

Description behavior:

```text
java kubernetes
```

Requires both terms anywhere in the description.

### Recommended filters

| Filter | User value |
|---|---|
| Work mode | Remote, hybrid, onsite, unknown |
| Country | Search or select a country when location data is available |
| City or region | Search normalized city, province/state, or region values |
| Remote scope | Worldwide, multi-country/region, country-specific, or unspecified |
| Employment type | Full-time, contract, part-time, or unknown when provided |
| Seniority | Junior, intermediate, senior |
| Active only | Default on |
| New since last visit | Browser-local state; no account required |
| Sort | Newest, company, or title |
| Page size | Bounded options; no unbounded result tables |

ATS provider and evidence type are internal operational metadata. They should
not be primary public filters. The UI should instead show a plain-language date
label such as `Published` or `First detected`, and sort using that displayed
date.

Location presets must not assume Ottawa or Canada. If the public audience is
worldwide, country and region choices must come from the normalized location
data and be presented as availability filters rather than claims about legal
work authorization.

### Date presets

- Last 24 hours
- Last 7 days
- Last 30 days
- Last 90 days
- Last 6 months
- Custom range

### Missing and incomplete data

ATS feeds do not consistently provide country, city, work mode, remote scope,
or employment type. The public product must not manufacture certainty.

Rules:

- Store missing values as `NULL` or an explicit `UNKNOWN` state.
- Display `Not specified` instead of guessing.
- Keep inferred values distinguishable from structured provider values.
- Do not treat generic `Remote` as worldwide eligibility.
- Do not treat a company office location as proof of remote eligibility.
- Do not treat an incidental country mention as legal work authorization.

Filter behavior:

- Setting a work-mode, country, region, or remote-scope filter excludes unknown
  values by default.
- Provide an **Include unspecified** option so users can broaden the search.
- When that option is enabled, missing values appear in a separate clearly
  labeled group or with a visible `Not specified` badge.
- Text searches still apply to all fields regardless of whether optional
  structured filters are missing.

Data-quality monitoring should track the percentage of jobs with each missing
field so the ingestion adapters can be improved over time.

## 6. Public UX

### Jobs page

The main page contains:

1. Product title and short explanation.
2. Data-freshness indicator.
3. Filter panel.
4. Total matching count.
5. Paginated result list.
6. A "View job" link on every result.

Recommended list columns:

- Company
- Job title
- Location
- Work mode
- Published or first-detected date
- View job

The filter state must be represented in the URL:

```text
/jobs?company=acme&title=java&location=ottawa&page=2
```

### Job-detail page

Each job has a permanent URL:

```text
/jobs/{job_id}
```

The detail page includes:

- Company and title
- Location and work mode
- Description
- Published or first-detected date
- Match rationale
- Canonical "View job" link

External links must open the official job-description URL, not an `/apply` or
`/application` endpoint.

### Empty and error states

- Distinguish "no matches" from "data is stale".
- Show a retry action for transient server failures.
- Never show raw SQL exceptions or tracebacks.
- Provide loading feedback for description searches.

### Responsive behavior

- Stack filters vertically on narrow screens.
- Use cards rather than a wide table on mobile.
- Keep filters and result count visible without excessive scrolling.

## 7. Software refactor

### Phase 1: Define global ingestion and normalization

- Replace the Ottawa-or-Canada-remote eligibility rule with a worldwide scope.
- Admit jobs that contain Java and have either an explicit remote scope or
  usable location metadata.
- Normalize city, region/state, country, work mode, remote scope, and
  employment type when the provider exposes them.
- Store whether each value is structured, inferred from posting text, or
  unknown.
- Mark worldwide remote only when the posting explicitly supports it.
- Keep the Canada/Ottawa classification as an optional location facet rather
  than the product boundary.

The current location classifier rejects non-Ottawa onsite roles and labels
worldwide remote roles as ambiguous. That rule must be replaced before the
public product can be marketed as worldwide.

Exit criteria:

- A job from any country can be represented without fitting a Canada-only
  schema.
- Missing optional fields remain queryable as `Not specified`.
- Ambiguous remote roles are not marketed as worldwide-eligible.

### Phase 2: Remove out-of-scope behavior

- Remove public application-status editing.
- Remove application-status history from the public UI.
- Remove CSV/download controls.
- Remove public scan-coverage and audit-history pages.
- Replace the dashboard with a Jobs page and job-detail page.

Exit criteria:

- Public routes expose only job search and job detail.
- No public mutation endpoints exist.

### Phase 3: Public query service

- Add validated filter parameters.
- Whitelist sort fields and page sizes.
- Return total result counts.
- Add stable pagination.
- Add cached company/location facets.
- Preserve canonical job URLs.

Exit criteria:

- All filters can be combined.
- Every result set has deterministic ordering.
- Invalid parameters return a user-safe validation response.

### Phase 4: Search performance

- Add an FTS5 index for company, title, location, and description.
- Update the index from current immutable job versions.
- Use FTS5 for description searches.
- Keep structured Java/location eligibility in the `jobs` table.

Targets:

- Search p95 below 500 ms.
- No unbounded table response.
- No Python regex call for every description during a public request.

Exit criteria:

- Whole-word and multi-term description searches meet latency targets.
- Search results have automated relevance and filter tests.

### Phase 5: Retention and database maintenance

- Implement configurable six-month job retention.
- Implement 90-day scan-attempt pruning.
- Add WAL checkpoint maintenance.
- Add database-size reporting.
- Add scheduled backup and restore verification.

Exit criteria:

- Boundary tests cover exact cutoff, leap-month behavior, and undated jobs.
- Deletions cascade without orphan records.
- Database size is bounded by the configured retention policy.

### Phase 6: Production operations

- Package worker and web process independently.
- Provision a persistent volume.
- Enforce one worker per database.
- Add automatic process restart.
- Add HTTPS termination.
- Add readiness and liveness checks.
- Add rate limiting for search traffic.
- Add logs, error tracking, and alerts.
- Add CI for tests, linting, type checking, dependency audit, and image build.
- Pin dependencies with a lock file.

Exit criteria:

- The system recovers automatically from a worker or web-process restart.
- A stale worker raises an alert.
- A failed backup raises an alert.
- A restore succeeds in a clean environment.

### Phase 7: Beta and launch

- Run a private beta.
- Compare filtered results with direct database queries.
- Verify canonical outbound URLs per provider.
- Review ATS rate limits and terms.
- Test desktop and mobile layouts.
- Review accessibility and keyboard navigation.

## 8. Security and operational requirements

- Require HTTPS in production.
- Apply public rate limits even though there are no accounts.
- Validate all outbound URLs as HTTP or HTTPS.
- Keep scanning diagnostics and database maintenance on operator-only routes.
- Store the database and backups outside the public web root.
- Do not expose raw provider payloads or database paths to users.
- Keep source attribution and provider term information accessible.
- Monitor database and WAL growth.
- Record backup age and restore-test age.
- Treat the ATS payload and source dataset as untrusted input.

## 9. Monitoring and alerts

Required metrics:

- Worker heartbeat age
- Last successful board scan
- Scan success/failure/incomplete counts by provider
- Number of due boards
- Rate-limit and HTTP failure counts
- Database size and WAL size
- Disk free space
- Public search latency and error rate
- Backup completion and restore-test status

Alerts:

- No completed scan within two configured scan intervals.
- No successful data update for four intervals.
- Database or WAL growth beyond the expected retention envelope.
- Disk free space below the operational threshold.
- Public error rate above the launch threshold.
- Backup older than its schedule.

## 10. Launch acceptance criteria

- No accounts, application tracking, or downloads are exposed.
- Jobs page and job-detail page work on desktop and mobile.
- Required and recommended filters work independently and together.
- Country, region, and remote-scope fields do not assume Ottawa or Canada.
- Missing optional fields display as `Not specified`; applying a structured
  filter excludes unknowns unless **Include unspecified** is enabled.
- Ambiguous remote roles are not presented as worldwide-eligible.
- Evidence type remains an internal provenance detail rather than a public
  filter.
- URLs preserve filter and page state.
- Whole-word search excludes `JavaScript` when searching `Java`.
- Description terms use AND semantics.
- Search p95 is below 500 ms.
- Result counts and pagination are accurate.
- Every "View job" link opens a canonical job-description page.
- Jobs outside the retention policy are removed in bounded batches.
- Database integrity and foreign-key checks pass after retention runs.
- Worker and web process are supervised and restart automatically.
- Health, freshness, backup, and disk alerts are active.
- A database restore has been tested successfully.
- Deterministic tests, migration tests, and live-provider smoke tests pass.

## 11. Deferred features

The following are explicitly deferred:

- User accounts
- Saved jobs
- Application status tracking
- Notes and reminders
- Saved searches
- Email alerts
- CSV exports
- Resume matching
- Automated application submission
- Multi-region deployment
- PostgreSQL migration

These require a separate product decision and should not delay the initial
read-only public launch.
